# 质量抽样报告与清洗管道消费

替代「全量扫描、不应用采样比例」的旧质量报告口径，实现**可复算的分组质量抽样**，
并由清洗管道按组持久化可交付行、隔离行与待检范围。

## 核心口径

- **比例预算**：每轮预算可由工序「采样比例」推导（`budget_from_ratio`），
  再用最大余数法按组占比分配。90 常规页 + 10 新模板页、预算 10 → 常规 9、新模板 1。
- **预算只计实际检查**：报告 `budget_total_consumed` 与每轮 `budget_consumed`
  只统计真正产生判定（pass/abnormal）的样本，未给结论的行不计预算、留回待检。
- **组级判定互不借用**：组状态只看本组已检分母与异常数。已检分母为 0 的组恒为
  `not_adjudicated`，即使兄弟组全部合格也不放行；异常超阈值 → `blocked`；
  全量覆盖且零异常 → `released`；其余 → `in_progress`。
- **样本身份可复算**：报告内嵌工序设置快照（含设置指纹）与全量样本身份
  （sample_id、group、seq、内容指纹）。复算只用报告内嵌设置，不读工序当前设置，
  因此切换工序设置后旧报告仍能复算旧抽样范围；内容被篡改会在身份校验中暴露。
- **确定性抽样**：选择顺序由 `设置指纹|组|sample_id|内容指纹` 的哈希排名决定，
  无随机数，同输入必得同结果。
- **行级幂等出数**：SQLite 以 `(report_id, sample_id)` 唯一约束持久化
  delivered/quarantined/pending 三态；重复消费不重复生成已放行产物。
- **后续轮只处理剩余样本**：`claim_pending` 只返回 PENDING 行，已交付/已隔离行
  不会再次出现，支持按组定向领取（如第二轮只领新模板剩余行）。

## 模块划分

| 文件 | 职责 |
| --- | --- |
| `model.py` | 样本身份、工序设置快照、检核记录、组级判定口径 |
| `planner.py` | 确定性排序、最大余数法预算分配、比例推导预算、本轮选择 |
| `report.py` | 抽样报告聚合根、轮次推进、原子持久化、身份校验与复算、人读版 |
| `pipeline.py` | 清洗管道消费：行级三态、交付物、隔离/待检台账、待检领取 |
| `scenario.py` | 90 常规页 + 10 新模板页场景与规则检核器 |
| `service.py` | 应用门面，编排建报告/轮次/消费/领取/复算 |

## HTTP 接口（前缀 `/api/quality-sampling`）

- `POST /reports`：建报告，固化工序设置快照与全量样本身份
- `POST /reports/{id}/rounds`：按显式预算跑比例轮
- `POST /reports/{id}/ratio-rounds`：按工序采样比例推导预算跑轮
- `POST /reports/{id}/claimed-rounds`：定向领取轮（只处理指定组剩余待检）
- `POST /reports/{id}/consume`：清洗管道消费，按组出数（幂等）
- `POST /reports/{id}/claim`：领取待检范围（可按组、限量）
- `GET  /reports/{id}/pipeline-state`：三态计数与隔离清单
- `GET  /reports/{id}`：读取报告
- `POST /reports/{id}/replay`：按内嵌设置复算旧抽样范围

## 落盘布局（均在仓库外的 `data/`，已被 .gitignore 忽略）

```
data/sampling/
├── reports/<report_id>.json        # 自描述抽样报告（设置快照+样本身份+轮次+检核）
├── report_index.jsonl              # 报告索引
├── batches/<report_id>.pages.json  # 原始页面批次（复算输入）
├── pipeline/<process_code>.sqlite3 # 行级三态（幂等约束）
├── deliverables/<report_id>/<group>/<sample_id>.json  # 放行产物
├── ledgers/<report_id>/{deliverable,quarantine,pending}_ids.json
└── pipeline_events.jsonl           # 每次消费事件
```

## 验收场景（见 `tests/test_quality_sampling.py`）

90 常规页 + 10 新模板页（新模板第 3/6/9 条异常），两轮：

1. 首轮预算 10 → 分配 9:1，实际检查 10；常规分母 9 异常 0、新模板分母 1，
   其余样本全部保留待检；
2. 第二轮从原工序只领取新模板剩余 9 条，累计分母 10、异常 3 全部隔离、整组阻断；
3. 重复消费不重复出数；常规 81 条仍在待检台账可继续检核；
4. 切换工序设置后复算旧报告，抽样范围、组分母、隔离清单与样本身份完全一致；
   篡改样本内容后复算身份校验失败。

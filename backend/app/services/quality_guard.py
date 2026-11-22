"""质量防护领域服务：数据质量校验、异常检测、阻断策略，消费clean_pipeline和transform_rule输出。

质量检核分两条口径：

* ``run_quality_report``：历史全量扫描口径，仅汇总整体合规率，
  不应用工序的采样比例，保留给兼容调用；
* ``build_sampling_report`` 等抽样方法：应用清洗工序设定的采样比例/
  检查预算，按组生成可复算的抽样报告，并驱动清洗管道按组出数。
"""
from __future__ import annotations

from typing import Any, Sequence

from app.store import store
from app.services.base import BaseModuleService
from app.sampling.model import ProcessSettings
from app.sampling.service import quality_sampling_service


class QualityGuardService(BaseModuleService):
    MODULE = "quality_guard"
    ENTITY = "质量规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则编号", "检核对象", "检核维度", "合规阈值", "阻断策略", "实例范围", "采样率", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "检核对象", "检核维度"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def run_quality_report(self, target_module: str) -> dict[str, Any]:
        """全量扫描口径（不应用采样比例）：保留兼容，新流程请用抽样报告。"""
        from app import persistence as persist

        rows = store.rows(target_module)
        rules = [r for r in store.rows(self.MODULE) if r.get("检核对象") == target_module]
        total = len(rows)
        abnormal = sum(1 for r in rows if r.get("abnormal"))
        compliance = 1.0 if total == 0 else (total - abnormal) / total
        report = {
            "target": target_module,
            "scan_mode": "full_scan",
            "total_records": total,
            "abnormal_records": abnormal,
            "compliance_rate": compliance,
            "applied_rules": len(rules),
            "timestamp": persist.now_iso(),
        }
        persist.append_jsonl("reports", "quality_report.jsonl", report)
        return report

    # ---- 可复算抽样报告（应用工序采样设定） ------------------------

    def build_sampling_report(
        self,
        report_id: str,
        settings: ProcessSettings,
        pages: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        """建立抽样报告：固化工序设置快照与全量样本身份。"""
        report = quality_sampling_service.build_report(report_id, settings, pages)
        return report.to_dict()

    def run_sampling_round(
        self, report_id: str, budget: int, only_groups: set[str] | None = None
    ) -> dict[str, Any]:
        """按比例预算执行一轮抽样检核（预算只计实际检查）。"""
        return quality_sampling_service.run_proportional_round(
            report_id, budget, only_groups
        )

    def run_ratio_round(
        self,
        report_id: str,
        sampling_ratio: float | None = None,
        only_groups: set[str] | None = None,
    ) -> dict[str, Any]:
        """应用清洗工序采样比例推导预算并执行一轮检核。"""
        return quality_sampling_service.run_ratio_round(
            report_id, sampling_ratio, only_groups
        )

    def run_claimed_round(self, report_id: str, groups: set[str]) -> dict[str, Any]:
        """后续轮次：从原工序只领取指定组的剩余待检样本。"""
        return quality_sampling_service.run_claimed_round(report_id, groups)

    def consume_sampling_report(self, report_id: str) -> dict[str, Any]:
        """清洗管道消费：按组持久化可交付/隔离/待检并产出放行物。"""
        return quality_sampling_service.consume(report_id)

    def claim_pending_samples(
        self, report_id: str, groups: set[str] | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        return quality_sampling_service.claim_pending(report_id, groups, limit)

    def replay_sampling_report(
        self, report_id: str, pages: Sequence[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """切换工序设置后，仍按旧报告内嵌设置复算旧抽样范围。"""
        return quality_sampling_service.replay(report_id, pages)

    def sampling_report(self, report_id: str) -> dict[str, Any]:
        return quality_sampling_service.get_report_data(report_id)

    def domain_snapshot(self) -> dict[str, Any]:
        from app import persistence as persist

        base = super().domain_snapshot()
        # 质量报告汇总
        reports = persist.read_jsonl("reports", "quality_report.jsonl")
        if reports:
            latest = reports[-1]
            base["latest_compliance"] = latest.get("compliance_rate")
            base["total_reports"] = len(reports)
        # 抽样报告汇总
        index = persist.read_jsonl("sampling", "report_index.jsonl")
        base["sampling_reports"] = len(index)
        # 关联的schema校验
        schemas = store.rows("schema_check")
        base["linked_schema_rules"] = len(schemas)
        return base

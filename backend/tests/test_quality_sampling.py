"""质量抽样报告与清洗管道验收测试。

覆盖需求：
1. 90 常规页 + 10 新模板页、首轮预算 10：按 90:10 比例分配为 9:1，
   预算只计实际检查，未覆盖样本保留待检；
2. 第二轮只领取第一轮留下的新模板行，已放行产物不重复生成；
3. 报告保留样本身份、工序版本、各组已检分母与异常数；
4. 无判定样本的组不借别组结论放行；
5. 切换工序设置后，旧报告仍按内嵌设置复算旧抽样范围；
6. 清洗管道按组持久化可交付行、隔离行、待检范围。
"""
from __future__ import annotations

import json
import os
import sys

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app import persistence as persist
from app.sampling.model import GroupStatus, ProcessSettings, Verdict
from app.sampling.planner import allocate_budget, budget_from_ratio
from app.sampling.scenario import (
    REGULAR_GROUP,
    TEMPLATE_GROUP,
    build_page_batch,
    pages_to_identities,
    rule_based_inspector,
)
from app.sampling.service import quality_sampling_service
from app.services.quality_guard import QualityGuardService

# 与现有测试套件一致：每个测试进程从干净的 data/ 开始。
for sub in ["runs", "snapshots", "reports", "exports", "sampling"]:
    folder = persist.data_path(sub, "")
    if os.path.isdir(folder):
        for root, _dirs, files in os.walk(folder):
            for name in files:
                os.remove(os.path.join(root, name))

REPORT_ID = "RPT-9010-ACCEPT"
PROCESS_CODE = "CLEA-SAMPLING-01"
ABNORMAL_TEMPLATE_SEQS = (3, 6, 9)


def _settings(version: str = "PROC-V1", ratio: float = 0.10) -> ProcessSettings:
    return ProcessSettings(
        process_code=PROCESS_CODE,
        process_version=version,
        sampling_ratio=ratio,
        abnormal_threshold=0,
    )


def _section(data: dict, group: str) -> dict:
    return next(s for s in data["group_sections"] if s["group"] == group)


def test_allocator_proportional_share():
    """预算分配：90:10、预算 10 → 9:1；最大余数法确定且不超总样本。"""
    alloc = allocate_budget({REGULAR_GROUP: 90, TEMPLATE_GROUP: 10}, 10)
    assert alloc == {REGULAR_GROUP: 9, TEMPLATE_GROUP: 1}, alloc
    # 小容量组配额不足 1 时名额按余数让给仍有待检样本的大组
    alloc2 = allocate_budget({REGULAR_GROUP: 90, TEMPLATE_GROUP: 2}, 10)
    assert sum(alloc2.values()) == 10
    assert alloc2 == {REGULAR_GROUP: 10, TEMPLATE_GROUP: 0}
    # 余数并列时按组名确定打破平局
    alloc3 = allocate_budget({"a": 5, "b": 5}, 3)
    assert alloc3 == {"a": 2, "b": 1}
    # 预算不超过总样本
    assert sum(allocate_budget({REGULAR_GROUP: 3, TEMPLATE_GROUP: 2}, 10).values()) == 5
    # 零预算与空组
    assert allocate_budget({REGULAR_GROUP: 5}, 0) == {REGULAR_GROUP: 0}


def test_two_round_sampling_and_pipeline():
    """90:10 两轮主流程。"""
    svc = QualityGuardService()
    pages = build_page_batch(abnormal_template_seqs=ABNORMAL_TEMPLATE_SEQS)
    assert len(pages) == 100
    abnormal_ids = {
        p["sample_id"] for p in pages
        if p["group"] == TEMPLATE_GROUP and p["payload"].get("defect")
    }
    assert len(abnormal_ids) == 3  # 新模板有少数异常

    # --- 建报告：固化设置快照与全量样本身份 -------------------------
    svc.build_sampling_report(REPORT_ID, _settings(), pages)

    # --- 第一轮：预算 10，比例抽样 ---------------------------------
    round1 = svc.run_sampling_round(REPORT_ID, budget=10)
    assert round1["budget_granted"] == 10
    assert round1["budget_consumed"] == 10  # 预算只计实际检查
    assert round1["allocations"] == {REGULAR_GROUP: 9, TEMPLATE_GROUP: 1}

    data = svc.sampling_report(REPORT_ID)
    regular = _section(data, REGULAR_GROUP)
    template = _section(data, TEMPLATE_GROUP)

    # 各组已检分母与异常数分别独立
    assert regular["inspected_denominator"] == 9
    assert regular["abnormal_count"] == 0
    assert regular["status"] == GroupStatus.IN_PROGRESS.value
    assert len(regular["pending_ids"]) == 81

    assert template["inspected_denominator"] == 1
    assert len(template["quarantined_ids"]) == template["abnormal_count"]
    assert template["abnormal_count"] in (0, 1)
    assert len(template["pending_ids"]) == 9
    # 未检完且（若检出异常则阻断），新模板组绝不能在第一轮整体放行
    assert template["status"] in (
        GroupStatus.IN_PROGRESS.value, GroupStatus.BLOCKED.value
    )

    # 报告保留样本身份与工序版本
    assert len(data["sample_manifest"]) == 100
    manifest_ids = {m["sample_id"] for m in data["sample_manifest"]}
    assert manifest_ids == {p["sample_id"] for p in pages}
    assert data["settings"]["process_version"] == "PROC-V1"
    assert data["settings"]["settings_fingerprint"] == _settings().fingerprint()

    # --- 管道消费第一轮：按组持久化三类状态 -------------------------
    consume1 = svc.consume_sampling_report(REPORT_ID)
    counts1 = consume1["counts"]
    assert counts1["delivered"] == 9 + (1 - template["abnormal_count"])
    assert counts1["quarantined"] == template["abnormal_count"]
    assert counts1["pending"] == 81 + 9
    state = quality_sampling_service.pipeline_state(REPORT_ID)
    assert {q["sample_id"] for q in state["quarantine"]} == set(
        template["quarantined_ids"]
    )

    deliver_dir = persist.data_path(
        "sampling", f"deliverables/{REPORT_ID}/{REGULAR_GROUP}"
    )
    first_deliver_files = (
        set(os.listdir(deliver_dir)) if os.path.isdir(deliver_dir) else set()
    )
    assert len(first_deliver_files) == 9

    # --- 第二轮：从原工序只领取新模板剩余待检行 ---------------------
    claimed = svc.claim_pending_samples(REPORT_ID, groups={TEMPLATE_GROUP})
    assert len(claimed) == 9
    assert all(item["group"] == TEMPLATE_GROUP for item in claimed)
    # 常规页已交付/未检的都不会出现在新模板领取结果里
    assert {item["sample_id"] for item in claimed}.isdisjoint(
        set(regular["passed_ids"])
    )

    round2 = svc.run_claimed_round(REPORT_ID, groups={TEMPLATE_GROUP})
    assert round2["mode"] == "claimed"
    assert round2["budget_consumed"] == 9  # 只检查领取到的剩余新模板行
    data2 = svc.sampling_report(REPORT_ID)
    regular2 = _section(data2, REGULAR_GROUP)
    template2 = _section(data2, TEMPLATE_GROUP)

    # 常规组分母未被第二轮改动
    assert regular2["inspected_denominator"] == 9
    assert len(regular2["pending_ids"]) == 81
    # 新模板累计已检 10，三条异常全部隔离，整组阻断
    assert template2["inspected_denominator"] == 10
    assert template2["abnormal_count"] == 3
    assert set(template2["quarantined_ids"]) == abnormal_ids
    assert template2["status"] == GroupStatus.BLOCKED.value
    assert template2["pending_ids"] == []
    assert data2["budget_total_consumed"] == 19

    # --- 再次消费：只处理剩余样本，不重复生成已放行产物 --------------
    consume2 = svc.consume_sampling_report(REPORT_ID)
    counts2 = consume2["counts"]
    assert counts2["quarantined"] == 3
    assert counts2["delivered"] == 9 + 7  # 常规 9 + 新模板合格 7
    assert counts2["pending"] == 81       # 常规未检样本仍保留
    second_files = (
        set(os.listdir(deliver_dir)) if os.path.isdir(deliver_dir) else set()
    )
    assert second_files == first_deliver_files  # 常规交付物零重复生成

    # 重复消费完全幂等
    consume3 = svc.consume_sampling_report(REPORT_ID)
    assert consume3["counts"] == counts2
    assert consume3["transitioned"] == {"delivered": [], "quarantined": [], "pending": []}

    # 新模板合格行各自只产出一份交付物
    t_dir = persist.data_path(
        "sampling", f"deliverables/{REPORT_ID}/{TEMPLATE_GROUP}"
    )
    assert len(os.listdir(t_dir)) == 7

    # 待检台账仍保留常规页 81 条，后续轮次可继续检核
    ledger = persist.read_json(
        "sampling", f"ledgers/{REPORT_ID}/pending_ids.json"
    )
    assert len(ledger[REGULAR_GROUP]) == 81
    assert TEMPLATE_GROUP not in ledger


def test_unadjudicated_group_not_released():
    """无判定样本的组不拿别组结论放行。"""
    report_id = "RPT-NO-ADJUDICATION"
    pages = build_page_batch(abnormal_template_seqs=ABNORMAL_TEMPLATE_SEQS)
    svc = QualityGuardService()
    svc.build_sampling_report(report_id, _settings(ratio=0.05), pages)
    # 预算 1：最大余数法下名额落到占比 90% 的常规组，新模板组完全未检
    record = svc.run_sampling_round(report_id, budget=1)
    assert record["budget_consumed"] == 1
    data = svc.sampling_report(report_id)
    regular = _section(data, REGULAR_GROUP)
    template = _section(data, TEMPLATE_GROUP)
    assert regular["inspected_denominator"] == 1
    assert regular["status"] == GroupStatus.IN_PROGRESS.value
    assert template["inspected_denominator"] == 0
    assert template["status"] == GroupStatus.NOT_ADJUDICATED.value
    assert "禁止借用别组结论放行" in template["next_step"]

    consume = svc.consume_sampling_report(report_id)
    # 新模板组没有任何行被交付
    assert all(
        row["sample_group"] != TEMPLATE_GROUP
        for row in _delivered_rows(consume)
    )
    t_dir = persist.data_path(
        "sampling", f"deliverables/{report_id}/{TEMPLATE_GROUP}"
    )
    assert not os.path.isdir(t_dir) or os.listdir(t_dir) == []


def _delivered_rows(consume: dict) -> list[dict]:
    from app.sampling.pipeline import SamplingPipeline

    pipeline = SamplingPipeline(PROCESS_CODE)
    with pipeline._connect() as conn:
        cur = conn.execute(
            "SELECT * FROM row_state WHERE report_id=? AND state='delivered'",
            (consume["report_id"],),
        )
        return [dict(r) for r in cur.fetchall()]


def test_report_replay_after_settings_switch():
    """切换工序设置后，旧报告仍能复算旧抽样范围与样本身份。"""
    svc = QualityGuardService()
    old_data = svc.sampling_report(REPORT_ID)
    old_fingerprint = old_data["settings"]["settings_fingerprint"]

    # 切换工序设置（新版本、不同采样比例），另建一份新报告
    new_pages = build_page_batch(process_version="PROC-V2")
    svc.build_sampling_report(
        "RPT-NEW-SETTINGS", _settings(version="PROC-V2", ratio=0.25), new_pages
    )

    # 旧报告复算：用的是报告内嵌设置，不受当前工序设置影响
    replay = svc.replay_sampling_report(REPORT_ID)
    assert replay["matched"] is True, replay["mismatches"]
    assert replay["identity_check"]["ok"] is True
    assert replay["settings_fingerprint"] == old_fingerprint
    assert replay["replayed_budget_consumed"] == 19
    sections = {s["group"]: s for s in replay["replayed_sections"]}
    assert sections[REGULAR_GROUP]["inspected_denominator"] == 9
    assert sections[TEMPLATE_GROUP]["inspected_denominator"] == 10
    assert sections[TEMPLATE_GROUP]["abnormal_count"] == 3

    # 偷换一个样本内容后复算：身份校验必须失败，拒绝复用旧结论
    tampered = build_page_batch(abnormal_template_seqs=ABNORMAL_TEMPLATE_SEQS)
    tampered[0]["payload"]["title"] = "被篡改的页面内容"
    replay_bad = svc.replay_sampling_report(REPORT_ID, tampered)
    assert replay_bad["identity_check"]["ok"] is False
    assert tampered[0]["sample_id"] in replay_bad["identity_check"]["changed_ids"]
    assert replay_bad["matched"] is False


def test_ratio_round_uses_process_sampling_ratio():
    """应用工序采样比例推导预算：100 条 × 10% → 预算 10，且只计实际检查。"""
    assert budget_from_ratio(100, 0.10) == 10
    assert budget_from_ratio(97, 0.10) == 10  # 四舍五入
    assert budget_from_ratio(100, 0.0) == 0
    assert budget_from_ratio(5, 1.0) == 5

    report_id = "RPT-RATIO"
    pages = build_page_batch(abnormal_template_seqs=ABNORMAL_TEMPLATE_SEQS)
    svc = QualityGuardService()
    svc.build_sampling_report(report_id, _settings(ratio=0.10), pages)
    record = svc.run_ratio_round(report_id)  # 缺省取报告内嵌比例
    assert record["sampling_ratio_applied"] == 0.10
    assert record["budget_granted"] == 10
    assert record["budget_consumed"] == 10
    assert record["allocations"] == {REGULAR_GROUP: 9, TEMPLATE_GROUP: 1}


def test_deterministic_selection_across_runs():
    """同样的样本与设置，独立重跑得到同一批首轮样本（可复算基础）。"""
    from app.sampling.report import SamplingReport

    pages = build_page_batch(abnormal_template_seqs=ABNORMAL_TEMPLATE_SEQS)
    settings = _settings()
    r1 = SamplingReport("det-1", settings, pages_to_identities(pages))
    r2 = SamplingReport("det-2", settings, pages_to_identities(pages))
    r1.run_round(rule_based_inspector(), budget=10)
    r2.run_round(rule_based_inspector(), budget=10)
    assert r1.rounds[0]["selected"] == r2.rounds[0]["selected"]
    assert r1.rounds[0]["allocations"] == {REGULAR_GROUP: 9, TEMPLATE_GROUP: 1}


if __name__ == "__main__":
    test_allocator_proportional_share()
    print("PASS: test_allocator_proportional_share")
    test_two_round_sampling_and_pipeline()
    print("PASS: test_two_round_sampling_and_pipeline")
    test_unadjudicated_group_not_released()
    print("PASS: test_unadjudicated_group_not_released")
    test_report_replay_after_settings_switch()
    print("PASS: test_report_replay_after_settings_switch")
    test_ratio_round_uses_process_sampling_ratio()
    print("PASS: test_ratio_round_uses_process_sampling_ratio")
    test_deterministic_selection_across_runs()
    print("PASS: test_deterministic_selection_across_runs")
    print("\nALL 6 SAMPLING TESTS PASSED")

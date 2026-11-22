"""质量抽样门面服务：编排报告轮次、复算与清洗管道消费。

与通用资源路由不同，抽样流程是多步有状态工作流（建报告 → 轮次检核 →
管道消费 → 领取剩余样本 → 再检核），由本门面统一编排，状态全部落盘：
报告在 data/sampling/reports/，管道状态在 data/sampling/pipeline/。
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from app import persistence as persist
from app.sampling.model import ProcessSettings, SampleIdentity, Verdict
from app.sampling.pipeline import SamplingPipeline
from app.sampling.planner import budget_from_ratio
from app.sampling.report import (
    SamplingReport,
    load_report,
    render_text,
    replay_report,
)
from app.sampling.scenario import pages_to_identities, rule_based_inspector


class QualitySamplingService:
    """抽样报告与清洗管道的应用服务。"""

    def __init__(self) -> None:
        # 批次注册表：报告运行期内保留原始页面（含 payload），供领取与复算。
        self._batches: dict[str, list[dict[str, Any]]] = {}

    # ---- 报告生命周期 ---------------------------------------------

    def build_report(
        self,
        report_id: str,
        settings: ProcessSettings,
        pages: Sequence[dict[str, Any]],
    ) -> SamplingReport:
        samples = pages_to_identities(pages)
        report = SamplingReport(report_id, settings, samples)
        report.save()
        self._batches[report_id] = [dict(p) for p in pages]
        self._save_batch(report_id, pages)
        return report

    def run_proportional_round(
        self, report_id: str, budget: int, only_groups: set[str] | None = None
    ) -> dict[str, Any]:
        """比例预算轮：按组占比分配预算并执行检核。"""
        report = self._open(report_id)
        record = report.run_round(
            rule_based_inspector(), budget=budget, only_groups=only_groups
        )
        report.save()
        return record

    def run_ratio_round(
        self,
        report_id: str,
        sampling_ratio: float | None = None,
        only_groups: set[str] | None = None,
    ) -> dict[str, Any]:
        """按工序采样比例推导本轮预算，再按组占比分配。

        不显式传比例时使用报告内嵌（即建报告那一刻的工序设置）比例，
        保证切换工序设置不会改变旧报告的预算口径。
        """
        report = self._open(report_id)
        ratio = (
            report.settings.sampling_ratio if sampling_ratio is None else sampling_ratio
        )
        pending = report.pending_samples(only_groups)
        budget = budget_from_ratio(len(pending), ratio)
        record = report.run_round(
            rule_based_inspector(), budget=budget, only_groups=only_groups
        )
        record["sampling_ratio_applied"] = ratio
        report.save()
        return record

    def run_claimed_round(self, report_id: str, groups: set[str]) -> dict[str, Any]:
        """定向领取轮：从原工序管道只领取指定组的剩余待检样本。"""
        report = self._open(report_id)
        pipeline = SamplingPipeline(report.settings.process_code)
        claimed = pipeline.claim_pending(report, groups=groups)
        by_id = {s.sample_id: s for s in report.samples}
        selection: dict[str, list[SampleIdentity]] = {}
        for item in claimed:
            sample = by_id[item["sample_id"]]
            selection.setdefault(sample.group, []).append(sample)
        record = report.run_round(rule_based_inspector(), explicit_selection=selection)
        report.save()
        return record

    def consume(self, report_id: str) -> dict[str, Any]:
        """清洗管道消费报告：按组持久化交付/隔离/待检并出数。"""
        report = self._open(report_id)
        pipeline = SamplingPipeline(report.settings.process_code)
        return pipeline.consume_report(report)

    def claim_pending(
        self, report_id: str, groups: set[str] | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        report = self._open(report_id)
        pipeline = SamplingPipeline(report.settings.process_code)
        return pipeline.claim_pending(report, groups=groups, limit=limit)

    # ---- 查询与复算 -----------------------------------------------

    def get_report_data(self, report_id: str) -> dict[str, Any]:
        path = persist.data_path("sampling", f"reports/{report_id}.json")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def render_report(self, report_id: str) -> str:
        return render_text(self.get_report_data(report_id))

    def replay(self, report_id: str, pages: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
        """按旧报告内嵌设置复算；默认使用归档的原始页面批次。"""
        data = self.get_report_data(report_id)
        if pages is None:
            pages = self._load_batch(report_id)
        return replay_report(pages, data)

    def pipeline_state(self, report_id: str) -> dict[str, Any]:
        report = self._open(report_id)
        pipeline = SamplingPipeline(report.settings.process_code)
        return {
            "counts": pipeline.state_counts(report_id),
            "quarantine": pipeline.quarantine_list(report_id),
        }

    # ---- 批次归档 -------------------------------------------------

    def _save_batch(self, report_id: str, pages: Sequence[dict[str, Any]]) -> str:
        return persist.write_json(
            "sampling", f"batches/{report_id}.pages.json", {"pages": list(pages)}
        )

    def _load_batch(self, report_id: str) -> list[dict[str, Any]]:
        if report_id in self._batches:
            return self._batches[report_id]
        data = persist.read_json("sampling", f"batches/{report_id}.pages.json")
        if data is None:
            raise FileNotFoundError(f"报告 {report_id} 的原始页面批次未归档")
        self._batches[report_id] = list(data["pages"])
        return self._batches[report_id]

    def _open(self, report_id: str) -> SamplingReport:
        pages = self._load_batch(report_id)
        return load_report(report_id, {p["sample_id"]: p.get("payload", {}) for p in pages})


# 单例，路由与测试共用。
quality_sampling_service = QualitySamplingService()

"""质量抽样工作流路由：建报告、轮次检核、管道消费、待检领取与复算。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.sampling.model import ProcessSettings
from app.sampling.service import quality_sampling_service
from app.services.quality_guard import QualityGuardService

router = APIRouter(prefix="/api/quality-sampling", tags=["质量抽样"])


class BuildPayload(BaseModel):
    report_id: str
    pages: list[dict[str, Any]]
    process_code: str
    process_version: str
    sampling_ratio: float = Field(ge=0.0, le=1.0)
    group_field: str = "group"
    abnormal_threshold: int = 0


class RoundPayload(BaseModel):
    budget: int = Field(ge=0)
    only_groups: list[str] | None = None


class RatioRoundPayload(BaseModel):
    sampling_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    only_groups: list[str] | None = None


class ClaimedRoundPayload(BaseModel):
    groups: list[str]


class ClaimPayload(BaseModel):
    groups: list[str] | None = None
    limit: int | None = None


def _settings(payload: BuildPayload) -> ProcessSettings:
    return ProcessSettings(
        process_code=payload.process_code,
        process_version=payload.process_version,
        sampling_ratio=payload.sampling_ratio,
        group_field=payload.group_field,
        abnormal_threshold=payload.abnormal_threshold,
    )


@router.post("/reports")
def build_report(payload: BuildPayload) -> dict[str, Any]:
    """建立抽样报告：固化工序设置快照与全量样本身份。"""
    svc = QualityGuardService()
    return svc.build_sampling_report(payload.report_id, _settings(payload), payload.pages)


@router.get("/reports/{report_id}")
def get_report(report_id: str) -> dict[str, Any]:
    return QualityGuardService().sampling_report(report_id)


@router.post("/reports/{report_id}/rounds")
def run_round(report_id: str, payload: RoundPayload) -> dict[str, Any]:
    """比例预算轮：按组占比分配预算，预算只计实际检查。"""
    groups = set(payload.only_groups) if payload.only_groups else None
    return QualityGuardService().run_sampling_round(report_id, payload.budget, groups)


@router.post("/reports/{report_id}/ratio-rounds")
def run_ratio_round(report_id: str, payload: RatioRoundPayload) -> dict[str, Any]:
    """按清洗工序采样比例推导预算的检核轮（缺省用报告内嵌比例）。"""
    groups = set(payload.only_groups) if payload.only_groups else None
    return QualityGuardService().run_ratio_round(
        report_id, payload.sampling_ratio, groups
    )


@router.post("/reports/{report_id}/claimed-rounds")
def run_claimed_round(report_id: str, payload: ClaimedRoundPayload) -> dict[str, Any]:
    """定向领取轮：从原工序只领取指定组的剩余待检样本。"""
    return QualityGuardService().run_claimed_round(report_id, set(payload.groups))


@router.post("/reports/{report_id}/consume")
def consume_report(report_id: str) -> dict[str, Any]:
    """清洗管道消费：按组持久化可交付/隔离/待检并产出放行物。"""
    return QualityGuardService().consume_sampling_report(report_id)


@router.post("/reports/{report_id}/claim")
def claim_pending(report_id: str, payload: ClaimPayload) -> dict[str, Any]:
    rows = QualityGuardService().claim_pending_samples(
        report_id, set(payload.groups) if payload.groups else None, payload.limit
    )
    return {"items": rows, "total": len(rows)}


@router.get("/reports/{report_id}/pipeline-state")
def pipeline_state(report_id: str) -> dict[str, Any]:
    return quality_sampling_service.pipeline_state(report_id)


@router.post("/reports/{report_id}/replay")
def replay_report(report_id: str) -> dict[str, Any]:
    """按旧报告内嵌设置复算旧抽样范围（与当前工序设置解耦）。"""
    return QualityGuardService().replay_sampling_report(report_id)

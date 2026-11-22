"""质量抽样领域模型与判定口径。

设计要点（对应需求）：

* 样本身份 ``SampleIdentity`` 带内容指纹，报告保存全量样本身份，
  切换工序设置后仍能按旧报告复算旧抽样范围；
* 分组状态只由本组的已检分母/异常数决定，未检（分母为 0）的组
  状态恒为 ``NOT_ADJUDICATED``，不能借别组结论放行；
* 行级状态与组级状态分离：组未整体放行时，已检合格行仍可按行交付，
  异常行隔离，未检行保留在待检范围里。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    """单条样本检核结论。"""

    PASS = "pass"          # 合格，可进入可交付行
    ABNORMAL = "abnormal"  # 异常，进入隔离行
    PENDING = "pending"    # 尚未检核，留在待检范围


class GroupStatus(str, Enum):
    """分组级放行口径。"""

    NOT_ADJUDICATED = "not_adjudicated"  # 已检分母为 0：无判定，不得放行
    RELEASED = "released"                # 全量覆盖且零异常，整组放行
    BLOCKED = "blocked"                  # 已检中出现异常，整组阻断
    IN_PROGRESS = "in_progress"          # 已检但未覆盖全量且暂无异常


class RowState(str, Enum):
    """清洗管道侧的行级持久化状态。"""

    DELIVERED = "delivered"  # 可交付行，已产出放行产物
    QUARANTINED = "quarantined"  # 隔离行，禁止出数
    PENDING = "pending"      # 待检范围，等待后续轮次领取


def content_hash(payload: Any) -> str:
    """对样本内容做稳定指纹（排序键 + 紧凑 JSON + blake2b）。"""
    import json

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()


@dataclass(frozen=True)
class SampleIdentity:
    """样本身份：业务标识 + 所属组 + 内容指纹。

    报告里全量保存这些身份，复算时先核对指纹，避免样本被偷换后
    仍复用旧抽样结论。
    """

    sample_id: str
    group: str
    fingerprint: str
    seq: int
    payload: dict[str, Any] = field(default_factory=dict, compare=False)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "group": self.group,
            "seq": self.seq,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_page(cls, page: dict[str, Any]) -> "SampleIdentity":
        seq = int(page["seq"])
        return cls(
            sample_id=str(page["sample_id"]),
            group=str(page["group"]),
            fingerprint=str(page["fingerprint"]),
            seq=seq,
            payload=dict(page.get("payload") or {}),
        )


@dataclass(frozen=True)
class ProcessSettings:
    """工序设置快照。

    报告内嵌的是创建报告那一刻的设置副本，而不是对当前工序的引用，
    因此后续切换工序设置不会改写旧报告，旧报告始终能按内嵌设置复算。
    """

    process_code: str
    process_version: str
    sampling_ratio: float          # 工序设定的采样比例（0~1）
    group_field: str = "group"     # 分组字段
    abnormal_threshold: int = 0    # 组级放行允许的异常数上限
    extra: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        raw = (
            f"{self.process_code}|{self.process_version}|{self.sampling_ratio:.6f}|"
            f"{self.group_field}|{self.abnormal_threshold}"
        )
        return hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_code": self.process_code,
            "process_version": self.process_version,
            "sampling_ratio": self.sampling_ratio,
            "group_field": self.group_field,
            "abnormal_threshold": self.abnormal_threshold,
            "extra": dict(self.extra),
            "settings_fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProcessSettings":
        return cls(
            process_code=str(data["process_code"]),
            process_version=str(data["process_version"]),
            sampling_ratio=float(data["sampling_ratio"]),
            group_field=str(data.get("group_field", "group")),
            abnormal_threshold=int(data.get("abnormal_threshold", 0)),
            extra=dict(data.get("extra") or {}),
        )


@dataclass(frozen=True)
class InspectionRecord:
    """一次单样本检核记录（实际检查才会产生，预算只计这些记录）。"""

    sample_id: str
    group: str
    round_no: int
    verdict: Verdict
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "group": self.group,
            "round_no": self.round_no,
            "verdict": self.verdict.value,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InspectionRecord":
        return cls(
            sample_id=str(data["sample_id"]),
            group=str(data["group"]),
            round_no=int(data["round_no"]),
            verdict=Verdict(data["verdict"]),
            reason=str(data.get("reason", "")),
        )


def adjudicate_group(
    group_size: int,
    inspected: int,
    abnormal: int,
    abnormal_threshold: int = 0,
) -> GroupStatus:
    """只依据本组数据计算组级状态（跨组不共享结论）。

    * 已检分母为 0 → ``NOT_ADJUDICATED``，哪怕兄弟组全部合格也不放行；
    * 已检异常数超过阈值 → ``BLOCKED``；
    * 全量覆盖且异常未超阈值 → ``RELEASED``；
    * 其余 → ``IN_PROGRESS``，待检样本必须保留可继续检核。
    """
    if inspected <= 0:
        return GroupStatus.NOT_ADJUDICATED
    if abnormal > abnormal_threshold:
        return GroupStatus.BLOCKED
    if inspected >= group_size and group_size > 0:
        return GroupStatus.RELEASED
    return GroupStatus.IN_PROGRESS

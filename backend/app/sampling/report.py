"""可复算的质量抽样报告。

报告是自描述的 JSON：内嵌工序设置快照、全量样本身份清单、每一轮的
预算与选择依据、全部检核记录。后续轮次和复算都从这份文件出发，
不依赖内存状态，也不依赖工序的当前设置。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from app import persistence as persist
from app.sampling.model import (
    GroupStatus,
    InspectionRecord,
    ProcessSettings,
    SampleIdentity,
    Verdict,
    adjudicate_group,
    content_hash,
)
from app.sampling.planner import plan_selection

REPORT_FORMAT_VERSION = "sampling-report/v1"
Inspector = Callable[[SampleIdentity], tuple[Verdict, str]]


@dataclass
class GroupSection:
    """报告中的分组小节：分母、异常数、三类清单与组级状态。"""

    group: str
    group_size: int
    inspected_count: int
    abnormal_count: int
    passed_ids: list[str] = field(default_factory=list)
    quarantined_ids: list[str] = field(default_factory=list)
    pending_ids: list[str] = field(default_factory=list)
    status: GroupStatus = GroupStatus.NOT_ADJUDICATED
    next_step: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "group_size": self.group_size,
            "inspected_denominator": self.inspected_count,
            "abnormal_count": self.abnormal_count,
            "passed_ids": list(self.passed_ids),
            "quarantined_ids": list(self.quarantined_ids),
            "pending_ids": list(self.pending_ids),
            "status": self.status.value,
            "next_step": self.next_step,
        }


def _next_step(status: GroupStatus, pending: int, abnormal: int) -> str:
    if status is GroupStatus.NOT_ADJUDICATED:
        return "本组尚无判定样本，禁止借用别组结论放行；下一轮须领取本组样本检核"
    if status is GroupStatus.BLOCKED:
        return (
            f"已隔离 {abnormal} 条异常且整组阻断；先复核隔离清单，"
            f"仍有 {pending} 条待检样本保留可继续检核"
        )
    if status is GroupStatus.IN_PROGRESS:
        return f"继续领取并检核本组剩余 {pending} 条待检样本，未覆盖前不得整组放行"
    return "本组已全量检核且零异常，整组放行"


class SamplingReport:
    """抽样报告聚合根：负责轮次推进、分组口径与序列化。"""

    def __init__(
        self,
        report_id: str,
        settings: ProcessSettings,
        samples: Sequence[SampleIdentity],
    ) -> None:
        self.report_id = report_id
        self.settings = settings
        self.samples: list[SampleIdentity] = list(samples)
        self.rounds: list[dict[str, Any]] = []
        self.inspections: list[InspectionRecord] = []

    # ---- 基础查询 -------------------------------------------------

    @property
    def groups(self) -> list[str]:
        names = sorted({s.group for s in self.samples})
        return names

    def _inspected_ids(self) -> set[str]:
        return {r.sample_id for r in self.inspections}

    def budget_consumed(self) -> int:
        """预算只计实际检查：一条检核记录算一个预算单位。"""
        return len(self.inspections)

    def group_sections(self) -> list[GroupSection]:
        by_id = {s.sample_id: s for s in self.samples}
        inspected: dict[str, set[str]] = {g: set() for g in self.groups}
        abnormal: dict[str, set[str]] = {g: set() for g in self.groups}
        for rec in self.inspections:
            inspected.setdefault(rec.group, set()).add(rec.sample_id)
            if rec.verdict is Verdict.ABNORMAL:
                abnormal.setdefault(rec.group, set()).add(rec.sample_id)

        sections: list[GroupSection] = []
        for g in self.groups:
            members = [s for s in self.samples if s.group == g]
            ids = [s.sample_id for s in members]
            done = inspected.get(g, set())
            bad = abnormal.get(g, set())
            pending = [sid for sid in ids if sid not in done]
            section = GroupSection(
                group=g,
                group_size=len(members),
                inspected_count=len(done),
                abnormal_count=len(bad),
                passed_ids=[sid for sid in ids if sid in done and sid not in bad],
                quarantined_ids=[sid for sid in ids if sid in bad],
                pending_ids=pending,
            )
            section.status = adjudicate_group(
                section.group_size,
                section.inspected_count,
                section.abnormal_count,
                self.settings.abnormal_threshold,
            )
            section.next_step = _next_step(
                section.status, len(pending), section.abnormal_count
            )
            sections.append(section)
        return sections

    def pending_samples(self, only_groups: set[str] | None = None) -> list[SampleIdentity]:
        done = self._inspected_ids()
        return [
            s for s in self.samples
            if s.sample_id not in done and (only_groups is None or s.group in only_groups)
        ]

    # ---- 轮次执行 -------------------------------------------------

    def run_round(
        self,
        inspector: Inspector,
        *,
        budget: int | None = None,
        explicit_selection: dict[str, list[SampleIdentity]] | None = None,
        only_groups: set[str] | None = None,
    ) -> dict[str, Any]:
        """执行一轮检核。

        两种领取方式：
        * 比例预算模式（首轮）：给 ``budget``，由采样器按组占比分名额；
        * 定向领取模式（后续轮）：给 ``explicit_selection``，即清洗管道
          从待检范围领取的剩余样本，预算按实际检查条数结算。
        """
        round_no = len(self.rounds) + 1
        if explicit_selection is not None:
            selection = {g: list(rows) for g, rows in explicit_selection.items() if rows}
            mode = "claimed"
            granted = sum(len(rows) for rows in selection.values())
        else:
            if budget is None:
                raise ValueError("比例预算模式必须提供 budget")
            selection = plan_selection(
                self.samples,
                self.settings.fingerprint(),
                int(budget),
                already_inspected=self._inspected_ids(),
                only_groups=only_groups,
            )
            mode = "proportional"
            granted = int(budget)

        # 防御：任何已检样本都不允许再次进入选择，杜绝重复检查/重复计数。
        known_done = self._inspected_ids()
        chosen_ids = [s.sample_id for rows in selection.values() for s in rows]
        overlap = known_done.intersection(chosen_ids)
        if overlap:
            raise ValueError(f"样本已在既往轮次检核，不能重复选择: {sorted(overlap)}")

        new_records: list[InspectionRecord] = []
        for g in self.groups_order_of(selection):
            for sample in selection[g]:
                verdict, reason = inspector(sample)
                if verdict is Verdict.PENDING:
                    # 检核器未给出结论的行不计预算，留回待检范围。
                    continue
                new_records.append(
                    InspectionRecord(
                        sample_id=sample.sample_id,
                        group=sample.group,
                        round_no=round_no,
                        verdict=verdict,
                        reason=reason,
                    )
                )
        self.inspections.extend(new_records)

        round_record = {
            "round_no": round_no,
            "mode": mode,
            "budget_granted": granted,
            "budget_consumed": len(new_records),
            "only_groups": sorted(only_groups) if only_groups else None,
            "allocations": {g: len(rows) for g, rows in selection.items()},
            "selected": {
                g: [s.sample_id for s in rows] for g, rows in selection.items()
            },
            "inspected_ids": [r.sample_id for r in new_records],
            "abnormal_ids": [
                r.sample_id for r in new_records if r.verdict is Verdict.ABNORMAL
            ],
            "timestamp": persist.now_iso(),
        }
        self.rounds.append(round_record)
        return round_record

    @staticmethod
    def groups_order_of(selection: dict[str, list[SampleIdentity]]) -> list[str]:
        return sorted(selection)

    # ---- 序列化 ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_format": REPORT_FORMAT_VERSION,
            "report_id": self.report_id,
            "created_at": self.rounds[0]["timestamp"] if self.rounds else persist.now_iso(),
            "updated_at": persist.now_iso(),
            "settings": self.settings.to_dict(),
            "budget_total_consumed": self.budget_consumed(),
            "sample_manifest": [s.to_manifest() for s in self.samples],
            "rounds": list(self.rounds),
            "inspections": [r.to_dict() for r in self.inspections],
            "group_sections": [sec.to_dict() for sec in self.group_sections()],
        }

    def save(self) -> str:
        """原子写入 data/sampling/reports/<report_id>.json，并登记索引。"""
        path = persist.data_path("sampling", f"reports/{self.report_id}.json")
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
        persist.append_jsonl("sampling", "report_index.jsonl", {
            "report_id": self.report_id,
            "process_code": self.settings.process_code,
            "process_version": self.settings.process_version,
            "settings_fingerprint": self.settings.fingerprint(),
            "rounds": len(self.rounds),
            "budget_consumed": self.budget_consumed(),
            "timestamp": persist.now_iso(),
        })
        return path

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        sample_payloads: dict[str, dict[str, Any]] | None = None,
    ) -> "SamplingReport":
        settings = ProcessSettings.from_dict(data["settings"])
        payloads = sample_payloads or {}
        samples = [
            SampleIdentity(
                sample_id=str(item["sample_id"]),
                group=str(item["group"]),
                fingerprint=str(item["fingerprint"]),
                seq=int(item["seq"]),
                payload=dict(payloads.get(str(item["sample_id"]), {})),
            )
            for item in data["sample_manifest"]
        ]
        report = cls(str(data["report_id"]), settings, samples)
        report.rounds = list(data.get("rounds") or [])
        report.inspections = [
            InspectionRecord.from_dict(item) for item in data.get("inspections") or []
        ]
        return report


def load_report(
    report_id: str, sample_payloads: dict[str, dict[str, Any]] | None = None
) -> SamplingReport:
    path = persist.data_path("sampling", f"reports/{report_id}.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return SamplingReport.from_dict(data, sample_payloads)


def verify_manifest(
    report_data: dict[str, Any], pages: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """核对样本身份：清单完整性与内容指纹一致性。

    只要页面带 payload，就重算内容指纹做比对（不直接信任页面自带的
    fingerprint 字段），从而能发现内容被篡改但指纹未同步的情况。
    """
    saved = {str(item["sample_id"]): item for item in report_data["sample_manifest"]}
    current = {str(p["sample_id"]): p for p in pages}
    missing = sorted(set(saved) - set(current))
    extra = sorted(set(current) - set(saved))
    changed: list[str] = []
    for sid, item in saved.items():
        page = current.get(sid)
        if page is None:
            continue
        payload = page.get("payload")
        actual_fp = content_hash(payload) if payload else str(page.get("fingerprint", ""))
        if actual_fp != item["fingerprint"]:
            changed.append(sid)
        if str(page.get("group")) != item["group"]:
            changed.append(sid)
    return {
        "ok": not missing and not extra and not changed,
        "missing_ids": missing,
        "extra_ids": extra,
        "changed_ids": sorted(set(changed)),
    }


def replay_report(pages: Sequence[dict[str, Any]], report_data: dict[str, Any]) -> dict[str, Any]:
    """用旧报告内嵌的设置重放全部轮次，核对抽样范围与样本身份。

    复算完全不读取工序的当前设置：设置来自报告快照，样本来自调用方
    提供的原始页面（先过指纹校验）。因此切换工序设置后，旧报告仍能
    复算出与当初一致的分组、预算和样本选择。
    """
    identity = verify_manifest(report_data, pages)
    settings = ProcessSettings.from_dict(report_data["settings"])
    samples = [SampleIdentity.from_page(p) for p in pages]
    sample_ids = {s.sample_id for s in samples}

    replay = SamplingReport(report_id=str(report_data["report_id"]) + "::replay",
                            settings=settings, samples=samples)
    mismatches: list[str] = []
    for saved_round in report_data.get("rounds", []):
        done = replay._inspected_ids()
        if saved_round["mode"] == "claimed":
            # 定向领取：按保存的 only_groups / 已选清单重建选择。
            only = set(saved_round.get("only_groups") or ()) or None
            pending = replay.pending_samples(only)
            wanted = {
                sid for rows in saved_round["selected"].values() for sid in rows
            }
            selection: dict[str, list[SampleIdentity]] = {}
            for s in pending:
                if s.sample_id in wanted:
                    selection.setdefault(s.group, []).append(s)
            replay_round = replay.run_round(
                _identity_inspector(report_data), explicit_selection=selection,
                only_groups=only,
            )
        else:
            replay_round = replay.run_round(
                _identity_inspector(report_data),
                budget=int(saved_round["budget_granted"]),
                only_groups=set(saved_round.get("only_groups") or ()) or None,
            )
        if replay_round["selected"] != saved_round["selected"]:
            mismatches.append(
                f"第{saved_round['round_no']}轮选择不一致: "
                f"复算={replay_round['selected']} 报告={saved_round['selected']}"
            )
        if replay_round["allocations"] != saved_round["allocations"]:
            mismatches.append(
                f"第{saved_round['round_no']}轮预算分配不一致: "
                f"复算={replay_round['allocations']} 报告={saved_round['allocations']}"
            )

    saved_sections = {s["group"]: s for s in report_data["group_sections"]}
    for section in replay.group_sections():
        saved = saved_sections.get(section.group)
        if saved is None:
            mismatches.append(f"复算缺少分组小节: {section.group}")
            continue
        for key in ("inspected_denominator", "abnormal_count", "quarantined_ids",
                    "pending_ids", "passed_ids", "status"):
            if saved.get(key) != section.to_dict().get(key):
                mismatches.append(
                    f"分组 {section.group} 字段 {key} 不一致: "
                    f"复算={section.to_dict().get(key)} 报告={saved.get(key)}"
                )

    unknown_saved = set()
    for rec in report_data.get("inspections", []):
        if rec["sample_id"] not in sample_ids:
            unknown_saved.add(rec["sample_id"])
    return {
        "report_id": report_data["report_id"],
        "settings_fingerprint": settings.fingerprint(),
        "identity_check": identity,
        "matched": identity["ok"] and not mismatches and not unknown_saved,
        "mismatches": mismatches + [f"报告引用了不存在的样本: {sorted(unknown_saved)}"]
        if unknown_saved else [],
        "replayed_rounds": len(replay.rounds),
        "replayed_budget_consumed": replay.budget_consumed(),
        "replayed_sections": [s.to_dict() for s in replay.group_sections()],
        "replayed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _identity_inspector(report_data: dict[str, Any]) -> Inspector:
    """复算用检核器：直接回放报告中保存的原始判定，不引入新判断。"""
    verdicts = {
        item["sample_id"]: (Verdict(item["verdict"]), item.get("reason", ""))
        for item in report_data.get("inspections", [])
    }

    def _inspect(sample: SampleIdentity) -> tuple[Verdict, str]:
        return verdicts.get(sample.sample_id, (Verdict.PENDING, "replay:no-record"))

    return _inspect


def render_text(report_data: dict[str, Any]) -> str:
    """生成人读版抽样报告，便于随产物归档与核对。"""
    settings = report_data["settings"]
    lines = [
        f"质量抽样报告 {report_data['report_id']}",
        f"工序: {settings['process_code']}  工序版本: {settings['process_version']}",
        f"设置指纹: {settings['settings_fingerprint']}  采样比例: {settings['sampling_ratio']}",
        f"累计实际检查(预算消耗): {report_data['budget_total_consumed']}  "
        f"轮次: {len(report_data['rounds'])}",
        "",
        "分组口径（分母只计本组已检，未检组不借别组结论放行）:",
    ]
    for sec in report_data["group_sections"]:
        lines.append(
            f"- [{sec['group']}] 组规模={sec['group_size']} "
            f"已检分母={sec['inspected_denominator']} 异常={sec['abnormal_count']} "
            f"状态={sec['status']}"
        )
        lines.append(f"    隔离: {sec['quarantined_ids']}")
        lines.append(f"    待检: {sec['pending_ids']}")
        lines.append(f"    下一步: {sec['next_step']}")
    lines.append("")
    lines.append("轮次记录:")
    for rnd in report_data["rounds"]:
        lines.append(
            f"- 第{rnd['round_no']}轮 模式={rnd['mode']} "
            f"授予预算={rnd['budget_granted']} 实际检查={rnd['budget_consumed']} "
            f"分配={rnd['allocations']}"
        )
    return "\n".join(lines)

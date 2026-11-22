"""清洗管道对质量抽样报告的消费。

按组持久化三类行级状态：

* ``DELIVERED``  可交付行：写出放行产物，重复消费不会重复生成；
* ``QUARANTINED`` 隔离行：只写隔离清单，禁止出数；
* ``PENDING``    待检范围：保留身份，后续轮次从原工序领取剩余样本。

状态落在 SQLite（标准库 sqlite3），按 (报告, 样本) 唯一约束保证幂等；
同时刷新三份 JSON 台账并追加事件 JSONL，方便离线核对。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any

from app import persistence as persist
from app.sampling.model import RowState, SampleIdentity
from app.sampling.report import SamplingReport


class SamplingPipeline:
    """消费抽样报告的清洗管道状态机。"""

    def __init__(self, process_code: str) -> None:
        self.process_code = process_code
        self.db_path = persist.data_path(
            "sampling", f"pipeline/{_safe(process_code)}.sqlite3"
        )
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _txn(self):
        """sqlite3 连接上下文：with 只托管事务，这里补上关闭。"""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS row_state (
                    report_id       TEXT NOT NULL,
                    sample_id       TEXT NOT NULL,
                    sample_group    TEXT NOT NULL,
                    process_code    TEXT NOT NULL,
                    process_version TEXT NOT NULL,
                    fingerprint     TEXT NOT NULL,
                    state           TEXT NOT NULL,
                    reason          TEXT NOT NULL DEFAULT '',
                    updated_at      TEXT NOT NULL,
                    PRIMARY KEY (report_id, sample_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_state ON row_state(process_code, state)"
            )
            conn.commit()
        finally:
            conn.close()

    # ---- 报告消费 -------------------------------------------------

    def consume_report(self, report: SamplingReport) -> dict[str, Any]:
        """按报告当前口径刷新行级状态并产出交付物（幂等）。"""
        verdict_reason = {r.sample_id: r.reason for r in report.inspections}
        sample_by_id = {s.sample_id: s for s in report.samples}
        sections = {sec.group: sec for sec in report.group_sections()}

        transitioned: dict[str, list[str]] = {
            RowState.DELIVERED.value: [],
            RowState.QUARANTINED.value: [],
            RowState.PENDING.value: [],
        }
        for group, sec in sections.items():
            for sample_id in sec.passed_ids:
                if self._transition(report, sample_by_id[sample_id],
                                    RowState.DELIVERED, ""):
                    self._write_deliverable(report, sample_by_id[sample_id])
                    transitioned[RowState.DELIVERED.value].append(sample_id)
            for sample_id in sec.quarantined_ids:
                reason = verdict_reason.get(sample_id, "")
                if self._transition(report, sample_by_id[sample_id],
                                    RowState.QUARANTINED, reason):
                    transitioned[RowState.QUARANTINED.value].append(sample_id)
            for sample_id in sec.pending_ids:
                if self._transition(report, sample_by_id[sample_id],
                                    RowState.PENDING, ""):
                    transitioned[RowState.PENDING.value].append(sample_id)

        ledgers = self._write_ledgers(report)
        summary = {
            "report_id": report.report_id,
            "process_code": self.process_code,
            "process_version": report.settings.process_version,
            "transitioned": transitioned,
            "counts": self.state_counts(report.report_id),
            "ledgers": ledgers,
            "timestamp": persist.now_iso(),
        }
        persist.append_jsonl("sampling", "pipeline_events.jsonl", summary)
        return summary

    def _transition(
        self,
        report: SamplingReport,
        sample: SampleIdentity,
        new_state: RowState,
        reason: str,
    ) -> bool:
        """状态推进；已处于目标状态则返回 False（幂等，不重复出产物）。"""
        with self._txn() as conn:
            row = conn.execute(
                "SELECT state FROM row_state WHERE report_id=? AND sample_id=?",
                (report.report_id, sample.sample_id),
            ).fetchone()
            if row is not None and row["state"] == new_state.value:
                return False
            conn.execute(
                """
                INSERT INTO row_state(
                    report_id, sample_id, sample_group, process_code,
                    process_version, fingerprint, state, reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id, sample_id) DO UPDATE SET
                    state=excluded.state, reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (
                    report.report_id, sample.sample_id, sample.group,
                    self.process_code, report.settings.process_version,
                    sample.fingerprint, new_state.value, reason,
                    persist.now_iso(),
                ),
            )
            return True

    def _write_deliverable(self, report: SamplingReport, sample: SampleIdentity) -> str:
        payload = dict(sample.payload)
        body = {
            "sample_id": sample.sample_id,
            "group": sample.group,
            "fingerprint": sample.fingerprint,
            "report_id": report.report_id,
            "process_code": self.process_code,
            "process_version": report.settings.process_version,
            "delivered_at": persist.now_iso(),
            "payload": payload,
        }
        path = persist.data_path(
            "sampling",
            f"deliverables/{_safe(report.report_id)}/{_safe(sample.group)}/"
            f"{_safe(sample.sample_id)}.json",
        )
        with open(path, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=2, default=str)
        return path

    def _write_ledgers(self, report: SamplingReport) -> dict[str, str]:
        base: dict[str, dict[str, list[str]]] = {
            RowState.DELIVERED.value: {},
            RowState.QUARANTINED.value: {},
            RowState.PENDING.value: {},
        }
        with self._txn() as conn:
            rows = conn.execute(
                "SELECT sample_id, sample_group, state, reason FROM row_state "
                "WHERE report_id=? ORDER BY sample_group, sample_id",
                (report.report_id,),
            ).fetchall()
        quarantine_detail: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            base[row["state"]].setdefault(row["sample_group"], []).append(row["sample_id"])
            if row["state"] == RowState.QUARANTINED.value:
                quarantine_detail.setdefault(row["sample_group"], []).append(
                    {"sample_id": row["sample_id"], "reason": row["reason"]}
                )
        ledger_dir = f"ledgers/{_safe(report.report_id)}"
        paths = {
            "deliverable": persist.write_json(
                "sampling", f"{ledger_dir}/deliverable_ids.json", base[RowState.DELIVERED.value]
            ),
            "quarantine": persist.write_json(
                "sampling", f"{ledger_dir}/quarantine_ids.json", quarantine_detail
            ),
            "pending": persist.write_json(
                "sampling", f"{ledger_dir}/pending_ids.json", base[RowState.PENDING.value]
            ),
        }
        return paths

    # ---- 待检领取 -------------------------------------------------

    def claim_pending(
        self,
        report: SamplingReport,
        *,
        groups: set[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """从原工序领取仍处于待检状态的剩余样本。

        只返回 PENDING 行；已交付/已隔离行不会再次出现，因此后续轮次
        不会重复生成已放行产物。``groups`` 可限定只领某一组
        （如第二轮只领第一轮留下的新模板行）。
        """
        with self._txn() as conn:
            sql = (
                "SELECT sample_id FROM row_state WHERE report_id=? AND state=?"
            )
            params: list[Any] = [report.report_id, RowState.PENDING.value]
            if groups:
                placeholders = ",".join("?" for _ in groups)
                sql += f" AND sample_group IN ({placeholders})"
                params.extend(sorted(groups))
            sql += " ORDER BY sample_group, sample_id"
            rows = conn.execute(sql, params).fetchall()
        claimed_ids = [r["sample_id"] for r in rows]
        if limit is not None:
            claimed_ids = claimed_ids[: max(0, int(limit))]
        by_id = {s.sample_id: s for s in report.samples}
        return [
            {
                "sample_id": sid,
                "group": by_id[sid].group,
                "fingerprint": by_id[sid].fingerprint,
                "seq": by_id[sid].seq,
                "payload": dict(by_id[sid].payload),
            }
            for sid in claimed_ids if sid in by_id
        ]

    def state_counts(self, report_id: str) -> dict[str, int]:
        with self._txn() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(1) AS n FROM row_state WHERE report_id=? GROUP BY state",
                (report_id,),
            ).fetchall()
        counts = {s.value: 0 for s in RowState}
        for row in rows:
            counts[row["state"]] = row["n"]
        return counts

    def state_of(self, report_id: str, sample_id: str) -> str | None:
        with self._txn() as conn:
            row = conn.execute(
                "SELECT state FROM row_state WHERE report_id=? AND sample_id=?",
                (report_id, sample_id),
            ).fetchone()
        return row["state"] if row else None

    def quarantine_list(self, report_id: str) -> list[dict[str, str]]:
        with self._txn() as conn:
            rows = conn.execute(
                "SELECT sample_id, sample_group, reason FROM row_state "
                "WHERE report_id=? AND state=? ORDER BY sample_group, sample_id",
                (report_id, RowState.QUARANTINED.value),
            ).fetchall()
        return [dict(r) for r in rows]


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(name))

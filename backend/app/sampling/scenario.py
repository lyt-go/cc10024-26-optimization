"""抽样场景构建：生成 90 常规页 + 10 新模板页的样本批次与检核器。

样本在生成时即计算内容指纹，异常页通过 payload 内的 defect 标记，
检核规则只读样本内容，不依赖内存外状态，保证可复算。
"""
from __future__ import annotations

from typing import Any, Sequence

from app.sampling.model import SampleIdentity, Verdict, content_hash

REGULAR_GROUP = "regular_page"
TEMPLATE_GROUP = "new_template_page"
GROUP_LABELS = {
    REGULAR_GROUP: "常规页",
    TEMPLATE_GROUP: "新模板页",
}


def build_page_batch(
    *,
    n_regular: int = 90,
    n_template: int = 10,
    abnormal_template_seqs: Sequence[int] = (3, 6, 9),
    process_version: str = "PROC-V1",
) -> list[dict[str, Any]]:
    """构建一批带身份与指纹的页面样本。

    新模板页中 ``abnormal_template_seqs`` 指定的序号带 defect 标记
    （默认 3 条，即「少数异常」），常规页全部合格。
    """
    abnormal_seqs = {int(i) for i in abnormal_template_seqs}
    pages: list[dict[str, Any]] = []
    seq = 0
    for i in range(1, n_regular + 1):
        seq += 1
        sample_id = f"PAGE-R-{i:04d}"
        payload = {
            "url": f"https://shop.example.com/regular/{i:04d}",
            "template": "regular",
            "title": f"常规商品页 {i}",
            "fields": ["title", "price", "sku"],
        }
        pages.append(_wrap(sample_id, REGULAR_GROUP, seq, payload, process_version))
    for i in range(1, n_template + 1):
        seq += 1
        sample_id = f"PAGE-T-{i:04d}"
        payload: dict[str, Any] = {
            "url": f"https://shop.example.com/template/{i:04d}",
            "template": "new_v2",
            "title": f"新模板商品页 {i}",
            "fields": ["title", "price", "sku", "promo_banner"],
        }
        if i in abnormal_seqs:
            payload["defect"] = "missing_required_field:promo_banner"
        pages.append(_wrap(sample_id, TEMPLATE_GROUP, seq, payload, process_version))
    return pages


def _wrap(
    sample_id: str, group: str, seq: int, payload: dict[str, Any], process_version: str
) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "group": group,
        "seq": seq,
        "fingerprint": content_hash(payload),
        "process_version": process_version,
        "payload": payload,
    }


def pages_to_identities(pages: Sequence[dict[str, Any]]) -> list[SampleIdentity]:
    return [SampleIdentity.from_page(p) for p in pages]


def rule_based_inspector():
    """质量规则检核器：payload 带 defect 即异常，否则合格。

    判定完全由样本内容决定，复算时可由报告保存的 verdict 原样回放。
    """

    def inspect(sample: SampleIdentity) -> tuple[Verdict, str]:
        defect = sample.payload.get("defect")
        if defect:
            return Verdict.ABNORMAL, f"quality_rule:{defect}"
        return Verdict.PASS, "quality_rule:ok"

    return inspect

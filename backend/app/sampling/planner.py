"""确定性抽样与预算分配。

两份逻辑都是纯函数、无随机数：给定相同的样本清单、工序设置和预算，
任意机器、任意时间重放都得到同一批样本，这是「可复算」的基础。
"""
from __future__ import annotations

import hashlib
from typing import Sequence

from app.sampling.model import SampleIdentity


def deterministic_rank(identity: SampleIdentity, settings_fingerprint: str) -> str:
    """样本在指定工序设置下的确定性抽样排名键。

    排名键混入工序设置指纹：同一批样本在不同工序设置下排名不同，
    但同一份旧报告内嵌的设置指纹不变，复算结果就不变。
    """
    raw = f"{settings_fingerprint}|{identity.group}|{identity.sample_id}|{identity.fingerprint}"
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()


def order_samples(
    samples: Sequence[SampleIdentity], settings_fingerprint: str
) -> list[SampleIdentity]:
    """按 (组内确定性排名, seq) 排序，组内顺序稳定可复算。"""
    return sorted(
        samples,
        key=lambda s: (s.group, deterministic_rank(s, settings_fingerprint), s.seq),
    )


def allocate_budget(
    group_sizes: dict[str, int], budget: int
) -> dict[str, int]:
    """按组样本量占比分配检查预算（最大余数法）。

    约束：
    * 每组分配数不超过该组待检样本数；
    * 某组用不满的名额（如新模板组只有 10 条却分到更多）按余数
      顺序让给其他仍有待检样本的组，预算始终只对应实际可检查的行；
    * 并列时按组名排序，保证结果确定。

    例：90 常规页 + 10 新模板页、预算 10 → 常规 9、新模板 1。
    """
    if budget < 0:
        raise ValueError("预算不能为负")
    sizes = {g: max(0, int(n)) for g, n in group_sizes.items() if int(n) > 0}
    total = sum(sizes.values())
    if total == 0 or budget == 0:
        return {g: 0 for g in sizes}

    budget = min(budget, total)
    quota = {g: budget * size / total for g, size in sizes.items()}
    alloc = {g: min(sizes[g], int(quota[g])) for g in sizes}
    remaining = budget - sum(alloc.values())

    # 余数从大到小补名额；受组容量限制的组把名额让给后续组。
    while remaining > 0:
        candidates = [g for g in sizes if alloc[g] < sizes[g]]
        if not candidates:
            break
        candidates.sort(key=lambda g: (-(quota[g] - int(quota[g])), g))
        progressed = False
        for g in candidates:
            if remaining == 0:
                break
            alloc[g] += 1
            remaining -= 1
            progressed = True
        if not progressed:
            break
    return alloc


def budget_from_ratio(population: int, sampling_ratio: float) -> int:
    """由工序采样比例推导每轮检查预算。

    比例支持 0~1 小数（0.1）或百分数（10 表示 10%）；结果四舍五入且
    不超过总体规模。例：100 条样本、比例 0.10 → 预算 10。
    """
    if population <= 0:
        return 0
    ratio = min(1.0, max(0.0, float(sampling_ratio)))
    return min(population, round(population * ratio))


def plan_selection(
    samples: Sequence[SampleIdentity],
    settings_fingerprint: str,
    budget: int,
    already_inspected: frozenset[str] | set[str] | None = None,
    only_groups: set[str] | None = None,
) -> dict[str, list[SampleIdentity]]:
    """产出本轮按组选择的待检样本。

    ``already_inspected`` 中的样本不会被再次选中（不重复检查、
    不重复消耗预算）；``only_groups`` 用于第二轮只领取指定组
    （如第一轮留下的新模板行）。
    """
    done = set(already_inspected or ())
    eligible = [
        s for s in samples
        if s.sample_id not in done and (only_groups is None or s.group in only_groups)
    ]
    ordered = order_samples(eligible, settings_fingerprint)
    by_group: dict[str, list[SampleIdentity]] = {}
    for s in ordered:
        by_group.setdefault(s.group, []).append(s)

    alloc = allocate_budget({g: len(rows) for g, rows in by_group.items()}, budget)
    return {g: rows[: alloc[g]] for g, rows in by_group.items() if alloc[g] > 0}

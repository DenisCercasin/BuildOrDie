"""Group optimisation: decide who buys with whom, and where.

Objective: maximise total savings across all open requests, subject to
* everyone in a group shares one pickup point and one merchant,
* every member's expected delivery is on or before their deadline,
* every member stays within budget,
* every member saves at least ``min_saving_per_member`` vs buying alone
  (nobody is made worse off by joining a group),
* groups are at most ``max_group_size`` requests.

For up to ``exact_solver_max_requests`` requests per pickup point we solve the
set-partitioning problem exactly (subset DP, ~3^n). Beyond that we fall back to
a greedy agglomerative merge, which is fast and usually close to optimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations

from .costing import Ctx, GroupCosting, IndividualOption, baseline, cost_group
from .models import BookRequest
from .money import ZERO, total


@dataclass(frozen=True)
class BlockPlan:
    """A feasible group: its members, chosen merchant and costing."""

    requests: tuple[BookRequest, ...]
    costing: GroupCosting
    savings: Decimal  # total savings vs everyone buying alone


def candidate_merchants(reqs: list[BookRequest], ctx: Ctx) -> list[str]:
    common: set[str] | None = None
    for r in reqs:
        ms = set(ctx.book.allowed_merchants(r))
        common = ms if common is None else common & ms
        if not common:
            return []
    return sorted(common or [])


def evaluate_block(
    reqs: list[BookRequest], ctx: Ctx, baselines: dict[str, IndividualOption]
) -> BlockPlan | None:
    """Best merchant for this exact set of requests, or None if no feasible group."""
    if len(reqs) < 2 or len(reqs) > ctx.cfg.max_group_size:
        return None
    if len({r.pickup_point for r in reqs}) != 1:
        return None
    if any(r.request_id not in baselines for r in reqs):
        return None

    best: BlockPlan | None = None
    for mid in candidate_merchants(reqs, ctx):
        gc = cost_group(reqs, mid, ctx)
        if gc is None:
            continue
        ok = True
        savings = []
        for r, line in zip(reqs, gc.lines):
            if gc.expected_delivery > r.deadline:
                ok = False
                break
            if r.max_budget is not None and line.total > r.max_budget:
                ok = False
                break
            s = baselines[r.request_id].total - line.total
            if s < ctx.cfg.min_saving_per_member:
                ok = False
                break
            savings.append(s)
        if not ok:
            continue
        plan = BlockPlan(tuple(reqs), gc, total(savings))
        if best is None or _better(plan, best):
            best = plan
    return best


def _better(a: BlockPlan, b: BlockPlan) -> bool:
    """Higher savings wins; then earlier delivery; then merchant id (determinism)."""
    return (-a.savings, a.costing.expected_delivery, a.costing.merchant_id) < (
        -b.savings,
        b.costing.expected_delivery,
        b.costing.merchant_id,
    )


def _solve_exact(reqs: list[BookRequest], ctx: Ctx, baselines) -> list[BlockPlan]:
    n = len(reqs)
    k_max = min(ctx.cfg.max_group_size, n)

    blocks: dict[int, BlockPlan] = {}
    for k in range(2, k_max + 1):
        for idx in combinations(range(n), k):
            plan = evaluate_block([reqs[i] for i in idx], ctx, baselines)
            if plan is not None:
                mask = 0
                for i in idx:
                    mask |= 1 << i
                blocks[mask] = plan

    # best[mask] = (savings, chosen block masks) for an optimal partition of `mask`
    memo: dict[int, tuple[Decimal, tuple[int, ...]]] = {0: (ZERO, ())}

    def solve(mask: int) -> tuple[Decimal, tuple[int, ...]]:
        if mask in memo:
            return memo[mask]
        low = mask & -mask
        rest = mask ^ low
        # option 1: lowest request stays solo
        best_val, best_sel = solve(rest)
        # option 2: lowest request joins a block within `mask`
        sub = rest
        while True:
            m = sub | low
            plan = blocks.get(m)
            if plan is not None:
                v, sel = solve(mask ^ m)
                cand = v + plan.savings
                if cand > best_val or (cand == best_val and len(sel) + 1 < len(best_sel)):
                    best_val, best_sel = cand, sel + (m,)
            if sub == 0:
                break
            sub = (sub - 1) & rest
        memo[mask] = (best_val, best_sel)
        return memo[mask]

    _, chosen = solve((1 << n) - 1)
    return [blocks[m] for m in chosen]


def _solve_greedy(reqs: list[BookRequest], ctx: Ctx, baselines) -> list[BlockPlan]:
    """Agglomerative: repeatedly apply the merge with the largest savings gain."""
    clusters: list[list[BookRequest]] = [[r] for r in reqs]

    def value(c: list[BookRequest]) -> Decimal:
        if len(c) < 2:
            return ZERO
        p = evaluate_block(c, ctx, baselines)
        return p.savings if p else Decimal("-1")

    while True:
        best_gain, best_pair = ZERO, None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                merged = clusters[i] + clusters[j]
                if len(merged) > ctx.cfg.max_group_size:
                    continue
                p = evaluate_block(merged, ctx, baselines)
                if p is None:
                    continue
                gain = p.savings - value(clusters[i]) - value(clusters[j])
                if gain > best_gain:
                    best_gain, best_pair = gain, (i, j)
        if best_pair is None:
            break
        i, j = best_pair
        merged = clusters[i] + clusters[j]
        clusters = [c for k, c in enumerate(clusters) if k not in (i, j)] + [merged]

    out = []
    for c in clusters:
        if len(c) >= 2:
            p = evaluate_block(c, ctx, baselines)
            if p is not None:
                out.append(p)
    return out


def optimise(
    reqs: list[BookRequest], ctx: Ctx
) -> tuple[list[BlockPlan], dict[str, IndividualOption]]:
    """Partition open requests into groups. Returns (groups, baselines).

    Requests with no baseline (nothing purchasable) are excluded from grouping.
    """
    baselines: dict[str, IndividualOption] = {}
    for r in reqs:
        b = baseline(r, ctx)
        if b is not None:
            baselines[r.request_id] = b

    by_pickup: dict[str, list[BookRequest]] = {}
    for r in sorted(reqs, key=lambda r: (r.deadline, r.request_id)):
        if r.request_id in baselines:
            by_pickup.setdefault(r.pickup_point, []).append(r)

    groups: list[BlockPlan] = []
    for pickup in sorted(by_pickup):
        cluster = by_pickup[pickup]
        if len(cluster) < 2:
            continue
        if len(cluster) <= ctx.cfg.exact_solver_max_requests:
            groups.extend(_solve_exact(cluster, ctx, baselines))
        else:
            groups.extend(_solve_greedy(cluster, ctx, baselines))
    return groups, baselines

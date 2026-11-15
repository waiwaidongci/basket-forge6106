"""Joint bundle-pilot optimiser.

A receipt may carry several *non-overlapping* bundles at once: each product on
a receipt is allocated at most once.  A bundle type can occupy at most one
display slot (``max_slots`` <= 8) and every SKU has a global supply cap.  The
objective is discounted bundle gross profit.

This is a weighted set-packing problem with cardinality and knapsack
constraints (NP-hard).  The optimiser returns a deterministic feasible plan, a
recomputable upper bound, the gap, and an explicit ``optimal`` flag.  It is
pure Python, single-threaded, and every phase is deadline aware so a plan over
5000 receipts finishes inside the caller's wall-clock budget.

Profit bookkeeping (the caller freezes the price basis in the stored plan)
----------------------------------------------------------------------------
list_price(bundle)  = sum of product list prices
discounted_revenue  = list_price * (1 - discount)
gross_profit / hit  = discounted_revenue - sum of product costs

Fixed costs, shrink and display cost are not modelled.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import time
from collections import Counter

ALGORITHM_VERSION = "joint-v1"
MAX_CANDIDATES = 24
MAX_SLOTS = 8
MIN_TIME_LIMIT = 1.0
MAX_TIME_LIMIT = 60.0
_EPS = 1e-7


def _ceil_cent(value: float) -> float:
    """Round a bound upwards to whole cents; an upper bound must never shrink."""
    return math.ceil(value * 100 - 1e-6) / 100.0


def _r2(value: float) -> float:
    return round(value + _EPS, 2)


def _canon_items(value) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(sorted({str(x).strip() for x in value if str(x).strip()}))


def _presence(bits: int) -> list[int]:
    out = []
    while bits:
        low = bits & -bits
        out.append(low.bit_length() - 1)
        bits ^= low
    return out


def _normalise_receipts(receipts) -> list[dict]:
    if not receipts:
        raise ValueError("批次没有收据")
    rows = []
    for index, receipt in enumerate(receipts):
        if isinstance(receipt, dict):
            reference = str(receipt.get("reference") or f"R{index + 1:05d}")
            items = receipt.get("items") or []
        else:
            reference = f"R{index + 1:05d}"
            items = receipt
        if not isinstance(items, (list, tuple, set)):
            raise ValueError("收据商品必须是数组")
        clean = _canon_items(items)
        if not clean:
            raise ValueError("收据不得为空")
        rows.append({"index": index, "reference": reference, "items": clean})
    return rows


def _normalise_candidates(candidates) -> list[dict]:
    if not candidates:
        raise ValueError("候选组合不能为空")
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError(f"候选组合最多 {MAX_CANDIDATES} 个")
    result = []
    for position, raw in enumerate(candidates):
        items = _canon_items(raw.get("items"))
        if not items:
            items = _canon_items(
                list(raw.get("antecedent") or []) + list(raw.get("consequent") or [])
            )
        if not items:
            raise ValueError("候选组合不能为空项集")
        rule_index = raw.get("rule_index")
        if rule_index is None:
            rule_index = position
        result.append(
            {
                "input_position": position,
                "rule_index": int(rule_index),
                "items": items,
                "antecedent": list(raw.get("antecedent") or []),
                "consequent": list(raw.get("consequent") or []),
                "support_count": raw.get("support_count"),
                "support": raw.get("support"),
                "confidence": raw.get("confidence"),
                "lift": raw.get("lift"),
            }
        )
    return result


class Packer:
    """Exact per-receipt packing with a shared memo and a hard state budget.

    A receipt's present-bundle mask has at most ``max_slots`` relevant bits
    during the search, so each call is tiny.  ``bound=True`` falls back to the
    product-share relaxation (a valid upper bound); ``bound=False`` falls back
    to a deterministic greedy packing (feasible).  The fallbacks stop one
    adversarial basket from exhausting the time budget.
    """

    def __init__(self, weights, item_masks, bound: bool, budget: int = 300_000):
        self.weights = weights
        self.item_masks = item_masks
        self.bound = bound
        self.budget = budget
        self.memo: dict[int, tuple[float, tuple[int, ...]]] = {0: (0.0, ())}
        self.states = 0
        self.fallbacks = 0
        self.conflicts = []
        for index, mask in enumerate(item_masks):
            conflict = 1 << index
            for other, other_mask in enumerate(item_masks):
                if mask & other_mask:
                    conflict |= 1 << other
            self.conflicts.append(conflict)

    def _share_bound(self, mask: int) -> float:
        best: dict[int, float] = {}
        for index in _presence(mask):
            weight = self.weights[index]
            if weight <= 0:
                continue
            share = weight / len(self.item_masks[index])
            for product in _presence(self.item_masks[index]):
                if share > best.get(product, 0.0):
                    best[product] = share
        return sum(best.values())

    def _greedy(self, mask: int) -> tuple[float, tuple[int, ...]]:
        order = sorted(_presence(mask), key=lambda i: (-self.weights[i], i))
        used = 0
        chosen = []
        value = 0.0
        for index in order:
            if self.item_masks[index] & used:
                continue
            used |= self.item_masks[index]
            chosen.append(index)
            value += self.weights[index]
        return value, tuple(sorted(chosen))

    def pack(self, mask: int) -> tuple[float, tuple[int, ...]]:
        """Exact max-weight conflict-free packing of candidate bits ``mask``."""
        found = self.memo.get(mask)
        if found is not None:
            return found
        if self.states >= self.budget:
            self.fallbacks += 1
            answer = (self._share_bound(mask), ()) if self.bound else self._greedy(mask)
            self.memo[mask] = answer
            return answer
        self.states += 1
        low = mask & -mask
        index = low.bit_length() - 1
        rest = mask ^ low
        skip_value, skip_set = self.pack(rest)
        take_value, take_set = self.pack(rest & ~self.conflicts[index])
        take_value += self.weights[index]
        if take_value > skip_value + 1e-9:
            answer = (take_value, (index,) + take_set)
        else:
            answer = (skip_value, skip_set)
        self.memo[mask] = answer
        return answer


# ---------------------------------------------------------------------------
# Upper bounds (all valid independently of the heuristic)
# ---------------------------------------------------------------------------


def _slot_bound_value(beta, weights, hit_caps, item_masks, caps, max_slots):
    """Slots / caps dual bound.  ``beta`` and ``caps`` are index aligned."""
    contributions = []
    for index, weight in enumerate(weights):
        if hit_caps[index] <= 0:
            continue
        adjusted = weight - sum(beta.get(p, 0.0) for p in _presence(item_masks[index]))
        if adjusted > 0:
            contributions.append((hit_caps[index] * adjusted, index, adjusted))
    contributions.sort(key=lambda item: (-item[0], item[1]))
    supply_term = sum(beta.get(index, 0.0) * caps[index] for index in range(len(caps)))
    total = supply_term + sum(item[0] for item in contributions[:max_slots])
    return total, contributions[:max_slots], supply_term


def _slot_bound(weights, hit_caps, item_masks, cap_list, max_slots, deadline):
    """Minimise the supply-dual / slot upper bound over non-negative beta.

    For any beta >= 0 and any feasible plan:
        profit <= sum_p beta_p * cap_p
                  + sum of the max_slots largest positive
                    hit_cap_b * (profit_b - sum_{p in b} beta_p)
    At most ``max_slots`` bundle types receive hits and one type can be hit at
    most ``hit_cap_b = min(support_b, cap_p for p in b)`` times.  Coordinate
    descent over beta breakpoints tightens the bound; any beta stays valid so
    an early return is harmless.
    """
    products = sorted({p for mask in item_masks for p in _presence(mask)})
    if not products:
        return 0.0, {}, [], 0.0
    beta = {p: 0.0 for p in products}
    best_total, _, _ = _slot_bound_value(
        beta, weights, hit_caps, item_masks, cap_list, max_slots
    )
    for _sweep in range(6):
        improved = False
        for product in products:
            if time.monotonic() >= deadline:
                break
            values = {0.0}
            for index, mask in enumerate(item_masks):
                if product not in _presence(mask):
                    continue
                other = sum(beta[p] for p in _presence(mask) if p != product)
                candidate = weights[index] - other
                if candidate > 0:
                    values.add(candidate)
            local_total = best_total
            local_value = beta[product]
            for value in sorted(values):
                if abs(value - beta[product]) < 1e-12:
                    continue
                trial = dict(beta)
                trial[product] = value
                total, _, _ = _slot_bound_value(
                    trial, weights, hit_caps, item_masks, cap_list, max_slots
                )
                if total < local_total - 1e-9:
                    local_total = total
                    local_value = value
            if abs(local_value - beta[product]) > 1e-12:
                beta[product] = local_value
                best_total = local_total
                improved = True
        if not improved or time.monotonic() >= deadline:
            break
    beta = {p: round(value, 6) for p, value in beta.items()}
    total, contributions, supply_term = _slot_bound_value(
        beta, weights, hit_caps, item_masks, cap_list, max_slots
    )
    return total, beta, contributions, supply_term


def _conflict_bound(weights, beta, item_masks, cap_list, present_counts, deadline, budget=300_000):
    """Slots-relaxed, per-receipt conflict-aware bound with supply duals."""
    adjusted = [
        max(0.0, weight - sum(beta.get(p, 0.0) for p in _presence(item_masks[index])))
        for index, weight in enumerate(weights)
    ]
    supply_term = sum(beta.get(index, 0.0) * cap_list[index] for index in range(len(cap_list)))
    packer = Packer(adjusted, item_masks, bound=True, budget=budget)
    total = supply_term
    for mask, count in present_counts.items():
        if time.monotonic() >= deadline:
            return None, False, packer
        value, _ = packer.pack(mask)
        total += value * count
    return _ceil_cent(total), True, packer


def _exact_selection_bound(weights, present_counts, item_masks, max_slots, deadline, work_budget=8_000_000):
    """Exact best selection value ignoring supply caps (a valid relaxation).

    Enumerates every bundle-type selection of size <= max_slots and evaluates
    it with exact per-receipt packing.  Returns ``(value, complete, work)``.
    """
    count = len(weights)
    packer = Packer(weights, item_masks, bound=True)
    best = 0.0
    work = 0
    for size in range(1, min(max_slots, count) + 1):
        for combo in itertools.combinations(range(count), size):
            if time.monotonic() >= deadline or work > work_budget:
                return best, False, work
            selection = 0
            for index in combo:
                selection |= 1 << index
            total = 0.0
            for mask, group_count in present_counts.items():
                present = mask & selection
                if present:
                    total += packer.pack(present)[0] * group_count
            work += len(present_counts)
            if total > best + 1e-9:
                best = total
    return best, True, work


# ---------------------------------------------------------------------------
# Selection search (ignores supply caps; exact per-receipt conflicts)
# ---------------------------------------------------------------------------


def _selection_profile(selection, present_counts):
    """Aggregate receipts by their selected-candidate pattern."""
    profile: dict[int, int] = {}
    for mask, count in present_counts.items():
        pattern = mask & selection
        if pattern:
            profile[pattern] = profile.get(pattern, 0) + count
    return profile


def _marginal_gain(candidate, selection, present_counts, packer, masks_with_candidate):
    bit = 1 << candidate
    gain = 0.0
    for mask, count in masks_with_candidate:
        pattern = mask & selection
        gain += (packer.pack(pattern | bit)[0] - packer.pack(pattern)[0]) * count
    return gain


def _greedy_selection(weights, present_counts, packer, max_slots, deadline, masks_by_candidate, start=0):
    selection = start
    if selection.bit_count() >= max_slots:
        return selection
    for _round in range(max_slots - selection.bit_count()):
        if time.monotonic() >= deadline:
            break
        best_gain = 1e-7
        best_index = None
        for candidate in range(len(weights)):
            if selection & (1 << candidate):
                continue
            if time.monotonic() >= deadline:
                break
            gain = _marginal_gain(
                candidate, selection, present_counts, packer, masks_by_candidate[candidate]
            )
            if gain > best_gain:
                best_gain = gain
                best_index = candidate
        if best_index is None:
            break
        selection |= 1 << best_index
    return selection


class _CappedEvaluator:
    """Score a selection by its capacity-feasible allocation (memoised).

    ``_allocate`` is cheap on distinct-basket groups, so the search can use
    the true capped objective instead of an uncapped surrogate.  ``None``
    candidates (evaluation cut off by the deadline) keep the incumbent.
    """

    def __init__(self, receipt_patterns, present_counts, packer, weights,
                 candidate_items, caps, deadline):
        self.receipt_patterns = receipt_patterns
        self.present_counts = present_counts
        self.packer = packer
        self.weights = weights
        self.candidate_items = candidate_items
        self.caps = caps
        self.deadline = deadline
        self.memo: dict[int, tuple[float, Counter]] = {}

    def score(self, selection):
        found = self.memo.get(selection)
        if found is not None:
            return found
        if time.monotonic() >= self.deadline:
            return None
        allowed = 0
        for index in _presence(selection):
            if all(self.caps.get(sku, 0) > 0 for sku in self.candidate_items[index]):
                allowed |= 1 << index
        if not allowed:
            return (0.0, Counter())
        profile = _selection_profile(allowed, self.present_counts)
        variants, usage, _ideal, _timed = _allocate(
            allowed, profile, self.packer, self.weights, self.candidate_items,
            self.caps, self.deadline,
        )
        total = 0.0
        for pattern, entries in variants.items():
            for hits, count in entries:
                total += sum(self.weights[index] for index in hits) * count
        found = (round(total, 6), usage)
        self.memo[selection] = found
        return found


def _capped_swap_search(selection, evaluator, max_slots, candidate_count, deadline, rounds=8):
    """Best-improvement add / one-swap search on the capped objective."""
    for _round in range(rounds):
        if time.monotonic() >= deadline:
            break
        selected = _presence(selection)
        unselected = [index for index in range(candidate_count) if not selection & (1 << index)]
        moves = []
        if len(selected) < max_slots:
            moves.extend((None, new) for new in unselected)
        moves.extend((old, new) for old in selected for new in unselected)
        current = evaluator.score(selection)
        if current is None:
            break
        best_value = current[0]
        best_selection = selection
        for old, new in moves:
            if time.monotonic() >= deadline:
                break
            trial = (selection | (1 << new)) if old is None else (
                (selection & ~(1 << old)) | (1 << new)
            )
            scored = evaluator.score(trial)
            if scored is not None and scored[0] > best_value + 1e-7:
                best_value = scored[0]
                best_selection = trial
        if best_selection == selection:
            break
        selection = best_selection
    return selection


def _top_selection(scores, max_slots):
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    selection = 0
    for index in order[:max_slots]:
        if scores[index] > 0:
            selection |= 1 << index
    return selection


# ---------------------------------------------------------------------------
# Capacity-aware allocation of one selection
# ---------------------------------------------------------------------------


def _allocate(selection, profile, packer, weights, candidate_items, caps, deadline):
    """Allocate a fixed selection to receipt patterns under SKU caps.

    ``profile`` maps selected patterns to receipt counts.  Returns
    ``(pattern_variants, usage, profit, timed_out)`` where ``pattern_variants``
    maps a pattern to an ordered list of ``(hits_tuple, count)``.  The sum of
    counts per pattern equals its profile count; every hit set is conflict-free
    and inside the pattern.
    """
    states: dict[int, list[list]] = {}
    usage: Counter[str] = Counter()
    timed_out = False
    for pattern, count in sorted(profile.items()):
        if pattern == 0:
            continue
        hits = packer.pack(pattern)[1]
        if not hits:
            continue
        states[pattern] = [[tuple(sorted(hits)), count]]
        for index in hits:
            for sku in candidate_items[index]:
                usage[sku] += count

    # ---- Bulk drop until every supply cap holds -------------------------
    # Dropping never increases usage, so each iteration permanently resolves
    # one violated product; at most len(caps)+1 iterations are needed.
    for _guard in range(len(caps) + 2):
        violations = [(usage[sku] - cap, sku) for sku, cap in caps.items() if usage[sku] > cap]
        if not violations:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        excess, product = max(violations, key=lambda item: (item[0], item[1]))
        options = []
        for pattern in sorted(states):
            for entry in states[pattern]:
                hits, count = entry
                if count <= 0:
                    continue
                for index in hits:
                    if product in candidate_items[index]:
                        options.append((weights[index], pattern, tuple(hits), index, count))
        options.sort(key=lambda item: (item[0], -item[4], item[1], item[3]))
        removed = 0
        for _, pattern, hits, index, _ in options:
            if removed >= excess:
                break
            entry = None
            for item in states.get(pattern, ()):
                if tuple(item[0]) == hits and item[1] > 0:
                    entry = item
                    break
            if entry is None:
                continue
            take = min(entry[1], excess - removed)
            if take <= 0:
                continue
            entry[1] -= take
            reduced = tuple(i for i in hits if i != index)
            target = states.setdefault(pattern, [])
            placed = False
            for item in target:
                if item[0] == reduced:
                    item[1] += take
                    placed = True
                    break
            if not placed:
                target.append([reduced, take])
            for sku in candidate_items[index]:
                usage[sku] -= take
            removed += take
        if removed == 0:
            # Defensive: no removable hit carries the product (should not
            # happen because usage counts only packed hits).  Drop every entry
            # so feasibility is guaranteed rather than assumed.
            states = {pattern: [] for pattern in states}
            usage = Counter()
            break

    # ---- Hard feasibility repair ----------------------------------------
    # Guarantee usage <= caps even if the deadline cut the drop loop short.
    violated = [sku for sku, cap in caps.items() if usage[sku] > cap]
    if violated:
        for pattern in sorted(states):
            for entry in states[pattern]:
                hits, count = entry
                if count <= 0:
                    continue
                kept = tuple(
                    index for index in hits
                    if not any(sku in candidate_items[index] for sku in violated)
                )
                if kept != tuple(hits):
                    entry[1] = 0
                    for item in states[pattern]:
                        if item[0] == kept:
                            item[1] += count
                            break
                    else:
                        states[pattern].append([kept, count])
        usage = Counter()
        for pattern, entries in states.items():
            for hits, count in entries:
                if count <= 0:
                    continue
                for index in hits:
                    for sku in candidate_items[index]:
                        usage[sku] += count
        still = [sku for sku, cap in caps.items() if usage[sku] > cap]
        if still:
            states = {pattern: [] for pattern in states}
            usage = Counter()
            timed_out = True
    states = {
        pattern: [entry for entry in entries if entry[1] > 0]
        for pattern, entries in states.items()
        if any(entry[1] > 0 for entry in entries)
    }

    # ---- Deterministic add-back into residual supply --------------------
    if not timed_out:
        improved = True
        while improved:
            improved = False
            if time.monotonic() >= deadline:
                timed_out = True
                break
            for pattern in sorted(states):
                entries = states[pattern]
                for position, entry in enumerate(list(entries)):
                    hits, count = entry
                    if count <= 0:
                        continue
                    added = False
                    used_items = set()
                    for index in hits:
                        used_items.update(candidate_items[index])
                    for index in _presence(pattern):
                        if index in hits:
                            continue
                        if used_items & set(candidate_items[index]):
                            continue
                        headroom = min(
                            caps.get(sku, 0) - usage[sku] for sku in candidate_items[index]
                        )
                        add = min(count, headroom)
                        if add <= 0:
                            continue
                        entry[1] -= add
                        new_hits = tuple(sorted(hits + (index,)))
                        target = states.setdefault(pattern, [])
                        placed = False
                        for item in target:
                            if item[0] == new_hits:
                                item[1] += add
                                placed = True
                                break
                        if not placed:
                            target.append([new_hits, add])
                        for sku in candidate_items[index]:
                            usage[sku] += add
                        improved = True
                        added = True
                        break
                    if added:
                        break
                if improved:
                    break

    variants = {}
    profit = 0.0
    for pattern in sorted(states):
        entries = sorted(states[pattern], key=lambda item: (-len(item[0]), item[0]))
        variants[pattern] = [
            (tuple(hits), int(count)) for hits, count in entries if count > 0
        ]
        for hits, count in entries:
            profit += sum(weights[index] for index in hits) * count
    return variants, usage, profit, timed_out


def _materialise(pattern_variants, receipt_patterns):
    """Expand per-pattern variants to every receipt, preserving input order."""
    cursors: dict[int, list] = {}
    hits_by_receipt = []
    for pattern in receipt_patterns:
        if pattern == 0:
            hits_by_receipt.append(())
            continue
        entries = pattern_variants.get(pattern)
        if not entries:
            hits_by_receipt.append(())
            continue
        cursor = cursors.get(pattern, 0)
        hits = ()
        remaining = cursor
        for hit_set, count in entries:
            if remaining < count:
                hits = hit_set
                break
            remaining -= count
        cursors[pattern] = cursor + 1
        hits_by_receipt.append(hits)
    return hits_by_receipt


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def optimize(receipts, candidates, products, discount, max_slots, supply, time_limit=25.0):
    """Optimise a joint bundle plan.

    ``receipts``: [{'reference', 'items'}]
    ``candidates``: <= 24 rule-like dicts (antecedent/consequent or items)
    ``products``: {sku: {'price','cost','name'}}
    ``discount``: 0 <= d < 1 applied to bundle list price
    ``max_slots``: 1..8 display slots
    ``supply``: {sku: non-negative integer cap}
    ``time_limit``: wall-clock seconds for the whole optimisation

    Returns a deterministic dict.  ``optimal`` is True only when a recomputable
    upper bound has been reached; otherwise ``status`` is ``feasible`` and the
    gap is reported.
    """
    try:
        limit = float(time_limit)
    except (TypeError, ValueError):
        raise ValueError("时间上限不合法")
    if not math.isfinite(limit) or not MIN_TIME_LIMIT <= limit <= MAX_TIME_LIMIT:
        raise ValueError(f"时间上限必须在 {MIN_TIME_LIMIT:g} 到 {MAX_TIME_LIMIT:g} 秒之间")
    started = time.monotonic()
    deadline = started + max(0.4, limit - 0.4)
    rows = _normalise_receipts(receipts)
    cands = _normalise_candidates(candidates)
    if not isinstance(discount, (int, float)) or not math.isfinite(discount) or not 0 <= discount < 1:
        raise ValueError("折扣必须在 0（含）到 1（不含）之间")
    if isinstance(max_slots, bool) or not isinstance(max_slots, int) or not 1 <= max_slots <= MAX_SLOTS:
        raise ValueError(f"陈列名额必须在 1 到 {MAX_SLOTS} 之间")
    if not isinstance(products, dict) or not products:
        raise ValueError("商品价格表不能为空")
    if not isinstance(supply, dict):
        raise ValueError("供给上限格式不合法")

    eligible = []
    excluded = []
    seen_items = set()
    for cand in cands:
        missing = [sku for sku in cand["items"] if sku not in products]
        if missing:
            raise ValueError("存在未知商品: " + ",".join(sorted(missing)))
        price = sum(float(products[sku]["price"]) for sku in cand["items"])
        cost = sum(float(products[sku]["cost"]) for sku in cand["items"])
        revenue = price * (1.0 - discount)
        per_hit = _r2(revenue - cost)
        record = {
            **cand,
            "list_price": _r2(price),
            "list_cost": _r2(cost),
            "discounted_revenue": _r2(revenue),
            "gross_profit_per_hit": per_hit,
        }
        if per_hit <= 0:
            record["excluded_reason"] = "折扣后单次毛利不为正"
            excluded.append(record)
            continue
        if cand["items"] in seen_items:
            record["excluded_reason"] = "与前序候选商品集合重复"
            excluded.append(record)
            continue
        seen_items.add(cand["items"])
        eligible.append(record)

    if not eligible:
        return _empty_result(rows, cands, excluded, discount, max_slots, supply, products, started, limit)

    product_skus = sorted({sku for cand in eligible for sku in cand["items"]})
    caps: dict[str, int] = {}
    for sku in product_skus:
        if sku not in supply:
            raise ValueError(f"缺少商品供给上限: {sku}")
        value = supply[sku]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"商品供给上限必须是非负整数: {sku}")
        if int(value) != value or value < 0:
            raise ValueError(f"商品供给上限必须是非负整数: {sku}")
        caps[sku] = int(value)

    weights = [cand["gross_profit_per_hit"] for cand in eligible]
    candidate_items = [cand["items"] for cand in eligible]
    cap_list = [caps[sku] for sku in product_skus]
    order = {sku: index for index, sku in enumerate(product_skus)}
    item_masks = [sum(1 << order[sku] for sku in cand["items"]) for cand in eligible]

    receipt_patterns = []
    present_counts: dict[int, int] = {}
    for row in rows:
        items = set(row["items"])
        mask = 0
        for index, cand in enumerate(eligible):
            if all(sku in items for sku in cand["items"]):
                mask |= 1 << index
        receipt_patterns.append(mask)
        present_counts[mask] = present_counts.get(mask, 0) + 1
    masks_by_candidate = []
    for candidate in range(len(eligible)):
        bit = 1 << candidate
        masks_by_candidate.append(
            [(mask, count) for mask, count in present_counts.items() if mask & bit]
        )
    support = [sum(count for mask, count in masks_by_candidate[i]) for i in range(len(eligible))]

    packer = Packer(weights, item_masks, bound=False)

    # ---- Immediate feasible fallback (never leave the caller empty) ------
    fallback_deadline = min(deadline, started + max(0.15, limit * 0.2))
    capacity_scores = []
    for index, cand in enumerate(eligible):
        cap_limited = min([support[index]] + [caps[sku] for sku in cand["items"]])
        capacity_scores.append(max(0, cap_limited) * weights[index])
    fallback_selection = _top_selection(capacity_scores, max_slots)
    fallback = None
    if fallback_selection:
        fallback = _evaluate_patterns(
            fallback_selection, receipt_patterns, present_counts, packer,
            weights, candidate_items, caps, fallback_deadline,
        )

    # ---- Upper bounds ----------------------------------------------------
    hit_caps = [
        min([support[index]] + [caps[sku] for sku in candidate_items[index]])
        for index in range(len(weights))
    ]
    slot_total, beta, contributions, supply_term = _slot_bound(
        weights, hit_caps, item_masks, cap_list, max_slots, deadline
    )
    slot_bound = _ceil_cent(slot_total)
    conflict_bound, conflict_complete, _ = _conflict_bound(
        weights, beta, item_masks, cap_list, present_counts, deadline
    )
    upper_bound = slot_bound if conflict_bound is None else min(slot_bound, conflict_bound)

    # ---- Heuristic selection search -------------------------------------
    heuristic_deadline = started + max(0.25, limit * 0.72)
    capped = _CappedEvaluator(
        receipt_patterns, present_counts, packer, weights, candidate_items, caps,
        heuristic_deadline,
    )
    selections: list[tuple[str, int]] = []
    if fallback_selection:
        selections.append(("top_capacity_profit", fallback_selection))
    greedy = _greedy_selection(
        weights, present_counts, packer, max_slots, heuristic_deadline, masks_by_candidate
    )
    if greedy:
        selections.append(("greedy_marginal", greedy))
        if time.monotonic() < heuristic_deadline:
            from_capacity = _capped_swap_search(
                fallback_selection or greedy, capped, max_slots, len(weights), heuristic_deadline
            )
            selections.append(("capped_search_from_capacity", from_capacity))
        if time.monotonic() < heuristic_deadline:
            from_greedy = _capped_swap_search(
                greedy, capped, max_slots, len(weights), heuristic_deadline
            )
            selections.append(("capped_search_from_greedy", from_greedy))
    top_support = _top_selection(
        [support[i] * weights[i] for i in range(len(weights))], max_slots
    )
    if top_support:
        selections.append(("top_support_profit", top_support))
    if any(value > 0 for value in beta.values()):
        adjusted = [
            max(0.0, weights[index] - sum(beta.get(p, 0.0) for p in _presence(item_masks[index])))
            for index in range(len(weights))
        ]
        dual_scores = [
            min([support[index]] + [caps[sku] for sku in candidate_items[index]]) * adjusted[index]
            for index in range(len(weights))
        ]
        dual_selection = _top_selection(dual_scores, max_slots)
        if dual_selection:
            selections.append(("supply_dual_top", dual_selection))

    best = fallback
    best_name = "top_capacity_profit" if fallback else "none"
    seen_selections = set()
    for name, selection in selections:
        if selection in seen_selections:
            continue
        seen_selections.add(selection)
        if time.monotonic() >= deadline:
            break
        evaluated = _evaluate_patterns(
            selection, receipt_patterns, present_counts, packer,
            weights, candidate_items, caps, deadline, max_slots,
        )
        if evaluated and (best is None or evaluated["profit"] > best["profit"] + 1e-9):
            best = evaluated
            best_name = name
    if best is None or not best["selection"]:
        best = _empty_plan(len(rows))
        best_name = "none"

    # ---- Exact selection relaxation (can close the gap) ------------------
    exact_bound = None
    exact_complete = False
    if time.monotonic() < deadline:
        exact_value, exact_complete, _exact_work = _exact_selection_bound(
            weights, present_counts, item_masks, max_slots, deadline
        )
        if exact_complete:
            exact_bound = _ceil_cent(exact_value)
            upper_bound = min(upper_bound, exact_bound)

    # ---- Improve the incumbent while budget remains ----------------------
    # Kicks (drop a selected bundle, greedily refill) escape local optima and
    # every evaluation is capacity-feasible, so the incumbent only improves.
    if time.monotonic() < deadline and best["selection"]:
        k = 0
        while time.monotonic() < deadline and k < 12:
            k += 1
            selected_now = _presence(best["selection"])
            if not selected_now:
                break
            dropped = best["selection"] & ~(1 << selected_now[k % len(selected_now)])
            kick = _greedy_selection(
                weights, present_counts, packer, max_slots, deadline, masks_by_candidate,
                start=dropped,
            )
            for name, variant in (("kick", kick), ("kick_search", _capped_swap_search(
                    kick, capped, max_slots, len(weights), deadline, rounds=3))
                    if kick else ("", 0)):
                if not variant:
                    continue
                scored = capped.score(variant)
                if scored is None:
                    break
                if scored[0] > best["profit"] + 1e-9:
                    candidate = _evaluate_patterns(
                        variant, receipt_patterns, present_counts, packer,
                        weights, candidate_items, caps, deadline, max_slots,
                    )
                    if candidate and candidate["profit"] > best["profit"] + 1e-9:
                        best = candidate
                        best_name = name
            if time.monotonic() >= deadline:
                break

    profit = _r2(best["profit"])
    upper_bound = _r2(upper_bound)
    if profit > upper_bound + 1e-6:
        # A valid bound can never be below a feasible plan; this only triggers
        # on rounding noise.  Keep the plan and mark the bound as tightened.
        upper_bound = profit
    gap = _r2(max(0.0, upper_bound - profit))
    optimal = profit + 1e-9 >= upper_bound
    if optimal:
        reason = "upper_bound_reached"
    elif time.monotonic() >= deadline:
        reason = "deadline_feasible"
    else:
        reason = "gap_unproven"

    selections_hit_counts = [0] * len(weights)
    for hits in best["hits"]:
        for index in hits:
            selections_hit_counts[index] += 1

    allocations = []
    uncovered = []
    covered = 0
    for row, hits in zip(rows, best["hits"]):
        if hits:
            covered += 1
            allocations.append(
                {
                    "reference": row["reference"],
                    "covered": True,
                    "hit_count": len(hits),
                    "bundle_rule_indexes": [eligible[index]["rule_index"] for index in hits],
                    "bundle_items": [list(eligible[index]["items"]) for index in hits],
                    "gross_profit": _r2(sum(weights[index] for index in hits)),
                }
            )
        else:
            allocations.append(
                {
                    "reference": row["reference"],
                    "covered": False,
                    "hit_count": 0,
                    "bundle_rule_indexes": [],
                    "bundle_items": [],
                    "gross_profit": 0.0,
                }
            )
            uncovered.append({"reference": row["reference"], "items": list(row["items"])})

    candidate_rows = []
    for index, cand in enumerate(eligible):
        hits = selections_hit_counts[index]
        candidate_rows.append(
            {
                "rule_index": cand["rule_index"],
                "items": list(cand["items"]),
                "antecedent": cand["antecedent"],
                "consequent": cand["consequent"],
                "support_count": cand["support_count"],
                "support": cand["support"],
                "confidence": cand["confidence"],
                "lift": cand["lift"],
                "list_price": cand["list_price"],
                "list_cost": cand["list_cost"],
                "discounted_revenue": cand["discounted_revenue"],
                "gross_profit_per_hit": cand["gross_profit_per_hit"],
                "selected": hits > 0,
                "hits": hits,
                "gross_profit": _r2(hits * cand["gross_profit_per_hit"]),
            }
        )

    inventory = []
    for sku in product_skus:
        used = best["usage"].get(sku, 0)
        cap = caps[sku]
        inventory.append(
            {
                "sku": sku,
                "name": products[sku].get("name"),
                "cap": cap,
                "used": used,
                "remaining": max(0, cap - used),
                "utilization": round(used / cap, 6) if cap else None,
            }
        )

    contributions_public = [
        {
            "rule_index": eligible[index]["rule_index"],
            "support": support[index],
            "hit_cap": hit_caps[index],
            "weight": round(weights[index], 6),
            "adjusted": round(adjusted_value, 6),
            "contribution": round(contribution, 6),
            "counted": True,
        }
        for contribution, index, adjusted_value in contributions
    ]
    counted_indexes = {index for _, index, _ in contributions}
    for index, cand in enumerate(eligible):
        if index in counted_indexes:
            continue
        adjusted_value = weights[index] - sum(
            beta.get(p, 0.0) for p in _presence(item_masks[index])
        )
        contributions_public.append(
            {
                "rule_index": cand["rule_index"],
                "support": support[index],
                "hit_cap": hit_caps[index],
                "weight": round(weights[index], 6),
                "adjusted": round(max(0.0, adjusted_value), 6),
                "contribution": 0.0,
                "counted": False,
            }
        )

    report = {
        "status": "optimal" if optimal else "feasible",
        "optimal": optimal,
        "summary": {
            "receipt_count": len(rows),
            "covered_receipt_count": covered,
            "uncovered_receipt_count": len(uncovered),
            "hit_count": sum(row["hit_count"] for row in allocations),
            "selected_bundle_count": sum(1 for row in candidate_rows if row["selected"]),
            "discount": discount,
            "max_slots": max_slots,
            "gross_profit": profit,
            "upper_bound": upper_bound,
            "gap": gap,
            "gap_pct": round(gap / upper_bound, 6) if upper_bound > 0 else 0.0,
            "optimality_reason": reason,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "time_limit_ms": round(limit * 1000),
        },
        "bound": {
            "upper_bound": upper_bound,
            "slot_bound": slot_bound,
            "conflict_bound": conflict_bound,
            "conflict_bound_complete": conflict_complete,
            "exact_selection_bound": exact_bound,
            "exact_selection_complete": exact_complete,
            "supply_term": _r2(supply_term),
            "method": "min(slot_dual, receipt_conflict, exact_selection_relaxation)",
            "beta": {product_skus[index]: value for index, value in beta.items()},
            "contributions": contributions_public,
            "note": (
                "上界 = 最多名额条候选的正贡献之和（min(支持数, 商品供给上限) × "
                "(单次毛利 − 商品对偶价)）+ Σ 商品供给上限 × 对偶价；"
                "并取逐篮冲突松弛与无供给约束的精确选择松弛，三者取小并向上取整到分。"
            ),
        },
        "candidates": candidate_rows,
        "excluded_candidates": excluded,
        "inventory": inventory,
        "allocations": allocations,
        "uncovered": uncovered,
        "diagnostics": {
            "algorithm": ALGORITHM_VERSION,
            "variant": best_name,
            "timed_out": reason == "deadline_feasible",
            "packer_fallbacks": packer.fallbacks,
            "hit_caps": hit_caps,
            "distinct_basket_patterns": len(present_counts),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "time_limit_ms": round(limit * 1000),
        },
    }
    report["audit"] = audit_result(report, rows, products, supply, discount, max_slots)
    return report


def _evaluate_patterns(
    selection, receipt_patterns, present_counts, packer, weights, candidate_items, caps, deadline,
    max_slots=MAX_SLOTS,
):
    if selection.bit_count() > max_slots:
        return None
    allowed = 0
    for index in _presence(selection):
        if all(caps.get(sku, 0) > 0 for sku in candidate_items[index]):
            allowed |= 1 << index
    if not allowed:
        return None
    profile = _selection_profile(allowed, present_counts)
    variants, usage, _ideal_profit, timed_out = _allocate(
        allowed, profile, packer, weights, candidate_items, caps, deadline
    )
    hits = _materialise(variants, [pattern & allowed for pattern in receipt_patterns])
    profit = 0.0
    for chosen in hits:
        profit += sum(weights[index] for index in chosen)
    return {
        "selection": allowed,
        "hits": hits,
        "usage": usage,
        "profit": _r2(profit),
        "timed_out": timed_out,
    }


def _empty_plan(receipt_count):
    return {
        "selection": 0,
        "hits": [()] * receipt_count,
        "usage": Counter(),
        "profit": 0.0,
        "timed_out": False,
    }


def _empty_result(rows, cands, excluded, discount, max_slots, supply, products, started, limit):
    product_skus = sorted({sku for cand in cands for sku in cand["items"]})
    return {
        "status": "optimal",
        "optimal": True,
        "summary": {
            "receipt_count": len(rows),
            "covered_receipt_count": 0,
            "uncovered_receipt_count": len(rows),
            "hit_count": 0,
            "selected_bundle_count": 0,
            "discount": discount,
            "max_slots": max_slots,
            "gross_profit": 0.0,
            "upper_bound": 0.0,
            "gap": 0.0,
            "gap_pct": 0.0,
            "optimality_reason": "no_eligible_candidate",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "time_limit_ms": round(float(limit) * 1000),
        },
        "bound": {
            "upper_bound": 0.0,
            "slot_bound": 0.0,
            "conflict_bound": 0.0,
            "conflict_bound_complete": True,
            "exact_selection_bound": 0.0,
            "exact_selection_complete": True,
            "supply_term": 0.0,
            "method": "no_eligible_candidate",
            "beta": {},
            "contributions": [],
            "note": "折扣后所有候选单次毛利均不为正，空方案即最优。",
        },
        "candidates": [],
        "excluded_candidates": excluded,
        "inventory": [
            {
                "sku": sku,
                "name": products[sku].get("name"),
                "cap": int(supply.get(sku, 0)),
                "used": 0,
                "remaining": int(supply.get(sku, 0)),
                "utilization": 0.0,
            }
            for sku in product_skus
        ],
        "allocations": [
            {
                "reference": row["reference"],
                "covered": False,
                "hit_count": 0,
                "bundle_rule_indexes": [],
                "bundle_items": [],
                "gross_profit": 0.0,
            }
            for row in rows
        ],
        "uncovered": [{"reference": row["reference"], "items": list(row["items"])} for row in rows],
        "diagnostics": {
            "algorithm": ALGORITHM_VERSION,
            "variant": "none",
            "timed_out": False,
            "packer_fallbacks": 0,
            "hit_caps": [],
            "distinct_basket_patterns": 0,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "time_limit_ms": round(float(limit) * 1000),
        },
    }


def audit_result(result, receipts, products, supply, discount, max_slots):
    """Independently re-check a stored plan against receipts and its bound.

    Verifies per-basket allocation (known bundle, subset of the basket, no
    product reuse), supply caps, profit arithmetic, the uncovered list, and
    the published slot bound recomputed from its beta, supports and caps.
    """
    checks = []

    def check(name, passed, detail=""):
        checks.append({"check": name, "passed": bool(passed), "detail": str(detail)})

    rows = _normalise_receipts(receipts)
    by_ref = {row["reference"]: row for row in rows}
    candidates = result.get("candidates", [])
    by_rule = {c["rule_index"]: c for c in candidates}
    allocations = result.get("allocations", [])

    refs = [a.get("reference") for a in allocations]
    check("allocation_count", len(allocations) == len(rows), f"{len(allocations)} vs {len(rows)}")
    check("allocation_references_unique", len(refs) == len(set(refs)))
    check("allocation_references_known", set(refs) <= set(by_ref))

    usage: Counter[str] = Counter()
    total_profit = 0.0
    covered = 0
    overlap_free = True
    subset_ok = True
    profit_ok = True
    bundle_known = True
    hit_arithmetic = True
    for allocation in allocations:
        row = by_ref.get(allocation.get("reference"))
        if not row:
            continue
        chosen_indexes = allocation.get("bundle_rule_indexes", [])
        chosen_items = allocation.get("bundle_items", [])
        if allocation.get("hit_count"):
            covered += 1
        used: set[str] = set()
        row_profit = 0.0
        for rule_index, items in zip(chosen_indexes, chosen_items):
            candidate = by_rule.get(rule_index)
            if candidate is None or list(candidate["items"]) != list(items):
                bundle_known = False
                continue
            if not set(items).issubset(row["items"]):
                subset_ok = False
            if used & set(items):
                overlap_free = False
            used |= set(items)
            usage.update(items)
            row_profit += candidate["gross_profit_per_hit"]
        if allocation.get("hit_count") != len(chosen_indexes):
            hit_arithmetic = False
        if abs(_r2(row_profit) - float(allocation.get("gross_profit", 0))) > 0.011:
            profit_ok = False
        total_profit += float(allocation.get("gross_profit", 0))

    check("bundles_known", bundle_known)
    check("bundles_subset_of_basket", subset_ok)
    check("receipt_items_not_reused", overlap_free)
    check("hit_count_arithmetic", hit_arithmetic)
    check("row_profit_arithmetic", profit_ok)
    check("covered_count", covered == result["summary"]["covered_receipt_count"], covered)
    uncovered_expected = {
        row["reference"]
        for row in rows
        if not any(
            a.get("reference") == row["reference"] and a.get("hit_count") for a in allocations
        )
    }
    uncovered_actual = {row["reference"] for row in result.get("uncovered", [])}
    check("uncovered_list", uncovered_expected == uncovered_actual, len(uncovered_actual))
    check(
        "hit_count_total",
        sum(len(a.get("bundle_rule_indexes", [])) for a in allocations)
        == result["summary"]["hit_count"],
        result["summary"]["hit_count"],
    )
    over_caps = [f"{sku}:{usage[sku]}/{cap}" for sku, cap in supply.items() if usage[sku] > cap]
    check("supply_caps", not over_caps, ";".join(over_caps))
    profit = _r2(total_profit)
    check(
        "total_profit_recomputed",
        abs(profit - float(result["summary"]["gross_profit"])) <= 0.011,
        f"{profit} vs {result['summary']['gross_profit']}",
    )
    upper = float(result["summary"]["upper_bound"])
    check("upper_bound_not_below_profit", upper + 1e-9 >= profit, f"{upper} >= {profit}")

    # Recompute the slot bound from published beta, supports and caps.
    beta = result.get("bound", {}).get("beta", {})
    supply_int = {sku: int(cap) for sku, cap in supply.items()}
    product_skus = sorted({sku for candidate in candidates for sku in candidate["items"]})
    contributions = []
    for candidate in candidates:
        items = candidate["items"]
        support = sum(1 for row in rows if set(items).issubset(row["items"]))
        hit_cap = min([support] + [supply_int[sku] for sku in items])
        adjusted = candidate["gross_profit_per_hit"] - sum(beta.get(sku, 0.0) for sku in items)
        if adjusted > 0 and hit_cap > 0:
            contributions.append(hit_cap * adjusted)
    contributions.sort(reverse=True)
    supply_term = sum(supply_int[sku] * beta.get(sku, 0.0) for sku in product_skus)
    recomputed = _ceil_cent(sum(contributions[:max_slots]) + supply_term)
    reported_slot = float(result.get("bound", {}).get("slot_bound", upper))
    check(
        "slot_bound_recomputed",
        abs(recomputed - reported_slot) <= 0.011,
        f"{recomputed} vs {reported_slot}",
    )
    components = [reported_slot]
    reported_conflict = result.get("bound", {}).get("conflict_bound")
    if reported_conflict is not None:
        components.append(float(reported_conflict))
    reported_exact = result.get("bound", {}).get("exact_selection_bound")
    if reported_exact is not None:
        components.append(float(reported_exact))
    expected_upper = min(components)
    check("upper_bound_is_min", abs(upper - expected_upper) <= 0.011, f"{upper} vs {expected_upper}")
    check(
        "optimal_flag_consistent",
        (not result.get("optimal")) or profit + 1e-9 >= upper,
        f"optimal={result.get('optimal')}",
    )
    passed = all(item["passed"] for item in checks)
    return {
        "passed": passed,
        "checks": checks,
        "recomputed_profit": profit,
        "reported_upper_bound": upper,
        "reported_gap": _r2(upper - profit),
        "optimal_claimed": bool(result.get("optimal")),
        "optimal_verified": bool(result.get("optimal")) and profit + 1e-9 >= upper,
    }


def input_fingerprint(receipts, candidates, products, discount, max_slots, supply):
    """Stable SHA-256 over the exact computation inputs (for freezing)."""
    rows = _normalise_receipts(receipts)
    cands = _normalise_candidates(candidates)
    payload = {
        "receipts": [{"reference": row["reference"], "items": list(row["items"])} for row in rows],
        "candidates": [
            {"rule_index": cand["rule_index"], "items": list(cand["items"])} for cand in cands
        ],
        "products": {sku: [products[sku]["price"], products[sku]["cost"]] for sku in sorted(products)},
        "discount": discount,
        "max_slots": max_slots,
        "supply": {sku: supply[sku] for sku in sorted(supply)},
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

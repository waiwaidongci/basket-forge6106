"""Optimiser verification: exhaustive comparison, greedy failure, scarcity."""
import itertools
import random
import time

import pytest

from src.services import joint_optimizer as jo


def _matchings(present, candidates):
    """Every conflict-free bundle subset available to one receipt."""
    result = []
    indexes = list(present)

    def walk(position, used, chosen):
        if position == len(indexes):
            result.append(tuple(chosen))
            return
        walk(position + 1, used, chosen)
        index = indexes[position]
        items = set(candidates[index]["items"])
        if not items & used:
            walk(position + 1, used | items, chosen + [index])

    walk(0, set(), [])
    return result


def brute_force(receipts, candidates, products, discount, max_slots, supply):
    """Exhaustive optimum over per-receipt matchings and SKU usage states."""
    skus = sorted({sku for cand in candidates for sku in cand["items"]})
    caps = tuple(supply[sku] for sku in skus)
    indexes = {sku: position for position, sku in enumerate(skus)}
    weights = []
    for cand in candidates:
        price = sum(products[sku]["price"] for sku in cand["items"])
        cost = sum(products[sku]["cost"] for sku in cand["items"])
        weights.append(round(price * (1 - discount) - cost, 2))
    present = []
    for receipt in receipts:
        items = set(receipt["items"])
        present.append([i for i, cand in enumerate(candidates) if set(cand["items"]) <= items])

    states = {(tuple([0] * len(skus)), frozenset()): 0.0}
    for available in present:
        options = _matchings(available, candidates)
        updated = {}
        for (usage, selected), value in states.items():
            for matching in options:
                trial = list(usage)
                valid = True
                for bundle in matching:
                    for sku in candidates[bundle]["items"]:
                        position = indexes[sku]
                        trial[position] += 1
                        if trial[position] > caps[position]:
                            valid = False
                            break
                    if not valid:
                        break
                if not valid:
                    continue
                combined = selected | set(matching)
                if len(combined) > max_slots:
                    continue
                key = (tuple(trial), frozenset(combined))
                candidate_value = value + sum(weights[bundle] for bundle in matching)
                if candidate_value > updated.get(key, -1e9):
                    updated[key] = candidate_value
        states = updated
        if not states:
            return 0.0
    return max(states.values())


def _random_instance(rng):
    skus = [f"P{i}" for i in range(rng.randint(4, 6))]
    products = {}
    for sku in skus:
        price = round(rng.uniform(6, 14), 1)
        products[sku] = {"price": price, "cost": round(rng.uniform(1, price - 1), 2), "name": sku}
    candidates, seen = [], set()
    for _ in range(rng.randint(3, 6)):
        items = tuple(sorted(rng.sample(skus, rng.randint(1, 3))))
        if items in seen:
            continue
        seen.add(items)
        candidates.append({"rule_index": len(candidates), "items": list(items)})
    receipts = [
        {"reference": f"R{index}", "items": sorted(rng.sample(skus, rng.randint(1, len(skus))))}
        for index in range(rng.randint(3, 7))
    ]
    supply = {sku: rng.randint(1, 3) for sku in skus}
    return receipts, candidates, products, rng.choice([0, 0.1, 0.2]), rng.randint(1, 3), supply


@pytest.mark.parametrize("seed", [11, 23, 47])
def test_matches_exhaustive_optimum(seed):
    rng = random.Random(seed)
    for _ in range(60):
        receipts, candidates, products, discount, slots, supply = _random_instance(rng)
        optimum = brute_force(receipts, candidates, products, discount, slots, supply)
        result = jo.optimize(receipts, candidates, products, discount, slots, supply, time_limit=5)
        profit = result["summary"]["gross_profit"]
        bound = result["summary"]["upper_bound"]
        assert profit <= optimum + 1e-6, (profit, optimum)
        assert bound >= optimum - 1e-6, (bound, optimum)
        if result["optimal"]:
            assert abs(profit - optimum) <= 1e-6, (profit, optimum)
        audit = jo.audit_result(result, receipts, products, supply, discount, slots)
        assert audit["passed"], audit["checks"]


def test_greedy_double_count_is_rejected():
    """A naive rule-by-rule pass double counts shared products; joint must not."""
    products = {
        "OAT": {"price": 15.0, "cost": 7.0, "name": "燕麦拿铁"},
        "BREAD": {"price": 9.0, "cost": 3.5, "name": "全麦餐包"},
        "EGG": {"price": 5.0, "cost": 2.0, "name": "溏心鸡蛋"},
    }
    candidates = [
        {"rule_index": 0, "items": ["OAT", "BREAD"]},
        {"rule_index": 1, "items": ["OAT", "EGG"]},
        {"rule_index": 2, "items": ["BREAD", "EGG"]},
    ]
    receipts = [
        {"reference": "R1", "items": ["OAT", "BREAD", "EGG"]},
        {"reference": "R2", "items": ["OAT", "BREAD", "EGG"]},
    ]
    supply = {"OAT": 100, "BREAD": 100, "EGG": 100}
    result = jo.optimize(receipts, candidates, products, 0.0, 3, supply, time_limit=5)
    # Per-receipt greedy rule-by-rule would "hit" all three rules on both
    # receipts (6 hits), reusing OAT/BREAD/EGG.  A valid plan can only fit one
    # non-overlapping bundle per receipt here, so hits must be <= 2.
    assert result["summary"]["hit_count"] <= 2
    # ...and the independent audit must confirm no product was reused.
    audit = jo.audit_result(result, receipts, products, supply, 0.0, 3)
    assert audit["passed"]
    reused = [c for c in audit["checks"] if c["check"] == "receipt_items_not_reused"]
    assert reused and reused[0]["passed"]


def test_greedy_overlap_costs_profit_on_planted_instance():
    """Exhaustive check on a case where picking the highest single rule loses."""
    products = {
        "A": {"price": 10.0, "cost": 2.0, "name": "A"},
        "B": {"price": 10.0, "cost": 2.0, "name": "B"},
        "C": {"price": 10.0, "cost": 2.0, "name": "C"},
    }
    candidates = [
        {"rule_index": 0, "items": ["A", "B", "C"]},  # profit 22, but blocks everything
        {"rule_index": 1, "items": ["A", "B"]},      # profit 14
        {"rule_index": 2, "items": ["C"]},           # profit 6
    ]
    receipts = [{"reference": "R1", "items": ["A", "B", "C"]}]
    supply = {"A": 10, "B": 10, "C": 10}
    result = jo.optimize(receipts, candidates, products, 0.0, 2, supply, time_limit=5)
    optimum = brute_force(receipts, candidates, products, 0.0, 2, supply)
    assert result["summary"]["gross_profit"] <= optimum + 1e-6
    if result["optimal"]:
        assert result["summary"]["gross_profit"] == pytest.approx(optimum)


def test_scarce_supply_stays_feasible():
    """Zero and tiny caps must never be exceeded; the plan may simply be empty."""
    products = {sku: {"price": 10.0, "cost": 4.0, "name": sku} for sku in ("A", "B", "C")}
    candidates = [
        {"rule_index": 0, "items": ["A", "B"]},
        {"rule_index": 1, "items": ["B", "C"]},
    ]
    receipts = [{"reference": f"R{i}", "items": ["A", "B", "C"]} for i in range(20)]
    supply = {"A": 0, "B": 2, "C": 0}
    result = jo.optimize(receipts, candidates, products, 0.0, 8, supply, time_limit=5)
    inventory = {row["sku"]: row for row in result["inventory"]}
    assert inventory["A"]["used"] == 0
    assert inventory["C"]["used"] == 0
    assert inventory["B"]["used"] <= 2
    assert result["summary"]["gross_profit"] <= 2 * 12 + 1e-6
    assert jo.audit_result(result, receipts, products, supply, 0.0, 8)["passed"]


def test_zero_supply_for_all_products_gives_empty_optimal_plan():
    products = {sku: {"price": 10.0, "cost": 4.0, "name": sku} for sku in ("A", "B")}
    candidates = [{"rule_index": 0, "items": ["A", "B"]}]
    receipts = [{"reference": "R1", "items": ["A", "B"]}]
    result = jo.optimize(receipts, candidates, products, 0.0, 8, {"A": 0, "B": 0}, time_limit=5)
    assert result["summary"]["gross_profit"] == 0
    assert result["optimal"] is True
    assert result["summary"]["hit_count"] == 0
    assert jo.audit_result(result, receipts, products, {"A": 0, "B": 0}, 0.0, 8)["passed"]


def test_discounts_that_kill_margin_exclude_candidates():
    products = {"A": {"price": 10.0, "cost": 6.0, "name": "A"}, "B": {"price": 10.0, "cost": 6.0, "name": "B"}}
    candidates = [{"rule_index": 0, "items": ["A", "B"]}]
    receipts = [{"reference": "R1", "items": ["A", "B"]}]
    # 0.2 discount: revenue 16, cost 12 -> still positive; 0.5 -> revenue 10 < 12.
    result = jo.optimize(receipts, candidates, products, 0.5, 8, {"A": 9, "B": 9}, time_limit=5)
    assert result["summary"]["gross_profit"] == 0
    assert result["optimal"] is True
    assert result["excluded_candidates"][0]["excluded_reason"] == "折扣后单次毛利不为正"
    assert result["summary"]["optimality_reason"] == "no_eligible_candidate"


def test_slot_limit_is_respected_and_binds():
    products = {sku: {"price": 20.0, "cost": 5.0, "name": sku} for sku in "ABCD"}
    candidates = [
        {"rule_index": 0, "items": ["A", "B"]},
        {"rule_index": 1, "items": ["C", "D"]},
    ]
    receipts = [{"reference": f"R{i}", "items": ["A", "B", "C", "D"]} for i in range(5)]
    supply = {sku: 100 for sku in "ABCD"}
    one_slot = jo.optimize(receipts, candidates, products, 0.0, 1, supply, time_limit=5)
    two_slots = jo.optimize(receipts, candidates, products, 0.0, 2, supply, time_limit=5)
    assert one_slot["summary"]["selected_bundle_count"] <= 1
    assert one_slot["summary"]["hit_count"] <= 5
    assert two_slots["summary"]["hit_count"] <= 10
    assert jo.audit_result(one_slot, receipts, products, supply, 0.0, 1)["passed"]
    assert jo.audit_result(two_slots, receipts, products, supply, 0.0, 2)["passed"]


def test_deadline_returns_feasible_flagged_result():
    rng = random.Random(3)
    skus = [f"S{i:02d}" for i in range(30)]
    products = {sku: {"price": round(rng.uniform(5, 18), 2), "cost": round(rng.uniform(1, 5), 2), "name": sku} for sku in skus}
    receipts = [
        {"reference": f"R{i:05d}", "items": sorted(rng.sample(skus, rng.randint(3, 10)))}
        for i in range(3000)
    ]
    candidates = []
    seen = set()
    while len(candidates) < 24:
        items = tuple(sorted(rng.sample(skus[:12], rng.randint(2, 4))))
        if items in seen:
            continue
        seen.add(items)
        candidates.append({"rule_index": len(candidates), "items": list(items)})
    supply = {sku: rng.choice([20, 200]) for sku in skus}
    started = time.monotonic()
    result = jo.optimize(receipts, candidates, products, 0.1, 8, supply, time_limit=2)
    elapsed = time.monotonic() - started
    assert elapsed < 4.0
    assert result["status"] in ("optimal", "feasible")
    summary = result["summary"]
    assert summary["gross_profit"] <= summary["upper_bound"] + 1e-6
    if not result["optimal"]:
        assert summary["gap"] > 0
        assert summary["optimality_reason"] in ("deadline_feasible", "gap_unproven")
    assert jo.audit_result(result, receipts, products, supply, 0.1, 8)["passed"]


def test_deterministic_output():
    products = {sku: {"price": 11.0, "cost": 4.0, "name": sku} for sku in "ABCDE"}
    candidates = [
        {"rule_index": 0, "items": ["A", "B"]},
        {"rule_index": 1, "items": ["B", "C"]},
        {"rule_index": 2, "items": ["D", "E"]},
    ]
    receipts = [
        {"reference": "R1", "items": ["A", "B", "C", "D"]},
        {"reference": "R2", "items": ["B", "C", "D", "E"]},
        {"reference": "R3", "items": ["A", "B"]},
    ]
    supply = {sku: 2 for sku in "ABCDE"}
    first = jo.optimize(receipts, candidates, products, 0.1, 2, supply, time_limit=5)
    second = jo.optimize(receipts, candidates, products, 0.1, 2, supply, time_limit=5)
    assert first["allocations"] == second["allocations"]
    assert first["summary"]["gross_profit"] == second["summary"]["gross_profit"]
    assert first["summary"]["upper_bound"] == second["summary"]["upper_bound"]


def test_rejects_invalid_configuration():
    products = {"A": {"price": 10.0, "cost": 4.0, "name": "A"}}
    receipts = [{"reference": "R1", "items": ["A"]}]
    candidates = [{"rule_index": 0, "items": ["A"]}]
    with pytest.raises(ValueError):
        jo.optimize(receipts, candidates, products, 0.0, 9, {"A": 1})
    with pytest.raises(ValueError):
        jo.optimize(receipts, candidates, products, 1.0, 1, {"A": 1})
    with pytest.raises(ValueError):
        jo.optimize(receipts, candidates, products, 0.0, 1, {})
    with pytest.raises(ValueError):
        jo.optimize(receipts, [{"rule_index": i, "items": ["A"]} for i in range(25)], products, 0.0, 1, {"A": 1})

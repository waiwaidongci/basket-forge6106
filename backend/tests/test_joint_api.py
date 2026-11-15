"""API/permission/export coverage for joint plans (sqlite, stubbed queue)."""
import os
import tempfile

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "api.db"))
os.environ.setdefault("ARTIFACT_DIR", tempfile.mkdtemp())
os.environ.setdefault("SEED_DEMO", "true")

import pytest
from fastapi.testclient import TestClient

import src.worker as worker
from src.main import app


@pytest.fixture(scope="module")
def client():
    original = worker.run_joint_plan.run

    class Immediate:
        def delay(self, plan_id):
            original(plan_id)

    worker.run_joint_plan = Immediate()
    with TestClient(app) as test_client:
        yield test_client


def _token(client, email):
    response = client.post("/auth/login", json={"email": email, "password": "Gucang2026!"})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["token"]}


def _payload(client, headers, **overrides):
    run = client.get("/runs", headers=headers).json()[0]
    rules = run["result"]["rules"]
    indexes = list(range(min(6, len(rules))))
    skus = sorted({sku for i in indexes for sku in rules[i]["antecedent"] + rules[i]["consequent"]})
    body = {
        "name": "联合测算 API 测试",
        "run_id": run["id"],
        "rule_indexes": indexes,
        "discount": 0.1,
        "max_slots": 4,
        "supply": {sku: 500 for sku in skus},
        "time_limit_ms": 8000,
    }
    body.update(overrides)
    return body


def test_viewer_cannot_create_but_can_read_and_download(client):
    analyst = _token(client, "analyst@gucang.demo")
    viewer = _token(client, "viewer@gucang.demo")
    created = client.post("/joint-plans", headers=analyst, json=_payload(client, analyst))
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]
    assert client.get("/joint-plans", headers=viewer).status_code == 200
    detail = client.get(f"/joint-plans/{plan_id}", headers=viewer)
    assert detail.status_code == 200
    assert detail.json()["status"] in ("optimal", "feasible")
    assert client.post("/joint-plans", headers=viewer, json=_payload(client, viewer)).status_code == 403
    for kind in ("allocations", "inventory", "candidates", "uncovered"):
        response = client.get(f"/joint-plans/{plan_id}/export/{kind}", headers=viewer)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert response.text.startswith("﻿")


def test_result_contains_verifiable_fields(client):
    headers = _token(client, "analyst@gucang.demo")
    created = client.post("/joint-plans", headers=headers, json=_payload(client, headers))
    plan_id = created.json()["id"]
    body = client.get(f"/joint-plans/{plan_id}", headers=headers).json()
    result = body["result"]
    summary = result["summary"]
    for key in ("gross_profit", "upper_bound", "gap", "optimality_reason", "covered_receipt_count", "uncovered_receipt_count"):
        assert key in summary
    assert summary["gross_profit"] <= summary["upper_bound"] + 1e-6
    assert result["audit"]["passed"] is True
    assert result["fingerprint"]
    assert result["basis"]["prices"]
    assert len(result["allocations"]) == summary["receipt_count"]
    assert len(result["inventory"]) >= 1
    assert result["bound"]["beta"]
    if not result["optimal"]:
        assert summary["gap"] > 0


def test_rejects_non_succeeded_run_and_bad_index(client):
    headers = _token(client, "analyst@gucang.demo")
    body = _payload(client, headers)
    bad_run = dict(body, run_id=99999)
    assert client.post("/joint-plans", headers=headers, json=bad_run).status_code == 422
    bad_index = dict(body, rule_indexes=[0, 9999])
    assert client.post("/joint-plans", headers=headers, json=bad_index).status_code == 422
    duplicate = dict(body, rule_indexes=[0, 0])
    assert client.post("/joint-plans", headers=headers, json=duplicate).status_code == 422
    too_many = dict(body, rule_indexes=list(range(25)))
    assert client.post("/joint-plans", headers=headers, json=too_many).status_code == 422
    missing_supply = dict(body, supply={})
    assert client.post("/joint-plans", headers=headers, json=missing_supply).status_code == 422
    bad_discount = dict(body, discount=1.0)
    assert client.post("/joint-plans", headers=headers, json=bad_discount).status_code == 422
    nine_slots = dict(body, max_slots=9)
    assert client.post("/joint-plans", headers=headers, json=nine_slots).status_code == 422


def test_duplicate_supply_entries_are_trimmed(client):
    headers = _token(client, "analyst@gucang.demo")
    body = _payload(client, headers)
    body["supply"] = dict(body["supply"], ZZ_EXTRA=5)
    response = client.post("/joint-plans", headers=headers, json=body)
    assert response.status_code == 201
    assert "ZZ_EXTRA" in response.json()["extra_supply_ignored"]
    assert "ZZ_EXTRA" not in response.json()["supply"]


def test_legacy_proposal_flow_unchanged(client):
    headers = _token(client, "analyst@gucang.demo")
    run = client.get("/runs", headers=headers).json()[0]
    response = client.post(
        "/proposals",
        headers=headers,
        json={"run_id": run["id"], "rule_index": 0, "name": "回归检查", "note": "旧提案不受联合测算影响"},
    )
    assert response.status_code == 201
    assert response.json()["rule"]["antecedent"]

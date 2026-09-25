from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def receiver(client, admin):
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": "receiver.one",
            "password": "Receive!23456",
            "display_name": "低温库接收员",
            "role_codes": ["sample_manager"],
        },
    )
    assert user.status_code == 201, user.text
    login = client.post(
        "/api/auth/login",
        json={"username": "receiver.one", "password": "Receive!23456", "client_label": "tests"},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    return {"headers": {"Authorization": f"Bearer {body['token']}"}, "body": body}


def _location(client, admin, code, room, sensitivity="normal"):
    response = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": code,
            "building": "样品楼",
            "room": room,
            "cabinet": "柜一",
            "shelf": "一层",
            "sensitivity": sensitivity,
            "capacity_units": 100,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _sample(client, admin, quantity=20, sample_code="HD-SAMPLE"):
    source = _location(client, admin, "HD-SRC", "常温库")
    target = _location(client, admin, "HD-DST", "低温库", sensitivity="restricted")
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": f"HD-BATCH-{sample_code}", "project_code": "HD", "expected_count": 1},
    )
    assert batch.status_code == 201, batch.text
    sample = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={
            "sample_code": sample_code,
            "batch_id": batch.json()["id"],
            "sample_type": "水样",
            "quantity": quantity,
            "unit": "mL",
            "location_id": source["id"],
        },
    )
    assert sample.status_code == 201, sample.text
    return source, target, sample.json()


def _initiate(client, admin, sample, target, **overrides):
    payload = {
        "target_location_id": target["id"],
        "expected_version": sample["version"],
        "reason": "转入低温保存",
        "seal_code": "SEAL-001",
    }
    payload.update(overrides)
    return client.post(f"/api/sample-operations/{sample['id']}/handovers", headers=admin["headers"], json=payload)


def _receive(client, receiver, transfer_id, token, item_seq=1, quantity=20, seal="SEAL-001", target_id=None):
    return client.post(
        f"/api/sample-operations/handovers/{transfer_id}/receipts",
        headers=receiver["headers"],
        json={
            "item_seq": item_seq,
            "confirmed_quantity": quantity,
            "seal_code": seal,
            "target_location_id": target_id,
            "receive_token": token,
        },
    )


def test_split_receiving_moves_custody_atomically(client, admin, receiver):
    source, target, sample = _sample(client, admin)
    created = _initiate(
        client,
        admin,
        sample,
        target,
        seal_code=None,
        items=[
            {"item_seq": 1, "expected_quantity": 12, "seal_code": "SEAL-A"},
            {"item_seq": 2, "expected_quantity": 8, "seal_code": "SEAL-B"},
        ],
        transfer_code="TRF-SPLIT-01",
    )
    assert created.status_code == 201, created.text
    order = created.json()["transfer"]
    assert created.json()["replayed"] is False
    assert order["state"] == "in_transit"
    assert order["source_location_id"] == source["id"]
    assert order["quantity"] == 20
    assert len(order["manifest_digest"]) == 64
    assert [item["seal_code"] for item in order["items"]] == ["SEAL-A", "SEAL-B"]

    # 冻结期间：全局查询仍显示源位置，样品不可消耗
    listed = client.get("/api/samples", headers=admin["headers"]).json()
    row = next(item for item in listed if item["id"] == sample["id"])
    assert row["location_id"] == source["id"]
    assert row["active_transfers"] == 1
    assert row["reserved_quantity"] == 20
    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert detail["active_transfer"]["transfer_code"] == "TRF-SPLIT-01"
    consumed = client.post(
        f"/api/samples/{sample['id']}/consumptions",
        headers=admin["headers"],
        json={"experiment_code": "EXP-X", "quantity": 1, "idempotency_key": "frozen-consume"},
    )
    assert consumed.status_code == 409

    # 拆分接收：第一件确认后仍在途、位置未变
    first = _receive(client, receiver, order["id"], "scan-0001", item_seq=1, quantity=12, seal="SEAL-A", target_id=target["id"])
    assert first.status_code == 201, first.text
    halfway = first.json()["transfer"]
    assert halfway["state"] == "in_transit"
    assert halfway["received_quantity"] == 12
    still_source = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert still_source["location_id"] == source["id"]

    # 第二件确认后原子过户：位置、保管人、冻结同时变更
    second = _receive(client, receiver, order["id"], "scan-0002", item_seq=2, quantity=8, seal="SEAL-B", target_id=target["id"])
    assert second.status_code == 201, second.text
    finished = second.json()["transfer"]
    assert finished["state"] == "received"
    assert finished["received_quantity"] == 20
    assert finished["completed_at"]
    after = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert after["location_id"] == target["id"]
    assert after["custody_user_id"] == receiver["body"]["user"]["id"]
    assert after["reserved_quantity"] == 0
    assert after["active_transfer"] is None

    # 事件链可还原每段保管责任
    event_types = [event["event_type"] for event in after["events"]]
    assert event_types == ["received", "transfer.initiated", "transfer.item_confirmed", "transfer.item_confirmed", "transfer.received"]
    initiator = admin["body"]["user"]["id"]
    receiver_id = receiver["body"]["user"]["id"]
    assert after["events"][1]["actor_user_id"] == initiator
    assert after["events"][2]["actor_user_id"] == receiver_id
    assert after["events"][-1]["details"]["to_location_id"] == target["id"]
    order_events = client.get(f"/api/sample-operations/handovers/{order['id']}", headers=admin["headers"]).json()["events"]
    assert [event["event_type"] for event in order_events] == event_types[1:]
    assert len(finished["receipts"]) == 2


def test_initiate_is_idempotent_and_blocks_parallel_orders(client, admin, receiver):
    _, target, sample = _sample(client, admin)
    first = _initiate(client, admin, sample, target, transfer_code="TRF-IDEM-01")
    second = _initiate(client, admin, sample, target, transfer_code="TRF-IDEM-01")
    assert first.status_code == second.status_code == 201
    assert first.json()["transfer"]["id"] == second.json()["transfer"]["id"]
    assert second.json()["replayed"] is True

    different = _initiate(client, admin, sample, target, transfer_code="TRF-IDEM-01", reason="另一批理由")
    assert different.status_code == 409

    parallel = _initiate(client, admin, sample, target, transfer_code="TRF-IDEM-02")
    assert parallel.status_code == 409


def test_initiate_validates_version_state_and_manifest(client, admin, receiver):
    _, target, sample = _sample(client, admin)
    stale = _initiate(client, admin, sample, target, expected_version=sample["version"] + 1)
    assert stale.status_code == 409

    mismatched = _initiate(
        client,
        admin,
        sample,
        target,
        seal_code=None,
        items=[{"item_seq": 1, "expected_quantity": 5, "seal_code": "S-1"}],
    )
    assert mismatched.status_code == 422

    no_manifest = client.post(
        f"/api/sample-operations/{sample['id']}/handovers",
        headers=admin["headers"],
        json={"target_location_id": target["id"], "expected_version": sample["version"], "reason": "缺少清单"},
    )
    assert no_manifest.status_code == 422

    same_location = _initiate(client, admin, sample, {"id": sample["location_id"]})
    assert same_location.status_code == 422

    past_expiry = _initiate(client, admin, sample, target, expires_at="2020-01-01T00:00:00+00:00")
    assert past_expiry.status_code == 422
    bad_expiry = _initiate(client, admin, sample, target, expires_at="not-a-date!!")
    assert bad_expiry.status_code == 422
    far_expiry = _initiate(client, admin, sample, target, expires_at="2099-01-01T00:00:00+00:00")
    assert far_expiry.status_code == 422


def test_loaned_sample_cannot_enter_handover(client, admin, receiver):
    _, target, sample = _sample(client, admin)
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={
            "sample_id": sample["id"],
            "borrower_user_id": admin["body"]["user"]["id"],
            "quantity": 5,
            "due_at": "2026-10-01T00:00:00+00:00",
        },
    )
    assert loan.status_code == 201, loan.text
    blocked = _initiate(client, admin, sample, target)
    assert blocked.status_code == 409


def test_receiver_must_differ_from_initiator(client, admin, receiver):
    _, target, sample = _sample(client, admin)
    order = _initiate(client, admin, sample, target).json()["transfer"]
    own = client.post(
        f"/api/sample-operations/handovers/{order['id']}/receipts",
        headers=admin["headers"],
        json={
            "item_seq": 1,
            "confirmed_quantity": 20,
            "seal_code": "SEAL-001",
            "target_location_id": target["id"],
            "receive_token": "scan-self-1",
        },
    )
    assert own.status_code == 422


def test_duplicate_scan_is_replayed_not_double_counted(client, admin, receiver):
    _, target, sample = _sample(client, admin)
    order = _initiate(client, admin, sample, target).json()["transfer"]
    first = _receive(client, receiver, order["id"], "scan-dup-1", target_id=target["id"])
    assert first.status_code == 201
    again = _receive(client, receiver, order["id"], "scan-dup-1", target_id=target["id"])
    assert again.status_code == 201
    assert again.json()["replayed"] is True
    assert again.json()["receipt"]["id"] == first.json()["receipt"]["id"]

    detail = client.get(f"/api/sample-operations/handovers/{order['id']}", headers=admin["headers"]).json()
    assert len(detail["receipts"]) == 1
    assert detail["received_quantity"] == 20

    other_token = _receive(client, receiver, order["id"], "scan-dup-2", target_id=target["id"])
    assert other_token.status_code == 409


def test_receive_validates_quantity_seal_and_target(client, admin, receiver):
    _, target, sample = _sample(client, admin)
    order = _initiate(client, admin, sample, target).json()["transfer"]
    wrong_qty = _receive(client, receiver, order["id"], "scan-bad-1", quantity=19, target_id=target["id"])
    assert wrong_qty.status_code == 422
    wrong_seal = _receive(client, receiver, order["id"], "scan-bad-2", seal="SEAL-XXX", target_id=target["id"])
    assert wrong_seal.status_code == 422
    wrong_target = _receive(client, receiver, order["id"], "scan-bad-3", target_id=sample["location_id"])
    assert wrong_target.status_code == 422
    detail = client.get(f"/api/sample-operations/handovers/{order['id']}", headers=admin["headers"]).json()
    assert detail["state"] == "in_transit"
    assert detail["received_quantity"] == 0


def test_cancel_then_reject_converges_to_single_terminal_state(client, admin, receiver):
    source, target, sample = _sample(client, admin)
    order = _initiate(client, admin, sample, target).json()["transfer"]
    cancelled = client.post(f"/api/sample-operations/handovers/{order['id']}/cancel", headers=admin["headers"])
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"

    rejected = client.post(
        f"/api/sample-operations/handovers/{order['id']}/reject",
        headers=receiver["headers"],
        json={"reason": "双方同时操作，后到者应冲突"},
    )
    assert rejected.status_code == 409
    received = _receive(client, receiver, order["id"], "scan-late-1", target_id=target["id"])
    assert received.status_code == 409

    detail = client.get(f"/api/sample-operations/handovers/{order['id']}", headers=admin["headers"]).json()
    assert detail["state"] == "cancelled"
    sample_after = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert sample_after["reserved_quantity"] == 0
    assert sample_after["location_id"] == source["id"]
    anomalies = client.get("/api/samples/anomalies/list", headers=admin["headers"]).json()
    assert [a for a in anomalies if a["anomaly_type"] == "transfer_rejected"] == []


def test_reject_then_cancel_converges_and_creates_anomaly(client, admin, receiver):
    source, target, sample = _sample(client, admin)
    order = _initiate(client, admin, sample, target).json()["transfer"]
    rejected = client.post(
        f"/api/sample-operations/handovers/{order['id']}/reject",
        headers=receiver["headers"],
        json={"reason": "封签破损", "severity": "high"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == "returned"
    assert rejected.json()["return_reason"] == "rejected"

    cancelled = client.post(f"/api/sample-operations/handovers/{order['id']}/cancel", headers=admin["headers"])
    assert cancelled.status_code == 409

    sample_after = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert sample_after["reserved_quantity"] == 0
    assert sample_after["location_id"] == source["id"]
    anomalies = client.get("/api/samples/anomalies/list", headers=admin["headers"]).json()
    related = [a for a in anomalies if a["anomaly_type"] == "transfer_rejected" and a["sample_id"] == sample["id"]]
    assert len(related) == 1
    assert related[0]["severity"] == "high"
    event_types = [event["event_type"] for event in sample_after["events"]]
    assert event_types[-1] == "transfer.rejected"


def test_cancel_is_blocked_after_partial_receipt(client, admin, receiver):
    _, target, sample = _sample(client, admin)
    order = _initiate(
        client,
        admin,
        sample,
        target,
        seal_code=None,
        items=[
            {"item_seq": 1, "expected_quantity": 10, "seal_code": "SEAL-A"},
            {"item_seq": 2, "expected_quantity": 10, "seal_code": "SEAL-B"},
        ],
    ).json()["transfer"]
    _receive(client, receiver, order["id"], "scan-part-1", item_seq=1, quantity=10, seal="SEAL-A", target_id=target["id"])
    cancelled = client.post(f"/api/sample-operations/handovers/{order['id']}/cancel", headers=admin["headers"])
    assert cancelled.status_code == 409
    rejected = client.post(
        f"/api/sample-operations/handovers/{order['id']}/reject",
        headers=receiver["headers"],
        json={"reason": "剩余明细未到货"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "returned"


def test_only_initiator_can_cancel(client, admin, receiver):
    _, target, sample = _sample(client, admin)
    order = _initiate(client, admin, sample, target).json()["transfer"]
    stranger = client.post(f"/api/sample-operations/handovers/{order['id']}/cancel", headers=receiver["headers"])
    assert stranger.status_code == 422


def _backdate_expiry(transfer_id):
    from app.database import get_connection

    connection = get_connection()
    connection.execute(
        "UPDATE transfer_orders SET expires_at=? WHERE id=?",
        ("2020-01-01T00:00:00+00:00", transfer_id),
    )


def test_timeout_returns_to_source_with_anomaly(client, admin, receiver):
    source, target, sample = _sample(client, admin)
    order = _initiate(client, admin, sample, target).json()["transfer"]
    _backdate_expiry(order["id"])

    late = _receive(client, receiver, order["id"], "scan-late-exp", target_id=target["id"])
    assert late.status_code == 409

    detail = client.get(f"/api/sample-operations/handovers/{order['id']}", headers=admin["headers"]).json()
    assert detail["state"] == "returned"
    assert detail["return_reason"] == "expired"
    sample_after = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert sample_after["reserved_quantity"] == 0
    assert sample_after["location_id"] == source["id"]
    anomalies = client.get("/api/samples/anomalies/list", headers=admin["headers"]).json()
    expired = [a for a in anomalies if a["anomaly_type"] == "transfer_expired" and a["sample_id"] == sample["id"]]
    assert len(expired) == 1
    event_types = [event["event_type"] for event in sample_after["events"]]
    assert event_types[-1] == "transfer.expired"


def test_sweep_expired_is_idempotent(client, admin, receiver):
    _, target, sample = _sample(client, admin)
    order = _initiate(client, admin, sample, target).json()["transfer"]
    _backdate_expiry(order["id"])
    first = client.post("/api/sample-operations/handovers/sweep-expired", headers=admin["headers"])
    assert first.status_code == 200
    assert first.json()["expired_transfer_codes"] == [order["transfer_code"]]
    second = client.post("/api/sample-operations/handovers/sweep-expired", headers=admin["headers"])
    assert second.json()["expired_count"] == 0
    detail = client.get(f"/api/sample-operations/handovers/{order['id']}", headers=admin["headers"]).json()
    assert detail["state"] == "returned"


def test_handover_state_survives_service_restart(client, admin, receiver):
    _, target, sample = _sample(client, admin)
    order = _initiate(client, admin, sample, target, transfer_code="TRF-RESTART-01").json()["transfer"]
    _receive(client, receiver, order["id"], "scan-restart-1", target_id=target["id"])

    from app.database import close_connection
    from app.main import app

    close_connection()
    with TestClient(app) as restarted:
        detail = restarted.get(f"/api/sample-operations/handovers/{order['id']}", headers=admin["headers"])
        assert detail.status_code == 200
        assert detail.json()["state"] == "received"
        replay = restarted.post(
            f"/api/sample-operations/{sample['id']}/handovers",
            headers=admin["headers"],
            json={
                "target_location_id": target["id"],
                "expected_version": sample["version"],
                "reason": "转入低温保存",
                "seal_code": "SEAL-001",
                "transfer_code": "TRF-RESTART-01",
            },
        )
        assert replay.status_code == 201
        assert replay.json()["replayed"] is True
        sample_after = restarted.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
        assert sample_after["location_id"] == target["id"]


def test_location_masking_applies_to_handover_views(client, admin, receiver):
    source, target, sample = _sample(client, admin)
    order = _initiate(client, admin, sample, target).json()["transfer"]

    as_receiver = client.get(f"/api/sample-operations/handovers/{order['id']}", headers=receiver["headers"])
    assert as_receiver.status_code == 200
    assert as_receiver.json()["target_location_code"] == f"MASKED-{target['id']:04d}"
    assert as_receiver.json()["source_location_code"] == source["code"]

    as_admin = client.get(f"/api/sample-operations/handovers/{order['id']}", headers=admin["headers"])
    assert as_admin.json()["target_location_code"] == target["code"]

    sample_for_receiver = client.get(f"/api/samples/{sample['id']}", headers=receiver["headers"]).json()
    assert sample_for_receiver["active_transfer"]["target_location_code"] == f"MASKED-{target['id']:04d}"

    listed = client.get("/api/sample-operations/handovers?state=in_transit", headers=receiver["headers"]).json()
    row = next(item for item in listed if item["id"] == order["id"])
    assert row["target_location_code"] == f"MASKED-{target['id']:04d}"
    assert "items" not in row

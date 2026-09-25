from __future__ import annotations

from fastapi.testclient import TestClient


def _location(client, admin, code, sensitivity="normal", room="常温库"):
    response = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={"code": code, "building": "样品楼", "room": room, "cabinet": "柜一", "shelf": "一层", "sensitivity": sensitivity, "capacity_units": 100},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _batch(client, admin, code="TRF-BATCH"):
    response = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": code, "project_code": "TRF", "expected_count": 10},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _sample(client, admin, code, location_id, batch_id, quantity=20):
    response = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={"sample_code": code, "batch_id": batch_id, "sample_type": "水样", "quantity": quantity, "unit": "mL", "location_id": location_id},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _user(client, admin, username, display="低温库接收员"):
    created = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Receiver!23456", "display_name": display, "role_codes": ["sample_manager"]},
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Receiver!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}, "id": created.json()["id"]}


def _setup(client, admin, sample_count=2):
    source = _location(client, admin, "TRF-SRC", room="常温库")
    target = _location(client, admin, "TRF-DST", sensitivity="restricted", room="低温库")
    batch = _batch(client, admin)
    samples = [_sample(client, admin, f"TRF-S-{index}", source["id"], batch["id"]) for index in range(sample_count)]
    receiver = _user(client, admin, "receiver.trf")
    return source, target, samples, receiver


def _create_order(client, admin, target, samples, **overrides):
    payload = {"target_location_id": target["id"], "sample_ids": [sample["id"] for sample in samples], "reason": "常温转低温"}
    payload.update(overrides)
    response = client.post("/api/sample-operations/transfers", headers=admin["headers"], json=payload)
    assert response.status_code == 201, response.text
    return response.json()["transfer"]


def _confirm(client, headers, transfer_id, entries):
    return client.post(
        f"/api/sample-operations/transfers/{transfer_id}/confirmations",
        headers=headers,
        json={"items": entries},
    )


def _confirm_entry(sample, seal="SEAL-1"):
    return {"sample_id": sample["id"], "received_quantity": sample["quantity"], "seal_code": seal}


def test_two_way_handover_moves_custody_only_after_acceptance(client, admin):
    source, target, samples, receiver = _setup(client, admin)
    order = _create_order(client, admin, target, samples)
    assert order["state"] == "pending"
    assert order["manifest"]["item_count"] == 2
    assert order["manifest"]["total_by_unit"] == {"mL": 40}
    assert len(order["manifest_digest"]) == 64
    assert order["source_location_version"] >= 1
    assert [item["state"] for item in order["items"]] == ["pending", "pending"]
    assert all(item["sample_version"] >= 1 for item in order["items"])

    # 验收前：全局查询仍在源库，并标记在途
    listed = client.get("/api/samples", headers=admin["headers"]).json()
    entry = next(item for item in listed if item["id"] == samples[0]["id"])
    assert entry["location_id"] == source["id"]
    assert entry["active_transfer"]["state"] == "in_transit"
    assert entry["active_transfer"]["transfer_code"] == order["transfer_code"]
    stock = client.get("/api/sample-operations/stock/by-location", headers=admin["headers"]).json()
    source_stock = next(item for item in stock if item["id"] == source["id"])
    assert source_stock["in_transit_count"] == 2

    confirmed = _confirm(client, receiver["headers"], order["id"], [_confirm_entry(sample) for sample in samples])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["replayed"] is False
    assert confirmed.json()["transfer"]["state"] == "completed"

    detail = client.get(f"/api/samples/{samples[0]['id']}", headers=admin["headers"]).json()
    assert detail["location_id"] == target["id"]
    assert detail["custody_user_id"] == receiver["id"]
    assert detail["active_transfer"] is None
    stock = client.get("/api/sample-operations/stock/by-location", headers=admin["headers"]).json()
    source_stock = next(item for item in stock if item["id"] == source["id"])
    assert source_stock["in_transit_count"] == 0


def test_initiator_cannot_receive_and_designated_receiver_is_enforced(client, admin):
    _, target, samples, receiver = _setup(client, admin, 1)
    order = _create_order(client, admin, target, samples)
    own = _confirm(client, admin["headers"], order["id"], [_confirm_entry(samples[0])])
    assert own.status_code == 422

    # 指定接收人后，其他人不能接收；先取消原单释放样品
    other = _user(client, admin, "receiver.other")
    cancelled = client.post(f"/api/sample-operations/transfers/{order['id']}/cancel", headers=admin["headers"])
    assert cancelled.status_code == 200
    response = client.post(
        "/api/sample-operations/transfers",
        headers=admin["headers"],
        json={"target_location_id": target["id"], "sample_ids": [samples[0]["id"]], "reason": "常温转低温", "receiver_user_id": receiver["id"]},
    )
    assert response.status_code == 201, response.text
    transfer_id = response.json()["transfer"]["id"]
    wrong = _confirm(client, other["headers"], transfer_id, [_confirm_entry(samples[0])])
    assert wrong.status_code == 422
    right = _confirm(client, receiver["headers"], transfer_id, [_confirm_entry(samples[0])])
    assert right.status_code == 200


def test_in_transit_sample_is_frozen_for_other_operations(client, admin):
    _, target, samples, _ = _setup(client, admin, 1)
    sample = samples[0]
    order = _create_order(client, admin, target, samples)

    consumed = client.post(
        f"/api/samples/{sample['id']}/consumptions",
        headers=admin["headers"],
        json={"experiment_code": "EXP-FROZEN", "quantity": 1, "idempotency_key": "frozen-consume"},
    )
    assert consumed.status_code == 409
    aliquot = client.post(
        f"/api/samples/{sample['id']}/aliquots",
        headers=admin["headers"],
        json={"requested_quantity": 5, "children": [{"sample_code": "TRF-S-0-A", "quantity": 5}]},
    )
    assert aliquot.status_code == 409
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={"sample_id": sample["id"], "borrower_user_id": admin["body"]["user"]["id"], "quantity": 1, "due_at": "2026-10-01T00:00:00+00:00"},
    )
    assert loan.status_code == 409
    again = client.post(
        "/api/sample-operations/transfers",
        headers=admin["headers"],
        json={"target_location_id": target["id"], "sample_ids": [sample["id"]], "reason": "重复交接"},
    )
    assert again.status_code == 409

    # 取消后解冻，可以正常操作
    cancelled = client.post(f"/api/sample-operations/transfers/{order['id']}/cancel", headers=admin["headers"])
    assert cancelled.status_code == 200
    consumed = client.post(
        f"/api/samples/{sample['id']}/consumptions",
        headers=admin["headers"],
        json={"experiment_code": "EXP-FROZEN", "quantity": 1, "idempotency_key": "frozen-consume"},
    )
    assert consumed.status_code == 201


def test_duplicate_confirmation_scan_is_idempotent(client, admin):
    _, target, samples, receiver = _setup(client, admin, 1)
    sample = samples[0]
    order = _create_order(client, admin, target, samples)
    first = _confirm(client, receiver["headers"], order["id"], [_confirm_entry(sample)])
    assert first.status_code == 200
    assert first.json()["transfer"]["state"] == "completed"

    replay = _confirm(client, receiver["headers"], order["id"], [_confirm_entry(sample)])
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["transfer"]["state"] == "completed"

    different = _confirm(client, receiver["headers"], order["id"], [_confirm_entry(sample, seal="SEAL-OTHER")])
    assert different.status_code == 409

    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    received_events = [event for event in detail["events"] if event["event_type"] == "transfer.received"]
    assert len(received_events) == 1
    assert detail["quantity"] == sample["quantity"]


def test_split_acceptance_completes_order(client, admin):
    source, target, samples, receiver = _setup(client, admin, 3)
    order = _create_order(client, admin, target, samples)

    first = _confirm(client, receiver["headers"], order["id"], [_confirm_entry(samples[0])])
    assert first.status_code == 200
    assert first.json()["transfer"]["state"] == "pending"
    states = {item["sample_id"]: item["state"] for item in first.json()["transfer"]["items"]}
    assert states[samples[0]["id"]] == "received"
    assert states[samples[1]["id"]] == "pending"
    # 先确认的样品已原子切换到目标位置，其余仍在源库
    moved = client.get(f"/api/samples/{samples[0]['id']}", headers=admin["headers"]).json()
    waiting = client.get(f"/api/samples/{samples[1]['id']}", headers=admin["headers"]).json()
    assert moved["location_id"] == target["id"]
    assert waiting["location_id"] == source["id"]

    second = _confirm(client, receiver["headers"], order["id"], [_confirm_entry(samples[1]), _confirm_entry(samples[2])])
    assert second.status_code == 200
    assert second.json()["transfer"]["state"] == "completed"


def test_rejection_returns_to_source_with_anomaly(client, admin):
    source, target, samples, receiver = _setup(client, admin)
    order = _create_order(client, admin, target, samples)
    rejected = client.post(
        f"/api/sample-operations/transfers/{order['id']}/rejections",
        headers=receiver["headers"],
        json={"items": [{"sample_id": samples[0]["id"], "reason": "封签破损"}]},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["transfer"]["state"] == "pending"

    confirmed = _confirm(client, receiver["headers"], order["id"], [_confirm_entry(samples[1])])
    assert confirmed.status_code == 200
    transfer = confirmed.json()["transfer"]
    assert transfer["state"] == "partially_received"
    states = {item["sample_id"]: item["state"] for item in transfer["items"]}
    assert states[samples[0]["id"]] == "rejected"
    assert states[samples[1]["id"]] == "received"

    stayed = client.get(f"/api/samples/{samples[0]['id']}", headers=admin["headers"]).json()
    assert stayed["location_id"] == source["id"]
    moved = client.get(f"/api/samples/{samples[1]['id']}", headers=admin["headers"]).json()
    assert moved["location_id"] == target["id"]

    anomalies = client.get("/api/samples/anomalies/list", headers=admin["headers"]).json()
    matched = [case for case in anomalies if case["anomaly_type"] == "transfer_rejected" and case["sample_id"] == samples[0]["id"]]
    assert len(matched) == 1
    item = next(item for item in transfer["items"] if item["sample_id"] == samples[0]["id"])
    assert item["anomaly_id"] == matched[0]["id"]


def test_full_rejection_ends_rejected(client, admin):
    source, target, samples, receiver = _setup(client, admin)
    order = _create_order(client, admin, target, samples)
    rejected = client.post(
        f"/api/sample-operations/transfers/{order['id']}/rejections",
        headers=receiver["headers"],
        json={"items": [{"sample_id": sample["id"], "reason": "整批拒收"} for sample in samples]},
    )
    assert rejected.status_code == 200
    assert rejected.json()["transfer"]["state"] == "rejected"
    for sample in samples:
        detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
        assert detail["location_id"] == source["id"]


def test_simultaneous_cancel_and_reject_get_unique_terminal_state(client, admin):
    # 方向一：发起人取消先落库，接收方拒收得到 409，重复取消幂等
    _, target, samples, receiver = _setup(client, admin, 1)
    order = _create_order(client, admin, target, samples)
    cancelled = client.post(f"/api/sample-operations/transfers/{order['id']}/cancel", headers=admin["headers"])
    assert cancelled.status_code == 200
    assert cancelled.json()["transfer"]["state"] == "cancelled"
    too_late = client.post(
        f"/api/sample-operations/transfers/{order['id']}/rejections",
        headers=receiver["headers"],
        json={"items": [{"sample_id": samples[0]["id"], "reason": "拒收"}]},
    )
    assert too_late.status_code == 409
    replay = client.post(f"/api/sample-operations/transfers/{order['id']}/cancel", headers=admin["headers"])
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    view = client.get(f"/api/sample-operations/transfers/{order['id']}", headers=admin["headers"]).json()
    assert view["state"] == "cancelled"
    assert view["items"][0]["state"] == "cancelled"

    # 方向二：接收方全部拒收先落库，发起人取消得到 409
    order2 = _create_order(client, admin, target, samples)
    rejected = client.post(
        f"/api/sample-operations/transfers/{order2['id']}/rejections",
        headers=receiver["headers"],
        json={"items": [{"sample_id": samples[0]["id"], "reason": "整批拒收"}]},
    )
    assert rejected.status_code == 200
    assert rejected.json()["transfer"]["state"] == "rejected"
    cancel_late = client.post(f"/api/sample-operations/transfers/{order2['id']}/cancel", headers=admin["headers"])
    assert cancel_late.status_code == 409
    view = client.get(f"/api/sample-operations/transfers/{order2['id']}", headers=admin["headers"]).json()
    assert view["state"] == "rejected"


def test_cancel_blocked_after_receiving_started(client, admin):
    _, target, samples, receiver = _setup(client, admin)
    order = _create_order(client, admin, target, samples)
    confirmed = _confirm(client, receiver["headers"], order["id"], [_confirm_entry(samples[0])])
    assert confirmed.status_code == 200
    blocked = client.post(f"/api/sample-operations/transfers/{order['id']}/cancel", headers=admin["headers"])
    assert blocked.status_code == 409


def test_expired_transfer_returns_to_source_with_anomaly(client, admin):
    source, target, samples, receiver = _setup(client, admin, 1)
    sample = samples[0]
    order = _create_order(client, admin, target, samples, expires_in_hours=1)

    from app.database import get_connection

    get_connection().execute(
        "UPDATE transfer_orders SET expires_at=? WHERE id=?",
        ("2020-01-01T00:00:00+00:00", order["id"]),
    )
    swept = client.post("/api/sample-operations/transfers/expire-due", headers=admin["headers"])
    assert swept.status_code == 200, swept.text
    assert swept.json()["expired_count"] == 1
    assert swept.json()["transfers"][0]["state"] == "expired"

    view = client.get(f"/api/sample-operations/transfers/{order['id']}", headers=admin["headers"]).json()
    assert view["state"] == "expired"
    assert view["items"][0]["state"] == "returned"
    assert view["items"][0]["anomaly_id"] is not None

    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    assert detail["location_id"] == source["id"]
    assert detail["active_transfer"] is None
    event_types = [event["event_type"] for event in detail["events"]]
    assert "transfer.returned" in event_types

    anomalies = client.get("/api/samples/anomalies/list", headers=admin["headers"]).json()
    assert any(case["anomaly_type"] == "transfer_expired" and case["sample_id"] == sample["id"] for case in anomalies)

    late = _confirm(client, receiver["headers"], order["id"], [_confirm_entry(sample)])
    assert late.status_code == 409


def test_service_restart_preserves_handover_state(client, admin):
    _, target, samples, receiver = _setup(client, admin, 1)
    sample = samples[0]
    order = _create_order(client, admin, target, samples)

    from app.database import close_connection
    from app.main import app

    close_connection()
    with TestClient(app) as restarted:
        view = restarted.get(f"/api/sample-operations/transfers/{order['id']}", headers=admin["headers"])
        assert view.status_code == 200
        assert view.json()["state"] == "pending"
        assert view.json()["items"][0]["state"] == "pending"

        confirmed = _confirm(restarted, receiver["headers"], order["id"], [_confirm_entry(sample)])
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["transfer"]["state"] == "completed"

        replay = _confirm(restarted, receiver["headers"], order["id"], [_confirm_entry(sample)])
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True

        detail = restarted.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
        assert detail["location_id"] == target["id"]


def test_transfer_locations_follow_masking_permissions(client, admin):
    _, target, samples, receiver = _setup(client, admin, 1)
    order = _create_order(client, admin, target, samples)

    masked = client.get(f"/api/sample-operations/transfers/{order['id']}", headers=receiver["headers"]).json()
    assert masked["target_location_code"] == f"MASKED-{target['id']:04d}"
    exact = client.get(f"/api/sample-operations/transfers/{order['id']}", headers=admin["headers"]).json()
    assert exact["target_location_code"] == "TRF-DST"

    listed = client.get("/api/samples", headers=receiver["headers"]).json()
    entry = next(item for item in listed if item["id"] == samples[0]["id"])
    assert entry["active_transfer"]["target_location_code"] == f"MASKED-{target['id']:04d}"


def test_inventory_skips_in_transit_samples(client, admin):
    source, target, samples, _ = _setup(client, admin, 1)
    sample = samples[0]
    _create_order(client, admin, target, samples)

    session = client.post(
        "/api/sample-operations/inventory",
        headers=admin["headers"],
        json={"location_id": source["id"], "session_code": "INV-TRF-01"},
    )
    assert session.status_code == 201
    counted = client.post(
        f"/api/sample-operations/inventory/{session.json()['id']}/counts",
        headers=admin["headers"],
        json={"sample_id": sample["id"], "observed_present": True, "observed_quantity": 20},
    )
    assert counted.status_code == 409
    reconciled = client.post(
        f"/api/sample-operations/inventory/{session.json()['id']}/reconcile",
        headers=admin["headers"],
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["differences"] == []


def test_transfer_creation_is_idempotent(client, admin):
    _, target, samples, _ = _setup(client, admin, 1)
    payload = {
        "transfer_code": "TRF-IDEM-01",
        "target_location_id": target["id"],
        "sample_ids": [samples[0]["id"]],
        "reason": "常温转低温",
    }
    first = client.post("/api/sample-operations/transfers", headers=admin["headers"], json=payload)
    assert first.status_code == 201
    assert first.json()["replayed"] is False
    second = client.post("/api/sample-operations/transfers", headers=admin["headers"], json=payload)
    assert second.status_code == 201
    assert second.json()["replayed"] is True
    assert second.json()["transfer"]["id"] == first.json()["transfer"]["id"]
    conflict = client.post(
        "/api/sample-operations/transfers",
        headers=admin["headers"],
        json={**payload, "reason": "不同的请求"},
    )
    assert conflict.status_code == 409


def test_concurrent_cancel_and_reject_converge_to_single_terminal_state(client, admin):
    import threading

    _, target, samples, receiver = _setup(client, admin, 1)
    order = _create_order(client, admin, target, samples)
    results: dict[str, int] = {}

    def do_cancel():
        response = client.post(f"/api/sample-operations/transfers/{order['id']}/cancel", headers=admin["headers"])
        results["cancel"] = response.status_code

    def do_reject():
        response = client.post(
            f"/api/sample-operations/transfers/{order['id']}/rejections",
            headers=receiver["headers"],
            json={"items": [{"sample_id": samples[0]["id"], "reason": "整批拒收"}]},
        )
        results["reject"] = response.status_code

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_reject)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 两个并发操作必须一胜一负，且终态唯一
    assert sorted(results.values()) == [200, 409]
    view = client.get(f"/api/sample-operations/transfers/{order['id']}", headers=admin["headers"]).json()
    expected = "cancelled" if results["cancel"] == 200 else "rejected"
    assert view["state"] == expected
    assert view["closed_at"] is not None


def test_event_chain_reconstructs_custody_segments(client, admin):
    _, target, samples, receiver = _setup(client, admin, 1)
    sample = samples[0]
    order = _create_order(client, admin, target, samples)
    confirmed = _confirm(client, receiver["headers"], order["id"], [_confirm_entry(sample)])
    assert confirmed.status_code == 200

    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"]).json()
    event_types = [event["event_type"] for event in detail["events"]]
    assert event_types == ["received", "transfer.initiated", "transfer.received"]
    transfer_events = [event for event in detail["events"] if event["event_type"].startswith("transfer.")]
    assert all(event["correlation_id"] == order["transfer_code"] for event in transfer_events)
    assert transfer_events[0]["actor_user_id"] == admin["body"]["user"]["id"]
    assert transfer_events[1]["actor_user_id"] == receiver["id"]

    view = client.get(f"/api/sample-operations/transfers/{order['id']}", headers=admin["headers"]).json()
    segments = view["items"][0]["custody_segments"]
    assert [segment["segment"] for segment in segments] == ["in_transit", "delivered"]
    assert segments[0]["holder_user_id"] == admin["body"]["user"]["id"]
    assert segments[0]["to"] == view["items"][0]["confirmed_at"]
    assert segments[1]["holder_user_id"] == receiver["id"]
    assert segments[1]["location_id"] == target["id"]

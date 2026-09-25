from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.samples.repository import AnomalyRepository, LocationRepository, SampleRepository, TransferRepository
from app.services.audit import AuditContext, AuditService

DEFAULT_EXPIRY_HOURS = 48
MAX_EXPIRY_HOURS = 24 * 30
QUANTITY_EPSILON = 1e-6

STATE_LABELS = {
    "in_transit": "在途",
    "received": "已接收",
    "returned": "已退回",
    "cancelled": "已取消",
}

SYSTEM_ACTOR = AuditContext(actor_user_id=None, actor_name="系统")


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class HandoverService:
    """样品位置双向交接：发起即冻结、逐项验收、验收齐全后原子过户、拒收或超时退回。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.samples = SampleRepository(connection)
        self.locations = LocationRepository(connection)
        self.transfers = TransferRepository(connection)
        self.anomalies = AnomalyRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def initiate(self, principal: Principal, sample_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        transfer_code = data.get("transfer_code") or f"TRF-{uuid.uuid4().hex[:12]}"
        request_digest = _digest(
            {
                "sample_id": sample_id,
                "target_location_id": data["target_location_id"],
                "reason": data["reason"],
                "seal_code": data.get("seal_code"),
                "items": data.get("items"),
                "expires_at": data.get("expires_at"),
            }
        )
        existing = self.transfers.by_code(transfer_code)
        if existing:
            if existing["request_digest"] != request_digest:
                raise ConflictError("交接单号已被不同请求占用")
            return {"transfer": self._present(principal, self.transfers.get_order(existing["id"])), "replayed": True}

        sample = self.samples.get(sample_id)
        if sample["lifecycle_state"] not in {"available", "partially_consumed"}:
            raise ConflictError("当前状态禁止发起位置交接")
        if sample["reserved_quantity"] > 0:
            raise ConflictError("样品数量已被占用，不能发起交接")
        if self.transfers.active_for_sample(sample_id):
            raise ConflictError("样品已有在途交接单")
        target = self.locations.get(data["target_location_id"])
        if not target["active"]:
            raise ValidationError("目标位置已停用")
        if sample["location_id"] == target["id"]:
            raise ValidationError("目标位置与当前位置相同")

        now_dt = self.clock.now()
        expires_at = self._resolve_expiry(data.get("expires_at"), now_dt)
        items = self._normalize_items(data, sample)
        manifest_digest = _digest(
            {
                "transfer_code": transfer_code,
                "sample_id": sample["id"],
                "sample_code": sample["sample_code"],
                "source_location_id": sample["location_id"],
                "target_location_id": target["id"],
                "quantity": sample["quantity"],
                "unit": sample["unit"],
                "items": items,
            }
        )

        now = to_storage(now_dt)
        cursor = self.connection.execute(
            """UPDATE samples SET reserved_quantity=quantity,version=version+1,updated_at=?
               WHERE id=? AND version=? AND reserved_quantity=0 AND lifecycle_state IN ('available','partially_consumed')""",
            (now, sample_id, data["expected_version"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("样品版本已变化或数量被占用，请刷新后重试")
        source_location_version = 0
        if sample["location_id"]:
            source_location_version = self.locations.get(sample["location_id"])["version"]
        try:
            order = self.transfers.create_order(
                {
                    "transfer_code": transfer_code,
                    "sample_id": sample_id,
                    "source_location_id": sample["location_id"],
                    "target_location_id": target["id"],
                    "quantity": sample["quantity"],
                    "unit": sample["unit"],
                    "manifest_digest": manifest_digest,
                    "request_digest": request_digest,
                    "frozen_sample_version": data["expected_version"] + 1,
                    "source_location_version": source_location_version,
                    "initiated_by": principal.user_id,
                    "expires_at": expires_at,
                },
                now,
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("交接单号已被占用") from exc
        for item in items:
            self.transfers.add_item(order["id"], item)
        self.samples.append_event(
            sample_id,
            "transfer.initiated",
            principal.user_id,
            now,
            from_state=sample["lifecycle_state"],
            details={
                "transfer_code": transfer_code,
                "from_location_id": sample["location_id"],
                "to_location_id": target["id"],
                "quantity": sample["quantity"],
                "unit": sample["unit"],
                "manifest_digest": manifest_digest,
                "expires_at": expires_at,
                "reason": data["reason"],
            },
            correlation_id=transfer_code,
        )
        self.audit.record(
            principal,
            "handover.initiate",
            "transfer_order",
            str(order["id"]),
            after=order,
            metadata={"transfer_code": transfer_code, "manifest_digest": manifest_digest},
        )
        return {"transfer": self._present(principal, self.transfers.get_order(order["id"])), "replayed": False}

    def receive(self, principal: Principal, transfer_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        now = to_storage(self.clock.now())
        order = self._expire_if_due(self.transfers.get_order(transfer_id), now)
        receipt = self.transfers.receipt_by_token(transfer_id, data["receive_token"])
        if receipt:
            original_item = self.transfers.item_by_seq(transfer_id, data["item_seq"])
            if original_item is None or receipt["item_id"] != original_item["id"]:
                raise ConflictError("接收令牌已被其他明细使用")
            return {"transfer": self._present(principal, order), "receipt": receipt, "replayed": True}
        if order["state"] != "in_transit":
            raise ConflictError(f"交接单已结束（{STATE_LABELS[order['state']]}），无法验收")
        if principal.user_id == order["initiated_by"]:
            raise ValidationError("接收人不能与交接发起人相同")
        item = self.transfers.item_by_seq(transfer_id, data["item_seq"])
        if item is None:
            raise NotFoundError("交接明细不存在")
        if item["state"] == "confirmed":
            raise ConflictError("该明细已确认，请勿重复扫描")
        if abs(data["confirmed_quantity"] - item["expected_quantity"]) > QUANTITY_EPSILON:
            raise ValidationError("确认数量与清单不符，如实物短缺请拒收")
        if data["seal_code"] != item["seal_code"]:
            raise ValidationError("封签号与清单不符")
        if data["target_location_id"] != order["target_location_id"]:
            raise ValidationError("目标位置与交接单不符")
        try:
            receipt = self.transfers.add_receipt(
                {
                    "transfer_id": transfer_id,
                    "item_id": item["id"],
                    "receive_token": data["receive_token"],
                    "confirmed_quantity": data["confirmed_quantity"],
                    "seal_code": data["seal_code"],
                    "target_location_id": data["target_location_id"],
                    "confirmed_by": principal.user_id,
                    "note": data.get("note", ""),
                },
                now,
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("接收令牌冲突，请勿重复提交") from exc
        self.transfers.confirm_item(item["id"], principal.user_id, now)
        received_quantity = round(order["received_quantity"] + data["confirmed_quantity"], 9)
        self.transfers.set_progress(transfer_id, received_quantity, now)
        self.samples.append_event(
            order["sample_id"],
            "transfer.item_confirmed",
            principal.user_id,
            now,
            details={
                "transfer_code": order["transfer_code"],
                "item_seq": data["item_seq"],
                "confirmed_quantity": data["confirmed_quantity"],
                "seal_code": data["seal_code"],
            },
            correlation_id=order["transfer_code"],
        )
        if received_quantity >= order["quantity"] - QUANTITY_EPSILON:
            self._complete_receipt(principal, order, now)
        order = self.transfers.get_order(transfer_id)
        self.audit.record(
            principal,
            "handover.receive",
            "transfer_order",
            str(transfer_id),
            after=order,
            metadata={"transfer_code": order["transfer_code"], "item_seq": data["item_seq"], "completed": order["state"] == "received"},
        )
        return {"transfer": self._present(principal, order), "receipt": receipt, "replayed": False}

    def cancel(self, principal: Principal, transfer_id: int) -> dict[str, Any]:
        principal.require("samples.write")
        now = to_storage(self.clock.now())
        order = self._expire_if_due(self.transfers.get_order(transfer_id), now)
        if order["state"] != "in_transit":
            raise ConflictError(f"交接单已结束（{STATE_LABELS[order['state']]}），无法取消")
        if principal.user_id != order["initiated_by"]:
            raise ValidationError("只有交接发起人可以取消交接单")
        if order["received_quantity"] > QUANTITY_EPSILON:
            raise ConflictError("交接单已有验收记录，需由接收方拒收处理")
        self.transfers.transition(transfer_id, "cancelled", now)
        self._unfreeze(order["sample_id"], order["quantity"], now)
        self.samples.append_event(
            order["sample_id"],
            "transfer.cancelled",
            principal.user_id,
            now,
            details={
                "transfer_code": order["transfer_code"],
                "from_location_id": order["source_location_id"],
                "to_location_id": order["target_location_id"],
            },
            correlation_id=order["transfer_code"],
        )
        after = self.transfers.get_order(transfer_id)
        self.audit.record(principal, "handover.cancel", "transfer_order", str(transfer_id), before=order, after=after)
        return self._present(principal, after)

    def reject(self, principal: Principal, transfer_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        now = to_storage(self.clock.now())
        order = self._expire_if_due(self.transfers.get_order(transfer_id), now)
        if order["state"] != "in_transit":
            raise ConflictError(f"交接单已结束（{STATE_LABELS[order['state']]}），无法拒收")
        if principal.user_id == order["initiated_by"]:
            raise ValidationError("发起人不能拒收自己的交接单，请使用取消")
        self.transfers.transition(transfer_id, "returned", now, return_reason="rejected")
        self._unfreeze(order["sample_id"], order["quantity"], now)
        anomaly = self.anomalies.create(
            {
                "sample_id": order["sample_id"],
                "anomaly_type": "transfer_rejected",
                "severity": data.get("severity", "medium"),
                "description": f"交接单 {order['transfer_code']} 被接收方拒收：{data['reason']}",
            },
            principal.user_id,
            f"ANM-{uuid.uuid4().hex[:12]}",
            now,
        )
        self.samples.append_event(
            order["sample_id"],
            "transfer.rejected",
            principal.user_id,
            now,
            details={
                "transfer_code": order["transfer_code"],
                "reason": data["reason"],
                "anomaly_id": anomaly["id"],
                "anomaly_code": anomaly["case_code"],
                "received_quantity": order["received_quantity"],
            },
            correlation_id=order["transfer_code"],
        )
        after = self.transfers.get_order(transfer_id)
        self.audit.record(
            principal,
            "handover.reject",
            "transfer_order",
            str(transfer_id),
            before=order,
            after=after,
            metadata={"anomaly_id": anomaly["id"]},
        )
        return self._present(principal, after)

    def detail(self, principal: Principal, transfer_id: int) -> dict[str, Any]:
        principal.require("samples.read")
        now = to_storage(self.clock.now())
        order = self._expire_if_due(self.transfers.get_order(transfer_id), now)
        return self._present(principal, order)

    def list(self, principal: Principal, state: str | None, sample_id: int | None) -> list[dict[str, Any]]:
        principal.require("samples.read")
        if state and state not in STATE_LABELS:
            raise ValidationError("未知的交接单状态")
        now = to_storage(self.clock.now())
        for due in self.transfers.due_orders(now):
            self._expire_order(due, now)
        return [self._present(principal, order, with_children=False) for order in self.transfers.list_orders(state=state, sample_id=sample_id)]

    def sweep_expired(self, principal: Principal) -> dict[str, Any]:
        principal.require("samples.write")
        now = to_storage(self.clock.now())
        expired = []
        for order in self.transfers.due_orders(now):
            if self._expire_order(order, now):
                expired.append(order["transfer_code"])
        return {"expired_count": len(expired), "expired_transfer_codes": expired}

    def _complete_receipt(self, principal: Principal, order: dict[str, Any], now: str) -> None:
        sample = self.samples.get(order["sample_id"])
        if sample["version"] != order["frozen_sample_version"]:
            raise ConflictError("样品在交接期间被变更，无法完成交接")
        cursor = self.connection.execute(
            """UPDATE samples SET location_id=?,custody_user_id=?,reserved_quantity=reserved_quantity-?,version=version+1,updated_at=?
               WHERE id=? AND version=? AND reserved_quantity>=?""",
            (
                order["target_location_id"],
                principal.user_id,
                order["quantity"],
                now,
                order["sample_id"],
                order["frozen_sample_version"],
                order["quantity"],
            ),
        )
        if cursor.rowcount != 1:
            raise ConflictError("样品在交接期间被变更，无法完成交接")
        self.transfers.transition(order["id"], "received", now)
        self.samples.append_event(
            order["sample_id"],
            "transfer.received",
            principal.user_id,
            now,
            details={
                "transfer_code": order["transfer_code"],
                "from_location_id": order["source_location_id"],
                "to_location_id": order["target_location_id"],
                "custody_user_id": principal.user_id,
                "manifest_digest": order["manifest_digest"],
            },
            correlation_id=order["transfer_code"],
        )

    def _expire_if_due(self, order: dict[str, Any], now: str) -> dict[str, Any]:
        if order["state"] == "in_transit" and order["expires_at"] <= now:
            self._expire_order(order, now)
            return self.transfers.get_order(order["id"])
        return order

    def _expire_order(self, order: dict[str, Any], now: str) -> bool:
        try:
            self.transfers.transition(order["id"], "returned", now, return_reason="expired")
        except ConflictError:
            return False
        self._unfreeze(order["sample_id"], order["quantity"], now)
        anomaly = self.anomalies.create(
            {
                "sample_id": order["sample_id"],
                "anomaly_type": "transfer_expired",
                "severity": "high",
                "description": f"交接单 {order['transfer_code']} 超过验收时限，已按规则退回源库",
            },
            order["initiated_by"],
            f"ANM-{uuid.uuid4().hex[:12]}",
            now,
        )
        self.samples.append_event(
            order["sample_id"],
            "transfer.expired",
            None,
            now,
            details={
                "transfer_code": order["transfer_code"],
                "anomaly_id": anomaly["id"],
                "anomaly_code": anomaly["case_code"],
                "received_quantity": order["received_quantity"],
            },
            correlation_id=order["transfer_code"],
        )
        self.audit.record(
            SYSTEM_ACTOR,
            "handover.expire",
            "transfer_order",
            str(order["id"]),
            before=order,
            after=self.transfers.get_order(order["id"]),
            metadata={"anomaly_id": anomaly["id"]},
        )
        return True

    def _unfreeze(self, sample_id: int, quantity: float, now: str) -> None:
        cursor = self.connection.execute(
            "UPDATE samples SET reserved_quantity=reserved_quantity-?,version=version+1,updated_at=? WHERE id=? AND reserved_quantity>=?",
            (quantity, now, sample_id, quantity),
        )
        if cursor.rowcount != 1:
            raise ConflictError("样品冻结数量异常，无法解除冻结")

    def _resolve_expiry(self, raw: str | None, now_dt) -> str:
        if raw:
            try:
                expires_dt = from_storage(raw)
            except ValueError:
                expires_dt = None
            if expires_dt is None:
                raise ValidationError("验收截止时间格式不正确")
        else:
            expires_dt = now_dt + timedelta(hours=DEFAULT_EXPIRY_HOURS)
        if expires_dt <= now_dt:
            raise ValidationError("验收截止时间必须晚于当前时间")
        if expires_dt > now_dt + timedelta(hours=MAX_EXPIRY_HOURS):
            raise ValidationError("验收截止时间不能超过 30 天")
        return to_storage(expires_dt)

    def _normalize_items(self, data: dict[str, Any], sample: dict[str, Any]) -> list[dict[str, Any]]:
        if data.get("items"):
            items = [
                {"item_seq": item["item_seq"], "expected_quantity": item["expected_quantity"], "seal_code": item["seal_code"]}
                for item in data["items"]
            ]
        else:
            items = [{"item_seq": 1, "expected_quantity": sample["quantity"], "seal_code": data["seal_code"]}]
        items.sort(key=lambda item: item["item_seq"])
        total = round(sum(item["expected_quantity"] for item in items), 9)
        if abs(total - sample["quantity"]) > QUANTITY_EPSILON:
            raise ValidationError("明细数量之和必须等于样品当前数量")
        return items

    def _present(self, principal: Principal, order: dict[str, Any], *, with_children: bool = True) -> dict[str, Any]:
        exact = principal.can("*") or principal.can("locations.read_sensitive")
        result = dict(order)
        for side in ("source", "target"):
            sensitivity = result.get(f"{side}_location_sensitivity")
            if sensitivity and sensitivity != "normal" and not exact:
                location_id = result.get(f"{side}_location_id")
                result[f"{side}_location_code"] = f"MASKED-{location_id:04d}" if location_id else None
        if with_children:
            result["items"] = self.transfers.items(order["id"])
            result["receipts"] = self.transfers.receipts(order["id"])
            result["events"] = self.samples.events_by_correlation(order["transfer_code"])
        return result

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.samples.repository import AnomalyRepository, LocationRepository, SampleRepository, row_dict
from app.services.audit import AuditContext, AuditService

DEFAULT_EXPIRES_HOURS = 24.0
MAX_EXPIRES_HOURS = 168.0
QUANTITY_EPSILON = 1e-9

# 交接途中禁止变更样品保管属性的生命周期状态
BLOCKED_LIFECYCLE_STATES = {"loaned", "pending_destruction", "destroyed", "quarantined", "consumed"}

_CLOSED_REASONS = {
    "completed": None,
    "partially_received": "部分明细未接收，已按规则退回源库",
    "rejected": "接收方拒收，明细退回源库",
    "expired": "超时未接收，按规则退回源库",
    "cancelled": "发起人取消",
}


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def active_transfer_items(connection: sqlite3.Connection, sample_ids: list[int]) -> list[dict[str, Any]]:
    """查询仍处于在途冻结状态（明细 pending）的样品交接信息。"""
    if not sample_ids:
        return []
    placeholders = ",".join("?" for _ in sample_ids)
    rows = connection.execute(
        f"""SELECT ti.sample_id,ti.state AS item_state,t.id AS transfer_id,t.transfer_code,
                   t.target_location_id,t.expires_at,t.state AS transfer_state
            FROM transfer_items ti JOIN transfer_orders t ON t.id=ti.transfer_id
            WHERE ti.state='pending' AND ti.sample_id IN ({placeholders})""",
        tuple(sample_ids),
    ).fetchall()
    return [dict(row) for row in rows]


def expire_due_transfers(connection: sqlite3.Connection, clock: Clock | None = None) -> list[dict[str, Any]]:
    """惰性清理超时交接单：任何读写路径都会先调用，保证服务重启后也能收敛到唯一终态。"""
    return TransferService(connection, clock).expire_due()


def active_transfer_map(connection: sqlite3.Connection, sample_ids: list[int]) -> dict[int, dict[str, Any]]:
    """按样品返回在途交接信息（含目标位置编码与敏感度），用于全局查询展示。"""
    if not sample_ids:
        return {}
    placeholders = ",".join("?" for _ in sample_ids)
    rows = connection.execute(
        f"""SELECT ti.sample_id,t.transfer_code,t.expires_at,t.target_location_id,
                   tl.code AS target_location_code,tl.sensitivity AS target_sensitivity
            FROM transfer_items ti
            JOIN transfer_orders t ON t.id=ti.transfer_id
            JOIN storage_locations tl ON tl.id=t.target_location_id
            WHERE ti.state='pending' AND ti.sample_id IN ({placeholders})""",
        tuple(sample_ids),
    ).fetchall()
    return {int(row["sample_id"]): dict(row) for row in rows}


def guard_samples_not_in_transit(connection: sqlite3.Connection, clock: Clock | None, sample_ids: list[int]) -> None:
    """冻结守卫：在途样品禁止消耗、分装、借用、销毁、再次交接与盘点。"""
    expire_due_transfers(connection, clock)
    active = active_transfer_items(connection, sample_ids)
    if active:
        codes = sorted({row["transfer_code"] for row in active})
        raise ConflictError("样品在交接途中已冻结，待交接终态后才能操作", context={"transfer_codes": codes})


class TransferRepository:
    ORDER_SELECT = """SELECT t.*,sl.code AS source_location_code,sl.sensitivity AS source_sensitivity,
                      tl.code AS target_location_code,tl.sensitivity AS target_sensitivity,
                      iu.display_name AS initiator_name,ru.display_name AS receiver_name
               FROM transfer_orders t
               JOIN storage_locations sl ON sl.id=t.source_location_id
               JOIN storage_locations tl ON tl.id=t.target_location_id
               JOIN users iu ON iu.id=t.initiator_user_id
               LEFT JOIN users ru ON ru.id=t.receiver_user_id"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create_order(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO transfer_orders(
                   transfer_code,request_digest,state,initiator_user_id,receiver_user_id,
                   source_location_id,source_location_version,target_location_id,target_location_version,
                   item_count,manifest_json,manifest_digest,reason,expires_at,created_at,updated_at
               ) VALUES(?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["transfer_code"], data["request_digest"], data["initiator_user_id"], data.get("receiver_user_id"),
                data["source_location_id"], data["source_location_version"], data["target_location_id"],
                data["target_location_version"], data["item_count"], json.dumps(data["manifest"], ensure_ascii=False, sort_keys=True),
                data["manifest_digest"], data["reason"], data["expires_at"], now, now,
            ),
        )
        return self.get_order(cursor.lastrowid)

    def get_order(self, transfer_id: int) -> dict[str, Any]:
        return row_dict(
            self.connection.execute(self.ORDER_SELECT + " WHERE t.id=?", (transfer_id,)).fetchone()
        )

    def find_by_code(self, transfer_code: str) -> dict[str, Any] | None:
        row = self.connection.execute(self.ORDER_SELECT + " WHERE t.transfer_code=?", (transfer_code,)).fetchone()
        return dict(row) if row else None

    def list_orders(self, state: str | None, location_id: int | None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("t.state=?")
            params.append(state)
        if location_id:
            clauses.append("(t.source_location_id=? OR t.target_location_id=?)")
            params.extend([location_id, location_id])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            self.ORDER_SELECT + where + " ORDER BY t.id DESC LIMIT 200",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def due_orders(self, now: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            self.ORDER_SELECT + " WHERE t.state='pending' AND t.expires_at<=? ORDER BY t.id",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    def create_item(self, transfer_id: int, manifest_item: dict[str, Any], now: str) -> int:
        cursor = self.connection.execute(
            """INSERT INTO transfer_items(transfer_id,sample_id,sample_code,expected_quantity,sample_version,state,created_at,updated_at)
               VALUES(?,?,?,?,?,'pending',?,?)""",
            (
                transfer_id, manifest_item["sample_id"], manifest_item["sample_code"],
                manifest_item["expected_quantity"], manifest_item["sample_version"], now, now,
            ),
        )
        return int(cursor.lastrowid)

    def items(self, transfer_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM transfer_items WHERE transfer_id=? ORDER BY id",
            (transfer_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def events_by_sample(self, transfer_code: str, sample_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not sample_ids:
            return {}
        placeholders = ",".join("?" for _ in sample_ids)
        rows = self.connection.execute(
            f"SELECT * FROM sample_events WHERE correlation_id=? AND sample_id IN ({placeholders}) ORDER BY id",
            (transfer_code, *sample_ids),
        ).fetchall()
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            grouped.setdefault(item["sample_id"], []).append(item)
        return grouped


class TransferService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.transfers = TransferRepository(connection)
        self.samples = SampleRepository(connection)
        self.locations = LocationRepository(connection)
        self.anomalies = AnomalyRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ---------- 发起 ----------

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        self.expire_due()
        sample_ids = [int(value) for value in data["sample_ids"]]
        if len(set(sample_ids)) != len(sample_ids):
            raise ValidationError("交接清单中存在重复样品")
        expires_hours = data.get("expires_in_hours") or DEFAULT_EXPIRES_HOURS
        expires_hours = min(float(expires_hours), MAX_EXPIRES_HOURS)
        transfer_code = data.get("transfer_code") or f"TRF-{uuid.uuid4().hex[:12]}"
        request_digest = _digest(
            {
                "target_location_id": data["target_location_id"],
                "receiver_user_id": data.get("receiver_user_id"),
                "reason": data["reason"],
                "expires_in_hours": expires_hours,
                "sample_ids": sorted(sample_ids),
            }
        )
        existing = self.transfers.find_by_code(transfer_code)
        if existing:
            if existing["request_digest"] != request_digest:
                raise ConflictError("交接单编号已被不同请求占用")
            return {"transfer": self.present(principal, existing, with_items=True), "replayed": True}
        samples = [self.samples.get(sample_id) for sample_id in sorted(sample_ids)]
        for sample in samples:
            if sample["lifecycle_state"] in BLOCKED_LIFECYCLE_STATES:
                raise ConflictError(
                    f"样品 {sample['sample_code']} 当前状态禁止交接",
                    context={"sample_code": sample["sample_code"], "lifecycle_state": sample["lifecycle_state"]},
                )
        location_ids = {sample["location_id"] for sample in samples}
        if None in location_ids:
            raise ValidationError("存在没有保管位置的样品，无法生成交接清单")
        if len(location_ids) != 1:
            raise ValidationError("同一交接单的样品必须位于同一源位置")
        source = self.locations.get(samples[0]["location_id"])
        target = self.locations.get(data["target_location_id"])
        if not target["active"]:
            raise ValidationError("目标保管位置已停用")
        if source["id"] == target["id"]:
            raise ValidationError("目标位置与源位置相同，无需交接")
        receiver_user_id = data.get("receiver_user_id")
        if receiver_user_id is not None:
            if receiver_user_id == principal.user_id:
                raise ValidationError("接收人不能是发起人自己")
            if not self.connection.execute("SELECT 1 FROM users WHERE id=?", (receiver_user_id,)).fetchone():
                raise NotFoundError("指定接收人不存在")
        active = active_transfer_items(self.connection, sorted(sample_ids))
        if active:
            codes = sorted({row["transfer_code"] for row in active})
            raise ConflictError("样品已在其他进行中的交接单内", context={"transfer_codes": codes})
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        expires_at = to_storage(now_dt + timedelta(hours=expires_hours))
        manifest_items = [
            {
                "sample_id": sample["id"],
                "sample_code": sample["sample_code"],
                "expected_quantity": sample["quantity"],
                "unit": sample["unit"],
                "sample_version": sample["version"],
            }
            for sample in samples
        ]
        totals: dict[str, float] = {}
        for sample in samples:
            totals[sample["unit"]] = round(totals.get(sample["unit"], 0.0) + float(sample["quantity"]), 9)
        manifest = {"item_count": len(manifest_items), "total_by_unit": dict(sorted(totals.items())), "items": manifest_items}
        manifest_digest = _digest(
            {
                "source_location_id": source["id"],
                "source_location_version": source["version"],
                "target_location_id": target["id"],
                "target_location_version": target["version"],
                "items": manifest_items,
            }
        )
        values = {
            "transfer_code": transfer_code,
            "request_digest": request_digest,
            "initiator_user_id": principal.user_id,
            "receiver_user_id": receiver_user_id,
            "source_location_id": source["id"],
            "source_location_version": source["version"],
            "target_location_id": target["id"],
            "target_location_version": target["version"],
            "item_count": len(manifest_items),
            "manifest": manifest,
            "manifest_digest": manifest_digest,
            "reason": data["reason"],
            "expires_at": expires_at,
        }
        try:
            order = self.transfers.create_order(values, now)
        except sqlite3.IntegrityError:
            existing = self.transfers.find_by_code(transfer_code)
            if existing and existing["request_digest"] == request_digest:
                return {"transfer": self.present(principal, existing, with_items=True), "replayed": True}
            raise ConflictError("交接单编号已被不同请求占用")
        try:
            for manifest_item in manifest_items:
                self.transfers.create_item(order["id"], manifest_item, now)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("样品已在其他进行中的交接单内") from exc
        for manifest_item in manifest_items:
            self.samples.append_event(
                manifest_item["sample_id"],
                "transfer.initiated",
                principal.user_id,
                now,
                details={
                    "transfer_id": order["id"],
                    "transfer_code": transfer_code,
                    "from_location_id": source["id"],
                    "to_location_id": target["id"],
                    "expected_quantity": manifest_item["expected_quantity"],
                    "sample_version": manifest_item["sample_version"],
                    "source_location_version": source["version"],
                },
                correlation_id=transfer_code,
            )
        self.audit.record(
            principal,
            "transfer.create",
            "transfer_order",
            str(order["id"]),
            after=order,
            metadata={"manifest_digest": manifest_digest, "item_count": len(manifest_items)},
        )
        return {"transfer": self.present(principal, self.transfers.get_order(order["id"]), with_items=True), "replayed": False}

    # ---------- 接收 ----------

    def confirm(self, principal: Principal, transfer_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        self.expire_due()
        order = self.transfers.get_order(transfer_id)
        items_by_sample = {item["sample_id"]: item for item in self.transfers.items(transfer_id)}
        if order["state"] != "pending":
            if self._is_confirm_replay(items_by_sample, data["items"]):
                return {"transfer": self.present(principal, order, with_items=True), "replayed": True}
            raise ConflictError("交接单已结束，无法接收", context={"state": order["state"]})
        self._require_receiver(principal, order)
        now = to_storage(self.clock.now())
        replayed = True
        for entry in data["items"]:
            item = items_by_sample.get(entry["sample_id"])
            if item is None:
                raise ValidationError("样品不在交接清单中", context={"sample_id": entry["sample_id"]})
            if entry.get("target_location_id") is not None and entry["target_location_id"] != order["target_location_id"]:
                raise ValidationError("确认的目标位置与交接单不一致")
            if not entry["seal_intact"]:
                raise ValidationError("封签破损的样品不能确认接收，请按拒收处理")
            if abs(entry["received_quantity"] - item["expected_quantity"]) > QUANTITY_EPSILON:
                raise ValidationError(
                    "确认数量与交接清单不一致，数量不符请按拒收处理",
                    context={"sample_id": item["sample_id"], "expected_quantity": item["expected_quantity"]},
                )
            if item["state"] == "received":
                if abs(float(item["received_quantity"]) - entry["received_quantity"]) <= QUANTITY_EPSILON and item["seal_code"] == entry["seal_code"]:
                    continue
                raise ConflictError("该样品已接收，重复扫描内容不一致", context={"sample_id": item["sample_id"]})
            if item["state"] != "pending":
                raise ConflictError(
                    "该样品明细已处理，不能重复接收",
                    context={"sample_id": item["sample_id"], "state": item["state"]},
                )
            replayed = False
            cursor = self.connection.execute(
                """UPDATE transfer_items SET state='received',received_quantity=?,seal_code=?,confirmed_by=?,confirmed_at=?,updated_at=?
                   WHERE id=? AND state='pending'""",
                (entry["received_quantity"], entry["seal_code"], principal.user_id, now, now, item["id"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("交接明细状态已变化，请刷新后重试")
            updated = self.connection.execute(
                """UPDATE samples SET location_id=?,custody_user_id=?,version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (order["target_location_id"], principal.user_id, now, item["sample_id"], item["sample_version"]),
            )
            if updated.rowcount != 1:
                raise ConflictError("样品在交接期间被变更，保管关系无法原子切换")
            self.samples.append_event(
                item["sample_id"],
                "transfer.received",
                principal.user_id,
                now,
                details={
                    "transfer_id": transfer_id,
                    "transfer_code": order["transfer_code"],
                    "from_location_id": order["source_location_id"],
                    "to_location_id": order["target_location_id"],
                    "received_quantity": entry["received_quantity"],
                    "seal_code": entry["seal_code"],
                },
                correlation_id=order["transfer_code"],
            )
        closed = self._close_if_ready(order, now)
        self.audit.record(
            principal,
            "transfer.confirm",
            "transfer_order",
            str(transfer_id),
            before=order,
            after=closed,
            metadata={"sample_ids": [entry["sample_id"] for entry in data["items"]]},
        )
        return {"transfer": self.present(principal, closed, with_items=True), "replayed": replayed}

    # ---------- 拒收 ----------

    def reject(self, principal: Principal, transfer_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        self.expire_due()
        order = self.transfers.get_order(transfer_id)
        items_by_sample = {item["sample_id"]: item for item in self.transfers.items(transfer_id)}
        if order["state"] != "pending":
            if self._is_reject_replay(items_by_sample, data["items"]):
                return {"transfer": self.present(principal, order, with_items=True), "replayed": True}
            raise ConflictError("交接单已结束，无法拒收", context={"state": order["state"]})
        self._require_receiver(principal, order)
        now = to_storage(self.clock.now())
        replayed = True
        for entry in data["items"]:
            item = items_by_sample.get(entry["sample_id"])
            if item is None:
                raise ValidationError("样品不在交接清单中", context={"sample_id": entry["sample_id"]})
            if item["state"] == "rejected":
                if item["note"] == entry["reason"]:
                    continue
                raise ConflictError("该样品已拒收，重复拒收内容不一致", context={"sample_id": item["sample_id"]})
            if item["state"] != "pending":
                raise ConflictError(
                    "该样品明细已处理，不能拒收",
                    context={"sample_id": item["sample_id"], "state": item["state"]},
                )
            replayed = False
            anomaly = self.anomalies.create(
                {
                    "sample_id": item["sample_id"],
                    "anomaly_type": "transfer_rejected",
                    "severity": "medium",
                    "description": f"交接单 {order['transfer_code']} 样品 {item['sample_code']} 被接收方拒收：{entry['reason']}",
                },
                principal.user_id,
                f"ANM-{uuid.uuid4().hex[:12]}",
                now,
            )
            cursor = self.connection.execute(
                """UPDATE transfer_items SET state='rejected',note=?,confirmed_by=?,confirmed_at=?,anomaly_id=?,updated_at=?
                   WHERE id=? AND state='pending'""",
                (entry["reason"], principal.user_id, now, anomaly["id"], now, item["id"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("交接明细状态已变化，请刷新后重试")
            self.samples.append_event(
                item["sample_id"],
                "transfer.rejected",
                principal.user_id,
                now,
                details={
                    "transfer_id": transfer_id,
                    "transfer_code": order["transfer_code"],
                    "reason": entry["reason"],
                    "anomaly_id": anomaly["id"],
                    "returned_to_location_id": order["source_location_id"],
                },
                correlation_id=order["transfer_code"],
            )
        closed = self._close_if_ready(order, now)
        self.audit.record(
            principal,
            "transfer.reject",
            "transfer_order",
            str(transfer_id),
            before=order,
            after=closed,
            metadata={"sample_ids": [entry["sample_id"] for entry in data["items"]]},
        )
        return {"transfer": self.present(principal, closed, with_items=True), "replayed": replayed}

    # ---------- 取消 ----------

    def cancel(self, principal: Principal, transfer_id: int) -> dict[str, Any]:
        principal.require("samples.write")
        self.expire_due()
        order = self.transfers.get_order(transfer_id)
        if order["initiator_user_id"] != principal.user_id:
            raise ValidationError("只有发起人可以取消交接单")
        if order["state"] == "cancelled":
            return {"transfer": self.present(principal, order, with_items=True), "replayed": True}
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            """UPDATE transfer_orders SET state='cancelled',closed_at=?,closed_reason=?,version=version+1,updated_at=?
               WHERE id=? AND state='pending'
               AND NOT EXISTS(SELECT 1 FROM transfer_items WHERE transfer_id=? AND state<>'pending')""",
            (now, _CLOSED_REASONS["cancelled"], now, transfer_id, transfer_id),
        )
        if cursor.rowcount != 1:
            current = self.transfers.get_order(transfer_id)
            if current["state"] == "pending":
                raise ConflictError("交接已开始接收，剩余明细请由接收方确认或拒收")
            raise ConflictError("交接单已结束，无法取消", context={"state": current["state"]})
        items = self.transfers.items(transfer_id)
        self.connection.execute(
            "UPDATE transfer_items SET state='cancelled',updated_at=? WHERE transfer_id=? AND state='pending'",
            (now, transfer_id),
        )
        for item in items:
            if item["state"] != "pending":
                continue
            self.samples.append_event(
                item["sample_id"],
                "transfer.cancelled",
                principal.user_id,
                now,
                details={"transfer_id": transfer_id, "transfer_code": order["transfer_code"]},
                correlation_id=order["transfer_code"],
            )
        cancelled = self.transfers.get_order(transfer_id)
        self.audit.record(principal, "transfer.cancel", "transfer_order", str(transfer_id), before=order, after=cancelled)
        return {"transfer": self.present(principal, cancelled, with_items=True), "replayed": False}

    # ---------- 超时清理 ----------

    def expire_due(self, principal: Principal | None = None) -> list[dict[str, Any]]:
        if principal is not None:
            principal.require("samples.write")
        now = to_storage(self.clock.now())
        expired = []
        for order in self.transfers.due_orders(now):
            for item in self.transfers.items(order["id"]):
                if item["state"] != "pending":
                    continue
                anomaly = self.anomalies.create(
                    {
                        "sample_id": item["sample_id"],
                        "anomaly_type": "transfer_expired",
                        "severity": "high",
                        "description": f"交接单 {order['transfer_code']} 超时未接收，样品 {item['sample_code']} 按规则退回源库",
                    },
                    order["initiator_user_id"],
                    f"ANM-{uuid.uuid4().hex[:12]}",
                    now,
                )
                cursor = self.connection.execute(
                    """UPDATE transfer_items SET state='returned',anomaly_id=?,updated_at=?
                       WHERE id=? AND state='pending'""",
                    (anomaly["id"], now, item["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                self.samples.append_event(
                    item["sample_id"],
                    "transfer.returned",
                    None,
                    now,
                    details={
                        "transfer_id": order["id"],
                        "transfer_code": order["transfer_code"],
                        "reason": "expired",
                        "anomaly_id": anomaly["id"],
                        "returned_to_location_id": order["source_location_id"],
                    },
                    correlation_id=order["transfer_code"],
                )
            closed = self._close_if_ready(order, now)
            self.audit.record(
                AuditContext(actor_user_id=None, actor_name="系统"),
                "transfer.expire",
                "transfer_order",
                str(order["id"]),
                before=order,
                after=closed,
            )
            expired.append(closed)
        return expired

    # ---------- 查询 ----------

    def get(self, principal: Principal, transfer_id: int) -> dict[str, Any]:
        principal.require("samples.read")
        self.expire_due()
        return self.present(principal, self.transfers.get_order(transfer_id), with_items=True)

    def list(self, principal: Principal, state: str | None, location_id: int | None) -> list[dict[str, Any]]:
        principal.require("samples.read")
        self.expire_due()
        return [self.present(principal, order) for order in self.transfers.list_orders(state, location_id)]

    def present(self, principal: Principal, order: dict[str, Any], *, with_items: bool = False) -> dict[str, Any]:
        exact = "*" in principal.permissions or "locations.read_sensitive" in principal.permissions
        result = dict(order)
        result["manifest"] = json.loads(result.pop("manifest_json"))
        if not exact:
            if result.get("source_sensitivity") and result["source_sensitivity"] != "normal":
                result["source_location_code"] = f"MASKED-{result['source_location_id']:04d}"
            if result.get("target_sensitivity") and result["target_sensitivity"] != "normal":
                result["target_location_code"] = f"MASKED-{result['target_location_id']:04d}"
        if with_items:
            items = self.transfers.items(order["id"])
            events = self.transfers.events_by_sample(order["transfer_code"], [item["sample_id"] for item in items])
            result["items"] = [
                self._present_item(order, item, events.get(item["sample_id"], []))
                for item in items
            ]
        return result

    # ---------- 内部 ----------

    def _present_item(self, order: dict[str, Any], item: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        result = dict(item)
        result["events"] = events
        result["custody_segments"] = self._custody_segments(order, item)
        return result

    def _custody_segments(self, order: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
        terminal_at = item["confirmed_at"]
        if terminal_at is None and item["state"] in {"returned", "cancelled"}:
            terminal_at = order["closed_at"]
        segments = [
            {
                "segment": "in_transit",
                "holder_user_id": order["initiator_user_id"],
                "location_id": order["source_location_id"],
                "from": order["created_at"],
                "to": terminal_at,
                "responsibility": "发起方运输责任",
            }
        ]
        if item["state"] == "received":
            segments.append(
                {
                    "segment": "delivered",
                    "holder_user_id": item["confirmed_by"],
                    "location_id": order["target_location_id"],
                    "from": item["confirmed_at"],
                    "to": None,
                    "responsibility": "接收方保管责任",
                }
            )
        elif item["state"] in {"rejected", "returned", "cancelled"}:
            segments.append(
                {
                    "segment": "returned_to_source",
                    "holder_user_id": order["initiator_user_id"],
                    "location_id": order["source_location_id"],
                    "from": terminal_at,
                    "to": None,
                    "responsibility": "退回源库，发起方保管责任",
                }
            )
        return segments

    def _require_receiver(self, principal: Principal, order: dict[str, Any]) -> None:
        if order["initiator_user_id"] == principal.user_id:
            raise ValidationError("发起人不能接收自己发起的交接单")
        if order["receiver_user_id"] and order["receiver_user_id"] != principal.user_id:
            raise ValidationError("该交接单指定了其他接收人")

    def _is_confirm_replay(self, items_by_sample: dict[int, dict[str, Any]], entries: list[dict[str, Any]]) -> bool:
        for entry in entries:
            item = items_by_sample.get(entry["sample_id"])
            if item is None or item["state"] != "received":
                return False
            if abs(float(item["received_quantity"]) - entry["received_quantity"]) > QUANTITY_EPSILON:
                return False
            if item["seal_code"] != entry["seal_code"]:
                return False
        return True

    def _is_reject_replay(self, items_by_sample: dict[int, dict[str, Any]], entries: list[dict[str, Any]]) -> bool:
        for entry in entries:
            item = items_by_sample.get(entry["sample_id"])
            if item is None or item["state"] != "rejected" or item["note"] != entry["reason"]:
                return False
        return True

    def _terminal_state(self, transfer_id: int) -> str | None:
        states = {item["state"] for item in self.transfers.items(transfer_id)}
        if "pending" in states:
            return None
        if "received" in states:
            return "completed" if states == {"received"} else "partially_received"
        if "returned" in states:
            return "expired"
        if "rejected" in states:
            return "rejected"
        return "cancelled"

    def _close_if_ready(self, order: dict[str, Any], now: str) -> dict[str, Any]:
        terminal = self._terminal_state(order["id"])
        if terminal is None:
            return self.transfers.get_order(order["id"])
        cursor = self.connection.execute(
            """UPDATE transfer_orders SET state=?,closed_at=?,closed_reason=?,version=version+1,updated_at=?
               WHERE id=? AND state='pending'""",
            (terminal, now, _CLOSED_REASONS[terminal], now, order["id"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("交接单状态已变化，请刷新后重试")
        return self.transfers.get_order(order["id"])

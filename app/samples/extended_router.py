from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.database import get_connection, transaction
from app.core.security import Principal
from app.samples.extended_schemas import (
    CollectionCreate,
    DestructionExecute,
    InventoryCount,
    InventoryStart,
    TransferConfirm,
    TransferOrderCreate,
    TransferReject,
)
from app.samples.inventory import InventoryService, StockSummaryService
from app.samples.operations import CollectionService, DestructionService, LineageService
from app.samples.reporting import BatchReconciliationService, ExceptionAgingService
from app.samples.transfers import TransferService

router = APIRouter(prefix="/api/sample-operations", tags=["样品作业"])


@router.post("/collections", status_code=status.HTTP_201_CREATED)
def register_collection(payload: CollectionCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return CollectionService(connection).register(principal, payload.model_dump())


@router.post("/transfers", status_code=status.HTTP_201_CREATED)
def create_transfer(payload: TransferOrderCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return TransferService(connection).create(principal, payload.model_dump())


@router.get("/transfers")
def list_transfers(
    state: str | None = Query(default=None),
    location_id: int | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    with transaction(immediate=True) as connection:
        return TransferService(connection).list(principal, state, location_id)


@router.post("/transfers/expire-due")
def expire_due_transfers(principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        service = TransferService(connection)
        expired = service.expire_due(principal)
        return {"expired_count": len(expired), "transfers": [service.present(principal, order) for order in expired]}


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return TransferService(connection).get(principal, transfer_id)


@router.post("/transfers/{transfer_id}/confirmations")
def confirm_transfer(transfer_id: int, payload: TransferConfirm, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return TransferService(connection).confirm(principal, transfer_id, payload.model_dump())


@router.post("/transfers/{transfer_id}/rejections")
def reject_transfer(transfer_id: int, payload: TransferReject, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return TransferService(connection).reject(principal, transfer_id, payload.model_dump())


@router.post("/transfers/{transfer_id}/cancel")
def cancel_transfer(transfer_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return TransferService(connection).cancel(principal, transfer_id)


@router.get("/{sample_id}/lineage")
def sample_lineage(sample_id: int, principal: Principal = Depends(current_principal)):
    return LineageService(get_connection()).graph(principal, sample_id)


@router.post("/inventory", status_code=status.HTTP_201_CREATED)
def start_inventory(payload: InventoryStart, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).start(principal, payload.location_id, payload.session_code)


@router.post("/inventory/{session_id}/counts")
def record_count(session_id: int, payload: InventoryCount, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).count(
            principal,
            session_id,
            payload.sample_id,
            payload.observed_present,
            payload.observed_quantity,
            payload.note,
        )


@router.post("/inventory/{session_id}/reconcile")
def reconcile_inventory(session_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).reconcile(principal, session_id)


@router.post("/inventory/{session_id}/close")
def close_inventory(session_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return InventoryService(connection).close_without_adjustment(principal, session_id)


@router.get("/stock/by-location")
def stock_by_location(principal: Principal = Depends(current_principal)):
    return StockSummaryService(get_connection()).by_location(principal)


@router.get("/stock/by-state")
def stock_by_state(principal: Principal = Depends(current_principal)):
    return StockSummaryService(get_connection()).by_state(principal)


@router.post("/destructions/{request_id}", status_code=status.HTTP_201_CREATED)
def execute_destruction(
    request_id: int,
    payload: DestructionExecute,
    principal: Principal = Depends(current_principal),
):
    with transaction(immediate=True) as connection:
        return DestructionService(connection).execute(principal, request_id, payload.model_dump())


@router.get("/batches/{batch_id}/reconciliation")
def batch_reconciliation(batch_id: int, principal: Principal = Depends(current_principal)):
    return BatchReconciliationService(get_connection()).detail(principal, batch_id)


@router.get("/batches/open")
def open_batches(principal: Principal = Depends(current_principal)):
    return BatchReconciliationService(get_connection()).open_batches(principal)


@router.get("/exceptions/aging")
def exception_aging(principal: Principal = Depends(current_principal)):
    return ExceptionAgingService(get_connection()).summary(principal)

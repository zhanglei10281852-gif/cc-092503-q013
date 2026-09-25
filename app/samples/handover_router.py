from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import transaction
from app.samples.extended_schemas import HandoverCreate, HandoverReceive, HandoverReject
from app.samples.handover import HandoverService

router = APIRouter(prefix="/api/sample-operations", tags=["样品交接"])


@router.post("/{sample_id}/handovers", status_code=status.HTTP_201_CREATED)
def initiate_handover(sample_id: int, payload: HandoverCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return HandoverService(connection).initiate(principal, sample_id, payload.model_dump())


@router.get("/handovers")
def list_handovers(
    state: str | None = Query(default=None),
    sample_id: int | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    with transaction(immediate=True) as connection:
        return HandoverService(connection).list(principal, state, sample_id)


@router.get("/handovers/{transfer_id}")
def get_handover(transfer_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return HandoverService(connection).detail(principal, transfer_id)


@router.post("/handovers/{transfer_id}/receipts", status_code=status.HTTP_201_CREATED)
def receive_handover(transfer_id: int, payload: HandoverReceive, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return HandoverService(connection).receive(principal, transfer_id, payload.model_dump())


@router.post("/handovers/{transfer_id}/cancel")
def cancel_handover(transfer_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return HandoverService(connection).cancel(principal, transfer_id)


@router.post("/handovers/{transfer_id}/reject")
def reject_handover(transfer_id: int, payload: HandoverReject, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return HandoverService(connection).reject(principal, transfer_id, payload.model_dump())


@router.post("/handovers/sweep-expired")
def sweep_expired_handovers(principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return HandoverService(connection).sweep_expired(principal)

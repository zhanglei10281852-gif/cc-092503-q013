from __future__ import annotations

from pydantic import BaseModel, Field


class InventoryStart(BaseModel):
    location_id: int = Field(gt=0)
    session_code: str | None = Field(default=None, min_length=3, max_length=64)


class InventoryCount(BaseModel):
    sample_id: int = Field(gt=0)
    observed_present: bool
    observed_quantity: float | None = Field(default=None, ge=0)
    note: str = Field(default="", max_length=500)


class CollectionCreate(BaseModel):
    field_code: str = Field(min_length=3, max_length=100)
    project_code: str = Field(min_length=2, max_length=64)
    collected_by: str = Field(min_length=1, max_length=100)
    collected_at: str = Field(min_length=10, max_length=40)
    source_kind: str = Field(min_length=1, max_length=100)
    source_reference: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=20)
    preservation: str = Field(min_length=1, max_length=200)


class TransferOrderCreate(BaseModel):
    transfer_code: str | None = Field(default=None, min_length=3, max_length=64)
    target_location_id: int = Field(gt=0)
    receiver_user_id: int | None = Field(default=None, gt=0)
    sample_ids: list[int] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=2, max_length=500)
    expires_in_hours: float | None = Field(default=None, gt=0, le=168)


class TransferConfirmItem(BaseModel):
    sample_id: int = Field(gt=0)
    received_quantity: float = Field(ge=0)
    seal_code: str = Field(min_length=1, max_length=100)
    seal_intact: bool = True
    target_location_id: int | None = Field(default=None, gt=0)


class TransferConfirm(BaseModel):
    items: list[TransferConfirmItem] = Field(min_length=1, max_length=200)


class TransferRejectItem(BaseModel):
    sample_id: int = Field(gt=0)
    reason: str = Field(min_length=2, max_length=500)


class TransferReject(BaseModel):
    items: list[TransferRejectItem] = Field(min_length=1, max_length=200)


class DestructionExecute(BaseModel):
    method: str = Field(min_length=2, max_length=200)
    witness_one: int = Field(gt=0)
    witness_two: int = Field(gt=0)

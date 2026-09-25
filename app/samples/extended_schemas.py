from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


class HandoverItem(BaseModel):
    item_seq: int = Field(gt=0, le=1000)
    expected_quantity: float = Field(gt=0)
    seal_code: str = Field(min_length=1, max_length=100)


class HandoverCreate(BaseModel):
    target_location_id: int = Field(gt=0)
    expected_version: int = Field(gt=0)
    reason: str = Field(min_length=2, max_length=500)
    seal_code: str | None = Field(default=None, min_length=1, max_length=100)
    items: list[HandoverItem] | None = Field(default=None, min_length=1, max_length=100)
    transfer_code: str | None = Field(default=None, min_length=3, max_length=64)
    expires_at: str | None = Field(default=None, min_length=10, max_length=40)

    @model_validator(mode="after")
    def ensure_manifest(self):
        if self.items is None and self.seal_code is None:
            raise ValueError("未提供明细清单时必须填写整件封签号 seal_code")
        if self.items is not None and self.seal_code is not None:
            raise ValueError("seal_code 与 items 只能填写一种")
        if self.items is not None:
            seqs = [item.item_seq for item in self.items]
            if len(set(seqs)) != len(seqs):
                raise ValueError("明细序号 item_seq 不能重复")
        return self


class HandoverReceive(BaseModel):
    item_seq: int = Field(gt=0)
    confirmed_quantity: float = Field(gt=0)
    seal_code: str = Field(min_length=1, max_length=100)
    target_location_id: int = Field(gt=0)
    receive_token: str = Field(min_length=4, max_length=100)
    note: str = Field(default="", max_length=500)


class HandoverReject(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    severity: Literal["low", "medium", "high", "critical"] = "medium"


class DestructionExecute(BaseModel):
    method: str = Field(min_length=2, max_length=200)
    witness_one: int = Field(gt=0)
    witness_two: int = Field(gt=0)

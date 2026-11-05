"""Pydantic schemas for the persistent emergency kill-switch subsystem."""

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KillSwitchStatus(BaseModel):
    """Current state of the emergency kill switch."""

    model_config = ConfigDict(extra="forbid")

    kill_switch_active: bool = Field(..., description="True if emergency stop is engaged.")
    engaged_at: datetime | None = Field(
        default=None, description="UTC timestamp when switch was engaged."
    )
    engaged_by: str | None = Field(
        default=None, description="Attribution text for engaging party."
    )
    reason: str | None = Field(
        default=None, description="Reason for engaging or disengaging the switch."
    )
    updated_at: datetime = Field(..., description="UTC timestamp of last control update.")

    @field_validator("engaged_at", "updated_at", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime | None) -> datetime | None:
        if v is not None:
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc)
        return None


class KillSwitchEngageRequest(BaseModel):
    """Payload to engage the emergency kill switch."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        ..., min_length=3, max_length=500, description="Detailed explanation for lockdown."
    )
    engaged_by: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Identity/role of the engaging administrator.",
    )


class KillSwitchDisengageRequest(BaseModel):
    """Payload to disengage the emergency kill switch."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        ...,
        min_length=3,
        max_length=500,
        description="Detailed explanation for releasing lockdown.",
    )
    disengaged_by: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Identity/role of the disengaging administrator.",
    )

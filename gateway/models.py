import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Weights(StrictModel):
    tps: float = Field(default=0.24, ge=0, le=1, allow_inf_nan=False)
    ttft: float = Field(default=0.08, ge=0, le=1, allow_inf_nan=False)
    duration: float = Field(default=0.24, ge=0, le=1, allow_inf_nan=False)
    success: float = Field(default=0.32, ge=0, le=1, allow_inf_nan=False)
    stake: float = Field(default=0.12, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def sum_to_one(self):
        if not math.isclose(sum(self.model_dump().values()), 1, abs_tol=1e-9):
            raise ValueError("Rating weights must add up to 1 (100%)")
        # Preserve the requested proportions. Only correct representation-level rounding.
        values = list(self.model_dump().values())
        for _ in range(8):
            total = sum(values)
            if total == 1:
                break
            i = max(range(len(values)), key=values.__getitem__)
            values[i] = math.nextafter(values[i], math.inf if total < 1 else -math.inf)
        if sum(values) != 1:
            raise ValueError("Rating weights could not be normalized")
        for name, value in zip(type(self).model_fields, values):
            setattr(self, name, value)
        return self


class Providers(StrictModel):
    mode: Literal["all", "allowlist"] = "all"
    allow: list[str] = Field(default_factory=list, max_length=1000)
    deny: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("allow", "deny")
    @classmethod
    def addresses(cls, values):
        if any(not ADDRESS.fullmatch(v) for v in values):
            raise ValueError("Provider addresses must be 0x followed by 40 hex characters")
        return sorted(set(v.lower() for v in values))

    def permits(self, provider: str) -> bool:
        value = provider.lower()
        return (
            bool(ADDRESS.fullmatch(value))
            and value not in self.deny
            and (self.mode == "all" or value in self.allow)
        )


class ModelPolicy(StrictModel):
    id: str
    alias: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")
    enabled: bool = True
    duration_seconds: int = Field(default=1800, ge=300, le=86400)
    retention: Literal["on_demand", "until_expiry", "maintain"] = "on_demand"
    idle_seconds: int = Field(default=300, ge=0, le=86400)
    max_sessions: int = Field(default=1, ge=1, le=8)
    warm_until: int | None = Field(default=None, gt=0)

    @field_validator("id")
    @classmethod
    def model_id(cls, value):
        if not HASH.fullmatch(value) or int(value, 16) == 0:
            raise ValueError("Use a nonzero on-chain model ID (0x + 64 hex characters)")
        return value.lower()


class RecoveryPolicy(StrictModel):
    enabled: bool = True
    auto_withdraw: bool = True
    cleanup_untracked_expired: bool = True
    cleanup_untracked_live: bool = False
    orphan_grace_seconds: int = Field(default=600, ge=120, le=86400)
    withdrawal_min_wei: str = Field(default="1000000000000000", pattern=r"^[0-9]{1,78}$")
    withdrawal_interval_seconds: int = Field(default=300, ge=30, le=86400)
    provider_cooldown_seconds: int = Field(default=120, ge=1, le=3600)


class Budget(StrictModel):
    max_session_stake_wei: str = Field(default="100000000000000000000", pattern=r"^[1-9][0-9]{0,77}$")
    max_total_stake_wei: str = Field(default="400000000000000000000", pattern=r"^[1-9][0-9]{0,77}$")
    max_price_per_second_wei: str = Field(default="1000000000000000000", pattern=r"^[1-9][0-9]{0,77}$")
    min_liquid_mor_wei: str = Field(default="0", pattern=r"^[0-9]{1,78}$")
    min_eth_wei: str = Field(default="100000000000000", pattern=r"^[0-9]{1,78}$")


class Policy(StrictModel):
    revision: int = Field(default=0, ge=0)
    models: list[ModelPolicy] = Field(default_factory=list, max_length=100)
    providers: Providers = Field(default_factory=Providers)
    weights: Weights = Field(default_factory=Weights)
    max_sessions: int = Field(default=4, ge=1, le=32)
    queue_seconds: int = Field(default=30, ge=1, le=300)
    paused: bool = False
    recovery: RecoveryPolicy = Field(default_factory=RecoveryPolicy)
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def unique_models(self):
        if len({m.id for m in self.models}) != len(self.models):
            raise ValueError("Model IDs must be unique")
        if len({m.alias.lower() for m in self.models}) != len(self.models):
            raise ValueError("Model aliases must be unique")
        if any(HASH.fullmatch(m.alias) for m in self.models):
            raise ValueError("An alias cannot itself be a blockchain ID")
        return self

    def resolve(self, name: str) -> ModelPolicy | None:
        return next((m for m in self.models if m.enabled and name.lower() in (m.id, m.alias.lower())), None)

    def rating(self) -> dict:
        # Strict empty allowlist is enforced in the gateway; the node's [] means all.
        return {
            "algorithm": "default",
            "providerAllowlist": self.providers.allow if self.providers.mode == "allowlist" else [],
            "providerDenylist": self.providers.deny,
            "params": {"weights": self.weights.model_dump()},
        }


class KeyCreate(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    models: list[str] = Field(default_factory=list, max_length=100)
    concurrency: int = Field(default=2, ge=1, le=32)
    requests_per_minute: int = Field(default=60, ge=1, le=10000)


class Restart(StrictModel):
    immediate: bool = False
    apply_rating: bool = False

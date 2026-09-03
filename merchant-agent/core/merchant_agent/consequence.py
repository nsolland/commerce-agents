# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""Optional consequence boundary for merchant writes.

The default adapter preserves the reference implementation's direct backend write.
``ValoConsequenceAdapter`` instead hands the complete effect to a VALO gateway. The
gateway resolves fresh authority and resource state, decides admissibility, commits
the effect, and returns its evidence receipt as one operation.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .backend import MerchantBackend
from .types import ChangeStatus, MerchantSessionContext, StagedChange

CONSEQUENCE_GATE = "consequence"


class ConsequenceDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ESCALATE = "ESCALATE"


class AuthorityReferences(BaseModel):
    """References VALO resolves at consequence time; never authority snapshots."""

    principal: str
    mandate: str
    purpose: str
    delegation: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)


class ApprovalEvidence(BaseModel):
    """Host-held evidence for the approval that selected this exact change."""

    approval_id: str
    approved_by: str
    approved_at: str
    change_digest: str


def staged_change_digest(change: StagedChange) -> str:
    """Stable SHA-256 binding for host approval of an exact staged change."""
    payload = json.dumps(
        change.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return sha256(payload).hexdigest()


class ConsequenceContext(BaseModel):
    authority: AuthorityReferences
    approval: ApprovalEvidence
    expected_resource_versions: dict[str, str] = Field(default_factory=dict)


class ConsequenceRequest(BaseModel):
    contract: str = "valo.merchant.consequence.v1"
    merchant_id: str
    session_id: str
    operator: str
    change: StagedChange
    context: ConsequenceContext


class ConsequenceReceipt(BaseModel):
    receipt_id: str
    decision: ConsequenceDecision
    decided_at: str
    reason_codes: list[str] = Field(default_factory=list)
    authority_state_digest: str | None = None
    resource_state_digest: str | None = None
    policy_state_digest: str | None = None
    effect_digest: str | None = None
    replay_reference: str | None = None


class ConsequenceResult(BaseModel):
    decision: ConsequenceDecision
    receipt: ConsequenceReceipt
    applied_change: StagedChange | None = None


class ConsequenceHeld(RuntimeError):
    """A governed effect that was denied or escalated."""

    def __init__(self, decision: ConsequenceDecision, receipt: ConsequenceReceipt):
        super().__init__(", ".join(receipt.reason_codes) or decision.value)
        self.decision = decision
        self.receipt = receipt


class ConsequenceGateway(Protocol):
    async def commit(self, request: ConsequenceRequest) -> ConsequenceResult:
        """Resolve, decide, commit, and receipt the effect atomically."""


ContextProvider = Callable[[MerchantSessionContext, StagedChange], Awaitable[ConsequenceContext]]


class MerchantConsequenceAdapter(Protocol):
    async def apply(
        self,
        *,
        backend: MerchantBackend,
        session: MerchantSessionContext,
        change: StagedChange,
    ) -> ConsequenceResult | StagedChange: ...


class DirectMerchantConsequenceAdapter:
    """Compatibility path: delegate the effect to the adopter's backend."""

    async def apply(
        self,
        *,
        backend: MerchantBackend,
        session: MerchantSessionContext,
        change: StagedChange,
    ) -> StagedChange:
        return await backend.apply_change(session, change.change_id)


class ValoConsequenceAdapter:
    """Replace the direct backend write with a VALO governed consequence."""

    def __init__(self, gateway: ConsequenceGateway, context_provider: ContextProvider):
        self._gateway = gateway
        self._context_provider = context_provider

    async def apply(
        self,
        *,
        backend: MerchantBackend,
        session: MerchantSessionContext,
        change: StagedChange,
    ) -> ConsequenceResult:
        del backend  # NO_DIRECT_EFFECT_PATH: VALO owns the live write.
        context = await self._context_provider(session, change)
        if not hmac.compare_digest(context.approval.change_digest, staged_change_digest(change)):
            raise ValueError("approval evidence does not bind the current staged change")
        request = ConsequenceRequest(
            merchant_id=session.merchant_id,
            session_id=session.session_id,
            operator=session.operator,
            change=change,
            context=context,
        )
        result = await self._gateway.commit(request)
        if result.receipt.decision is not result.decision:
            raise ValueError("VALO result and receipt decisions differ")
        if result.decision is not ConsequenceDecision.ALLOW:
            if result.applied_change is not None:
                raise ValueError("VALO returned an applied change for a non-ALLOW decision")
            raise ConsequenceHeld(result.decision, result.receipt)
        if result.applied_change is None:
            raise ValueError("VALO returned ALLOW without an applied change")
        if result.applied_change.change_id != change.change_id:
            raise ValueError("VALO returned an applied change for a different change id")
        if result.applied_change.status is not ChangeStatus.APPLIED:
            raise ValueError("VALO returned ALLOW without an applied status")
        if result.receipt.effect_digest is None:
            raise ValueError("VALO returned ALLOW without effect evidence")
        return result

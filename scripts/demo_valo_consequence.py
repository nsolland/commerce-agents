#!/usr/bin/env python3
# Copyright 2026 VALO Research
# SPDX-License-Identifier: Apache-2.0

"""Run the VALO merchant consequence boundary demonstrator."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256

from merchant_agent import (
    ActorKind,
    ApprovalEvidence,
    AuthorityReferences,
    ChangeItem,
    ChangeKind,
    ChangeStatus,
    ConsequenceContext,
    ConsequenceDecision,
    ConsequenceHeld,
    ConsequenceReceipt,
    ConsequenceResult,
    MerchantSessionContext,
    StagedChange,
    ValoConsequenceAdapter,
    staged_change_digest,
)


class DemoValoGateway:
    """Small in-process stand-in for the remote commit boundary, not a VALO kernel."""

    def __init__(self) -> None:
        self.authority_active = True
        self.resource_versions = {"listing-42": "v7"}
        self.effects: list[str] = []
        self.receipts = 0

    async def commit(self, request):
        reasons: list[str] = []
        if not self.authority_active:
            reasons.append("AUTHORITY_REVOKED")
        for resource, expected in request.context.expected_resource_versions.items():
            if self.resource_versions.get(resource) != expected:
                reasons.append("RESOURCE_STATE_CHANGED")

        decision = ConsequenceDecision.DENY if reasons else ConsequenceDecision.ALLOW
        self.receipts += 1
        receipt_id = f"valo-receipt-{self.receipts:04d}"
        applied = None
        effect_digest = None
        if decision is ConsequenceDecision.ALLOW:
            effect = f"{request.change.change_id}:price=110"
            self.effects.append(effect)
            self.resource_versions["listing-42"] = "v8"
            effect_digest = sha256(effect.encode()).hexdigest()
            applied = request.change.model_copy(
                update={
                    "status": ChangeStatus.APPLIED,
                    "applied_at": datetime.now(UTC),
                    "applied_by": request.operator,
                }
            )

        receipt = ConsequenceReceipt(
            receipt_id=receipt_id,
            decision=decision,
            decided_at=datetime.now(UTC).isoformat(),
            reason_codes=reasons,
            authority_state_digest=sha256(str(self.authority_active).encode()).hexdigest(),
            resource_state_digest=sha256(
                repr(sorted(self.resource_versions.items())).encode()
            ).hexdigest(),
            policy_state_digest=sha256(b"demo-policy-v1").hexdigest(),
            effect_digest=effect_digest,
            replay_reference=f"demo-replay:{receipt_id}",
        )
        return ConsequenceResult(
            decision=decision,
            receipt=receipt,
            applied_change=applied,
        )


def staged_change(change_id: str) -> StagedChange:
    return StagedChange(
        change_id=change_id,
        kind=ChangeKind.PRICE_UPDATE,
        summary="Change listing-42 price from 100 to 110",
        items=[ChangeItem(target="listing-42", field="price", before=100, after=110)],
        created_at=datetime.now(UTC),
        created_by="operator-7",
        created_by_kind=ActorKind.OPERATOR,
    )


def context_for(expected_version: str):
    async def build(session, change):
        return ConsequenceContext(
            authority=AuthorityReferences(
                principal=f"operator:{session.operator}",
                mandate="mandate:catalog-price-write",
                purpose="merchant-operations",
                constraints={"max_price_delta_pct": 15},
            ),
            approval=ApprovalEvidence(
                approval_id=f"approval:{change.change_id}",
                approved_by=session.operator,
                approved_at=datetime.now(UTC).isoformat(),
                change_digest=staged_change_digest(change),
            ),
            expected_resource_versions={"listing-42": expected_version},
        )

    return build


async def attempt(gateway, session, change_id, expected_version):
    adapter = ValoConsequenceAdapter(gateway, context_for(expected_version))
    try:
        result = await adapter.apply(
            backend=None,  # type: ignore[arg-type] -- VALO owns the effect path.
            session=session,
            change=staged_change(change_id),
        )
    except ConsequenceHeld as held:
        return held.decision, held.receipt
    return result.decision, result.receipt


async def main() -> None:
    session = MerchantSessionContext(
        session_id="demo-session", merchant_id="merchant-1", operator="operator-7"
    )
    gateway = DemoValoGateway()

    allowed, allowed_receipt = await attempt(gateway, session, "chg-allow", "v7")
    print(
        f"fresh authority + fresh reality -> {allowed.value} -> effect committed "
        f"-> {allowed_receipt.receipt_id}"
    )

    gateway.resource_versions["listing-42"] = "v9"
    stale, stale_receipt = await attempt(gateway, session, "chg-stale", "v8")
    print(
        f"fresh authority + changed reality -> {stale.value} -> no effect "
        f"({','.join(stale_receipt.reason_codes)}) -> {stale_receipt.receipt_id}"
    )

    gateway.authority_active = False
    revoked, revoked_receipt = await attempt(gateway, session, "chg-revoked", "v9")
    print(
        f"revoked authority + fresh reality -> {revoked.value} -> no effect "
        f"({','.join(revoked_receipt.reason_codes)}) -> {revoked_receipt.receipt_id}"
    )
    print(f"effects committed: {len(gateway.effects)}")


if __name__ == "__main__":
    asyncio.run(main())

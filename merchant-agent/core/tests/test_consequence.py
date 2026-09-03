# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

from datetime import UTC, datetime

from merchant_agent import (
    ApprovalEvidence,
    AuthorityReferences,
    ChangeStatus,
    ConsequenceContext,
    ConsequenceDecision,
    ConsequenceReceipt,
    ConsequenceResult,
    ValoConsequenceAdapter,
    staged_change_digest,
)
from merchant_agent.executor import MerchantToolExecutor


async def _context(session, change):
    return ConsequenceContext(
        authority=AuthorityReferences(
            principal=f"operator:{session.operator}",
            mandate=f"merchant:{session.merchant_id}:catalog-write",
            purpose="merchant-operations",
        ),
        approval=ApprovalEvidence(
            approval_id=f"approval:{change.change_id}",
            approved_by=session.operator,
            approved_at=datetime.now(UTC).isoformat(),
            change_digest=staged_change_digest(change),
        ),
        expected_resource_versions={item.target: "v1" for item in change.items},
    )


class Gateway:
    def __init__(self, decision=ConsequenceDecision.ALLOW):
        self.decision = decision
        self.requests = []

    async def commit(self, request):
        self.requests.append(request)
        receipt = ConsequenceReceipt(
            receipt_id=f"vr-{request.change.change_id}",
            decision=self.decision,
            decided_at=datetime.now(UTC).isoformat(),
            reason_codes=[] if self.decision is ConsequenceDecision.ALLOW else ["AUTHORITY_STALE"],
            authority_state_digest="authority-digest",
            resource_state_digest="resource-digest",
            effect_digest="effect-digest" if self.decision is ConsequenceDecision.ALLOW else None,
            replay_reference=f"replay:{request.change.change_id}",
        )
        applied = None
        if self.decision is ConsequenceDecision.ALLOW:
            applied = request.change.model_copy(
                update={
                    "status": ChangeStatus.APPLIED,
                    "applied_at": datetime.now(UTC),
                    "applied_by": request.operator,
                }
            )
        return ConsequenceResult(decision=self.decision, receipt=receipt, applied_change=applied)


async def _stage(executor, state):
    await executor.execute("search_listings", {"query": "planter"})
    await executor.execute(
        "stage_inventory_action",
        {"items": [{"listing_id": "L-202", "action": "restock", "quantity": 24}]},
    )
    return next(iter(state.seen_changes))


async def test_valo_owns_the_effect_and_emits_receipt(
    backend, config, skills, session, state, monkeypatch
):
    gateway = Gateway()
    adapter = ValoConsequenceAdapter(gateway, _context)
    executor = MerchantToolExecutor(
        backend=backend,
        config=config,
        skills=skills,
        session=session,
        state=state,
        consequence_adapter=adapter,
    )
    change_id = await _stage(executor, state)

    async def direct_write_must_not_run(*args, **kwargs):
        raise AssertionError("direct backend effect path was used")

    monkeypatch.setattr(backend, "apply_change", direct_write_must_not_run)
    outcome = await executor.execute("apply_change", {"change_id": change_id})

    assert not outcome.refused
    assert gateway.requests[0].change.change_id == change_id
    assert state.seen_changes[change_id].status is ChangeStatus.APPLIED
    assert [event.type for event in outcome.events] == [
        "change_update",
        "governance_receipt",
    ]
    assert outcome.events[1].data["receipt_id"] == f"vr-{change_id}"


async def test_valo_deny_fails_closed_without_backend_effect(
    backend, config, skills, session, state, monkeypatch
):
    gateway = Gateway(ConsequenceDecision.DENY)
    executor = MerchantToolExecutor(
        backend=backend,
        config=config,
        skills=skills,
        session=session,
        state=state,
        consequence_adapter=ValoConsequenceAdapter(gateway, _context),
    )
    change_id = await _stage(executor, state)
    called = False

    async def direct_write_must_not_run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(backend, "apply_change", direct_write_must_not_run)
    outcome = await executor.execute("apply_change", {"change_id": change_id})

    assert outcome.blocked == "consequence"
    assert "AUTHORITY_STALE" in outcome.result_text
    assert f"vr-{change_id}" in outcome.result_text
    assert outcome.events[0].type == "governance_receipt"
    assert outcome.events[0].data["decision"] == "DENY"
    assert not called
    assert state.seen_changes[change_id].status is ChangeStatus.STAGED


async def test_valo_escalate_fails_closed(backend, config, skills, session, state):
    gateway = Gateway(ConsequenceDecision.ESCALATE)
    executor = MerchantToolExecutor(
        backend=backend,
        config=config,
        skills=skills,
        session=session,
        state=state,
        consequence_adapter=ValoConsequenceAdapter(gateway, _context),
    )
    change_id = await _stage(executor, state)
    outcome = await executor.execute("apply_change", {"change_id": change_id})
    assert outcome.blocked == "consequence"
    assert "ESCALATE" in outcome.result_text

# VALO consequence adapter

The merchant agent already fences external data, stages writes, checks provenance and
guardrails, and requires host approval. The VALO adapter replaces only the final live
write:

```text
stage → preview → approval → VALO consequence commit → receipt
```

`ValoConsequenceAdapter` sends the staged change, authority references, approval
evidence, and expected resource versions to a `ConsequenceGateway`. The gateway owns
fresh authority resolution, fresh resource-state validation, the RACS admissibility
decision, the platform effect, and the durable receipt as one commit operation.

The adapter deliberately does not call `MerchantBackend.apply_change`. This preserves
`NO_DIRECT_EFFECT_PATH`; a deployment must register its live effect handler behind the
VALO gateway. `DENY`, `ESCALATE`, malformed results, and gateway failures leave the
change staged. Both allowed and held decisions emit a structured receipt event.

```python
from merchant_agent import ValoConsequenceAdapter

adapter = ValoConsequenceAdapter(
    gateway=valo_gateway,
    context_provider=build_consequence_context,
)

executor = MerchantToolExecutor(
    backend=merchant_backend,
    config=config,
    skills=skills,
    session=session,
    state=state,
    consequence_adapter=adapter,
)
```

`build_consequence_context(session, change)` returns `ConsequenceContext`. Its authority
fields are references, not cached assertions: VALO resolves their current state at the
effect boundary. Approval evidence binds the approver to the exact staged-change digest.

An allowed result carries the applied `StagedChange` and a `ConsequenceReceipt`. The
executor emits both `change_update` and `governance_receipt` host events. A held result
names the VALO decision, reason codes, and receipt id without exposing credentials or
policy internals to the model.

Run the local demonstrator:

[Open the live interactive demo](https://valo-consequence-demo.njaal-solland.chatgpt.site)

```bash
python scripts/demo_valo_consequence.py
```

It executes one allowed consequence, then proves that changed resource state and
revoked authority each fail closed without adding another effect. The in-process demo
gateway exists only to make the boundary observable; it is not a VALO kernel
implementation.

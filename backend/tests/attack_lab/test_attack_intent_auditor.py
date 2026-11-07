"""Adversarial Intent Auditor responses, snapshot bindings, and TOCTOU checks."""

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from app.core.config import settings
from app.core.security import proposal_content_hash, proposal_fingerprint, sign_payload
from app.db.database import get_connection
from app.schemas.intent_audit import IntentAuditorResult
from app.schemas.mandate import Mandate
from app.schemas.proposal import Proposal
from app.schemas.policy import DecisionOutcome
from app.services.intent_audit_service import require_current_assessment
from app.services.intent_auditor_client import (
    IntentAuditorClient,
    IntentAuditorUnavailable,
)
from app.services.approval_service import (
    consume_approval_token,
    issue_approval_token,
)
from app.services.mandate_service import (
    get_mandate,
    revoke_mandate,
)
from app.services.payment_service import create_payment_order
from app.services.proposal_service import evaluate_proposal_service, get_proposal
from tests.attack_lab.support import AttackCase, AsyncBarrier, ScriptedHTTPX


class _PayPalCallCounter:
    def __init__(self) -> None:
        self.create_calls = 0

    async def create_order(self, **kwargs: Any) -> dict[str, str]:
        self.create_calls += 1
        return {
            "order_id": "PAYPAL-STALE-ASSESSMENT",
            "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=synthetic",
            "status": "CREATED",
        }


def _response(risk: str = "LOW") -> httpx.Response:
    return httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{"text": json.dumps({
            "schema_version": "1.0",
            "risk_level": risk,
            "finding_codes": [],
            "affected_fields": [],
        })}]}}]},
    )


async def _forward_to_script(
    client: IntentAuditorClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted_assess = client.assess

    async def assess_with_script(
        self: IntentAuditorClient, payload: Any
    ) -> IntentAuditorResult:
        return await scripted_assess(payload)

    monkeypatch.setattr(IntentAuditorClient, "assess", assess_with_script)


async def _automatic_case(
    owner_user_id: str,
    attack_factories: dict[str, Any],
    *,
    product_id: str,
) -> tuple[Mandate, Proposal]:
    """Build a valid owned proposal whose policy permits automatic LOW-risk approval."""
    mandate = await attack_factories["mandate"](
        owner_user_id, require_user_approval=False
    )
    proposal = await attack_factories["proposal"](
        mandate.mandate_id, product_id=product_id
    )
    return mandate, proposal


async def _tamper_proposal_content(proposal_id: str) -> Proposal:
    """Change a persisted proposal while keeping its new DB hashes internally valid."""
    original = await get_proposal(proposal_id)
    changed = original.model_copy(update={
        "product_name": f"{original.product_name} changed after assessment",
        "fingerprint": "",
        "content_hash": "",
    })
    changed.fingerprint = proposal_fingerprint(changed.model_dump())
    changed.content_hash = proposal_content_hash(changed.model_dump())
    async with get_connection() as db:
        await db.execute(
            "UPDATE proposals SET product_name = ?, fingerprint = ?, content_hash = ? "
            "WHERE proposal_id = ?",
            (changed.product_name, changed.fingerprint, changed.content_hash, proposal_id),
        )
        await db.commit()
    return await get_proposal(proposal_id)


async def _tamper_mandate_permissions(mandate_id: str) -> Mandate:
    """Change a permission and re-sign it to isolate auditor-binding checks."""
    original = await get_mandate(mandate_id)
    changed = original.model_copy(update={"max_total_budget": Decimal("101.00")})
    changed.signature = sign_payload(
        changed.canonical_payload(), settings.MANDATE_SIGNING_KEY
    )
    async with get_connection() as db:
        await db.execute(
            "UPDATE mandates SET max_total_budget = ?, signature = ? "
            "WHERE mandate_id = ?",
            (str(changed.max_total_budget), changed.signature, mandate_id),
        )
        await db.commit()
    return await get_mandate(mandate_id)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(200, json={"candidates": [{"content": {"parts": [{
            "text": '{"schema_version":"1.0","risk_level":"LOW","approved":true}',
        }]}}]}),
        httpx.Response(503, text="synthetic provider failure"),
        httpx.ReadTimeout("synthetic provider timeout"),
    ],
    ids=("malformed-output", "provider-error", "timeout"),
)
async def test_provider_failure_or_malformed_result_is_persisted_fail_closed(
    failure: httpx.Response | Exception,
    attack_case: AttackCase,
    attack_intent_client: tuple[IntentAuditorClient, ScriptedHTTPX],
    verify_attack_audit_chain: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, script = attack_intent_client
    script.queue_response(failure)
    await _forward_to_script(client, monkeypatch)

    with pytest.raises(IntentAuditorUnavailable):
        await evaluate_proposal_service(attack_case.proposal_a.proposal_id)

    proposal = await get_proposal(attack_case.proposal_a.proposal_id)
    assert proposal.status.value == "EVALUATED"
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT status, risk_level, failure_code FROM intent_assessments "
            "WHERE proposal_id = ? ORDER BY rowid DESC LIMIT 1",
            (proposal.proposal_id,),
        )
        assessment = await cursor.fetchone()
    assert assessment[0] == "FAILED"
    assert assessment[1] == "UNCERTAIN"
    assert assessment[2] in {"AUDITOR_UNAVAILABLE", "AUDITOR_FAILED"}
    assert script.request_count == 1
    assert script.requested_tools == [()]
    assert script.model_tool_calls == [()]
    assert (await verify_attack_audit_chain()).valid is True


async def test_changed_proposal_content_invalidates_assessment(
    attack_case: AttackCase,
    attack_intent_client: tuple[IntentAuditorClient, ScriptedHTTPX],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, script = attack_intent_client
    script.queue_response(_response())
    await _forward_to_script(client, monkeypatch)
    await evaluate_proposal_service(attack_case.proposal_a.proposal_id)

    changed = attack_case.proposal_a.model_copy(
        update={"product_name": "Different synthetic product"}
    )
    changed.content_hash = proposal_content_hash(changed.model_dump())
    with pytest.raises(IntentAuditorUnavailable):
        await require_current_assessment(changed, attack_case.mandate_a)
    assert script.request_count == 1


async def test_changed_mandate_permissions_invalidate_assessment(
    attack_case: AttackCase,
    attack_intent_client: tuple[IntentAuditorClient, ScriptedHTTPX],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, script = attack_intent_client
    script.queue_response(_response())
    await _forward_to_script(client, monkeypatch)
    await evaluate_proposal_service(attack_case.proposal_a.proposal_id)

    changed = attack_case.mandate_a.model_copy(
        update={"max_total_budget": Decimal("101.00")}
    )
    changed.signature = sign_payload(
        changed.canonical_payload(), settings.MANDATE_SIGNING_KEY
    )
    with pytest.raises(IntentAuditorUnavailable):
        await require_current_assessment(attack_case.proposal_a, changed)
    assert script.request_count == 1


async def test_policy_evidence_change_invalidates_assessment(
    attack_case: AttackCase,
    attack_intent_client: tuple[IntentAuditorClient, ScriptedHTTPX],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, script = attack_intent_client
    script.queue_response(_response())
    await _forward_to_script(client, monkeypatch)
    proposal = attack_case.proposal_a

    await evaluate_proposal_service(
        proposal.proposal_id, daily_limit=Decimal("35.00")
    )
    with pytest.raises(IntentAuditorUnavailable):
        await require_current_assessment(
            await get_proposal(proposal.proposal_id),
            attack_case.mandate_a,
            daily_limit=Decimal("36.00"),
        )
    assert script.request_count == 1


@pytest.mark.parametrize("binding", ["proposal", "mandate", "policy_evidence"])
async def test_stale_binding_blocks_approval_token_issuance(
    binding: str,
    attack_case: AttackCase,
    attack_intent_client: tuple[IntentAuditorClient, ScriptedHTTPX],
    verify_attack_audit_chain: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, script = attack_intent_client
    script.queue_response(_response())
    await _forward_to_script(client, monkeypatch)
    proposal = attack_case.proposal_a
    await evaluate_proposal_service(
        proposal.proposal_id,
        daily_limit=(Decimal("35.00") if binding == "policy_evidence" else None),
    )

    if binding == "proposal":
        await _tamper_proposal_content(proposal.proposal_id)
    elif binding == "mandate":
        await _tamper_mandate_permissions(attack_case.mandate_a.mandate_id)

    with pytest.raises(IntentAuditorUnavailable):
        await issue_approval_token(proposal.proposal_id, attack_case.user_a.user_id)

    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM approval_tokens WHERE proposal_id = ?",
            (proposal.proposal_id,),
        )
        assert (await cursor.fetchone())[0] == 0
    assert (await get_proposal(proposal.proposal_id)).status.value == "EVALUATED"
    assert (await verify_attack_audit_chain()).valid is True
    assert script.request_count == 1


@pytest.mark.parametrize("binding", ["proposal", "mandate"])
async def test_stale_binding_blocks_approval_token_consumption(
    binding: str,
    attack_case: AttackCase,
    verify_attack_audit_chain: Any,
) -> None:
    proposal_id = attack_case.proposal_a.proposal_id
    await evaluate_proposal_service(proposal_id)
    issued = await issue_approval_token(proposal_id, attack_case.user_a.user_id)
    if binding == "proposal":
        await _tamper_proposal_content(proposal_id)
    else:
        await _tamper_mandate_permissions(attack_case.mandate_a.mandate_id)

    success, reason = await consume_approval_token(
        proposal_id=proposal_id,
        token=issued["token"],
        principal_id=attack_case.user_a.user_id,
        issued_at_iso=issued["issued_at"],
        nonce=issued["nonce"],
    )
    assert success is False
    assert "assessment" in reason.lower()
    assert (await get_proposal(proposal_id)).status.value == "EVALUATED"
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT consumed_at FROM approval_tokens WHERE proposal_id = ?",
            (proposal_id,),
        )
        token_rows = await cursor.fetchall()
    assert len(token_rows) == 1 and token_rows[0][0] is None
    assert (await verify_attack_audit_chain()).valid is True


@pytest.mark.parametrize("binding", ["proposal", "mandate", "policy_evidence"])
async def test_stale_binding_blocks_payment_order_creation(
    binding: str,
    attack_case: AttackCase,
    attack_factories: dict[str, Any],
    attack_intent_client: tuple[IntentAuditorClient, ScriptedHTTPX],
    verify_attack_audit_chain: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mandate, proposal = await _automatic_case(
        attack_case.user_a.user_id,
        attack_factories,
        product_id=f"stale-{binding.replace('_', '-')}-payment",
    )
    client, script = attack_intent_client
    script.queue_response(_response())
    await _forward_to_script(client, monkeypatch)
    await evaluate_proposal_service(
        proposal.proposal_id,
        daily_limit=(Decimal("35.00") if binding == "policy_evidence" else None),
    )
    assert (await get_proposal(proposal.proposal_id)).status.value == "APPROVED"

    if binding == "proposal":
        await _tamper_proposal_content(proposal.proposal_id)
    elif binding == "mandate":
        await _tamper_mandate_permissions(mandate.mandate_id)

    with pytest.raises(IntentAuditorUnavailable):
        await issue_approval_token(proposal.proposal_id, attack_case.user_a.user_id)
    paypal = _PayPalCallCounter()
    with pytest.raises(IntentAuditorUnavailable):
        await create_payment_order(
            proposal.proposal_id,
            attack_case.user_a.user_id,
            paypal_client=paypal,  # type: ignore[arg-type]
        )

    assert paypal.create_calls == 0
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM paypal_orders WHERE proposal_id = ?",
            (proposal.proposal_id,),
        )
        assert (await cursor.fetchone())[0] == 0
        cursor = await db.execute(
            "SELECT COUNT(*) FROM approval_tokens WHERE proposal_id = ?",
            (proposal.proposal_id,),
        )
        assert (await cursor.fetchone())[0] == 0
    assert (await verify_attack_audit_chain()).valid is True
    assert script.request_count == 1


async def test_revoke_during_auditor_call_wins_over_low_risk_response(
    attack_case: AttackCase,
    attack_intent_client: tuple[IntentAuditorClient, ScriptedHTTPX],
    attack_barrier: type[AsyncBarrier],
    verify_attack_audit_chain: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, script = attack_intent_client
    script.queue_response(_response("LOW"))
    entered = attack_barrier(2)
    released = attack_barrier(2)
    scripted_assess = client.assess

    async def synchronized_assess(
        self: IntentAuditorClient, payload: Any
    ) -> IntentAuditorResult:
        await entered.wait()
        await released.wait()
        return await scripted_assess(payload)

    monkeypatch.setattr(IntentAuditorClient, "assess", synchronized_assess)
    task = asyncio.create_task(
        evaluate_proposal_service(attack_case.proposal_a.proposal_id)
    )
    await entered.wait()
    await revoke_mandate(attack_case.mandate_a.mandate_id)
    await released.wait()

    decision = await task
    assert decision.decision == DecisionOutcome.BLOCK
    assert (await get_proposal(attack_case.proposal_a.proposal_id)).status.value == "REJECTED"
    assert script.request_count == 1
    assert script.model_tool_calls == [()]
    assert (await verify_attack_audit_chain()).valid is True


@pytest.mark.parametrize("binding", ["proposal", "mandate", "policy_evidence"])
async def test_stale_binding_blocks_token_issuance(
    binding: str,
    attack_case: AttackCase,
    attack_intent_client: tuple[IntentAuditorClient, ScriptedHTTPX],
    verify_attack_audit_chain: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, script = attack_intent_client
    script.queue_response(_response())
    await _forward_to_script(client, monkeypatch)
    proposal = attack_case.proposal_a
    await evaluate_proposal_service(
        proposal.proposal_id,
        daily_limit=(Decimal("35.00") if binding == "policy_evidence" else None),
    )

    if binding == "proposal":
        await _tamper_proposal_content(proposal.proposal_id)
    elif binding == "mandate":
        await _tamper_mandate_permissions(attack_case.mandate_a.mandate_id)

    with pytest.raises(IntentAuditorUnavailable):
        await issue_approval_token(proposal.proposal_id, attack_case.user_a.user_id)

    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM approval_tokens WHERE proposal_id = ?",
            (proposal.proposal_id,),
        )
        assert (await cursor.fetchone())[0] == 0
    assert (await get_proposal(proposal.proposal_id)).status.value == "EVALUATED"
    assert (await verify_attack_audit_chain()).valid is True
    assert script.request_count == 1


@pytest.mark.parametrize("binding", ["proposal", "mandate"])
async def test_stale_binding_blocks_token_consumption(
    binding: str,
    attack_case: AttackCase,
    verify_attack_audit_chain: Any,
) -> None:
    proposal_id = attack_case.proposal_a.proposal_id
    await evaluate_proposal_service(proposal_id)
    issued = await issue_approval_token(proposal_id, attack_case.user_a.user_id)
    if binding == "proposal":
        await _tamper_proposal_content(proposal_id)
    else:
        await _tamper_mandate_permissions(attack_case.mandate_a.mandate_id)

    success, reason = await consume_approval_token(
        proposal_id=proposal_id,
        token=issued["token"],
        principal_id=attack_case.user_a.user_id,
        issued_at_iso=issued["issued_at"],
        nonce=issued["nonce"],
    )
    assert success is False
    assert "assessment" in reason.lower()
    assert (await get_proposal(proposal_id)).status.value == "EVALUATED"
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT consumed_at FROM approval_tokens WHERE proposal_id = ?",
            (proposal_id,),
        )
        token_rows = await cursor.fetchall()
    assert len(token_rows) == 1 and token_rows[0][0] is None
    assert (await verify_attack_audit_chain()).valid is True


@pytest.mark.parametrize("binding", ["proposal", "mandate", "policy_evidence"])
async def test_stale_binding_blocks_payment_order_creation(
    binding: str,
    attack_case: AttackCase,
    attack_factories: dict[str, Any],
    attack_intent_client: tuple[IntentAuditorClient, ScriptedHTTPX],
    verify_attack_audit_chain: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mandate, proposal = await _automatic_case(
        attack_case.user_a.user_id,
        attack_factories,
        product_id=f"stale-{binding.replace('_', '-')}-payment",
    )
    client, script = attack_intent_client
    script.queue_response(_response())
    await _forward_to_script(client, monkeypatch)
    await evaluate_proposal_service(
        proposal.proposal_id,
        daily_limit=(Decimal("35.00") if binding == "policy_evidence" else None),
    )
    assert (await get_proposal(proposal.proposal_id)).status.value == "APPROVED"

    if binding == "proposal":
        await _tamper_proposal_content(proposal.proposal_id)
    elif binding == "mandate":
        await _tamper_mandate_permissions(mandate.mandate_id)

    with pytest.raises(IntentAuditorUnavailable):
        await issue_approval_token(proposal.proposal_id, attack_case.user_a.user_id)
    paypal = _PayPalCallCounter()
    with pytest.raises(IntentAuditorUnavailable):
        await create_payment_order(
            proposal.proposal_id,
            attack_case.user_a.user_id,
            paypal_client=paypal,  # type: ignore[arg-type]
        )

    assert paypal.create_calls == 0
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM paypal_orders WHERE proposal_id = ?",
            (proposal.proposal_id,),
        )
        assert (await cursor.fetchone())[0] == 0
        cursor = await db.execute(
            "SELECT COUNT(*) FROM approval_tokens WHERE proposal_id = ?",
            (proposal.proposal_id,),
        )
        assert (await cursor.fetchone())[0] == 0
    assert (await verify_attack_audit_chain()).valid is True
    assert script.request_count == 1


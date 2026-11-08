"""Adversarial approval and review-lifecycle scenarios."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.db.database import get_connection
from app.schemas.payment import PayPalOrderStatus
from app.schemas.proposal import ProposalStatus
from app.services.approval_service import (
    consume_approval_token,
    issue_approval_token,
)
from app.services.intent_auditor_client import IntentAuditorClient
from app.services.payment_service import (
    PaymentProposalNotApprovedError,
    create_payment_order,
)
from app.services.mandate_service import revoke_mandate
from app.services.proposal_service import evaluate_proposal_service, get_proposal
from tests.attack_lab.support import AttackCase, AsyncBarrier, ScriptedHTTPX


def _risk_response(risk: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{"text": json.dumps({
            "schema_version": "1.0",
            "risk_level": risk,
            "finding_codes": [],
            "affected_fields": [],
        })}]}}]},
    )


class _PayPalCallCounter:
    def __init__(self, order_id: str) -> None:
        self.create_calls = 0
        self.order_id = order_id

    async def create_order(self, **kwargs: Any) -> dict[str, str]:
        self.create_calls += 1
        return {
            "order_id": self.order_id,
            "approval_url": (
                "https://www.sandbox.paypal.com/checkoutnow?token=" + self.order_id
            ),
            "status": "CREATED",
        }


@pytest.mark.parametrize("risk", ["LOW", "MEDIUM", "HIGH"])
async def test_auditor_risk_never_approves_without_explicit_token(
    risk: str,
    attack_case: AttackCase,
    attack_factories: dict[str, Any],
    attack_intent_client: tuple[IntentAuditorClient, ScriptedHTTPX],
    attack_barrier: type[AsyncBarrier],
    verify_attack_audit_chain: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mandate = await attack_factories["mandate"](
        attack_case.user_a.user_id, require_user_approval=False
    )
    proposal = await attack_factories["proposal"](
        mandate.mandate_id, product_id=f"risk-{risk.lower()}-item"
    )
    client, script = attack_intent_client
    script.queue_response(_risk_response(risk))
    scripted_assess = client.assess

    async def use_scripted_assessor(
        self: IntentAuditorClient, payload: Any
    ) -> Any:
        return await scripted_assess(payload)

    monkeypatch.setattr(IntentAuditorClient, "assess", use_scripted_assessor)
    decision = await evaluate_proposal_service(proposal.proposal_id)
    assert decision.decision.value == "ALLOW"
    reviewed_proposal = await get_proposal(proposal.proposal_id)
    assert script.request_count == 1
    assert script.requested_tools == [()]
    assert script.model_tool_calls == [()]
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT risk_level FROM intent_assessments WHERE proposal_id = ?",
            (proposal.proposal_id,),
        )
        assert (await cursor.fetchone())[0] == risk

    paypal = _PayPalCallCounter(order_id=f"PAYPAL-MOCK-{risk}")
    assert reviewed_proposal.status == ProposalStatus.EVALUATED
    with pytest.raises(PaymentProposalNotApprovedError):
        await create_payment_order(
            proposal.proposal_id,
            attack_case.user_a.user_id,
            paypal_client=paypal,  # type: ignore[arg-type]
        )
    assert paypal.create_calls == 0

    issued = await issue_approval_token(
        proposal.proposal_id, attack_case.user_a.user_id
    )
    rendezvous = attack_barrier(2)

    async def consume_once() -> tuple[bool, str]:
        await rendezvous.wait()
        return await consume_approval_token(
            proposal_id=proposal.proposal_id,
            token=issued["token"],
            principal_id=attack_case.user_a.user_id,
            issued_at_iso=issued["issued_at"],
            nonce=issued["nonce"],
        )

    results = await asyncio.gather(consume_once(), consume_once())
    assert sum(success for success, _ in results) == 1
    approved_proposal = await get_proposal(proposal.proposal_id)
    assert approved_proposal.status == ProposalStatus.APPROVED
    order = await create_payment_order(
        proposal.proposal_id,
        attack_case.user_a.user_id,
        paypal_client=paypal,  # type: ignore[arg-type]
    )
    assert order.status == PayPalOrderStatus.CREATED
    assert paypal.create_calls == 1
    assert (await verify_attack_audit_chain()).valid is True


async def test_mandate_revocation_between_token_issue_and_use_fails_closed(
    attack_case: AttackCase,
    verify_attack_audit_chain: Any,
) -> None:
    proposal_id = attack_case.proposal_a.proposal_id
    await evaluate_proposal_service(proposal_id)
    issued = await issue_approval_token(proposal_id, attack_case.user_a.user_id)

    await revoke_mandate(attack_case.mandate_a.mandate_id)
    success, reason = await consume_approval_token(
        proposal_id=proposal_id,
        token=issued["token"],
        principal_id=attack_case.user_a.user_id,
        issued_at_iso=issued["issued_at"],
        nonce=issued["nonce"],
    )

    assert success is False
    assert "assessment" in reason.lower()
    assert (await get_proposal(proposal_id)).status == ProposalStatus.EVALUATED
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT consumed_at FROM approval_tokens WHERE proposal_id = ?",
            (proposal_id,),
        )
        token_rows = await cursor.fetchall()
    assert token_rows and all(row[0] is None for row in token_rows)
    assert (await verify_attack_audit_chain()).valid is True


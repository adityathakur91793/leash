"""Payment execution races against stale pre-transaction order state."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import pytest

from app.db.database import get_paypal_order_from_db
from app.schemas.payment import PayPalOrderStatus
from app.schemas.proposal import ProposalStatus
from app.services.audit_service import AuditService
from app.services.approval_service import consume_approval_token, issue_approval_token
from app.services.paypal_service import PayPalCaptureError, PayPalNetworkError
from app.services.payment_service import (
    capture_payment_order,
    create_payment_order,
)
from app.services.proposal_service import evaluate_proposal_service, get_proposal
from tests.attack_lab.support import AttackPrincipal


@pytest.mark.asyncio
async def test_concurrent_capture_rereads_order_after_ambiguous_first_attempt(
    attack_database: Any,
    attack_factories: dict[str, Any],
    attack_users: tuple[AttackPrincipal, AttackPrincipal],
    verify_attack_audit_chain: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A serialized second caller must see the first caller's uncertain state."""
    from app.services import payment_service as service

    owner = attack_users[0]
    mandate = await attack_factories["mandate"](
        owner.user_id, require_user_approval=False
    )
    proposal = await attack_factories["proposal"](mandate.mandate_id)
    decision = await evaluate_proposal_service(proposal.proposal_id)
    assert decision.decision.value == "ALLOW"
    assert (await get_proposal(proposal.proposal_id)).status == ProposalStatus.EVALUATED
    issued = await issue_approval_token(proposal.proposal_id, owner.user_id)
    approved, _ = await consume_approval_token(
        proposal_id=proposal.proposal_id,
        token=issued["token"],
        principal_id=owner.user_id,
        issued_at_iso=issued["issued_at"],
        nonce=issued["nonce"],
    )
    assert approved is True

    class OrderCreator:
        async def create_order(self, **kwargs: Any) -> dict[str, str]:
            return {
                "order_id": "PAYPAL-RACE-ORDER",
                "approval_url": "https://www.sandbox.paypal.com/checkoutnow?token=PAYPAL-RACE-ORDER",
                "status": "CREATED",
            }

    await create_payment_order(
        proposal.proposal_id,
        owner.user_id,
        paypal_client=OrderCreator(),  # type: ignore[arg-type]
    )

    both_initial_reads = asyncio.Event()
    both_paypal_approvals = asyncio.Event()
    request_a_committed = asyncio.Event()
    initial_order_states: list[PayPalOrderStatus] = []
    approval_responses = 0
    capture_attempts: list[str] = []
    original_read_order = service.get_paypal_order_from_db
    original_get_connection = service.get_connection

    async def read_order(order_id: str, db: Any = None) -> Any:
        record = await original_read_order(order_id, db=db)
        task = asyncio.current_task()
        if db is None and task and task.get_name() in {"capture-A", "capture-B"}:
            initial_order_states.append(record.status)
            if len(initial_order_states) == 2:
                both_initial_reads.set()
            await both_initial_reads.wait()
        return record

    monkeypatch.setattr(service, "get_paypal_order_from_db", read_order)

    class PayPal:
        async def get_order(self, order_id: str) -> dict[str, Any]:
            nonlocal approval_responses
            approval_responses += 1
            if approval_responses <= 2:
                if approval_responses == 2:
                    both_paypal_approvals.set()
                await both_paypal_approvals.wait()
            return {
                "status": "APPROVED",
                "purchase_units": [{
                    "amount": {"currency_code": "USD", "value": "16.00"}
                }],
            }

        async def capture_order(
            self, order_id: str, capture_request_id: str
        ) -> dict[str, Any]:
            capture_attempts.append(asyncio.current_task().get_name())
            raise PayPalNetworkError("simulated ambiguous capture timeout")

    class CommitSignalConnection:
        def __init__(self, connection: Any, is_request_a: bool) -> None:
            self._connection = connection
            self._is_request_a = is_request_a

        def __getattr__(self, name: str) -> Any:
            return getattr(self._connection, name)

        def __setattr__(self, name: str, value: Any) -> None:
            if name in {"_connection", "_is_request_a"}:
                object.__setattr__(self, name, value)
            else:
                setattr(self._connection, name, value)

        async def commit(self) -> None:
            await self._connection.commit()
            if self._is_request_a:
                request_a_committed.set()

    @asynccontextmanager
    async def ordered_connection(*args: Any, **kwargs: Any):
        task = asyncio.current_task()
        is_request_a = bool(task and task.get_name() == "capture-A")
        if task and task.get_name() == "capture-B":
            await request_a_committed.wait()
        async with original_get_connection(*args, **kwargs) as connection:
            yield CommitSignalConnection(connection, is_request_a)

    monkeypatch.setattr(service, "get_connection", ordered_connection)
    paypal = PayPal()

    async def capture(name: str) -> BaseException | Any:
        task = asyncio.create_task(
            capture_payment_order(
                "PAYPAL-RACE-ORDER", owner.user_id, paypal_client=paypal  # type: ignore[arg-type]
            ),
            name=name,
        )
        try:
            return await task
        except BaseException as exc:
            return exc

    result_a, result_b = await asyncio.gather(capture("capture-A"), capture("capture-B"))

    assert initial_order_states == [
        PayPalOrderStatus.CREATED,
        PayPalOrderStatus.CREATED,
    ]
    assert approval_responses >= 2
    assert isinstance(result_a, PayPalCaptureError)
    assert "requires reconciliation" in str(result_a)
    assert isinstance(result_b, PayPalCaptureError)
    assert "explicit reconciliation" in str(result_b)
    assert capture_attempts == ["capture-A"]

    final_order = await get_paypal_order_from_db("PAYPAL-RACE-ORDER")
    final_proposal = await get_proposal(proposal.proposal_id)
    assert final_order is not None
    assert final_order.status == PayPalOrderStatus.RECONCILIATION_REQUIRED
    assert final_order.capture_id is None
    assert final_proposal.status == ProposalStatus.APPROVED

    from app.db.database import get_connection

    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT event_type FROM audit_logs WHERE entity_id = ? ORDER BY sequence_number",
            ("PAYPAL-RACE-ORDER",),
        )
        payment_events = [row[0] for row in await cursor.fetchall()]
    assert payment_events.count("PAYMENT_FAILED") == 1
    assert "PAYMENT_CAPTURED" not in payment_events
    assert (await verify_attack_audit_chain()).valid is True

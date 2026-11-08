"""Focused payment executor invariants."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import asyncio

import pytest

from app.services.payment_service import _confirmed_capture
from app.schemas.mandate import MandateStatus
from app.schemas.payment import PayPalOrderStatus
from app.schemas.proposal import ProposalStatus
from app.services.paypal_service import PayPalCaptureError, PayPalNetworkError


def _capture_response(
    *, status: str = "COMPLETED", capture_status: str = "COMPLETED",
    amount: str = "16.00", currency: str = "USD", capture_id: str = "CAP-1",
) -> dict[str, object]:
    return {
        "status": status,
        "purchase_units": [{
            "payments": {"captures": [{
                "id": capture_id,
                "status": capture_status,
                "amount": {"value": amount, "currency_code": currency},
            }]},
        }],
    }


def test_confirmed_capture_requires_matching_successful_capture() -> None:
    capture_id, _ = _confirmed_capture(_capture_response(), "USD", Decimal("16.00"))
    assert capture_id == "CAP-1"


@pytest.mark.parametrize(
    "response",
    [
        _capture_response(status="PENDING"),
        _capture_response(capture_status="PENDING"),
        _capture_response(amount="15.99"),
        _capture_response(currency="EUR"),
        _capture_response(capture_id=""),
    ],
)
def test_unconfirmed_or_mismatched_capture_never_settles(response) -> None:
    with pytest.raises(PayPalCaptureError):
        _confirmed_capture(response, "USD", Decimal("16.00"))


@pytest.mark.asyncio
async def test_ambiguous_capture_is_reconciled_without_blind_retry(monkeypatch) -> None:
    from app.services import payment_service as service
    from app.services import intent_audit_service
    monkeypatch.setattr(intent_audit_service, "require_current_assessment", _async_value({"status": "VALID", "risk_level": "LOW"}))
    monkeypatch.setattr(service, "require_current_purchase_consent", _async_value(None))

    now = datetime.now(timezone.utc)
    order = SimpleNamespace(
        status=PayPalOrderStatus.CREATED, owner_user_id="owner",
        proposal_id="proposal", mandate_id="mandate", order_id="order",
        capture_id=None,
    )
    proposal = SimpleNamespace(
        status=ProposalStatus.APPROVED, proposal_id="proposal", mandate_id="mandate",
        currency="USD", total=Decimal("16.00"), merchant="vendor-a.com",
        category="electronics", quantity=1, unit_price=Decimal("16.00"),
    )
    mandate = SimpleNamespace(
        status=MandateStatus.ACTIVE, owner_user_id="owner", mandate_id="mandate",
        expires_at=now + timedelta(days=1), max_total_budget=Decimal("100.00"),
        currency="USD", max_price_per_item=None, max_item_count=1,
        max_quantity_per_item=1,
        allowed_merchants=["vendor-a.com"], allowed_categories=["electronics"],
    )
    monkeypatch.setattr(service, "is_kill_switch_active", _async_value(False))
    monkeypatch.setattr(service, "get_paypal_order_from_db", _async_value(order))
    monkeypatch.setattr(service, "get_proposal_from_db", _async_value(proposal))
    monkeypatch.setattr(service, "get_mandate_from_db", _async_value(mandate))
    monkeypatch.setattr(service, "get_cumulative_mandate_spent", _async_value(0))
    statuses = []

    async def update_status(order_id, status, updated_at_iso, **kwargs):
        statuses.append(status)
        order.status = status
        return True

    monkeypatch.setattr(service, "update_paypal_order_status_in_db", update_status)

    class Client:
        capture_calls = 0
        reconcile_calls = 0

        async def get_order(self, order_id):
            self.reconcile_calls += 1
            return {
                "status": "APPROVED",
                "purchase_units": [{"amount": {"currency_code": "USD", "value": "16.00"}}],
            }

        async def capture_order(self, order_id, capture_request_id):
            self.capture_calls += 1
            raise PayPalNetworkError("timeout")

    client = Client()
    with pytest.raises(PayPalCaptureError, match="requires reconciliation"):
        await service.capture_payment_order("order", "owner", paypal_client=client)
    assert client.capture_calls == 1
    assert client.reconcile_calls == 2  # initial approval check plus timeout reconciliation
    assert statuses == [PayPalOrderStatus.RECONCILIATION_REQUIRED]

    # Subsequent and repeated ordinary requests must stop before contacting PayPal.
    for _ in range(3):
        with pytest.raises(PayPalCaptureError, match="requires explicit reconciliation"):
            await service.capture_payment_order("order", "owner", paypal_client=client)
    assert client.capture_calls == 1
    assert client.reconcile_calls == 2
    assert order.status == PayPalOrderStatus.RECONCILIATION_REQUIRED
    assert order.capture_id is None
    assert proposal.status == ProposalStatus.APPROVED


@pytest.mark.asyncio
async def test_reconciliation_required_rejects_concurrent_capture_requests(monkeypatch) -> None:
    from app.services import payment_service as service

    order = SimpleNamespace(
        status=PayPalOrderStatus.RECONCILIATION_REQUIRED,
        owner_user_id="owner",
        proposal_id="proposal",
        mandate_id="mandate",
        order_id="order",
        capture_id=None,
    )
    monkeypatch.setattr(service, "is_kill_switch_active", _async_value(False))
    arrivals = 0
    both_loaded = asyncio.Event()

    async def load_uncertain_order(order_id):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            both_loaded.set()
        await both_loaded.wait()
        return order

    monkeypatch.setattr(service, "get_paypal_order_from_db", load_uncertain_order)

    class Client:
        get_order_calls = 0
        capture_calls = 0

        async def get_order(self, order_id):
            self.get_order_calls += 1
            return {"status": "APPROVED"}

        async def capture_order(self, order_id, capture_request_id):
            self.capture_calls += 1
            return _capture_response()

    client = Client()
    results = await asyncio.gather(
        service.capture_payment_order("order", "owner", paypal_client=client),
        service.capture_payment_order("order", "owner", paypal_client=client),
        return_exceptions=True,
    )

    assert all(isinstance(result, PayPalCaptureError) for result in results)
    assert client.get_order_calls == 0
    assert client.capture_calls == 0
    assert order.status == PayPalOrderStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_ambiguous_capture_reconciles_only_verified_completed_evidence(
    monkeypatch,
) -> None:
    from app.services import intent_audit_service, payment_service as service
    from app.services.audit_service import AuditService

    now = datetime.now(timezone.utc)
    order = SimpleNamespace(
        status=PayPalOrderStatus.CREATED,
        owner_user_id="owner",
        proposal_id="proposal",
        mandate_id="mandate",
        order_id="order",
    )
    proposal = SimpleNamespace(
        status=ProposalStatus.APPROVED,
        proposal_id="proposal",
        mandate_id="mandate",
        currency="USD",
        total=Decimal("16.00"),
        merchant="vendor-a.com",
        category="electronics",
        quantity=1,
        unit_price=Decimal("16.00"),
    )
    mandate = SimpleNamespace(
        status=MandateStatus.ACTIVE,
        owner_user_id="owner",
        mandate_id="mandate",
        expires_at=now + timedelta(days=1),
        max_total_budget=Decimal("100.00"),
        currency="USD",
        max_price_per_item=None,
        max_item_count=1,
        max_quantity_per_item=1,
        allowed_merchants=["vendor-a.com"],
        allowed_categories=["electronics"],
    )
    monkeypatch.setattr(
        intent_audit_service,
        "require_current_assessment",
        _async_value({"status": "VALID", "risk_level": "LOW"}),
    )
    monkeypatch.setattr(service, "require_current_purchase_consent", _async_value(None))
    monkeypatch.setattr(service, "is_kill_switch_active", _async_value(False))
    monkeypatch.setattr(service, "get_paypal_order_from_db", _async_value(order))
    monkeypatch.setattr(service, "get_proposal_from_db", _async_value(proposal))
    monkeypatch.setattr(service, "get_mandate_from_db", _async_value(mandate))
    monkeypatch.setattr(service, "get_cumulative_mandate_spent", _async_value(0))

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, *args):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

    monkeypatch.setattr(service, "get_connection", Connection)
    update_calls: list[str] = []

    async def update_order(**kwargs):
        update_calls.append("order")
        return kwargs["capture_id"] == "REAL-PAYPAL-CAPTURE-7"

    async def update_proposal(**kwargs):
        update_calls.append("proposal")
        return kwargs["new_status"] == ProposalStatus.SETTLED

    async def append_event(**kwargs):
        update_calls.append("audit")

    monkeypatch.setattr(service, "update_paypal_order_captured_in_db", update_order)
    monkeypatch.setattr(service, "update_proposal_status_in_db", update_proposal)
    monkeypatch.setattr(AuditService, "append_event", append_event)

    class Client:
        get_order_calls = 0
        capture_calls = 0

        async def get_order(self, order_id):
            self.get_order_calls += 1
            if self.get_order_calls == 1:
                return {
                    "status": "APPROVED",
                    "purchase_units": [{
                        "amount": {"currency_code": "USD", "value": "16.00"}
                    }],
                }
            return _capture_response(capture_id="REAL-PAYPAL-CAPTURE-7")

        async def capture_order(self, order_id, capture_request_id):
            self.capture_calls += 1
            raise PayPalNetworkError("simulated timeout")

    client = Client()
    result = await service.capture_payment_order("order", "owner", paypal_client=client)

    assert result.status == PayPalOrderStatus.COMPLETED
    assert result.capture_id == "REAL-PAYPAL-CAPTURE-7"
    assert client.capture_calls == 1
    assert client.get_order_calls == 2
    assert update_calls == ["order", "proposal", "audit"]


@pytest.mark.asyncio
async def test_capture_finalization_stops_if_order_update_loses_race(monkeypatch) -> None:
    from app.services import payment_service as service
    from app.services import intent_audit_service
    monkeypatch.setattr(intent_audit_service, "require_current_assessment", _async_value({"status": "VALID", "risk_level": "LOW"}))
    monkeypatch.setattr(service, "require_current_purchase_consent", _async_value(None))

    now = datetime.now(timezone.utc)
    order = SimpleNamespace(
        status=PayPalOrderStatus.CREATED, owner_user_id="owner",
        proposal_id="proposal", mandate_id="mandate", order_id="order",
    )
    proposal = SimpleNamespace(
        status=ProposalStatus.APPROVED, proposal_id="proposal", mandate_id="mandate",
        currency="USD", total=Decimal("16.00"), merchant="vendor-a.com",
        category="electronics", quantity=1, unit_price=Decimal("16.00"),
    )
    mandate = SimpleNamespace(
        status=MandateStatus.ACTIVE, owner_user_id="owner", mandate_id="mandate",
        expires_at=now + timedelta(days=1), max_total_budget=Decimal("100.00"),
        currency="USD", max_price_per_item=None, max_item_count=1,
        max_quantity_per_item=1,
        allowed_merchants=["vendor-a.com"], allowed_categories=["electronics"],
    )
    monkeypatch.setattr(service, "is_kill_switch_active", _async_value(False))
    monkeypatch.setattr(service, "get_paypal_order_from_db", _async_value(order))
    monkeypatch.setattr(service, "get_proposal_from_db", _async_value(proposal))
    monkeypatch.setattr(service, "get_mandate_from_db", _async_value(mandate))
    monkeypatch.setattr(service, "get_cumulative_mandate_spent", _async_value(0))
    proposal_updates = []

    async def update_order(*args, **kwargs):
        return False

    async def update_proposal(*args, **kwargs):
        proposal_updates.append(True)
        return True

    monkeypatch.setattr(service, "update_paypal_order_captured_in_db", update_order)
    monkeypatch.setattr(service, "update_proposal_status_in_db", update_proposal)

    class Connection:
        committed = False
        rolled_back = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, *args):
            return None

        async def commit(self):
            self.committed = True

        async def rollback(self):
            self.rolled_back = True

    connection = Connection()
    monkeypatch.setattr(service, "get_connection", lambda: connection)

    class Client:
        async def get_order(self, order_id):
            return {
                "status": "APPROVED",
                "purchase_units": [{"amount": {"currency_code": "USD", "value": "16.00"}}],
            }

        async def capture_order(self, order_id, capture_request_id):
            return {
                "status": "COMPLETED",
                "raw_response": _capture_response(),
            }

    with pytest.raises(RuntimeError, match="changed during capture finalization"):
        await service.capture_payment_order("order", "owner", paypal_client=Client())
    assert proposal_updates == []
    assert connection.rolled_back is True
    assert connection.committed is False


def _async_value(value):
    async def resolve(*args, **kwargs):
        return value

    return resolve

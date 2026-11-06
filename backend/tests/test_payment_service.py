"""Focused payment executor invariants."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

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
    statuses = []

    async def update_status(order_id, status, updated_at_iso, **kwargs):
        statuses.append(status)
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


@pytest.mark.asyncio
async def test_capture_finalization_stops_if_order_update_loses_race(monkeypatch) -> None:
    from app.services import payment_service as service

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

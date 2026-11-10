"""Attack Lab fixtures layered over the existing backend test database/client."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.db.database import get_db_path, init_db
from app.schemas.mandate import Mandate, MandateCreate
from app.schemas.proposal import Proposal, ProposalCreate
from app.services.audit_service import AuditService
from app.services.mandate_service import (
    activate_mandate,
    confirm_mandate,
    create_draft_mandate,
    sign_mandate,
)
from app.services.proposal_service import create_proposal
from app.services.gemini_client import GeminiClient
from app.services.intent_auditor_client import IntentAuditorClient
from app.services.paypal_service import PayPalClient

from .support import (
    AttackCase,
    AttackClock,
    AttackPrincipal,
    AsyncBarrier,
    BlockedDestinations,
    ExternalNetworkBlocked,
    ScriptedHTTPX,
    ScriptedPayPal,
)

_REAL_INTENT_AUDITOR_ASSESS = IntentAuditorClient.assess


@pytest.fixture(autouse=True)
def init_test_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Attack Lab tests on their own DB instead of initializing the shared DB."""
    from app.schemas.intent_audit import IntentAuditorResult

    async def local_low_risk_assessment(
        self: IntentAuditorClient, payload: Any
    ) -> IntentAuditorResult:
        """Keep tests without an explicit provider script deterministic and offline."""
        return IntentAuditorResult(schema_version="1.0", risk_level="LOW")

    monkeypatch.setattr(IntentAuditorClient, "assess", local_low_risk_assessment)


@pytest.fixture(autouse=True)
def attack_lab_safety(monkeypatch: pytest.MonkeyPatch) -> BlockedDestinations:
    """Use dummy provider configuration and deny every unmocked HTTPX transport."""
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "attack-lab-gemini-key")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "attack-lab-groq-key")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "attack-lab-client-id")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "attack-lab-client-secret")
    monkeypatch.setattr(settings, "MANDATE_SIGNING_KEY", "attack-lab-signing-key-32-bytes!!")
    blocked = BlockedDestinations()

    def reject_request(request: httpx.Request) -> httpx.Response:
        host = request.url.host or "<missing>"
        blocked.hosts.append(host)
        raise ExternalNetworkBlocked(
            f"Attack Lab blocked unmocked network request to host {host}."
        )

    def guard_constructor(original: Any) -> Any:
        def guarded(self: Any, *args: Any, **kwargs: Any) -> None:
            if kwargs.get("proxy") is not None:
                raise ExternalNetworkBlocked(
                    "Attack Lab does not permit HTTPX proxy configuration."
                )
            mounts = kwargs.get("mounts")
            if mounts:
                raise ExternalNetworkBlocked(
                    "Attack Lab does not permit HTTPX mount configuration."
                )

            transport = kwargs.get("transport")
            if transport is None:
                kwargs["transport"] = httpx.MockTransport(reject_request)
            elif not isinstance(transport, (httpx.MockTransport, httpx.ASGITransport)):
                raise ExternalNetworkBlocked(
                    "Attack Lab permits only MockTransport or in-process ASGITransport."
                )
            original(self, *args, **kwargs)

        return guarded

    monkeypatch.setattr(
        httpx.AsyncClient,
        "__init__",
        guard_constructor(httpx.AsyncClient.__init__),
    )
    monkeypatch.setattr(
        httpx.Client,
        "__init__",
        guard_constructor(httpx.Client.__init__),
    )
    return blocked


@pytest.fixture
async def attack_database(
    init_test_db: None,
    attack_lab_safety: BlockedDestinations,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[Path]:
    """Give each Attack Lab test its own migrated SQLite file under pytest tmp_path."""
    database_path = tmp_path / "attack-lab.sqlite3"
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{database_path}")
    await init_db()
    yield database_path


@pytest.fixture
async def attack_users(
    client: httpx.AsyncClient,
    attack_database: Path,
) -> tuple[AttackPrincipal, AttackPrincipal]:
    """Create two distinct synthetic principals using the real session API."""
    principals: list[AttackPrincipal] = []
    for _ in range(2):
        response = await client.post("/api/v1/sessions", json={})
        assert response.status_code == 201
        body = response.json()
        principals.append(
            AttackPrincipal(user_id=body["user_id"], session_token=body["session_token"])
        )
    assert principals[0].user_id != principals[1].user_id
    return principals[0], principals[1]


@pytest.fixture
def attack_factories(attack_database: Path) -> dict[str, Any]:
    """Return local mandate and proposal factories using synthetic terms."""
    async def mandate(
        owner_user_id: str,
        *,
        active: bool = True,
        require_user_approval: bool = True,
    ) -> Mandate:
        created = await create_draft_mandate(
            MandateCreate(
                max_total_budget=Decimal("100.00"),
                currency="USD",
                allowed_merchants=["synthetic-shop.example"],
                allowed_categories=["test-goods"],
                max_item_count=3,
                max_quantity_per_item=2,
                max_price_per_item=Decimal("50.00"),
                require_user_approval=require_user_approval,
                expires_at=datetime.now(timezone.utc) + timedelta(days=2),
            ),
            owner_user_id=owner_user_id,
        )
        if active:
            await confirm_mandate(created.mandate_id)
            await sign_mandate(created.mandate_id)
            return await activate_mandate(created.mandate_id)
        return created

    async def proposal(
        mandate_id: str, *, product_id: str = "attack-item-001"
    ) -> Proposal:
        created, _ = await create_proposal(
            ProposalCreate(
                mandate_id=mandate_id,
                product_id=product_id,
                product_name="Synthetic Attack Lab Item",
                merchant="synthetic-shop.example",
                category="test-goods",
                quantity=1,
                unit_price=Decimal("12.50"),
                currency="USD",
                tax=Decimal("1.00"),
                shipping=Decimal("2.00"),
                other_charges=Decimal("0.50"),
                product_source="https://synthetic-shop.example/items/attack-item-001",
            )
        )
        return created

    return {"mandate": mandate, "proposal": proposal}


@pytest.fixture
async def attack_case(
    attack_users: tuple[AttackPrincipal, AttackPrincipal],
    attack_factories: dict[str, Any],
    attack_database: Path,
) -> AttackCase:
    """Create active owned mandates and valid proposals for both principals."""
    user_a, user_b = attack_users
    mandate_a = await attack_factories["mandate"](user_a.user_id)
    mandate_b = await attack_factories["mandate"](user_b.user_id)
    proposal_a = await attack_factories["proposal"](mandate_a.mandate_id)
    proposal_b = await attack_factories["proposal"](mandate_b.mandate_id)
    return AttackCase(
        user_a=user_a,
        user_b=user_b,
        mandate_a=mandate_a,
        mandate_b=mandate_b,
        proposal_a=proposal_a,
        proposal_b=proposal_b,
    )


@pytest.fixture
def attack_clock() -> AttackClock:
    """Provide a controllable clock; tests patch only the modules they exercise."""
    return AttackClock()


@pytest.fixture
def attack_barrier() -> type[AsyncBarrier]:
    """Provide explicit rendezvous barriers for deterministic concurrency tests."""
    return AsyncBarrier


@pytest.fixture
def verify_attack_audit_chain(
    attack_database: Path,
) -> Callable[[], Awaitable[Any]]:
    """Return a helper that verifies the current per-test audit chain."""
    async def verify() -> Any:
        return await AuditService.verify_chain()

    return verify


@pytest.fixture
def attack_gemini_script() -> ScriptedHTTPX:
    """Script Gemini responses without retaining request bodies or credentials."""
    return ScriptedHTTPX(expected_host="generativelanguage.googleapis.com")


@pytest.fixture
async def attack_gemini_client(
    attack_gemini_script: ScriptedHTTPX,
) -> AsyncIterator[tuple[GeminiClient, ScriptedHTTPX]]:
    async with attack_gemini_script.new_client() as client:
        yield GeminiClient(api_key="attack-lab-gemini-key", client=client), attack_gemini_script


@pytest.fixture
def attack_intent_script() -> ScriptedHTTPX:
    """Script the no-tools Groq/OpenAI-compatible Intent Auditor endpoint locally."""
    return ScriptedHTTPX(expected_host="api.groq.com")


@pytest.fixture
async def attack_intent_client(
    attack_intent_script: ScriptedHTTPX,
    attack_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[IntentAuditorClient, ScriptedHTTPX]]:
    # The shared legacy fixture stubs this call; restore the real HTTPX client
    # only for this fixture so the Attack Lab transport is exercised.
    monkeypatch.setattr(IntentAuditorClient, "assess", _REAL_INTENT_AUDITOR_ASSESS)
    async with attack_intent_script.new_client() as client:
        yield IntentAuditorClient(client=client), attack_intent_script


@pytest.fixture
def attack_paypal_script() -> ScriptedPayPal:
    """Return isolated synthetic PayPal OAuth/order/approval/capture responses."""
    return ScriptedPayPal()


@pytest.fixture
async def attack_paypal_client(
    attack_paypal_script: ScriptedPayPal,
) -> AsyncIterator[tuple[PayPalClient, ScriptedPayPal]]:
    async with attack_paypal_script.new_client() as client:
        yield PayPalClient(client=client), attack_paypal_script

"""Adversarial inputs for deterministic policy calculations and money schemas."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.policy import DecisionOutcome, EvaluationContext, RuleStatus
from app.schemas.mandate import MandateCreate
from app.schemas.proposal import Proposal, ProposalCreate, build_proposal_from_create
from app.services.policy_engine import evaluate_policy
from tests.attack_lab.support import AttackCase


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", "1.001"])
@pytest.mark.parametrize("field", ["max_total_budget", "max_price_per_item"])
def test_nonfinite_and_overprecise_mandate_money_is_rejected(
    field: str, amount: str
) -> None:
    with pytest.raises(ValidationError):
        MandateCreate(
            max_total_budget=(Decimal(amount) if field == "max_total_budget"
                              else Decimal("100.00")),
            currency="USD",
            allowed_merchants=["synthetic-shop.example"],
            allowed_categories=["test-goods"],
            max_item_count=2,
            max_quantity_per_item=1,
            max_price_per_item=(Decimal(amount) if field == "max_price_per_item"
                                else Decimal("50.00")),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )


@pytest.mark.parametrize(
    "field", ["unit_price", "tax", "shipping", "other_charges"]
)
@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", "1.001"])
def test_nonfinite_and_overprecise_proposal_money_is_rejected(
    field: str, amount: str
) -> None:
    values = {
        "unit_price": Decimal("10.00"),
        "tax": Decimal("0.00"),
        "shipping": Decimal("0.00"),
        "other_charges": Decimal("0.00"),
    }
    values[field] = Decimal(amount)
    with pytest.raises(ValidationError):
        ProposalCreate(
            mandate_id="man_synthetic",
            product_id="item-synthetic",
            product_name="Synthetic item",
            merchant="synthetic-shop.example",
            category="test-goods",
            quantity=1,
            currency="USD",
            product_source="https://synthetic-shop.example/item",
            **values,
        )


def _proposal(
    case: AttackCase,
    *,
    now: datetime,
    product_id: str,
    quantity: int,
    unit_price: Decimal,
) -> Proposal:
    payload = ProposalCreate(
        mandate_id=case.mandate_a.mandate_id,
        product_id=product_id,
        product_name="Synthetic policy boundary item",
        merchant="synthetic-shop.example",
        category="test-goods",
        quantity=quantity,
        unit_price=unit_price,
        currency="USD",
        product_source=f"https://synthetic-shop.example/{product_id}",
    )
    return build_proposal_from_create(
        payload,
        proposal_id=f"prp_{product_id}",
        created_at=now,
    )


async def test_policy_boundaries_and_repeated_evaluation_are_deterministic(
    attack_case: AttackCase,
) -> None:
    now = datetime.now(timezone.utc)
    context = EvaluationContext(now=now)
    at_limit = _proposal(
        attack_case,
        now=now,
        product_id="exact-limit-item",
        quantity=2,
        unit_price=Decimal("50.00"),
    )
    exact_first = evaluate_policy(at_limit, attack_case.mandate_a, context)
    exact_again = evaluate_policy(at_limit, attack_case.mandate_a, context)

    assert exact_first.model_dump(mode="json") == exact_again.model_dump(mode="json")
    assert exact_first.decision == DecisionOutcome.REQUIRE_APPROVAL
    assert exact_first.rules[2].status == RuleStatus.PASS  # P03 exact budget boundary
    assert exact_first.rules[3].status == RuleStatus.PASS  # P04 exact item-price boundary
    assert exact_first.rules[6].status == RuleStatus.PASS  # P07 exact quantity boundary

    over_budget = _proposal(
        attack_case,
        now=now,
        product_id="over-budget-item",
        quantity=2,
        unit_price=Decimal("50.01"),
    )
    over_budget_decision = evaluate_policy(
        over_budget, attack_case.mandate_a, context
    )
    assert over_budget_decision.decision == DecisionOutcome.BLOCK
    assert "P03" in over_budget_decision.blocking_rules

    over_quantity = _proposal(
        attack_case,
        now=now,
        product_id="over-quantity-item",
        quantity=3,
        unit_price=Decimal("30.00"),
    )
    over_quantity_decision = evaluate_policy(
        over_quantity, attack_case.mandate_a, context
    )
    assert over_quantity_decision.decision == DecisionOutcome.REQUIRE_APPROVAL
    assert over_quantity_decision.rules[6].status == RuleStatus.REQUIRE_APPROVAL


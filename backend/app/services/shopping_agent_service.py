"""Shopping Agent orchestration with a fixed, non-payment tool boundary."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.core.untrusted_content import inspect_untrusted_text
from app.schemas.mandate import MandateStatus
from app.schemas.authorization import public_policy_decision
from app.schemas.proposal import ProposalCreate
from app.schemas.shopping_agent import (
    ProductLookupArgs,
    ProductSearchArgs,
    ProductDataStatus,
    ProductRecord,
    ProposalToolResult,
    ProposePurchaseArgs,
    ShoppingAgentResponse,
)
from app.services.catalog_service import (
    ProductNotFoundError,
    get_product,
    search_products,
)
from app.services.gemini_client import (
    FUNCTION_DECLARATIONS,
    GeminiClient,
    GeminiConfigurationError,
    GeminiProviderError,
    GeminiRateLimitError,
)
from app.services.kill_switch_service import enforce_kill_switch_guard
from app.services.mandate_service import get_mandate, verify_mandate
from app.services.proposal_service import create_proposal, evaluate_proposal_service, get_proposal

MAX_TOOL_ROUNDS = 4
MAX_CONVERSATION_BYTES = 24_000
MAX_TOOL_RESULT_BYTES = 12_000


class ShoppingAgentInputError(Exception):
    """Raised for invalid or unauthorized agent requests."""


class ShoppingAgentBlockedError(Exception):
    """Raised when the emergency kill switch blocks proposal creation."""


class ShoppingAgentOwnershipError(ShoppingAgentInputError):
    """Raised when a selected mandate belongs to a different principal."""


def _product_json(product: ProductRecord) -> dict[str, Any]:
    return product.model_dump(mode="json")


async def _propose_purchase(
    args: ProposePurchaseArgs, *, user_id: str, mandate_id: str | None
) -> ProposalToolResult:
    if not mandate_id:
        raise ShoppingAgentInputError(
            "Select an owned active mandate before proposing a purchase."
        )

    # The catalogue, never model arguments, supplies every commercial term.
    try:
        product = get_product(args.product_id)
    except ProductNotFoundError as exc:
        raise ShoppingAgentInputError("The product ID is not in the local catalogue.") from exc
    if product.availability != "SIMULATED_AVAILABLE":
        raise ShoppingAgentInputError("This product is not marked simulated and available.")
    if product.description_status == ProductDataStatus.QUARANTINED:
        raise ShoppingAgentInputError("This product's catalogue text is quarantined.")
    if product.price_status != ProductDataStatus.SIMULATED or product.unit_price is None:
        raise ShoppingAgentInputError("This product has no usable simulated price.")
    if any(value is None for value in (product.tax, product.shipping, product.other_charges)):
        raise ShoppingAgentInputError("The catalogue does not provide complete simulated costs.")

    mandate = await get_mandate(mandate_id)
    if mandate.owner_user_id != user_id:
        raise ShoppingAgentOwnershipError(
            "Mandate is not owned by the authenticated session."
        )
    verification = await verify_mandate(mandate_id)
    if not verification.valid or verification.status != MandateStatus.ACTIVE:
        raise ShoppingAgentInputError("Mandate is not currently valid and active.")

    try:
        await enforce_kill_switch_guard()
    except RuntimeError as exc:
        raise ShoppingAgentBlockedError("Emergency control is blocking proposals.") from exc
    payload = ProposalCreate(
        mandate_id=mandate.mandate_id,
        product_id=product.product_id,
        product_name=product.name,
        merchant=product.merchant_id,
        category=product.category,
        quantity=args.quantity,
        unit_price=product.unit_price,
        currency=product.currency,
        tax=product.tax,
        shipping=product.shipping,
        other_charges=product.other_charges,
        product_source=f"https://catalog.leash.invalid/products/{product.product_id}",
    )
    proposal, _ = await create_proposal(payload)
    decision = await evaluate_proposal_service(proposal.proposal_id)
    final_proposal = await get_proposal(proposal.proposal_id)
    return ProposalToolResult(
        proposal_id=final_proposal.proposal_id,
        status=final_proposal.status,
        policy_decision=public_policy_decision(decision),
        simulated=True,
    )


async def _dispatch_tool(
    name: str,
    raw_args: Any,
    *,
    user_id: str,
    mandate_id: str | None,
) -> dict[str, Any]:
    """Validate model-generated args and dispatch only the three fixed tools."""
    if not isinstance(raw_args, dict):
        raise ShoppingAgentInputError("Tool arguments must be an object.")
    if name == "search_products":
        args = ProductSearchArgs.model_validate_json(json.dumps(raw_args), strict=True)
        return {"products": [_product_json(p) for p in search_products(args)]}
    if name == "get_product":
        args = ProductLookupArgs.model_validate_json(json.dumps(raw_args), strict=True)
        try:
            product = get_product(args.product_id)
        except ProductNotFoundError:
            return {"found": False, "message": "Unknown local catalogue product ID."}
        return {"found": True, "product": _product_json(product)}
    if name == "propose_purchase":
        args = ProposePurchaseArgs.model_validate_json(json.dumps(raw_args), strict=True)
        result = await _propose_purchase(args, user_id=user_id, mandate_id=mandate_id)
        return {"proposal": result.model_dump(mode="json")}
    raise ShoppingAgentInputError("Requested tool is not allowed.")


async def run_shopping_agent(
    message: str,
    *,
    user_id: str,
    mandate_id: str | None,
    client: GeminiClient | None = None,
) -> ShoppingAgentResponse:
    """Run a bounded Gemini conversation and execute only allowlisted tools."""
    sanitized, _, suspicious = inspect_untrusted_text(message, max_length=1500)
    if not sanitized:
        raise ShoppingAgentInputError("Message must contain readable text.")
    if suspicious:
        raise ShoppingAgentInputError(
            "Request contains instruction-like text and was not sent to the model."
        )
    # Keep user content explicitly marked as data inside the fixed system boundary.
    contents: list[dict[str, Any]] = [{
        "role": "user",
        "parts": [{"text": f"User request (untrusted): {sanitized}"}],
    }]

    serialized_size = len(json.dumps(contents).encode("utf-8"))
    if serialized_size > MAX_CONVERSATION_BYTES:
        raise ShoppingAgentInputError("Shopping request is too large.")
    gemini = client or GeminiClient()
    tool_schemas = {item["name"] for item in FUNCTION_DECLARATIONS}
    known_products: dict[str, ProductRecord] = {}
    for _ in range(MAX_TOOL_ROUNDS):
        response = await gemini.generate(contents)
        candidates = response.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise GeminiProviderError("Shopping Agent provider response was malformed.")
        candidate = candidates[0]
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list) or not parts:
            raise GeminiProviderError("Shopping Agent provider response was malformed.")
        function_calls = [
            part["functionCall"]
            for part in parts
            if isinstance(part, dict) and "functionCall" in part
        ]
        if not function_calls:
            answer = " ".join(
                part["text"] for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ).strip()
            if not answer:
                raise GeminiProviderError("Shopping Agent provider response was malformed.")
            plain, _, _ = inspect_untrusted_text(answer, max_length=2000)
            return ShoppingAgentResponse(
                message=plain or "I could not form a response.",
                products=list(known_products.values()),
            )
        # One call per model turn prevents parallel purchase proposals.
        if len(function_calls) != 1:
            raise GeminiProviderError("Shopping Agent returned an unsupported tool call batch.")
        call = function_calls[0]
        name = call.get("name") if isinstance(call, dict) else None
        args = call.get("args") if isinstance(call, dict) else None
        if not isinstance(name, str) or name not in tool_schemas:
            raise ShoppingAgentInputError("Requested tool is not allowed.")
        try:
            result = await _dispatch_tool(
                name, args, user_id=user_id, mandate_id=mandate_id
            )
        except ValidationError as exc:
            raise ShoppingAgentInputError("Tool arguments failed strict validation.") from exc
        if name == "search_products":
            for item in result["products"]:
                product = ProductRecord.model_validate(item)
                if product.product_id in known_products or len(known_products) < 25:
                    known_products[product.product_id] = product
        elif name == "get_product" and result.get("found"):
            product = ProductRecord.model_validate(result["product"])
            if product.product_id in known_products or len(known_products) < 25:
                known_products[product.product_id] = product
        # Once proposed, return directly; the model gets no second opportunity to
        # create another proposal or reinterpret the deterministic policy result.
        if name == "propose_purchase":
            proposal_data = result["proposal"]
            proposed_product = get_product(args["product_id"])
            if proposed_product.product_id in known_products or len(known_products) < 25:
                known_products[proposed_product.product_id] = proposed_product
            return ShoppingAgentResponse(
                message=(
                    "Proposal submitted to LEASH policy evaluation. Review the proposal "
                    "and policy result below; this does not execute a payment."
                ),
                products=list(known_products.values()),
                proposal=ProposalToolResult.model_validate(proposal_data),
            )
        result_text = json.dumps(result, separators=(",", ":"))
        if len(result_text.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
            raise GeminiProviderError("Shopping Agent catalogue response exceeded its limit.")
        contents.extend([
            {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": name, "response": result}}]},
        ])
        if len(json.dumps(contents).encode("utf-8")) > MAX_CONVERSATION_BYTES:
            raise GeminiProviderError("Shopping Agent conversation exceeded its limit.")
    return ShoppingAgentResponse(
        message="I could not complete that request within the tool limit."
    )

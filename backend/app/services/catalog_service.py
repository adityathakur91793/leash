"""Deterministic lookup for the explicitly simulated local product catalogue."""

import json
from pathlib import Path

from app.core.untrusted_content import (
    MAX_PRODUCT_DESCRIPTION_LENGTH,
    inspect_untrusted_text,
)
from app.schemas.shopping_agent import (
    ProductDataStatus,
    ProductRecord,
    ProductSearchArgs,
    SimulatedAvailability,
)

CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "mock_products.json"


class ProductNotFoundError(Exception):
    """Raised when an opaque product ID is not present in the local catalogue."""


def _load_catalogue() -> list[ProductRecord]:
    raw_records = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    records: list[ProductRecord] = []
    for raw in raw_records:
        name, _, name_suspicious = inspect_untrusted_text(
            str(raw["name"]), max_length=160
        )
        merchant_name, _, merchant_suspicious = inspect_untrusted_text(
            str(raw["merchant_name"]), max_length=100
        )
        description, truncated, suspicious = inspect_untrusted_text(
            str(raw.get("description", "")),
            max_length=MAX_PRODUCT_DESCRIPTION_LENGTH,
        )
        quarantined = suspicious or merchant_suspicious or name_suspicious
        records.append(
            ProductRecord(
                product_id=raw["product_id"],
                name="[quarantined product text]" if name_suspicious else name,
                description=None if quarantined else description or None,
                description_status=(
                    ProductDataStatus.QUARANTINED
                    if quarantined
                    else ProductDataStatus.SIMULATED
                    if description and not truncated
                    else ProductDataStatus.UNVERIFIED
                    if description
                    else ProductDataStatus.UNAVAILABLE
                ),
                merchant_id=raw["merchant_id"],
                merchant_name=(
                    "[quarantined merchant text]"
                    if merchant_suspicious
                    else merchant_name
                ),
                category=raw["category"],
                unit_price=raw.get("unit_price"),
                tax=raw.get("tax"),
                shipping=raw.get("shipping"),
                other_charges=raw.get("other_charges"),
                currency=raw.get("currency"),
                price_status=(
                    ProductDataStatus.SIMULATED
                    if raw.get("unit_price") is not None
                    else ProductDataStatus.UNAVAILABLE
                ),
                availability=SimulatedAvailability(raw["availability"]),
                availability_status=(
                    ProductDataStatus.UNVERIFIED
                    if raw["availability"] == "UNKNOWN"
                    else ProductDataStatus.SIMULATED
                ),
            )
        )
    if len({record.product_id for record in records}) != len(records):
        raise RuntimeError("Local product catalogue contains duplicate IDs.")
    return sorted(records, key=lambda record: record.product_id)


def search_products(args: ProductSearchArgs) -> list[ProductRecord]:
    """Search simulated records with exact, stable filtering and ordering."""
    keyword = (args.keyword or "").casefold()
    category = (args.category or "").casefold()
    matches: list[ProductRecord] = []
    for product in _load_catalogue():
        if keyword and keyword not in f"{product.name} {product.description or ''}".casefold():
            continue
        if category and category not in product.category.casefold():
            continue
        if args.merchant and product.merchant_id != args.merchant:
            continue
        if args.currency and product.currency != args.currency:
            continue
        if args.min_price is not None and (
            product.unit_price is None or product.unit_price < args.min_price
        ):
            continue
        if args.max_price is not None and (
            product.unit_price is None or product.unit_price > args.max_price
        ):
            continue
        matches.append(product)
    return matches[: args.limit]


def get_product(product_id: str) -> ProductRecord:
    """Return a validated simulated product or raise for an unknown ID."""
    for product in _load_catalogue():
        if product.product_id == product_id:
            return product
    raise ProductNotFoundError("Product ID is not present in the local catalogue.")

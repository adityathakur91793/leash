"""PayPal Sandbox API client (Phase 3).

Interacts strictly with the PayPal Sandbox environment (v2 Orders API).
Never exposes client secrets or access tokens to clients or log files.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.schemas.mandate import Mandate
from app.schemas.proposal import Proposal

logger = logging.getLogger(__name__)

PAYPAL_SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
ALLOWED_APPROVAL_HOSTS = {"www.sandbox.paypal.com", "sandbox.paypal.com"}


class PayPalConfigError(Exception):
    """Raised when PayPal Sandbox credentials or configuration are missing or invalid."""


class PayPalAuthError(Exception):
    """Raised when OAuth2 authentication with PayPal fails."""


class PayPalOrderError(Exception):
    """Raised when PayPal order creation or retrieval fails."""


class PayPalCaptureError(Exception):
    """Raised when PayPal payment capture fails."""


class PayPalNetworkError(Exception):
    """Raised on ambiguous network timeout or connection failure."""


class PayPalClient:
    """Async client for PayPal Sandbox Orders API v2."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._cached_token: str | None = None
        self._token_expires_at: datetime | None = None

    def _get_base_url(self) -> str:
        configured = settings.PAYPAL_SANDBOX_BASE_URL.rstrip("/")
        if configured != PAYPAL_SANDBOX_BASE_URL:
            raise PayPalConfigError("PayPal API endpoint must be the official Sandbox host.")
        return PAYPAL_SANDBOX_BASE_URL

    def _get_credentials(self) -> tuple[str, str]:
        client_id = settings.PAYPAL_CLIENT_ID.strip()
        client_secret = settings.PAYPAL_CLIENT_SECRET.strip()
        if not client_id or not client_secret:
            raise PayPalConfigError(
                "PayPal Sandbox credentials are not configured. "
                "Ensure PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are set."
            )
        return client_id, client_secret

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_data: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> httpx.Response:
        url = f"{self._get_base_url()}{path}"
        try:
            if self._client is not None:
                return await self._client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json_data,
                    data=data,
                    auth=auth,
                    timeout=15.0,
                )
            async with httpx.AsyncClient(timeout=15.0) as client:
                return await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json_data,
                    data=data,
                    auth=auth,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise PayPalNetworkError(f"PayPal communication error: {exc}") from exc

    async def get_access_token(self) -> str:
        """Obtain or return a cached OAuth2 access token using client credentials."""
        now = datetime.now(timezone.utc)
        if (
            self._cached_token
            and self._token_expires_at
            and now < (self._token_expires_at - timedelta(seconds=60))
        ):
            return self._cached_token

        client_id, client_secret = self._get_credentials()
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en_US",
        }
        data = {"grant_type": "client_credentials"}

        response = await self._request(
            "POST",
            "/v1/oauth2/token",
            headers=headers,
            data=data,
            auth=(client_id, client_secret),
        )

        if response.status_code != 200:
            logger.error("PayPal OAuth2 token request failed with status %d", response.status_code)
            raise PayPalAuthError(
                f"Failed to authenticate with PayPal Sandbox (status {response.status_code})."
            )

        token_data = response.json()
        token = token_data.get("access_token")
        expires_in = token_data.get("expires_in", 3600)
        if not token:
            raise PayPalAuthError("PayPal OAuth2 response did not contain access_token.")

        self._cached_token = str(token)
        self._token_expires_at = now + timedelta(seconds=int(expires_in))
        return self._cached_token

    @staticmethod
    def _validate_approval_url(url: str) -> str:
        """Ensure approval URL targets the expected PayPal Sandbox domain."""
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            raise PayPalOrderError(f"Insecure PayPal approval URL scheme '{parsed.scheme}'.")
        if parsed.hostname not in ALLOWED_APPROVAL_HOSTS:
            raise PayPalOrderError(
                f"Approval URL host '{parsed.hostname}' is not a permitted PayPal Sandbox host."
            )
        return url

    @staticmethod
    def _validate_return_url(url: str) -> str:
        """Allow only HTTPS return URLs and local development callbacks."""
        parsed = urlparse(url)
        local_host = parsed.hostname in {"localhost", "127.0.0.1"}
        if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
            raise ValueError("Invalid PayPal return URL.")
        configured_origins = set(settings.CORS_ORIGINS)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in configured_origins and not local_host:
            raise ValueError("PayPal return URL origin is not an approved LEASH frontend origin.")
        if parsed.scheme != "https" and not (local_host and parsed.scheme == "http"):
            raise ValueError("PayPal return URLs must use HTTPS (except localhost development URLs).")
        if not local_host and parsed.port not in (None, 443):
            raise ValueError("PayPal return URL uses a disallowed port.")
        return url

    async def create_order(
        self,
        proposal: Proposal,
        mandate: Mandate,
        paypal_request_id: str,
        return_url: str = "http://localhost:5173/checkout/complete",
        cancel_url: str = "http://localhost:5173/checkout/cancel",
    ) -> dict[str, Any]:
        """Create a PayPal Sandbox checkout order bound to the proposal's exact terms."""
        return_url = self._validate_return_url(return_url)
        cancel_url = self._validate_return_url(cancel_url)
        token = await self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": paypal_request_id,
        }

        # Build exact monetary breakdown with Decimal formatting
        currency = proposal.currency
        unit_price_str = f"{proposal.unit_price:.2f}"
        subtotal_str = f"{proposal.subtotal:.2f}"
        tax_str = f"{proposal.tax:.2f}"
        shipping_str = f"{proposal.shipping:.2f}"
        other_str = f"{proposal.other_charges:.2f}"
        total_str = f"{proposal.total:.2f}"

        order_payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": proposal.proposal_id,
                    "description": f"LEASH Mandate {mandate.mandate_id[:12]} purchase",
                    "amount": {
                        "currency_code": currency,
                        "value": total_str,
                        "breakdown": {
                            "item_total": {"currency_code": currency, "value": subtotal_str},
                            "tax_total": {"currency_code": currency, "value": tax_str},
                            "shipping": {"currency_code": currency, "value": shipping_str},
                            "handling": {"currency_code": currency, "value": other_str},
                        },
                    },
                    "items": [
                        {
                            "name": proposal.product_name[:127],
                            "unit_amount": {"currency_code": currency, "value": unit_price_str},
                            "quantity": str(proposal.quantity),
                            "category": "PHYSICAL_GOODS",
                        }
                    ],
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": settings.APP_NAME,
                        "user_action": "PAY_NOW",
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                    }
                }
            },
        }

        response = await self._request(
            "POST",
            "/v2/checkout/orders",
            headers=headers,
            json_data=order_payload,
        )

        if response.status_code not in (200, 201):
            logger.error("PayPal order creation failed with status %d", response.status_code)
            raise PayPalOrderError(
                f"Failed to create PayPal order (status {response.status_code})."
            )

        data = response.json()
        order_id = data.get("id")
        if not order_id:
            raise PayPalOrderError("PayPal order response missing order ID.")

        # Orders v2 uses `approve` for PayPal Checkout and `payer-action` for
        # some supported payment/approval flows. Both remain host-validated.
        approve_url = None
        for link in data.get("links", []):
            if link.get("rel") in {"approve", "payer-action"}:
                approve_url = link.get("href")
                break

        if not approve_url:
            raise PayPalOrderError("PayPal order response missing 'approve' link.")

        validated_approval_url = self._validate_approval_url(approve_url)

        return {
            "order_id": order_id,
            "status": data.get("status", "CREATED"),
            "approval_url": validated_approval_url,
            "raw_response": data,
        }

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """Fetch details of an existing order directly from PayPal to verify user approval."""
        token = await self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        response = await self._request(
            "GET",
            f"/v2/checkout/orders/{order_id}",
            headers=headers,
        )

        if response.status_code != 200:
            raise PayPalOrderError(
                f"Failed to retrieve PayPal order '{order_id}' (status {response.status_code})."
            )

        return response.json()

    async def capture_order(self, order_id: str, capture_request_id: str) -> dict[str, Any]:
        """Execute capture of an approved PayPal order with idempotency key."""
        token = await self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": capture_request_id,
        }

        response = await self._request(
            "POST",
            f"/v2/checkout/orders/{order_id}/capture",
            headers=headers,
        )

        if response.status_code not in (200, 201):
            logger.error("PayPal order capture failed with status %d", response.status_code)
            raise PayPalCaptureError(
                f"Failed to capture PayPal order '{order_id}' (status {response.status_code})."
            )

        data = response.json()
        status_str = data.get("status")

        # Extract capture details
        capture_id = None
        purchase_units = data.get("purchase_units", [])
        if purchase_units:
            payments = purchase_units[0].get("payments", {})
            captures = payments.get("captures", [])
            if captures:
                capture_id = captures[0].get("id")

        if status_str != "COMPLETED" or not capture_id:
            raise PayPalCaptureError("PayPal did not confirm a completed capture with a capture ID.")

        return {
            "order_id": order_id,
            "capture_id": capture_id,
            "status": status_str,
            "raw_response": data,
        }

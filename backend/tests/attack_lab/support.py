"""Deterministic local fixtures and evidence helpers for Attack Lab tests."""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel

from app.schemas.mandate import Mandate, MandateCreate
from app.schemas.proposal import Proposal, ProposalCreate

ResponseScript = httpx.Response | Exception
ResponseKey = tuple[str, str]


class ExternalNetworkBlocked(AssertionError):
    """Raised if an Attack Lab test attempts an unmocked network request."""


@dataclass
class BlockedDestinations:
    """Hosts denied by the per-test HTTPX network guard."""

    hosts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AttackPrincipal:
    """A synthetic principal and server-issued test session."""

    user_id: str
    session_token: str = field(repr=False)

    @property
    def headers(self) -> dict[str, str]:
        """Return the principal's Bearer header without logging the token."""
        return {"Authorization": f"Bearer {self.session_token}"}


@dataclass(frozen=True)
class AttackCase:
    """Synthetic owned and foreign records for a two-principal attack case."""

    user_a: AttackPrincipal
    user_b: AttackPrincipal
    mandate_a: Mandate
    mandate_b: Mandate
    proposal_a: Proposal
    proposal_b: Proposal


@dataclass(frozen=True)
class RequestEvidence:
    """Non-secret request metadata retained by scripted provider transports."""

    method: str
    host: str
    path: str
    declared_tools: tuple[str, ...] = ()
    model_tool_calls: tuple[str, ...] = ()


class ScriptedHTTPX:
    """One-host HTTPX MockTransport with queued responses and safe evidence."""

    def __init__(
        self,
        *,
        expected_host: str,
        responses: Iterable[ResponseScript] = (),
    ) -> None:
        self.expected_host = expected_host
        self._responses: deque[ResponseScript] = deque(responses)
        self.requests: list[RequestEvidence] = []

    @property
    def request_count(self) -> int:
        """Number of requests handled by this mock transport."""
        return len(self.requests)

    @property
    def requested_tools(self) -> list[tuple[str, ...]]:
        """Function declarations found in Gemini requests, without raw bodies."""
        return [record.declared_tools for record in self.requests]

    @property
    def model_tool_calls(self) -> list[tuple[str, ...]]:
        """Tool names returned by Gemini, excluding any untrusted arguments."""
        return [record.model_tool_calls for record in self.requests]

    def queue_response(self, response: ResponseScript) -> None:
        """Queue a response or transport exception for the next request."""
        self._responses.append(response)

    def new_client(self) -> httpx.AsyncClient:
        """Create a client using only this local MockTransport."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host != self.expected_host:
            raise ExternalNetworkBlocked(
                f"Mock rejected unexpected provider host: {host or '<missing>'}"
            )

        declared_tools: tuple[str, ...] = ()
        model_tool_calls: tuple[str, ...] = ()
        if host in {"generativelanguage.googleapis.com", "api.groq.com"}:
            declared_tools, model_tool_calls = self._provider_tool_names(request)
        self.requests.append(
            RequestEvidence(
                method=request.method,
                host=host,
                path=request.url.path,
                declared_tools=declared_tools,
                model_tool_calls=model_tool_calls,
            )
        )

        if not self._responses:
            raise AssertionError(
                f"No scripted response for {request.method} {host}{request.url.path}"
            )
        scripted = self._responses.popleft()
        if isinstance(scripted, Exception):
            raise scripted
        if host in {"generativelanguage.googleapis.com", "api.groq.com"}:
            self._record_response_tool_calls(scripted)
        return scripted

    @staticmethod
    def _provider_tool_names(request: httpx.Request) -> tuple[tuple[str, ...], tuple[str, ...]]:
        try:
            body = json.loads(request.content)
        except (TypeError, ValueError):
            return (), ()

        root_tools = body.get("tools", [])
        if isinstance(root_tools, list):
            names: list[str] = []
            for entry in root_tools:
                if not isinstance(entry, dict):
                    continue
                if "function" in entry and isinstance(entry.get("function"), dict):
                    function_name = entry["function"].get("name")
                    if isinstance(function_name, str):
                        names.append(function_name)
                elif isinstance(entry.get("name"), str):
                    names.append(entry["name"])
                else:
                    for item in entry.get("functionDeclarations", []):
                        if isinstance(item, dict) and isinstance(item.get("name"), str):
                            names.append(item["name"])
            return tuple(names), ()
        return (), ()

    @staticmethod
    def _response_tool_calls(body: Any) -> tuple[str, ...]:
        names: list[str] = []
        if not isinstance(body, dict):
            return ()

        for candidate in body.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            for part in candidate.get("content", {}).get("parts", []):
                if not isinstance(part, dict):
                    continue
                call = part.get("functionCall", {})
                if isinstance(call, dict) and isinstance(call.get("name"), str):
                    names.append(call["name"])

        for choice in body.get("choices", []):
            if not isinstance(choice, dict):
                continue
            message = choice.get("message", {})
            if not isinstance(message, dict):
                continue
            for tool_call in message.get("tool_calls", []) or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function", {})
                if isinstance(function, dict) and isinstance(function.get("name"), str):
                    names.append(function["name"])
        return tuple(names)

    def _record_response_tool_calls(self, response: httpx.Response) -> None:
        """Replace the last request record with response tool names only."""
        try:
            body = response.json()
            names = self._response_tool_calls(body)
        except (ValueError, AttributeError, TypeError):
            names = ()
        current = self.requests[-1]
        self.requests[-1] = RequestEvidence(
            method=current.method,
            host=current.host,
            path=current.path,
            declared_tools=current.declared_tools,
            model_tool_calls=names,
        )


class ScriptedPayPal:
    """PayPal Sandbox-shaped responses served only by a local MockTransport."""

    expected_host = "api-m.sandbox.paypal.com"

    def __init__(
        self,
        *,
        currency: str = "USD",
        amount: str = "16.00",
    ) -> None:
        self.currency = currency
        self.amount = amount
        self.http = ScriptedHTTPX(expected_host=self.expected_host)
        self._overrides: dict[ResponseKey, deque[ResponseScript]] = defaultdict(deque)
        self._orders: list[str] = []
        self._capture_ids: list[str] = []
        self.capture_request_ids: list[str | None] = []
        self._handler = self._handle

    @property
    def request_count(self) -> int:
        return self.http.request_count

    @property
    def capture_attempt_count(self) -> int:
        return len(self.capture_request_ids)

    @property
    def requests(self) -> list[RequestEvidence]:
        return self.http.requests

    def queue_response(
        self, method: str, path: str, response: ResponseScript
    ) -> None:
        """Override one exact route; exceptions simulate provider/network failures."""
        self._overrides[(method.upper(), path)].append(response)

    def new_client(self) -> httpx.AsyncClient:
        """Create an HTTP client that cannot escape this scripted transport."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        key = (request.method.upper(), request.url.path)
        if key in self._overrides and self._overrides[key]:
            response = self._overrides[key].popleft()
            self._record_request(request)
            if isinstance(response, Exception):
                raise response
            return response

        if key == ("POST", "/v1/oauth2/token"):
            response = httpx.Response(
                200, json={"access_token": "attack-lab-oauth-token", "expires_in": 3600}
            )
        elif key == ("POST", "/v2/checkout/orders"):
            order_id = f"ATTACK-LAB-ORDER-{len(self._orders) + 1}"
            self._orders.append(order_id)
            response = httpx.Response(
                201,
                json={
                    "id": order_id,
                    "status": "CREATED",
                    "links": [{
                        "rel": "approve",
                        "href": (
                            "https://www.sandbox.paypal.com/checkoutnow"
                            f"?token={order_id}"
                        ),
                    }],
                },
            )
        elif request.method == "GET" and request.url.path.startswith(
            "/v2/checkout/orders/"
        ):
            order_id = request.url.path.split("/")[4]
            response = httpx.Response(
                200,
                json={
                    "id": order_id,
                    "status": "APPROVED",
                    "purchase_units": [{
                        "amount": {"currency_code": self.currency, "value": self.amount}
                    }],
                },
            )
        elif request.method == "POST" and request.url.path.endswith("/capture"):
            order_id = request.url.path.split("/")[4]
            capture_id = f"ATTACK-LAB-CAPTURE-{len(self._capture_ids) + 1}"
            self._capture_ids.append(capture_id)
            response = httpx.Response(
                201,
                json={
                    "id": order_id,
                    "status": "COMPLETED",
                    "purchase_units": [{
                        "payments": {"captures": [{
                            "id": capture_id,
                            "status": "COMPLETED",
                            "amount": {
                                "currency_code": self.currency,
                                "value": self.amount,
                            },
                        }]},
                    }],
                },
            )
        else:
            self._record_request(request)
            raise AssertionError(
                f"No scripted PayPal route for {request.method} {request.url.path}"
            )

        self._record_request(request)
        return response

    def _record_request(self, request: httpx.Request) -> None:
        if request.method == "POST" and request.url.path.endswith("/capture"):
            self.capture_request_ids.append(request.headers.get("PayPal-Request-Id"))
        self.http.requests.append(
            RequestEvidence(
                method=request.method,
                host=request.url.host or "",
                path=request.url.path,
            )
        )


@dataclass
class AttackClock:
    """Controllable UTC clock that can be installed into selected modules."""

    current: datetime = field(
        default_factory=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
    )

    def advance(self, delta: timedelta) -> None:
        self.current += delta

    def install(self, monkeypatch: Any, modules: Iterable[object]) -> None:
        """Patch imported datetime symbols without changing application code."""
        clock = self

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                value = clock.current
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)

        for module in modules:
            monkeypatch.setattr(module, "datetime", FrozenDateTime)


class AsyncBarrier:
    """Small reusable async rendezvous for deterministic concurrency tests."""

    def __init__(self, parties: int) -> None:
        if parties < 1:
            raise ValueError("parties must be at least one")
        import asyncio

        self._parties = parties
        self._arrived = 0
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            self._arrived += 1
            if self._arrived == self._parties:
                self._event.set()
        await self._event.wait()


def http_evidence(response: httpx.Response) -> dict[str, Any]:
    """Return HTTP status/body with credential-like fields recursively redacted."""
    try:
        body: Any = response.json()
    except ValueError:
        body = redact_text(response.text)
    return {"status_code": response.status_code, "body": redact_value(body)}


def redact_value(value: Any, key: str = "") -> Any:
    """Redact fields likely to contain credentials or bearer tokens."""
    if re.search(r"token|secret|credential|authorization|api.?key", key, re.IGNORECASE):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    """Redact common bearer, OAuth, Gemini, and PayPal secret forms in text."""
    value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer <redacted>", value)
    value = re.sub(
        r"(?i)(x-goog-api-key|client_secret|access_token|refresh_token)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        value,
    )
    return value


def redacted_logs(caplog: Any) -> list[str]:
    """Return captured log messages without retaining common secret values."""
    return [redact_text(record.getMessage()) for record in caplog.records]


def safe_http_evidence(response: httpx.Response | BaseModel) -> dict[str, Any]:
    """Provide a compact, redacted evidence mapping for HTTP or schema results."""
    if isinstance(response, httpx.Response):
        return http_evidence(response)
    if isinstance(response, BaseModel):
        return redact_value(response.model_dump(mode="json"))
    raise TypeError("response must be an HTTPX response or Pydantic model")

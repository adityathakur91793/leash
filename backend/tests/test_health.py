"""Tests for health and status endpoints."""

import pytest
import httpx


pytestmark = pytest.mark.asyncio


async def test_health_returns_200(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "timestamp" in data


async def test_status_returns_200(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "operational"


async def test_unknown_route_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/nonexistent")
    assert response.status_code == 404

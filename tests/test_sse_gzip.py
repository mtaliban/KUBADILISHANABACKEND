"""Regression: SSE (text/event-stream) HAIWEZI kuwa gzip-compressed.

Tatizo: Starlette GZipMiddleware inagzip streaming responses kwa blocks za
~8KB — SSE events ndogo zilibaki kwenye buffer na browser ilipata BYTES 0
kutoka /admin/live-events (events page ilionekana "sio real-time" — data
ilionekana tu baada ya refresh). _SSEAwareGzipResponder inaruka gzip kwa
text/event-stream: mabodi yanapita RAW papo hapo.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import AsyncClient, ASGITransport

from app.main import _SSEAwareGzipResponder


@pytest.fixture
def app():
    """App yenye SSE endpoint + gzip middleware (sawa na main.py)."""
    application = FastAPI()

    @application.get("/events")
    async def events():
        async def gen():
            yield f"data: {json.dumps({'event_type': 'user.registered', 'n': 1})}\n\n"
            await asyncio.sleep(0.05)
            yield f"data: {json.dumps({'event_type': 'user.registered', 'n': 2})}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    application.add_middleware(_SSEAwareGzipResponder, minimum_size=1000)
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_sse_is_never_gzipped_even_with_accept_encoding_gzip(app, client):
    """Browser inatuma Accept-Encoding: gzip — SSE lazima ibaki RAW (isigzip)."""
    async with client.stream(
        "GET", "/events", headers={"Accept-Encoding": "gzip"}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        # MUST NOT be gzipped — otherwise the browser gets buffered/empty data.
        assert resp.headers.get("content-encoding", "").lower() != "gzip"

        body = b""
        async for chunk in resp.aiter_bytes():
            body += chunk

    text = body.decode()
    assert "user.registered" in text
    assert text.count("data: ") == 2  # both events arrived raw, unbuffered


async def test_sse_streams_immediately_without_waiting_for_buffer(app, client):
    """Events ndogo (mamia ya bytes) zinapaswa kufika PAPO HAPO — sio baada ya
    gzip buffer (~8KB) kujaa. Hili ndilo lililokuwa linavunja live events page."""
    async with client.stream(
        "GET", "/events", headers={"Accept-Encoding": "gzip"}
    ) as resp:
        first = await resp.aiter_bytes().__anext__()
    assert b"user.registered" in first

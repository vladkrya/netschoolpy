"""QR SSE regression tests; all network I/O is replaced locally."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from netschoolpy import NetSchool, exceptions

SSE_URL = "https://esia.gosuslugi.ru/aas/oauth2/api/qr/sse/session"


@pytest.fixture
def connection(monkeypatch):
    reader = Mock(read=AsyncMock())
    writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
    connect = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    return reader, writer, connect


@pytest.mark.parametrize("outcome", ["redirect", "expired", "timeout", "eof", "read_error"])
async def test_qr_sse_outcome_and_cleanup(connection, outcome):
    reader, writer, connect = connection
    data = {"redirect_url": "https://esia.gosuslugi.ru/complete"}
    if outcome == "expired":
        data = {"error": {"code": "QR_AUTHORIZATION_SESSION_EXPIRED"}}
    reader.read.return_value = b"data: " + json.dumps(data).encode() + b"\n\n"
    if outcome == "timeout":
        async def pending_read(_size):
            await asyncio.Future()
        reader.read.side_effect = pending_read
    elif outcome == "eof":
        reader.read.return_value = b""
    elif outcome == "read_error":
        reader.read.side_effect = OSError("read failed")

    async with httpx.AsyncClient() as client:
        if outcome == "redirect":
            assert await NetSchool._poll_esia_qr_sse(client, SSE_URL) == data
        else:
            error, match = {
                "expired": (exceptions.ESIAError, "QR_AUTHORIZATION_SESSION_EXPIRED"),
                "timeout": (asyncio.TimeoutError, None),
                "eof": (exceptions.ESIAError, "закрыто сервером"),
                "read_error": (OSError, "read failed"),
            }[outcome]
            with pytest.raises(error, match=match):
                await NetSchool._poll_esia_qr_sse(client, SSE_URL, timeout=0.01)

    connect.assert_awaited_once()
    writer.close.assert_called_once_with()
    writer.wait_closed.assert_awaited_once_with()


async def test_qr_sse_uses_shared_esia_ssl_context(connection, monkeypatch):
    reader, _, connect = connection
    reader.read.return_value = b'data: {"redirect_url": "https://example.org/ok"}\n\n'
    context = NetSchool._create_esia_ssl_context()
    factory = Mock(return_value=context)
    monkeypatch.setattr(NetSchool, "_create_esia_ssl_context", staticmethod(factory))

    async with httpx.AsyncClient() as client:
        await NetSchool._poll_esia_qr_sse(client, SSE_URL)

    factory.assert_called_once_with()
    connect.assert_awaited_once_with("esia.gosuslugi.ru", 443, ssl=context)

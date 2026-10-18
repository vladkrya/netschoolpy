"""9.6.1: проверка сертификатов, повторы только безопасных запросов, Tor,
keep-alive, перевход и шаги ЕСИА."""

import asyncio
import ssl

import httpx
import pytest

import netschoolpy.http as ns_http
from netschoolpy import NetSchool, exceptions
from netschoolpy.http import HttpSession

SITE = "https://sgo.example.ru"
ESIA = "https://esia.gosuslugi.ru/aas/oauth2/api/login"


def _session(handler) -> HttpSession:
    session = HttpSession(SITE)
    session._client = httpx.AsyncClient(
        base_url=f"{SITE}/webapi",
        transport=httpx.MockTransport(handler),
        event_hooks={"response": [HttpSession._check_status]},
    )
    return session


def _counting(response):
    calls = []

    def handler(request):
        calls.append(request)
        return response(request)
    return handler, calls


# ─────────────────────────── Сертификаты ───────────────────────────


def test_esia_context_verifies_certificates():
    ctx = NetSchool._create_esia_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname


def test_trusted_context_knows_russian_ca():
    ctx = ns_http.trusted_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    names = {dict(x[0] for x in c["subject"]).get("commonName") for c in ctx.get_ca_certs()}
    assert "Russian Trusted Root CA" in names


# ───────────────────────────── Повторы ─────────────────────────────


async def test_get_is_retried_on_5xx():
    handler, calls = _counting(lambda r: httpx.Response(502))
    with pytest.raises(httpx.HTTPStatusError):
        await _session(handler).get("context")
    assert len(calls) == 4


async def test_post_is_not_retried_on_5xx():
    handler, calls = _counting(lambda r: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await _session(handler).post("mail/messages", json={})
    assert len(calls) == 1


async def test_salt_post_is_retried_on_5xx():
    handler, calls = _counting(lambda r: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await _session(handler).post("auth/getdata")
    assert len(calls) == 4


async def test_post_read_timeout_is_not_retried():
    def response(request):
        raise httpx.ReadTimeout("timed out")
    handler, calls = _counting(response)
    with pytest.raises(exceptions.ServerUnavailable, match="мог быть выполнен"):
        await _session(handler).post("mail/messages", json={})
    assert len(calls) == 1


# ─────────────────────────────── Tor ───────────────────────────────


async def test_dead_tor_fails_fast_without_marking_host(monkeypatch):
    monkeypatch.setattr(ns_http, "_TOR_PROXY", "socks5://127.0.0.1:1")
    monkeypatch.setattr(ns_http, "_tor_hosts", ns_http._ExpiringHostSet(60))

    def response(request):
        raise httpx.ConnectError("refused")
    with pytest.raises(exceptions.ServerUnavailable, match="Tor"):
        await _session(response).get("context")
    assert SITE not in ns_http._tor_hosts


def test_tor_mark_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(ns_http.time, "monotonic", lambda: now[0])
    hosts = ns_http._ExpiringHostSet(10)
    hosts.add(SITE)
    assert SITE in hosts
    now[0] += 11
    assert SITE not in hosts


# ───────────────────────────── Перевход ────────────────────────────


async def test_parallel_401_relogin_once(monkeypatch):
    ns = NetSchool(SITE)
    ns._access_token = "old"
    ns._credentials = ("u", "p", 7)
    logins = []

    async def login(*args):
        logins.append(args)
        await asyncio.sleep(0)
        ns._access_token = "new"

    monkeypatch.setattr(ns, "login", login)

    def handler(request):
        if request.headers.get("at") == "new":
            return httpx.Response(200, json={})
        return httpx.Response(401)

    ns._http = _session(handler)
    original_get = ns._http.get

    async def get(path, **kw):
        ns._http.set_header("at", ns._access_token)
        return await original_get(path, **kw)

    monkeypatch.setattr(ns._http, "get", get)
    await asyncio.gather(*(ns._authed_get("context") for _ in range(5)))
    assert len(logins) == 1


async def test_401_without_credentials_is_session_expired():
    ns = NetSchool(SITE)
    ns._http = _session(lambda r: httpx.Response(401))
    with pytest.raises(exceptions.SessionExpired):
        await ns._authed_get("context")


# ──────────────────────────── Keep-alive ───────────────────────────


async def test_keepalive_stops_on_401():
    ns = NetSchool(SITE)
    ns._keepalive_interval = 0
    handler, calls = _counting(lambda r: httpx.Response(401))
    ns._http = _session(handler)
    await asyncio.wait_for(ns._keepalive_loop(), 1)
    assert len(calls) == 1


# ─────────────────────────────── ЕСИА ──────────────────────────────


async def _esia_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_anomaly_wrong_code_is_mfa_error():
    def handler(request):
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"phone": "+7***", "code_length": 6})
        return httpx.Response(200, json={"failed": "INVALID_CODE"})

    async with await _esia_client(handler) as client:
        with pytest.raises(exceptions.MFAError, match="INVALID_CODE"):
            await NetSchool(SITE)._handle_esia_anomaly(
                client, {"reaction_details": {"guid": "g", "type": "X"}}, otp_callback=lambda t, i: "123456",
            )


async def test_anomaly_start_failure_is_mfa_error():
    async with await _esia_client(lambda r: httpx.Response(500, text="oops")) as client:
        with pytest.raises(exceptions.MFAError, match="500"):
            await NetSchool(SITE)._handle_esia_anomaly(
                client, {"reaction_details": {"guid": "g"}}, otp_callback=lambda t, i: "123456",
            )


async def test_push_poll_survives_bad_answer_and_reports_refusal(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    answers = iter([httpx.Response(502, text="<html>"), httpx.Response(200, json={"failed": "DECLINED"})])
    async with await _esia_client(lambda r: next(answers)) as client:
        with pytest.raises(exceptions.MFAError, match="DECLINED"):
            await NetSchool(SITE)._poll_esia_push(client, {})


async def test_push_returns_redirect(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    async with await _esia_client(lambda r: httpx.Response(200, json={"redirect_url": "https://x/ok"})) as client:
        result = await NetSchool(SITE)._handle_esia_mfa(client, {"mfa_details": {"type": "PUSH"}})
    assert result == "https://x/ok"


_real_sleep = asyncio.sleep


async def _no_sleep(_delay):
    await _real_sleep(0)

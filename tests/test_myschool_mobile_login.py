"""Мобильный вход в «Мою школу» (ЕСИА, OAuth2+PKCE) на подставном транспорте."""

import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from myschoolpy import MySchool, exceptions

FAT = "gosuslugi-school://delegatedauth?state=ST&fat_code=FATCODE"
TOKENS = {
    "access_token": "ACCESS", "refresh_token": "REFRESH", "oid": "1000",
    "expires_in": 10800, "token_type": "Bearer", "state": "ST",
}


class Esia:
    """Подставной портал + ЕСИА; ``login_replies`` — очередь ответов шага логина."""

    def __init__(self, login_replies=None, *, te_reply=None, skip_status=200):
        self.login_replies = list(login_replies or [{"redirect_url": FAT}])
        self.te_reply = te_reply or (200, TOKENS)
        self.skip_status = skip_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/auth-provider/mobile/sign":
            return httpx.Response(200, json={"secret": "SEC", "time": "2026.10.28 10:00:00 +0300",
                                             "stateMarker": "ST"})
        if path == "/aas/oauth2/fc":
            return httpx.Response(302, headers={"location": "https://esia.gosuslugi.ru/login"})
        if path == "/aas/oauth2/api/login":
            return httpx.Response(200, json=self.login_replies.pop(0))
        if path.endswith("/verify"):
            return httpx.Response(200, json=self.login_replies.pop(0))
        if path.endswith("/skip"):
            if self.skip_status != 200:
                return httpx.Response(self.skip_status, json={})
            return httpx.Response(200, json=self.login_replies.pop(0))
        if path == "/aas/oauth2/te":
            status, body = self.te_reply
            return httpx.Response(status, json=body)
        if path == "/auth-provider/mobile/saveToken":
            return httpx.Response(200, json={"sessionId": "-1", "accessToken": "PORTAL"})
        if path == "/api/myschool/v1/current_year":
            ok = request.headers.get("authorization") == "Bearer ACCESS"
            return httpx.Response(200 if ok else 401, json={"current_year": "2026"})
        return httpx.Response(404)

    def sent(self, path):
        return [r for r in self.requests if r.url.path == path]


def make(esia: Esia) -> MySchool:
    ms = MySchool(keepalive_interval=None)
    ms._http = httpx.AsyncClient(
        transport=httpx.MockTransport(esia), base_url="https://www.gosuslugi.ru"
    )
    ms._flow_transport = httpx.MockTransport(esia)
    return ms


async def test_happy_path_sets_bearer():
    esia = Esia()
    ms = make(esia)
    await ms.login_via_gosuslugi_mobile("login", "pass")
    assert ms._http.headers["authorization"] == "Bearer ACCESS"
    assert await ms.current_year() == "2026"

    fc = esia.sent("/aas/oauth2/fc")[0].url.params
    assert fc["client_id"] == "PGU" and fc["response_type"] == "fat_code"
    assert fc["redirect_uri"] == "gosuslugi-school://delegatedauth"
    assert fc["code_challenge_method"] == "S256" and fc["state"] == "ST"
    assert fc["timestamp"] == "2026.10.28 10:00:00 +0300"

    te = parse_qs(esia.sent("/aas/oauth2/te")[0].content.decode())
    assert te["redirect_uri"] == ["https://www.gosuslugi.ru/tut-nichego-net"]
    assert te["fat_code"] == ["FATCODE"] and te["grant_type"] == ["fat_code"]
    assert te["instanceId"] == [fc["instanceId"]]
    # те же устройство и инстанс на всех шагах
    instance = json.loads(base64.b64decode(fc["instanceId"]))
    save = json.loads(esia.sent("/auth-provider/mobile/saveToken")[0].content)
    assert save["id"] == instance["persistent"]["device_id"]
    assert save["userId"] == "1000" and save["accessToken"] == "ACCESS"
    assert save["entToken"] == fc["instanceId"]
    # одноразовую ссылку со схемой приложения никуда не отправляли
    assert all(urlparse(str(r.url)).scheme == "https" for r in esia.requests)


async def test_totp_code_from_callback():
    esia = Esia([
        {"action": "ENTER_MFA", "mfa_details": {"type": "TTP", "ttp_details": {"code_length": 6}}},
        {"redirect_url": FAT},
    ])
    ms = make(esia)
    seen = []

    async def otp(kind, info):
        seen.append((kind, info))
        return "123456"

    await ms.login_via_gosuslugi_mobile("l", "p", otp_callback=otp)
    assert seen == [("TOTP", {"code_length": 6})]
    assert esia.sent("/aas/oauth2/api/login/otp/verify")[0].url.params["code"] == "123456"


async def test_sms_sync_callback_and_wrong_code():
    esia = Esia([
        {"action": "ENTER_MFA", "mfa_details": {"type": "SMS", "otp_details": {"phone": "+7"}}},
        {"failed": "WRONG_CODE", "mfa_details": {"otp_details": {"verify_attempts_left": 2}}},
    ])
    with pytest.raises(exceptions.MFAError, match="попыток осталось: 2"):
        await make(esia).login_via_gosuslugi_mobile("l", "p", otp_callback=lambda k, i: "000000")


async def test_mfa_without_callback():
    esia = Esia([{"action": "ENTER_MFA", "mfa_details": {"type": "SMS"}}])
    with pytest.raises(exceptions.MFAError, match="otp_callback"):
        await make(esia).login_via_gosuslugi_mobile("l", "p")


@pytest.mark.parametrize("skippable", [True, False, None])
async def test_max_quiz_always_tried_to_skip(skippable):
    details = {} if skippable is None else {"skippable": skippable}
    esia = Esia([{"action": "MAX_QUIZ", "max_details": details}, {"redirect_url": FAT}])
    await make(esia).login_via_gosuslugi_mobile("l", "p")
    assert len(esia.sent("/aas/oauth2/api/login/quiz-max/skip")) == 1


async def test_max_quiz_refused_by_esia():
    esia = Esia([{"action": "MAX_QUIZ"}], skip_status=400)
    with pytest.raises(exceptions.ESIAError, match="пропустить"):
        await make(esia).login_via_gosuslugi_mobile("l", "p")


async def test_anomaly_reaction_then_done():
    esia = Esia([
        {"action": "SOLVE_ANOMALY_REACTION", "reaction_details": {"guid": "g", "type": "SMS"}},
        {"redirect_url": FAT},
    ])
    ms = make(esia)
    orig = esia.__call__

    def handler(request):
        if request.url.path.endswith("/anomaly-reaction/start"):
            esia.requests.append(request)
            return httpx.Response(200, json={"phone": "+7***", "code_length": 4})
        return orig(request)

    ms._flow_transport = httpx.MockTransport(handler)
    kinds = []
    await ms.login_via_gosuslugi_mobile("l", "p", otp_callback=lambda k, i: kinds.append((k, i)) or "1111")
    assert kinds == [("SMS", {"phone": "+7***", "code_length": 4})]
    verify = esia.sent("/aas/oauth2/api/login/anomaly-reaction/verify")[0]
    assert json.loads(verify.content) == {"code": "1111", "guid": "g"}


@pytest.mark.parametrize("code, exc", [
    ("INVALID_PASSWORD", exceptions.InvalidCredentials),
    ("ACCOUNT_NOT_FOUND", exceptions.ESIAInvalidCredentials),
    ("ACCOUNT_LOCKED", exceptions.AccountLocked),
    ("SOMETHING_ELSE", exceptions.ESIAError),
])
async def test_failed_maps_to_exception(code, exc):
    with pytest.raises(exc):
        await make(Esia([{"failed": code}])).login_via_gosuslugi_mobile("l", "p")


async def test_te_without_token_reports_server_text():
    esia = Esia(te_reply=(400, {"error": "server_error", "error_description": "ESIA-007007"}))
    with pytest.raises(exceptions.ESIAError, match="ESIA-007007"):
        await make(esia).login_via_gosuslugi_mobile("l", "p")


async def test_empty_credentials_rejected():
    with pytest.raises(exceptions.LoginError):
        await make(Esia()).login_via_gosuslugi_mobile("", "p")


async def test_transport_error_is_server_unavailable():
    def boom(request):
        raise httpx.ConnectError("нет сети")

    ms = make(Esia())
    ms._flow_transport = httpx.MockTransport(boom)
    with pytest.raises(exceptions.ServerUnavailable):
        await ms.login_via_gosuslugi_mobile("l", "p")


async def test_bearer_session_roundtrip():
    esia = Esia()
    ms = make(esia)
    await ms.login_via_gosuslugi_mobile("l", "p")
    saved = json.loads(ms.export_session())
    assert saved["mode"] == "bearer" and saved["access_token"] == "ACCESS"
    assert saved["refresh_token"] == "REFRESH" and saved["expires_in"] == 10800
    assert saved["device_id"] and saved["instance_id"]

    restored = make(Esia())
    await restored.import_session(ms.export_session())
    assert restored._http.headers["authorization"] == "Bearer ACCESS"
    assert json.loads(restored.export_session()) == saved


async def test_import_bearer_expired_token():
    ms = make(Esia())
    data = json.dumps({"mode": "bearer", "access_token": "OLD"})
    with pytest.raises(exceptions.SessionExpired):
        await ms.import_session(data)


async def test_cookie_session_roundtrip_and_legacy_format():
    def alive(request):
        return httpx.Response(200 if request.headers.get("cookie") else 401,
                              json={"current_year": "2026"})

    ms = MySchool(keepalive_interval=None)
    ms._http = httpx.AsyncClient(transport=httpx.MockTransport(alive),
                                 base_url="https://www.gosuslugi.ru")
    await ms.login_with_cookies("a=1")
    assert json.loads(ms.export_session()) == {"a": "1"}
    await ms.import_session(ms.export_session())
    assert "authorization" not in ms._http.headers


async def test_cookie_login_drops_previous_bearer():
    esia = Esia()
    ms = make(esia)
    await ms.login_via_gosuslugi_mobile("l", "p")
    ms._http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"current_year": "2026"})),
        base_url="https://www.gosuslugi.ru",
    )
    await ms.login_with_cookies("a=1")
    assert json.loads(ms.export_session()) == {"a": "1"}
    assert "authorization" not in ms._http.headers


# ── обновление токена по refresh_token ──────────────────────

class Refreshing(Esia):
    """Портал, которому токен ACCESS протух; после обновления нужен ACCESS2."""

    def __init__(self, *, refresh_ok=True):
        super().__init__()
        self.refresh_ok = refresh_ok

    def __call__(self, request):
        path = request.url.path
        if path == "/aas/oauth2/te":
            self.requests.append(request)
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "ACCESS2", "refresh_token": "REFRESH2",
                                             "expires_in": 10800, "oid": "1000"})
        if path == "/api/myschool/v1/current_year":
            self.requests.append(request)
            ok = request.headers.get("authorization") == "Bearer ACCESS2"
            return httpx.Response(200 if ok else 401, json={"current_year": "2026"})
        return super().__call__(request)


def stored(issued_at=0.0):
    return json.dumps({"mode": "bearer", "access_token": "ACCESS", "refresh_token": "REFRESH",
                       "expires_in": 10800, "issued_at": issued_at, "oid": "1000",
                       "device_id": "d", "instance_id": "i"})


async def test_401_triggers_refresh_and_retry():
    esia = Refreshing()
    ms = make(esia)
    updates = []
    ms._on_update = lambda payload: updates.append(payload)
    await ms.import_session(stored())
    assert ms._http.headers["authorization"] == "Bearer ACCESS2"
    te = parse_qs(esia.sent("/aas/oauth2/te")[0].content.decode())
    assert te["grant_type"] == ["refresh_token"] and te["refresh_token"] == ["REFRESH"]
    assert te["redirect_uri"] == ["https://www.gosuslugi.ru/tut-nichego-net"]
    assert te["client_secret"] == ["SEC"] and te["instanceId"] == ["i"]
    saved = json.loads(ms.export_session())
    assert saved["access_token"] == "ACCESS2" and saved["refresh_token"] == "REFRESH2"
    assert saved["issued_at"] > 0 and updates  # новый токен отдан на сохранение


async def test_expiring_token_is_refreshed_before_request():
    import time
    esia = Refreshing()
    ms = make(esia)
    ms._http.headers["authorization"] = "Bearer ACCESS"
    from myschoolpy import _esia
    ms._bearer = _esia.Tokens("ACCESS", "REFRESH", 10800, "1000", "d", "i", issued_at=time.time() - 10300)  # осталось ~8 минут, меньше запаса в 10
    assert await ms.current_year() == "2026"
    # запрос сразу ушёл с новым токеном: старый вообще не пробовали
    years = esia.sent("/api/myschool/v1/current_year")
    assert [r.headers["authorization"] for r in years] == ["Bearer ACCESS2"]


async def test_refresh_rejected_means_login_again():
    ms = make(Refreshing(refresh_ok=False))
    with pytest.raises(exceptions.SessionExpired):
        await ms.import_session(stored())


async def test_no_refresh_token_means_login_again():
    ms = make(Refreshing())
    data = json.dumps({"mode": "bearer", "access_token": "ACCESS"})
    with pytest.raises(exceptions.SessionExpired):
        await ms.import_session(data)

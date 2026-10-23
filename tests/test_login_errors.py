"""9.6.0: причину отказа при входе называет класс исключения, а не текст."""

import httpx
import pytest

from netschoolpy import MobileNetSchool, NetSchool, exceptions
from netschoolpy._login_errors import conflict_error
from netschoolpy.http import HttpSession
from netschoolpy.mobile import exchange_code, refresh_token

SITE = "https://sgo.example.ru"
HTML = "<html><body>Ведутся работы</body></html>"

CONFLICTS = [
    ("Неправильный пароль или имя пользователя", exceptions.InvalidCredentials),
    ("Неправильный пароль или имя пользователя. Осталось попыток: 2", exceptions.InvalidCredentials),
    ("Неправильный пароль или логин", exceptions.InvalidCredentials),  # живой ответ
    ("Неверное имя пользователя или пароль", exceptions.InvalidCredentials),
    ("Invalid login or PASSWORD", exceptions.InvalidCredentials),
    ("Вы совершили 3 неудачные попытки входа. Следующая попытка может быть совершена "
     "не ранее чем через минуту", exceptions.AccountLocked),  # живой ответ sgo.e-mordovia.ru
    ("Пользователь заблокирован. Повторите попытку через 15 минут", exceptions.AccountLocked),
    ("Превышено количество попыток входа", exceptions.AccountLocked),
    ("Нет доступа", exceptions.LoginRejected),
    ("Нет доступа к системе", exceptions.LoginRejected),
    ("Технические работы", exceptions.ServerUnavailable),
    ("Ведутся технические работы", exceptions.ServerUnavailable),
    # 9.6.2: отказ без слов о логине или пароле — сбой сервера, а не пароль.
    # «Ошибка входа в систему» — живой ответ, когда у «Сетевого города» упала БД.
    (None, exceptions.ServerUnavailable),
    ("", exceptions.ServerUnavailable),
    ("Ошибка входа в систему", exceptions.ServerUnavailable),
    ("Произошла ошибка", exceptions.ServerUnavailable),
    ("Object reference not set to an instance of an object", exceptions.ServerUnavailable),
    ("Что-то новое, чего мы не знаем", exceptions.ServerUnavailable),
]


def test_hierarchy_keeps_old_except_clauses_working():
    for cls in (
        exceptions.InvalidCredentials, exceptions.AccountLocked, exceptions.LoginRejected,
        exceptions.SchoolNotFound, exceptions.ESIAInvalidCredentials, exceptions.ESIAAccountLocked,
        exceptions.RefreshTokenRejected,
    ):
        assert issubclass(cls, exceptions.LoginError)
    assert issubclass(exceptions.ESIAInvalidCredentials, exceptions.ESIAError)
    assert issubclass(exceptions.ESIAInvalidCredentials, exceptions.InvalidCredentials)
    assert issubclass(exceptions.ESIAAccountLocked, exceptions.ESIAError)
    assert issubclass(exceptions.ESIAAccountLocked, exceptions.AccountLocked)
    assert issubclass(exceptions.RefreshTokenRejected, exceptions.SessionExpired)
    assert not issubclass(exceptions.ServerUnavailable, exceptions.LoginError)


@pytest.mark.parametrize("message,expected", CONFLICTS)
def test_conflict_message_classification(message, expected):
    error = conflict_error(message)
    assert type(error) is expected
    if message:
        assert message in str(error)


def test_unclear_conflict_names_server_text():
    error = conflict_error("Ошибка входа в систему")
    assert str(error) == "вход: сервер отказал без слов о пароле: Ошибка входа в систему"


# ─────────────────────────────── Веб ───────────────────────────────


def _web(handler) -> NetSchool:
    ns = NetSchool(SITE)
    ns._http._client = httpx.AsyncClient(
        base_url=f"{SITE}/webapi",
        transport=httpx.MockTransport(handler),
        event_hooks={"response": [HttpSession._check_status]},
    )
    return ns


def _web_handler(login=None, getdata=None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/webapi/auth/getdata":
            return getdata() if getdata else httpx.Response(200, json={"lt": "1", "ver": "2", "salt": "3"})
        if path == "/webapi/login":
            return login()
        return httpx.Response(200, json={})
    return handler


@pytest.mark.parametrize("message,expected", CONFLICTS)
async def test_web_conflict(message, expected):
    body = {"message": message} if message is not None else {}
    ns = _web(_web_handler(login=lambda: httpx.Response(409, json=body)))
    with pytest.raises(expected):
        await ns.login("u", "p", 7)


async def test_web_conflict_without_json_is_server_unavailable():
    ns = _web(_web_handler(login=lambda: httpx.Response(409, text=HTML)))
    with pytest.raises(exceptions.ServerUnavailable, match="без слов о пароле"):
        await ns.login("u", "p", 7)


async def test_web_5xx_on_login_is_server_unavailable():
    ns = _web(_web_handler(login=lambda: httpx.Response(500, text=HTML)))
    with pytest.raises(exceptions.ServerUnavailable, match="500"):
        await ns.login("u", "p", 7)


async def test_web_5xx_on_getdata_is_server_unavailable():
    ns = _web(_web_handler(getdata=lambda: httpx.Response(503, text=HTML)))
    with pytest.raises(exceptions.ServerUnavailable, match="503"):
        await ns.login("u", "p", 7)


@pytest.mark.parametrize("where", ["login", "getdata"])
async def test_web_html_instead_of_json_is_server_unavailable(where):
    page = lambda: httpx.Response(200, text=HTML, headers={"content-type": "text/html"})
    ns = _web(_web_handler(**{where: page}))
    with pytest.raises(exceptions.ServerUnavailable, match="не JSON"):
        await ns.login("u", "p", 7)


async def test_web_getdata_without_salt_is_server_unavailable():
    ns = _web(_web_handler(getdata=lambda: httpx.Response(200, json={})))
    with pytest.raises(exceptions.ServerUnavailable, match="salt"):
        await ns.login("u", "p", 7)


async def test_web_connection_drop_is_server_unavailable():
    def handler(request):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    with pytest.raises(exceptions.ServerUnavailable, match="disconnected"):
        await _web(handler).login("u", "p", 7)


async def test_web_raw_timeout_is_server_unavailable(monkeypatch):
    # Так ведёт себя Tor-клиент HttpSession: его исключения httpx вылетают как есть.
    ns = _web(_web_handler())

    async def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(ns._http, "post", timeout)
    with pytest.raises(exceptions.ServerUnavailable):
        await ns.login("u", "p", 7)


async def test_web_response_without_at_is_login_rejected():
    ns = _web(_web_handler(login=lambda: httpx.Response(200, json={"message": "Вход невозможен"})))
    with pytest.raises(exceptions.LoginRejected, match="Вход невозможен"):
        await ns.login("u", "p", 7)


# ──────────────────────────── Госуслуги ────────────────────────────


def _esia(monkeypatch, handler) -> NetSchool:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    ns = NetSchool(SITE)

    async def crosslogin(*args):
        return "https://esia.gosuslugi.ru/login/"

    monkeypatch.setattr(ns, "_esia_crosslogin", crosslogin)
    return ns


@pytest.mark.parametrize("code,expected", [
    ("INVALID_PASSWORD", exceptions.ESIAInvalidCredentials),
    ("INVALID_LOGIN", exceptions.ESIAInvalidCredentials),
    ("ACCOUNT_NOT_FOUND", exceptions.ESIAInvalidCredentials),
    ("ACCOUNT_LOCKED", exceptions.ESIAAccountLocked),
    ("CAPTCHA_REQUIRED", exceptions.ESIAAccountLocked),
])
async def test_esia_failed_codes(monkeypatch, code, expected):
    ns = _esia(monkeypatch, lambda request: httpx.Response(200, json={"failed": code}))
    with pytest.raises(expected) as info:
        await ns.login_via_gosuslugi("login", "password")
    assert isinstance(info.value, exceptions.ESIAError)


async def test_esia_unknown_failed_code_stays_esia_error(monkeypatch):
    ns = _esia(monkeypatch, lambda request: httpx.Response(200, json={"failed": "SOMETHING_NEW"}))
    with pytest.raises(exceptions.ESIAError, match="SOMETHING_NEW") as info:
        await ns.login_via_gosuslugi("login", "password")
    assert not isinstance(info.value, (exceptions.InvalidCredentials, exceptions.AccountLocked))


async def test_esia_html_instead_of_json_is_esia_error(monkeypatch):
    ns = _esia(monkeypatch, lambda request: httpx.Response(502, text=HTML))
    with pytest.raises(exceptions.ESIAError, match="не JSON"):
        await ns.login_via_gosuslugi("login", "password")


async def _finalize(handler, school=None):
    ns = NetSchool(SITE)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ns._esia_finalize_login(client, SITE, "state", school)


async def test_esia_finalize_sgo_5xx_is_server_unavailable():
    with pytest.raises(exceptions.ServerUnavailable, match="503"):
        await _finalize(lambda request: httpx.Response(503, text=HTML))


async def test_esia_finalize_sgo_drop_is_server_unavailable():
    def handler(request):
        raise httpx.ConnectError("All connection attempts failed")

    with pytest.raises(exceptions.ServerUnavailable):
        await _finalize(handler)


async def test_esia_finalize_without_users_is_login_rejected():
    def handler(request):
        return httpx.Response(200, json={"users": []})

    with pytest.raises(exceptions.LoginRejected, match="Нет привязанных"):
        await _finalize(handler)


async def test_esia_unknown_organization_is_school_not_found():
    users = [{"id": 1, "displayName": "Школа №1"}, {"id": 2, "displayName": "Школа №2"}]
    with pytest.raises(exceptions.SchoolNotFound, match="Лицей"):
        await NetSchool(SITE)._pick_esia_user(users, "Лицей")


async def test_esia_idp_login_refusal_is_login_rejected():
    def handler(request):
        if request.url.path.endswith("/account-info"):
            return httpx.Response(200, json={"users": [{"id": 1, "roles": []}]})
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(400, json={"message": "bad state"})
        return httpx.Response(200, json={})

    with pytest.raises(exceptions.LoginRejected, match="IDP-логин"):
        await _finalize(handler)


# ───────────────────────────── Мобильный ─────────────────────────────


def _mobile_handler(login=None, getdata=None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/webapi/auth/login-state":
            return httpx.Response(200, json="s")
        if path == "/webapi/auth/getdata":
            return getdata() if getdata else httpx.Response(200, json={"lt": "1", "ver": "2", "salt": "3"})
        if path == "/webapi/auth/login":
            return login()
        return httpx.Response(200, text="")
    return handler


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("NETSCHOOL_MOBILE_CLIENT_SECRET", "cs")


@pytest.mark.parametrize("message,expected", CONFLICTS)
async def test_mobile_conflict(secret, message, expected):
    body = {"message": message} if message is not None else {}
    ns = MobileNetSchool(SITE, transport=httpx.MockTransport(
        _mobile_handler(login=lambda: httpx.Response(409, json=body))))
    with pytest.raises(expected):
        await ns.login("u", "p", 7)


async def test_mobile_conflict_without_json_is_server_unavailable(secret):
    ns = MobileNetSchool(SITE, transport=httpx.MockTransport(
        _mobile_handler(login=lambda: httpx.Response(409, text=HTML))))
    with pytest.raises(exceptions.ServerUnavailable, match="без слов о пароле"):
        await ns.login("u", "p", 7)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_mobile_5xx_on_login_is_server_unavailable(secret, status):
    ns = MobileNetSchool(SITE, transport=httpx.MockTransport(
        _mobile_handler(login=lambda: httpx.Response(status, text=HTML))))
    with pytest.raises(exceptions.ServerUnavailable, match=str(status)):
        await ns.login("u", "p", 7)


@pytest.mark.parametrize("where", ["login", "getdata"])
async def test_mobile_html_instead_of_json_is_server_unavailable(secret, where):
    page = lambda: httpx.Response(200, text=HTML, headers={"content-type": "text/html"})
    ns = MobileNetSchool(SITE, transport=httpx.MockTransport(_mobile_handler(**{where: page})))
    with pytest.raises(exceptions.ServerUnavailable, match="не JSON"):
        await ns.login("u", "p", 7)


async def test_mobile_timeout_is_server_unavailable(secret):
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(exceptions.ServerUnavailable):
        await MobileNetSchool(SITE, transport=httpx.MockTransport(handler)).login("u", "p", 7)


async def test_mobile_response_without_at_is_login_rejected(secret):
    ns = MobileNetSchool(SITE, transport=httpx.MockTransport(
        _mobile_handler(login=lambda: httpx.Response(200, json={"errorMessage": "Нет роли"}))))
    with pytest.raises(exceptions.LoginRejected, match="Нет роли"):
        await ns.login("u", "p", 7)


def _identity(status, body):
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body))


async def test_refresh_invalid_grant_is_refresh_token_rejected(secret):
    with pytest.raises(exceptions.RefreshTokenRejected) as info:
        await refresh_token("R", transport=_identity(400, {"error": "invalid_grant"}))
    assert isinstance(info.value, exceptions.SessionExpired)
    assert isinstance(info.value, exceptions.LoginError)


async def test_exchange_invalid_grant_is_login_rejected(secret):
    with pytest.raises(exceptions.LoginRejected) as info:
        await exchange_code("123", transport=_identity(400, {"error": "invalid_grant"}))
    assert not isinstance(info.value, (exceptions.SessionExpired, exceptions.InvalidCredentials))


async def test_invalid_client_is_login_rejected(secret):
    with pytest.raises(exceptions.LoginRejected, match="invalid_client"):
        await refresh_token("R", transport=_identity(401, {"error": "invalid_client"}))
    with pytest.raises(exceptions.LoginRejected) as info:
        await refresh_token("R", transport=_identity(401, {"error": "invalid_client"}))
    assert not isinstance(info.value, exceptions.RefreshTokenRejected)


async def test_identity_5xx_is_server_unavailable(secret):
    with pytest.raises(exceptions.ServerUnavailable):
        await exchange_code("123", transport=_identity(503, {}))


class _Stream(httpx.AsyncByteStream):
    """Тело приходит потоком, как с настоящего сервера (MockTransport читает его сразу)."""

    def __init__(self, data: bytes):
        self._data = data

    async def __aiter__(self):
        yield self._data


async def test_web_conflict_body_is_read_from_real_stream():
    # Вживую на sgo.e-mordovia.ru 409 приходил потоком, и message из него
    # не читался: ResponseNotRead, а раньше — пустой LoginError().
    body = '{"message": "Неправильный пароль или логин"}'.encode()
    ns = _web(_web_handler(login=lambda: httpx.Response(
        409, headers={"content-type": "application/json"}, stream=_Stream(body))))
    with pytest.raises(exceptions.InvalidCredentials, match="Неправильный пароль или логин"):
        await ns.login("u", "p", 7)

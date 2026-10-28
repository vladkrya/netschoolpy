"""Мобильный вход в «Мою школу» через ЕСИА (OAuth2 + PKCE), без браузера.

Схему использует приложение ``ru.gosuslugi.school``: у портала нет страницы
входа, которую можно пройти программно, а у приложения проверки браузера нет.
Результат входа — ``access_token`` для заголовка ``Authorization: Bearer``.

Разбор ответов ЕСИА (MFA, проверка безопасности, MAX_QUIZ) повторяет
``netschoolpy.client``: сервер тот же. Копия, а не импорт, чтобы пакеты
оставались независимыми.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Union
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx

from . import exceptions

PORTAL = "https://www.gosuslugi.ru"
ESIA = "https://esia.gosuslugi.ru"
LOGIN_API = f"{ESIA}/aas/oauth2/api/login"

CLIENT_ID = "PGU"
REDIRECT_URI = "gosuslugi-school://delegatedauth"
# На обмене кода адрес другой, чем на fc; с «родным» ЕСИА отвечает ESIA-007007.
TE_REDIRECT_URI = "https://www.gosuslugi.ru/tut-nichego-net"
SCOPE = (
    "http://esia.gosuslugi.ru/usr_inf?mode=w "
    "http://esia.gosuslugi.ru/usr_sec?mode=w"
)
TERMINAL_NAME = "Pixel 7"
TERMINAL_TYPE = "android"

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36 "
    "ru.gosuslugi.school/14.5"
)
_JSON_HEADERS = {
    "content-type": "application/json",
    "origin": ESIA,
    "referer": f"{ESIA}/login/",
}

OtpCallback = Callable[[str, dict], Union[str, Awaitable[str]]]

_CREDENTIALS = {
    "INVALID_PASSWORD": "Неверный пароль",
    "INVALID_LOGIN": "Неверный логин",
    "ACCOUNT_NOT_FOUND": "Аккаунт не найден",
}
_LOCKED = {
    "ACCOUNT_LOCKED": "Аккаунт заблокирован",
    "CAPTCHA_REQUIRED": "Требуется captcha (слишком много попыток)",
}


def failed_error(code: str) -> exceptions.ESIAError:
    """Исключение для ``{"failed": CODE}`` в ответе ЕСИА на логин."""
    if code in _CREDENTIALS:
        return exceptions.ESIAInvalidCredentials(f"Ошибка ЕСИА: {_CREDENTIALS[code]}")
    if code in _LOCKED:
        return exceptions.ESIAAccountLocked(f"Ошибка ЕСИА: {_LOCKED[code]}")
    return exceptions.ESIAError(f"Ошибка ЕСИА: {code}")


def make_pkce() -> tuple[str, str]:
    """``(code_verifier, code_challenge)``, S256."""
    alphabet = string.ascii_letters + string.digits + "-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(64))
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def make_instance_id(device_id: str) -> str:
    """Описание устройства, одинаковое на ``fc``, ``te`` и ``saveToken``."""
    raw = json.dumps({
        "persistent": {"device_id": device_id},
        "rnd_id": str(uuid.uuid4()),
        "app_name": "MP_SCHOOL",
        "phone_model": TERMINAL_NAME,
        "os_type": TERMINAL_TYPE,
        "os_version": "14",
    }, separators=(",", ":"))
    return base64.b64encode(raw.encode()).decode()


@dataclass
class Tokens:
    access_token: str
    refresh_token: str = ""
    expires_in: int = 0
    oid: str = ""
    device_id: str = ""
    instance_id: str = ""
    issued_at: float = 0.0  # unix-время получения; 0 — неизвестно

    @property
    def expires_at(self) -> float | None:
        """Когда access_token перестанет действовать (None — срок неизвестен)."""
        if not self.issued_at or not self.expires_in:
            return None
        return self.issued_at + self.expires_in


@dataclass
class _Flow:
    http: httpx.AsyncClient
    otp_callback: OtpCallback | None
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    verifier: str = ""
    challenge: str = ""
    secret: str = ""
    time: str = ""
    state: str = ""
    instance_id: str = ""


async def mobile_login(
    http: httpx.AsyncClient,
    login: str,
    password: str,
    otp_callback: OtpCallback | None = None,
) -> Tokens:
    """Шаги 1–5 схемы; ``http`` — отдельный клиент с cookie-банкой на весь вход."""
    flow = _Flow(http=http, otp_callback=otp_callback)
    flow.verifier, flow.challenge = make_pkce()
    flow.instance_id = make_instance_id(flow.device_id)
    try:
        await _sign(flow)
        await _authorize(flow)
        data = await _post_login(flow, login, password)
        redirect = await _resolve(flow, data)
        fat_code = await _fat_code(flow, redirect)
        tokens = await _exchange(flow, fat_code)
        await _save_token(flow, tokens)
    except httpx.TransportError as exc:
        raise exceptions.ServerUnavailable(f"Госуслуги не ответили: {exc}") from exc
    return tokens


def _json(response: httpx.Response, what: str) -> dict:
    try:
        data = response.json()
    except ValueError:
        raise exceptions.ESIAError(
            f"{what}: ответ не JSON (HTTP {response.status_code}) {response.text[:200]}"
        ) from None
    if not isinstance(data, dict):
        raise exceptions.ESIAError(f"{what}: неожиданный ответ {str(data)[:200]}")
    return data


async def _sign(flow: _Flow) -> None:
    r = await flow.http.post(
        f"{PORTAL}/auth-provider/mobile/sign", json={"scope": SCOPE, "client_id": CLIENT_ID}
    )
    if r.status_code >= 500:
        raise exceptions.ServerUnavailable(f"mobile/sign: HTTP {r.status_code}")
    data = _json(r, "mobile/sign")
    flow.secret = data.get("secret", "")
    flow.time = data.get("time", "")
    flow.state = data.get("stateMarker", "")
    if r.status_code != 200 or not flow.secret:
        raise exceptions.ESIAError(f"mobile/sign не отдал client_secret: {r.text[:300]}")


async def _authorize(flow: _Flow) -> None:
    """``GET aas/oauth2/fc`` — заводит cookie-сессию ЕСИА перед логином."""
    # timestamp уходит как есть, с пробелом/плюсом: подпись считается по нему.
    time_q = flow.time.replace("+", "%2B").replace(" ", "+")
    url = (
        f"{ESIA}/aas/oauth2/fc?client_id={CLIENT_ID}&client_secret={flow.secret}"
        f"&redirect_uri={quote(REDIRECT_URI, safe='')}&scope={SCOPE.replace(' ', '+')}"
        f"&response_type=fat_code&state={flow.state}&timestamp={time_q}"
        f"&code_challenge={flow.challenge}&code_challenge_method=S256"
        f"&access_type=offline&instanceId={quote(flow.instance_id, safe='')}"
    )
    r = await flow.http.get(url)
    location = r.headers.get("location", "")
    if r.status_code >= 500:
        raise exceptions.ServerUnavailable(f"ЕСИА fc: HTTP {r.status_code}")
    if r.status_code >= 400 or "error=" in location:
        raise exceptions.ESIAError(f"ЕСИА отклонила запрос входа: {location or r.text[:300]}")


async def _post_login(flow: _Flow, login: str, password: str) -> dict:
    r = await flow.http.post(
        LOGIN_API, json={"login": login, "password": password}, headers=_JSON_HEADERS
    )
    data = _json(r, "логин ЕСИА")
    if data.get("failed"):
        raise failed_error(str(data["failed"]))
    return data


def _redirect(data: dict) -> str | None:
    url = data.get("redirect_url") or data.get("redirectUrl") or data.get("redirect")
    return url or None


async def _resolve(flow: _Flow, data: dict) -> str:
    """Ответ ЕСИА на логин → ``redirect_url``, пройдя MFA и прочие шаги."""
    for _ in range(10):
        url = _redirect(data)
        if url:
            return url
        action = data.get("action", "")
        if action == "ENTER_MFA":
            data = await _mfa(flow, data)
        elif action == "SOLVE_ANOMALY_REACTION":
            data = await _anomaly(flow, data)
        elif action == "MAX_QUIZ":
            data = await _skip(flow, "quiz-max", data)
        elif action == "CHANGE_PASSWORD":
            data = await _skip(flow, "change-password", data)
        elif action == "DONE":
            raise exceptions.ESIAError("ЕСИА вернула DONE без redirect_url")
        else:
            raise exceptions.ESIAError(
                f"Неожиданный ответ ЕСИА: {json.dumps(data, ensure_ascii=False)[:500]}"
            )
    raise exceptions.ESIAError("Слишком много шагов ЕСИА, возможно зацикливание")


async def _skip(flow: _Flow, name: str, data: dict) -> dict:
    """Пропуск шага. ``skippable`` не проверяем: ЕСИА не всегда его ставит.

    Если пропуск невозможен, откажет сама.
    """
    r = await flow.http.post(f"{LOGIN_API}/{name}/skip", json={}, headers=_JSON_HEADERS)
    if r.status_code not in (200, 201, 202):
        raise exceptions.ESIAError(
            f"ЕСИА требует шаг {data.get('action')}, а пропустить его нельзя "
            f"(HTTP {r.status_code}). Пройдите его в личном кабинете Госуслуг."
        )
    return _json(r, f"{name}/skip")


async def _ask_code(flow: _Flow, mfa_type: str, details: dict) -> str:
    if flow.otp_callback is None:
        raise exceptions.MFAError(f"Нужен код подтверждения ({mfa_type}): передайте otp_callback")
    code = flow.otp_callback(mfa_type, details)
    if asyncio.iscoroutine(code):
        code = await code
    if not code:
        raise exceptions.MFAError("Код подтверждения не введён")
    return str(code).strip()


def _otp_details(mfa_details: dict) -> dict:
    return (
        mfa_details.get("otp_details")
        or mfa_details.get("ttp_details")
        or mfa_details.get("otp_max_details")
        or {}
    )


async def _mfa(flow: _Flow, data: dict) -> dict:
    mfa_details = data.get("mfa_details", {})
    raw_type = mfa_details.get("type", "UNKNOWN")
    mfa_type = "TOTP" if str(raw_type).upper() == "TTP" else str(raw_type).upper()
    if mfa_type == "PUSH":
        return await _poll_push(flow, data)
    if mfa_type not in ("SMS", "TOTP", "MAX"):
        raise exceptions.MFAError(f"Неизвестный тип MFA: {raw_type}")

    code = await _ask_code(flow, mfa_type, _otp_details(mfa_details))
    urls = [f"{LOGIN_API}/{p}/verify" for p in ("otp", "totp", "mfa", "otp-max")]
    r = None
    for url in urls:
        r = await flow.http.post(url, params={"code": code}, headers=_JSON_HEADERS)
        if r.status_code != 404:
            break
    if r is None or r.status_code == 404:
        raise exceptions.MFAError(f"Не найден endpoint проверки кода: {urls}")
    if r.status_code not in (200, 201):
        raise exceptions.MFAError(f"Ошибка подтверждения кода: {r.status_code} {r.text[:300]}")
    result = _json(r, "проверка кода")
    if result.get("failed"):
        left = _otp_details(result.get("mfa_details", {})).get("verify_attempts_left")
        extra = f" (попыток осталось: {left})" if left is not None else ""
        raise exceptions.MFAError(f"Неверный код подтверждения: {result['failed']}{extra}")
    return result


async def _anomaly(flow: _Flow, data: dict) -> dict:
    """``SOLVE_ANOMALY_REACTION``: ЕСИА присылает SMS-код проверки безопасности."""
    guid = data.get("reaction_details", {}).get("guid", "")
    r = await flow.http.post(
        f"{LOGIN_API}/anomaly-reaction/start", json={"guid": guid}, headers=_JSON_HEADERS
    )
    start = r.json() if r.status_code == 200 else {}
    details = {"phone": start.get("phone", "***"), "code_length": start.get("code_length", 6)}
    code = await _ask_code(flow, "SMS", details)
    r = await flow.http.post(
        f"{LOGIN_API}/anomaly-reaction/verify",
        json={"code": code, "guid": guid},
        headers=_JSON_HEADERS,
    )
    if r.status_code != 200:
        raise exceptions.MFAError(
            f"Ошибка подтверждения кода безопасности: {r.status_code} {r.text[:300]}"
        )
    return _json(r, "проверка безопасности")


async def _poll_push(flow: _Flow, data: dict, max_wait: int = 120) -> dict:
    body = {"challenge_id": data.get("challenge_id", ""), "state": data.get("state", "")}
    for _ in range(max_wait // 3):
        await asyncio.sleep(3)
        try:
            r = await flow.http.post(f"{LOGIN_API}/poll", json=body, headers=_JSON_HEADERS)
            result = r.json()
        except (httpx.TransportError, ValueError):
            continue
        if result.get("failed"):
            raise exceptions.MFAError(f"Push-подтверждение отклонено: {result['failed']}")
        if _redirect(result) or result.get("action"):
            return result
    raise exceptions.MFAError("Время ожидания push-подтверждения истекло")


async def _fat_code(flow: _Flow, url: str) -> str:
    """``fat_code`` из ``gosuslugi-school://...``. Эту ссылку не открывают: схема не http."""
    scheme = REDIRECT_URI.split(":", 1)[0] + ":"
    for _ in range(15):
        if url.startswith(scheme):
            query = parse_qs(urlparse(url).query)
            # именно fat_code: параметр code здесь не тот.
            code = (query.get("fat_code") or [""])[0]
            if not code:
                raise exceptions.ESIAError(f"В финальном редиректе нет fat_code: {url}")
            return code
        r = await flow.http.get(url)
        location = r.headers.get("location", "")
        if r.status_code not in (301, 302, 303, 307, 308) or not location:
            raise exceptions.ESIAError(f"Редиректы кончились, не дойдя до {scheme}//: {url}")
        url = urljoin(str(r.url), location)
    raise exceptions.ESIAError("Слишком много редиректов")


async def _exchange(flow: _Flow, fat_code: str) -> Tokens:
    """``POST aas/oauth2/te`` — одна попытка: fat_code одноразовый."""
    r = await flow.http.post(f"{ESIA}/aas/oauth2/te", data={
        "client_id": CLIENT_ID,
        "client_secret": flow.secret,
        "scope": SCOPE,
        "state": flow.state,
        "timestamp": flow.time,
        "instanceId": flow.instance_id,
        "redirect_uri": TE_REDIRECT_URI,
        "fat_code": fat_code,
        "grant_type": "fat_code",
        "token_type": "Bearer",
        "terminal_name": TERMINAL_NAME,
        "terminal_type": TERMINAL_TYPE,
        "code_verifier": flow.verifier,
    })
    if r.status_code >= 500:
        raise exceptions.ServerUnavailable(f"ЕСИА te: HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not isinstance(data, dict) or "access_token" not in data:
        # Текст ответа — единственная подсказка, что не так с телом запроса.
        raise exceptions.ESIAError(f"Обмен кода на токен не удался: HTTP {r.status_code} {r.text[:500]}")
    return Tokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", ""),
        expires_in=int(data.get("expires_in") or 0),
        oid=str(data.get("oid", "")),
        device_id=flow.device_id,
        instance_id=flow.instance_id,
        issued_at=time.time(),
    )


async def _save_token(flow: _Flow, tokens: Tokens) -> None:
    """``POST auth-provider/mobile/saveToken`` — регистрирует устройство на портале.

    Для доступа к ``/api/myschool`` не подтверждён как обязательный, поэтому
    сбой этого шага вход не ломает.
    """
    try:
        await flow.http.post(f"{PORTAL}/auth-provider/mobile/saveToken", json={
            "id": tokens.device_id,
            "userId": tokens.oid,
            "name": TERMINAL_NAME,
            "expires": tokens.expires_in,
            "accessToken": tokens.access_token,
            "type": TERMINAL_TYPE,
            "refreshToken": tokens.refresh_token,
            "entToken": tokens.instance_id,
        })
    except httpx.TransportError:
        pass


async def refresh_tokens(http: httpx.AsyncClient, tokens: Tokens) -> Tokens:
    """Новый ``access_token`` по ``refresh_token``: ``POST aas/oauth2/te``.

    Подтверждено живьём (28.10.2026): работает с подписью ``mobile/sign``,
    пока access_token ещё действует. Подпись одноразовая, поэтому каждое
    обновление берёт свою.

    :raises SessionExpired: ЕСИА не приняла refresh_token, нужен новый вход.
    """
    if not tokens.refresh_token:
        raise exceptions.SessionExpired("Нет refresh_token: войдите заново")
    try:
        signed = await http.post(
            f"{PORTAL}/auth-provider/mobile/sign", json={"scope": SCOPE, "client_id": CLIENT_ID}
        )
        sign = signed.json() if signed.status_code == 200 else {}
        if not sign.get("secret"):
            raise exceptions.ServerUnavailable(f"mobile/sign: HTTP {signed.status_code}")
        r = await http.post(f"{ESIA}/aas/oauth2/te", data={
            "client_id": CLIENT_ID,
            "client_secret": sign["secret"],
            "timestamp": sign.get("time", ""),
            "state": sign.get("stateMarker", ""),
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "token_type": "Bearer",
            "scope": SCOPE,
            "redirect_uri": TE_REDIRECT_URI,
            "instanceId": tokens.instance_id,
            "terminal_name": TERMINAL_NAME,
            "terminal_type": TERMINAL_TYPE,
        })
    except httpx.TransportError as exc:
        raise exceptions.ServerUnavailable(f"Госуслуги не ответили: {exc}") from exc
    if r.status_code >= 500:
        raise exceptions.ServerUnavailable(f"ЕСИА te: HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
        raise exceptions.SessionExpired(
            f"ЕСИА не обновила токен (HTTP {r.status_code} {r.text[:200]}): войдите заново"
        )
    return Tokens(
        access_token=data["access_token"],
        # refresh_token мог смениться; если в ответе его нет, остаётся прежний
        refresh_token=data.get("refresh_token") or tokens.refresh_token,
        expires_in=int(data.get("expires_in") or tokens.expires_in or 0),
        oid=str(data.get("oid") or tokens.oid),
        device_id=tokens.device_id,
        instance_id=tokens.instance_id,
        issued_at=time.time(),
    )

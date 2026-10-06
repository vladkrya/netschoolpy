"""Мобильное API «Сетевого города» — то, чем пользуется приложение NetSchool.

Отличия от веб-API (:class:`netschoolpy.NetSchool`):

* авторизация — Bearer-токен ИРТех (``identity.ir-tech.ru``), который живёт
  60 дней; ни cookie, ни keep-alive не нужны;
* данные — ``https://<сервер региона>/api/mobile/*``;
* есть то, чего нет в веб-дневнике: время публикации задания
  (``assignmentDate``), лента событий, аналитика по предмету, итоги одним
  запросом, портфолио.

Токен получают так же, как приложение: обычный вход в веб-часть, одноразовый
код из ``/webapi/mysettings/mobile/pincode`` и обмен кода в
``identity.ir-tech.ru/connect/token`` (OAuth ``device_code``). Для обмена нужен
``client_secret`` приложения ``parent-mobile`` — он не поставляется с
библиотекой: передайте его явно или через переменную окружения
``NETSCHOOL_MOBILE_CLIENT_SECRET``.

Пример::

    async with MobileNetSchool("https://sgo.e-mordovia.ru") as ns:
        await ns.login("ИвановИ", "password", 1110)
        diary = await ns.diary()
        for hw in await ns.current_term_assignments():
            print(hw["subjectName"], published_at(hw))

Сырые методы (``classmeetings``, ``assignments``, ``totals`` …) возвращают
JSON сервера как есть; совместимые с :class:`NetSchool` методы (``diary``,
``announcements``, ``mail_list`` …) — те же модели, что и веб-клиент.
"""

from __future__ import annotations

import datetime
import json
import logging
import mimetypes
import os
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from hashlib import md5
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx

from . import exceptions
from .models import (
    Announcement,
    Assignment,
    AssignmentDetails,
    Attachment,
    Author,
    Day,
    Diary,
    Lesson,
    MailDraft,
    MailEntry,
    MailPage,
    MailRecipient,
    Message,
    Student,
    Teacher,
    _Model,
    _parse_date,
    _parse_datetime,
)

log = logging.getLogger(__name__)

APP_VERSION = "2.0.3"
CLIENT_ID = "parent-mobile"
CLIENT_SECRET_ENV = "NETSCHOOL_MOBILE_CLIENT_SECRET"
IDENTITY_URL = "https://identity.ir-tech.ru"
CATALOG_URL = "https://mobile.ir-tech.ru/api/v1/mobile/parent"

# Так представляется само приложение: WebView на шагах входа и нативный
# клиент на запросах к API.
WEBVIEW_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)
APP_USER_AGENT = "NetSchool/231 CFNetwork/3896.200.41 Darwin/27.2.0"

_DEFAULT_TIMEOUT = 30.0
# Приложение шлёт до 75 id за раз; держим запас по длине URL.
_ASSIGNMENTS_BATCH = 50

MAIL_BOXES = {"Inbox": "INBOX", "Sent": "SENT", "Draft": "DRAFT", "Deleted": "DELETED"}

Params = List[tuple]


# ──────────────────────────── Модели ──────────────────────────────


class MobileToken(_Model):
    """Токен мобильного API. ``expires_at`` — в UTC."""

    access_token: str
    refresh_token: str = ""
    expires_at: datetime.datetime

    def expires_soon(self, margin: timedelta = timedelta(days=1)) -> bool:
        return datetime.datetime.now(datetime.timezone.utc) + margin >= self.expires_at

    @classmethod
    def from_raw(cls, data: dict) -> MobileToken:
        issued = datetime.datetime.now(datetime.timezone.utc)
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or "",
            expires_at=issued + timedelta(seconds=int(data.get("expires_in") or 0)),
        )


class MobileRegion(_Model):
    """Регион из каталога мобильного приложения."""

    server_id: str
    name: str
    api_url: str
    unavailable: bool = False
    comment: Optional[str] = None

    @property
    def url(self) -> str:
        """Корень сайта региона — то, что принимают ``NetSchool`` и ``MobileNetSchool``."""
        return _site_url(self.api_url)

    @classmethod
    def from_raw(cls, data: dict) -> MobileRegion:
        return cls(
            server_id=str(data["serverId"]).lower(),
            name=data.get("name") or "",
            api_url=data["url"],
            unavailable=bool(data.get("unavailable")),
            comment=data.get("comment"),
        )


# ──────────────────────────── Утилиты ─────────────────────────────


def password_hash(password: str, salt: str) -> str:
    """``pw2`` для ``/webapi/auth/login`` (та же формула, что у :class:`NetSchool`)."""
    inner = md5(password.encode("windows-1251")).hexdigest()
    return md5((salt + inner).encode()).hexdigest()


def published_at(assignment: dict) -> Optional[datetime.datetime]:
    """Когда учитель опубликовал задание — поле ``assignmentDate``.

    Приложение показывает его как «опубликовано 29.09 в 09:34». Есть в
    ответах ``assignments``, ``assignment``, ``current_term_assignments``
    (с часовым поясом) и в ``homework`` событий ``period_events`` (без пояса).
    """
    raw = assignment.get("assignmentDate")
    return _parse_datetime(raw) if raw else None


def _site_url(url: str) -> str:
    return url.rstrip("/").removesuffix("/api/mobile")


def _client_secret(value: Optional[str]) -> str:
    secret = value or os.environ.get(CLIENT_SECRET_ENV, "")
    if not secret:
        raise exceptions.LoginError(
            f"Не задан client_secret мобильного приложения ({CLIENT_SECRET_ENV})"
        )
    return secret


def _new_client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(**{k: v for k, v in kwargs.items() if v is not None})


def _raise_for(response: httpx.Response, what: str) -> None:
    status = response.status_code
    if status == httpx.codes.UNAUTHORIZED:
        raise exceptions.SessionExpired("Токен мобильного API истёк или отозван (401)")
    if status >= 500:
        # На отказ по смыслу («Ученики не могут отвечать всем!») сервер тоже
        # отвечает 500 — показываем его текст.
        try:
            detail = response.json().get("message") or ""
        except Exception:
            detail = ""
        raise exceptions.ServerUnavailable(f"{what}: сервер ответил {status}" + (f": {detail}" if detail else ""))
    if response.is_error:
        raise exceptions.NetSchoolError(f"{what}: HTTP {status} {response.text[:200]}")


def _json(response: httpx.Response, what: str) -> Any:
    _raise_for(response, what)
    try:
        return response.json()
    except ValueError as exc:
        raise exceptions.NetSchoolError(f"{what}: ответ не JSON") from exc


async def _send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        return await client.request(method, url, **kwargs)
    except httpx.TransportError as exc:
        raise exceptions.ServerUnavailable(f"{url}: {exc!r}") from exc


# ───────────────────────── Каталог регионов ───────────────────────


async def list_regions(
    *, proxy: Optional[str] = None, timeout: float = _DEFAULT_TIMEOUT,
) -> List[MobileRegion]:
    """Все регионы мобильного приложения. Без авторизации."""
    async with _new_client(proxy=proxy, timeout=timeout) as http:
        response = await _send(
            http, "GET", f"{CATALOG_URL}/end-points",
            params={"lng": "ru", "appVersion": APP_VERSION},
            headers={"User-Agent": APP_USER_AGENT},
        )
    return [MobileRegion.from_raw(i) for i in _json(response, "end-points")["items"]]


async def get_region(
    server_id: str, *, proxy: Optional[str] = None, timeout: float = _DEFAULT_TIMEOUT,
) -> MobileRegion:
    async with _new_client(proxy=proxy, timeout=timeout) as http:
        response = await _send(
            http, "GET", f"{CATALOG_URL}/end-points/{server_id}",
            params={"lng": "ru", "appVersion": APP_VERSION},
            headers={"User-Agent": APP_USER_AGENT},
        )
    return MobileRegion.from_raw(_json(response, "end-points/{id}"))


async def published_app_version(
    *, proxy: Optional[str] = None, timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    """Последняя опубликованная версия приложения NetSchool, например ``"2.0.2"``.

    Библиотека представляется версией :data:`APP_VERSION`; если сервер начнёт
    отказывать, сравните её с этой.
    """
    async with _new_client(proxy=proxy, timeout=timeout) as http:
        response = await _send(
            http, "GET", f"{CATALOG_URL}/app-versions/published",
            params={"lng": "ru", "appVersion": APP_VERSION},
            headers={"User-Agent": APP_USER_AGENT},
        )
    return str(_json(response, "app-versions/published"))


# ───────────────────────────── Токен ──────────────────────────────


async def _token_request(
    form: dict, *, proxy: Optional[str], timeout: float,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> MobileToken:
    async with _new_client(proxy=proxy, timeout=timeout, transport=transport) as http:
        response = await _send(
            http, "POST", f"{IDENTITY_URL}/connect/token",
            data={**form, "client_id": CLIENT_ID, "appVersion": APP_VERSION},
            headers={"User-Agent": APP_USER_AGENT},
        )
    if response.status_code in (400, 401):
        raise exceptions.LoginError(f"identity.ir-tech.ru отказал: {response.text[:200]}")
    return MobileToken.from_raw(_json(response, "connect/token"))


async def exchange_code(
    user_code: str,
    *,
    client_secret: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> MobileToken:
    """Обменять ``userCode`` из ``/webapi/mysettings/mobile/pincode`` на токен."""
    return await _token_request(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": user_code,
            "client_secret": _client_secret(client_secret),
        },
        proxy=proxy, timeout=timeout, transport=transport,
    )


async def refresh_token(
    token: str,
    *,
    client_secret: Optional[str] = None,
    proxy: Optional[str] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> MobileToken:
    """Продлить токен по ``refresh_token``.

    Стандартный OIDC-grant; в трафике приложения он не наблюдался.
    """
    return await _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": token,
            "client_secret": _client_secret(client_secret),
        },
        proxy=proxy, timeout=timeout, transport=transport,
    )


def _pick_role(roles: list) -> Optional[int]:
    for item in roles:
        role = item.get("role", item)
        name = (role.get("name") or "").lower()
        if "родитель" in name or "parent" in name:
            return role["id"]
    if roles:
        return roles[0].get("role", roles[0])["id"]
    return None


async def _web_login_code(
    http: httpx.AsyncClient, school: Union[int, str], user_name: str, password: str,
) -> str:
    """Веб-вход в режиме приложения; возвращает одноразовый ``userCode``."""
    await _send(http, "GET", "/login", params={"mobile": "", "appVer": APP_VERSION})
    await _send(http, "GET", "/webapi/logindata")
    if isinstance(school, str):
        school = await _resolve_school(http, school)
    login_state = _json(
        await _send(http, "POST", "/webapi/auth/login-state", data={"mobile": 1}),
        "auth/login-state",
    )

    rolegroup: Optional[int] = None
    result: dict = {}
    for _ in range(2):
        meta = _json(await _send(http, "POST", "/webapi/auth/getdata"), "auth/getdata")
        form: Dict[str, Any] = {
            "loginState": login_state,
            "loginType": 1,
            "lt": meta["lt"],
            "ver": meta["ver"],
            "scid": school,
            "un": user_name,
            "pw2": password_hash(password, meta["salt"]),
            "mobile": 1,
        }
        if rolegroup is not None:
            form["rolegroup"] = rolegroup
        response = await _send(http, "POST", "/webapi/auth/login", data=form)
        if response.status_code == httpx.codes.CONFLICT:
            try:
                message = response.json().get("message")
            except ValueError:
                message = None
            raise exceptions.LoginError(message or "Ошибка авторизации")
        result = _json(response, "auth/login")
        if rolegroup is None and "choose-session-role" in (result.get("entryPoint") or ""):
            rolegroup = _pick_role((result.get("accountInfo") or {}).get("userRoles") or [])
            if rolegroup is not None:
                await _send(http, "POST", "/webapi/auth/logout")
                continue
        break

    at = result.get("at")
    if not at:
        raise exceptions.LoginError(
            result.get("errorMessage") or result.get("message") or "Нет токена"
        )
    http.headers["at"] = at
    try:
        # Приложение открывает мобильную оболочку перед запросом кода.
        await _send(http, "POST", "/app/simple/mobile", data={"at": at})
        return await _pincode(http, "/webapi/mysettings/mobile/pincode")
    finally:
        # Веб-сессия дальше не нужна: всё остальное — по Bearer.
        try:
            await _send(http, "POST", "/webapi/auth/logout")
        except exceptions.NetSchoolError:
            log.debug("Не удалось закрыть веб-сессию после мобильного входа")


async def _pincode(http: httpx.AsyncClient, path: str) -> str:
    data = _json(await _send(http, "GET", path), "mysettings/mobile/pincode")
    code = data.get("userCode") if isinstance(data, dict) else None
    if not code:
        raise exceptions.LoginError("Сервер не выдал код для мобильного входа")
    return str(code)


async def _resolve_school(http: httpx.AsyncClient, name: str) -> int:
    items = _json(
        await _send(http, "GET", "/webapi/schools/search", params={"name": name}),
        "schools/search",
    )
    for key in ("shortName", "name"):
        for item in items:
            value = item.get(key) or ""
            if value == name or value.split(" (")[0] == name:
                return item["id"]
    if len(items) == 1:
        return items[0]["id"]
    raise exceptions.SchoolNotFound(name)


# ──────────────────────────── Клиент ──────────────────────────────


class MobileNetSchool:
    """Клиент мобильного API с тем же интерфейсом, что у :class:`NetSchool`.

    :param url: корень сайта региона (``https://sgo.e-mordovia.ru``) или
        сразу база API (``…/api/mobile``).
    :param client_secret: секрет приложения ``parent-mobile``; нужен только
        для входа и продления токена. По умолчанию —
        ``$NETSCHOOL_MOBILE_CLIENT_SECRET``.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: Optional[float] = None,
        proxy: Optional[str] = None,
        client_secret: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._site_url = _site_url(url)
        self._api_url = f"{self._site_url}/api/mobile"
        self._proxy = proxy
        self._timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        self._client_secret = client_secret
        self._transport = transport
        self._http = _new_client(
            base_url=self._api_url,
            proxy=proxy,
            timeout=self._timeout,
            transport=transport,
            headers={
                "User-Agent": APP_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
            },
        )
        self._token: Optional[MobileToken] = None
        self._user: dict = {}
        self._students: List[Student] = []
        self._student_id: int = -1
        self._school_id: int = -1
        self._year_id: int = -1

    def __repr__(self) -> str:
        return f"<MobileNetSchool url={self._site_url!r} student={self._student_id}>"

    async def __aenter__(self) -> MobileNetSchool:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ── вход ─────────────────────────────────────────────────

    @property
    def token(self) -> Optional[MobileToken]:
        return self._token

    async def login(
        self, user_name: str, password: str, school: Union[int, str], *, timeout: Optional[float] = None,
    ) -> None:
        """Вход по логину и паролю «Сетевого города», как в приложении."""
        async with _new_client(
            base_url=self._site_url,
            proxy=self._proxy,
            timeout=timeout or self._timeout,
            transport=self._transport,
            headers={
                "User-Agent": WEBVIEW_USER_AGENT,
                "Referer": f"{self._site_url}/authorize/login?mobile&appVer={APP_VERSION}",
            },
        ) as http:
            code = await _web_login_code(http, school, user_name, password)
        token = await exchange_code(
            code, client_secret=self._client_secret, proxy=self._proxy,
            timeout=timeout or self._timeout, transport=self._transport,
        )
        await self.login_with_token(token)

    async def login_with_token(self, token: Union[MobileToken, str]) -> None:
        """Войти готовым токеном (строка ``access_token`` или :class:`MobileToken`)."""
        if isinstance(token, str):
            token = MobileToken(
                access_token=token,
                expires_at=datetime.datetime.now(datetime.timezone.utc) + timedelta(days=60),
            )
        self._token = token
        self._http.headers["Authorization"] = f"Bearer {token.access_token}"
        await self._load_profile()

    @classmethod
    async def from_web(
        cls,
        web: Any,
        *,
        client_secret: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> MobileNetSchool:
        """Мобильный клиент из уже вошедшего :class:`NetSchool` — без пароля.

        Работает и для входа через Госуслуги: код берётся из живой веб-сессии.
        """
        token = await web.mobile_token(client_secret=client_secret)
        client = cls(
            web._http.base_url, timeout=timeout, proxy=getattr(web, "_proxy", None),
            client_secret=client_secret,
        )
        await client.login_with_token(token)
        if getattr(web, "_student_id", -1) in {s.id for s in client._students}:
            client._student_id = web._student_id
        return client

    async def refresh(self) -> MobileToken:
        """Продлить токен (не проверено на живом сервере — см. :func:`refresh_token`)."""
        if self._token is None or not self._token.refresh_token:
            raise exceptions.SessionExpired("Нет refresh_token — войдите заново")
        previous = self._token.refresh_token
        token = await refresh_token(
            previous, client_secret=self._client_secret,
            proxy=self._proxy, timeout=self._timeout, transport=self._transport,
        )
        if not token.refresh_token:
            # Сервер может не выдать новый refresh_token — тогда прежний
            # остаётся рабочим, и терять его нельзя: без него следующее
            # продление невозможно.
            token = token.model_copy(update={"refresh_token": previous})
        self._token = token
        self._http.headers["Authorization"] = f"Bearer {token.access_token}"
        return token

    async def _load_profile(self) -> None:
        users = await self.users()
        if not users:
            raise exceptions.LoginError("Мобильное API не вернуло пользователя")
        self._user = users[0]
        orgs = self._user.get("organizations") or []
        active = next((o for o in orgs if o.get("isActive")), orgs[0] if orgs else {})
        self._school_id = (active.get("organization") or {}).get("id", -1)
        class_name = ((active.get("classes") or [{}])[0]).get("className")

        children = self._user.get("children") or []
        if children:
            self._students = [
                Student(
                    id=c.get("id") or c.get("studentId"),
                    name=c.get("nickName") or c.get("name") or c.get("firstName") or "",
                    group_name=c.get("className"),
                )
                for c in children
                if c.get("id") or c.get("studentId")
            ]
        else:
            self._students = [
                Student(id=self._user["id"], name=self._user.get("nickName") or "", group_name=class_name)
            ]
        if self._student_id not in {s.id for s in self._students}:
            self._student_id = self._students[0].id if self._students else -1

    # ── сессия ───────────────────────────────────────────────

    def export_session(self) -> str:
        """JSON для :meth:`import_session`. Содержит токен — храните как пароль."""
        return json.dumps(
            {
                "kind": "mobile",
                "version": 1,
                "url": self._site_url,
                "access_token": self._token.access_token if self._token else None,
                "refresh_token": self._token.refresh_token if self._token else "",
                "expires_at": self._token.expires_at.isoformat() if self._token else None,
                "student_id": self._student_id,
            },
            ensure_ascii=False,
        )

    async def import_session(self, data: str, *, timeout: Optional[float] = None) -> None:
        """Восстановить сессию из :meth:`export_session`.

        :raises SessionExpired: токен не принят или это не мобильная сессия.
        :raises ServerUnavailable: сервер не ответил — о токене это ничего не говорит.
        """
        payload = json.loads(data)
        if payload.get("kind") != "mobile" or not payload.get("access_token"):
            raise exceptions.SessionExpired("Это не сессия мобильного API")
        token = MobileToken(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token") or "",
            expires_at=_parse_datetime(payload["expires_at"]),
        )
        self._student_id = payload.get("student_id", -1)
        await self.login_with_token(token)

    async def is_alive(self) -> bool:
        """Принимает ли сервер токен. Недоступность сервера — не повод считать его мёртвым."""
        try:
            await self.users()
        except exceptions.SessionExpired:
            return False
        except exceptions.NetSchoolError:
            return True
        return True

    async def logout(self, *, timeout: Optional[float] = None) -> None:
        """Токен ИРТех не отзывается из приложения — просто забываем его."""
        self._token = None
        self._http.headers.pop("Authorization", None)

    async def close(self, *, timeout: Optional[float] = None) -> None:
        await self._http.aclose()

    # ── ученики ──────────────────────────────────────────────

    @property
    def students(self) -> List[Student]:
        return list(self._students)

    @property
    def current_student(self) -> Optional[Student]:
        return next((s for s in self._students if s.id == self._student_id), None)

    async def switch_student(self, index_or_id: int) -> Student:
        if 0 <= index_or_id < len(self._students):
            student = self._students[index_or_id]
        else:
            student = next((s for s in self._students if s.id == index_or_id), None)
            if student is None:
                raise ValueError(
                    f"Ученик с ID {index_or_id} не найден. Доступные ID: {[s.id for s in self._students]}"
                )
        self._student_id = student.id
        return student

    async def diary_init(self, *, timeout: Optional[float] = None) -> dict:
        """Список учеников в форме веб-ответа ``student/diary/init``."""
        if not self._students:
            await self._load_profile()
        return {
            "students": [
                {"studentId": s.id, "nickName": s.name, "className": s.group_name}
                for s in self._students
            ],
            "currentStudentId": self._student_id,
        }

    # ── транспорт ────────────────────────────────────────────

    async def _request(
        self, method: str, path: str, params: Optional[Params] = None, *,
        json_body: Any = None, files: Any = None,
    ) -> httpx.Response:
        query: Params = [*(params or []), ("lng", "ru"), ("appVersion", APP_VERSION)]
        extra: Dict[str, Any] = {}
        if json_body is not None:
            extra["json"] = json_body
        if files is not None:
            extra["files"] = files
        return await _send(self._http, method, path, params=query, **extra)

    async def _get(self, path: str, params: Optional[Params] = None) -> Any:
        return _json(await self._request("GET", path, params), path)

    async def _download(self, path: str) -> bytes:
        response = await self._request("GET", path)
        _raise_for(response, path)
        return response.content

    def _sid(self, student_id: Optional[int]) -> int:
        return student_id if student_id is not None else self._student_id

    async def _ensure_year(self) -> int:
        if self._year_id <= 0:
            years = await self.education()
            if years:
                self._year_id = years[0]["schoolyear"]["id"]
        return self._year_id

    # ══ Сырые методы мобильного API ═════════════════════════

    async def users(self) -> List[dict]:
        """``/users?v=2`` — профиль, школы, классы, дети."""
        return await self._get("/users", [("v", 2)])

    async def education(self, student_id: Optional[int] = None) -> List[dict]:
        """Годы обучения ученика; текущий — первый."""
        return await self._get("/education", [("studentId", self._sid(student_id))])

    async def terms(self, school_year_id: Optional[int] = None, student_id: Optional[int] = None) -> List[dict]:
        year = school_year_id or await self._ensure_year()
        return await self._get("/terms", [("studentId", self._sid(student_id)), ("schoolYearId", year)])

    async def subjects_raw(self, school_year_id: Optional[int] = None, student_id: Optional[int] = None) -> List[dict]:
        year = school_year_id or await self._ensure_year()
        return await self._get("/subjects", [("studentId", self._sid(student_id)), ("schoolYearId", year)])

    async def classmeetings(
        self, start: date, end: date, student_ids: Union[int, Iterable[int], None] = None,
    ) -> List[dict]:
        """Уроки за период (расписание, тема, кабинет, учителя, посещаемость)."""
        if student_ids is None:
            ids = [self._student_id]
        elif isinstance(student_ids, int):
            ids = [student_ids]
        else:
            ids = list(student_ids)
        return await self._get(
            "/classmeetings",
            [*(("studentIds", i) for i in ids), ("startDate", start.isoformat()), ("endDate", end.isoformat())],
        )

    async def classmeeting(self, classmeeting_id: int, student_id: Optional[int] = None) -> dict:
        return await self._get(f"/classmeetings/{classmeeting_id}", [("studentId", self._sid(student_id))])

    async def assignments(self, assignment_ids: Sequence[int], student_id: Optional[int] = None) -> List[dict]:
        """Задания по списку id (из ``classmeetings[].assignmentId``), пачками."""
        result: List[dict] = []
        ids = list(assignment_ids)
        for i in range(0, len(ids), _ASSIGNMENTS_BATCH):
            result += await self._get(
                "/assignments",
                [("studentId", self._sid(student_id)), *(("assignmentId", a) for a in ids[i:i + _ASSIGNMENTS_BATCH])],
            )
        return result

    async def assignment(self, assignment_id: int, student_id: Optional[int] = None) -> dict:
        return await self._get(f"/assignments/{assignment_id}", [("studentId", self._sid(student_id))])

    async def current_term_assignments(
        self,
        student_id: Optional[int] = None,
        *,
        without_marks: bool = True,
        with_expired_class_assign: bool = False,
        only_homeworks: bool = False,
    ) -> List[dict]:
        """Вкладка «Д/З»: задания текущей четверти (умолчания — как в приложении)."""
        return await self._get(
            "/assignments/for-current-term",
            [
                ("studentId", self._sid(student_id)),
                ("withoutMarks", str(without_marks).lower()),
                ("withExpiredClassAssign", str(with_expired_class_assign).lower()),
                ("onlyHomeWorks", str(only_homeworks).lower()),
            ],
        )

    async def assignment_answer(self, assignment_id: int, student_id: Optional[int] = None) -> dict:
        """Ответ ученика на задание: ``{text, files}``."""
        return await self._get(f"/assignments/{assignment_id}/answer", [("studentId", self._sid(student_id))])

    async def attachments_raw(self, assignment_id: int) -> List[dict]:
        return await self._get("/attachments", [("assignmentId", assignment_id)])

    async def period_events(
        self,
        subject_group_ids: Optional[Iterable[int]] = None,
        *,
        types: Iterable[str] = (),
        period_days: int = 14,
        limit: int = 100,
        offset: int = 0,
        student_id: Optional[int] = None,
    ) -> dict:
        """Лента «Д/З → События»: ``{"events": [{"type": …}, …]}``.

        Типы событий: ``ResultInfo`` — оценки, ``HomeworkInfo`` — задания,
        ``TermTotalInfo``/``YearTotalInfo`` — итоговые за период и за год.
        ``types`` оставляет только нужные; без него сервер отдаёт все вперемешку,
        и задания могут вытеснить оценки за ``limit``.

        Без ``subject_group_ids`` берутся все группы из расписания на ±2 недели.
        """
        if subject_group_ids is None:
            today = date.today()
            meetings = await self.classmeetings(today - timedelta(days=14), today + timedelta(days=14), student_id and [student_id])
            subject_group_ids = sorted({m["subjectGroupId"] for m in meetings if m.get("subjectGroupId")})
        return await self._get(
            "/period-events",
            [
                ("studentId", self._sid(student_id)),
                ("limit", limit),
                ("offset", offset),
                *(("SubjectGroupId", g) for g in subject_group_ids),
                *(("Types", t) for t in types),
                ("PeriodDays", period_days),
            ],
        )

    async def totals(self, school_year_id: Optional[int] = None, student_id: Optional[int] = None) -> List[dict]:
        """Оценки за периоды и год по всем предметам одним запросом."""
        year = school_year_id or await self._ensure_year()
        return await self._get("/totals", [("studentId", self._sid(student_id)), ("schoolYearId", year)])

    async def subject_performance(self, subject_id: int, term_id: int, student_id: Optional[int] = None) -> dict:
        """«Итоги по предмету»: средний балл ученика и класса, все оценки, пропуски."""
        return await self._get(
            "/analytics/subject-performance",
            [("studentId", self._sid(student_id)), ("subjectId", subject_id), ("termId", term_id)],
        )

    async def mail_messages(self, box: str = "INBOX", *, page: int = 1, page_size: int = 27) -> dict:
        """Письма папки ``INBOX``/``SENT``/``DRAFT`` (``DELETED`` — по аналогии, не проверялась)."""
        return await self._get(
            "/mail/messages",
            [
                ("userId", self._user_id),
                ("boxType", box),
                ("page", page),
                ("pageSize", page_size),
                ("expand", "text"),
                ("expand", "subject"),
                ("expand", "author"),
                ("OrderInfo.Field", "Sent"),
                ("OrderInfo.Ascending", "false"),
            ],
        )

    async def mail_unread_count(self) -> int:
        return await self._get("/mail/messages/unread-count", [("userId", self._user_id)])

    async def mail_read_raw(self, message_id: int) -> dict:
        """Письмо целиком. Помечает его прочитанным, как и в приложении."""
        response = await self._request("POST", f"/mail/messages/{message_id}/read", [("userId", self._user_id)])
        return _json(response, "mail/messages/{id}/read")

    async def upload_attachment(
        self, name: str, data: bytes, content_type: Optional[str] = None,
    ) -> int:
        """Загрузить файл для письма: ``POST /attachments?userId=…`` → id вложения.

        Multipart-поле ``file``; имя файла — обычным UTF-8. Приложение шлёт
        его URL-кодированным (``%D0%9F…``), но сервер хранит имя как есть, и
        получатель видел бы кракозябры: с сырым именем оно показывается
        правильно (проверено на живом сервере). Id кладётся в ``fileAttachmentIds`` при отправке (:meth:`mail_send_raw`).
        """
        mime = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        response = await self._request(
            "POST", "/attachments", [("userId", self._user_id)],
            files={"file": (name, data, mime)},
        )
        result = _json(response, "attachments (upload)")
        try:
            return int(result)
        except (TypeError, ValueError) as exc:
            raise exceptions.NetSchoolError(f"attachments (upload): неожиданный ответ {result!r}") from exc

    async def mail_send_raw(
        self,
        subject: str,
        text: str,
        to: Sequence[str],
        *,
        cc: Sequence[str] = (),
        bcc: Sequence[str] = (),
        attachment_ids: Sequence[int] = (),
        notify: bool = False,
    ) -> dict:
        """Отправить письмо: ``POST /mail/messages`` (как «Отправить» в приложении).

        ``to``/``cc``/``bcc`` — идентификаторы получателей из адресной книги
        (``MailRecipient.id``), ``attachment_ids`` — из :meth:`upload_attachment`.
        Ответ: ``{messageId, author, dateSent}``.
        """
        response = await self._request(
            "POST",
            "/mail/messages",
            json_body={
                "to": list(to),
                "cc": list(cc),
                "bcc": list(bcc),
                "text": text,
                "subject": subject,
                "notify": notify,
                "draft": False,
                "authorId": self._user_id,
                "fileAttachmentIds": list(attachment_ids),
            },
        )
        return _json(response, "mail/messages (send)")

    async def _mail_action(self, path: str, message_ids: Iterable[int], params: Params = ()) -> None:
        # Мобильное API ждёт id полями multipart-формы ``messageIds``; на
        # JSON-тело, как у веб-API, отвечает «Не переданы идентификаторы».
        ids = [("messageIds", (None, str(i))) for i in message_ids]
        if not ids:
            return
        response = await self._request("POST", path, [("userId", self._user_id), *params], files=ids)
        _raise_for(response, path)

    async def mail_mark_read(self, message_ids: Iterable[int], read: bool = True) -> None:
        """Пометить письма прочитанными (или непрочитанными при ``read=False``)."""
        await self._mail_action("/mail/messages/mark-as", message_ids, [("read", str(read).lower())])

    async def mail_delete(self, message_ids: Iterable[int], *, permanent: bool = False) -> None:
        """Переместить письма в «Удалённые»; ``permanent=True`` — удалить навсегда.

        Навсегда удаляются письма из «Удалённых»: сначала удалите их обычным
        вызовом.
        """
        await self._mail_action("/mail/messages/delete" if permanent else "/mail/messages/moving", message_ids)

    async def mail_restore(self, message_ids: Iterable[int]) -> None:
        """Вернуть письма из «Удалённых» в папки, где они были."""
        await self._mail_action("/mail/messages/restore", message_ids)

    async def mail_draft(self, message_id: int, action: str = "reply") -> MailDraft:
        """Заготовка ответа: ``"reply"``, ``"replyAll"`` (ученикам запрещено) или ``"forward"``."""
        return MailDraft.from_raw(
            await self._get(f"/mail/messages/{message_id}/edit", [("userId", self._user_id), ("action", action)])
        )

    async def recipient(self, user_id: int) -> MailRecipient:
        """Получатель по числовому id пользователя — например, автора письма."""
        return MailRecipient.from_raw(
            await self._get("/address-book/recipients/get-by-userid", [("userId", user_id)])
        )

    async def mail_block(self, user_id: int) -> None:
        """Не получать письма от пользователя.

        Сервер принимает запрос (200), но на живом сервере результат не
        подтверждён: блокировка системной учётки ``admin`` в
        :meth:`mail_blocked_users` не появилась.
        """
        response = await self._request("POST", "/mail/blocked-users", [("userId", self._user_id), ("authorId", user_id)])
        _raise_for(response, "mail/blocked-users")

    async def mail_unblock(self, user_id: int) -> None:
        """Снять блокировку :meth:`mail_block`."""
        response = await self._request("DELETE", "/mail/blocked-users", [("userId", self._user_id), ("authorId", user_id)])
        _raise_for(response, "mail/blocked-users")

    async def mail_blocked_users(self) -> List[dict]:
        return await self._get("/mail/blocked-users", [("userId", self._user_id)])

    async def address_book(self) -> List[dict]:
        return await self._get("/address-book", [("userId", self._user_id), ("orgId", self._school_id)])

    async def announcements_raw(self, student_id: Optional[int] = None) -> List[dict]:
        return await self._get("/announcements", [("studentId", self._sid(student_id))])

    async def push_settings(self) -> List[dict]:
        """Настройки push: ``Homework``, ``Result``, ``TermTotal``, ``YearTotal``."""
        return await self._get("/v2/push-notifications/settings")

    async def portfolios(self) -> List[dict]:
        return await self._get("/portfolios")

    async def portfolio(self, portfolio_id: int) -> dict:
        return await self._get(f"/portfolios/{portfolio_id}")

    async def download_portfolio_doc(self, portfolio_id: int, doc_id: int) -> bytes:
        return await self._download(f"/portfolios/{portfolio_id}/docs/{doc_id}")

    async def download_attachment_as_bytes(self, attachment_id: int, *, timeout: Optional[float] = None) -> bytes:
        return await self._download(f"/attachments/{attachment_id}")

    async def token_endpoints(self) -> List[dict]:
        """К каким серверам привязан токен: ``[{serverId, userId}]``."""
        response = await _send(
            self._http, "GET", f"{IDENTITY_URL}/users/endpoints",
            params={"lng": "ru", "appVersion": APP_VERSION},
        )
        return _json(response, "users/endpoints")

    @property
    def _user_id(self) -> int:
        # Почта привязана к учётной записи, а не к выбранному ребёнку.
        return self._user.get("id", self._student_id)

    # ══ Совместимость с NetSchool ═══════════════════════════

    async def diary(
        self, start: Optional[date] = None, end: Optional[date] = None, *, timeout: Optional[float] = None,
    ) -> Diary:
        """Дневник за период (по умолчанию — текущая неделя) в модели веб-клиента.

        Два-три запроса: уроки, задания к ним и вложения там, где они есть.
        """
        if not start:
            start = date.today() - timedelta(days=date.today().weekday())
        if not end:
            end = start + timedelta(days=5)
        meetings = await self.classmeetings(start, end)
        ids = [a for m in meetings for a in m.get("assignmentId") or []]
        raw_assignments = await self.assignments(ids) if ids else []
        # Порядок заданий внутри урока задаём сами — по списку урока. Сервер
        # отдаёт их в порядке ответа, который меняется от запроса к запросу, а
        # порядковый номер задания входит в ключ оценки у потребителей.
        position = {a: i for m in meetings for i, a in enumerate(m.get("assignmentId") or [])}
        by_meeting: Dict[int, List[dict]] = {}
        for item in sorted(raw_assignments, key=lambda a: position.get(a.get("assignmentId"), 1 << 30)):
            by_meeting.setdefault(item.get("classmeetingId"), []).append(item)

        attachments: Dict[int, List[Attachment]] = {}
        for item in raw_assignments:
            if item.get("attachmentsExists"):
                attachments[item["assignmentId"]] = await self.attachments(item["assignmentId"])

        days: Dict[date, List[Lesson]] = {}
        for meeting in meetings:
            lesson = _lesson_from_mobile(meeting, by_meeting.get(meeting["classmeetingId"], []), attachments)
            days.setdefault(lesson.day, []).append(lesson)
        return Diary(
            start=start,
            end=end,
            schedule=[
                Day(day=d, lessons=sorted(ls, key=lambda l: (l.start, l.number)))
                for d, ls in sorted(days.items())
            ],
        )

    async def attachments(self, assignment_id: int, *, timeout: Optional[float] = None) -> List[Attachment]:
        return [
            Attachment(id=a["attachmentId"], name=a.get("fileName") or "", description=a.get("description") or "")
            for a in await self.attachments_raw(assignment_id)
        ]

    async def download_attachment(self, attachment_id: int, buffer: BytesIO, *, timeout: Optional[float] = None) -> None:
        buffer.write(await self.download_attachment_as_bytes(attachment_id))

    async def assignment_details(self, assignment_id: int, *, timeout: Optional[float] = None) -> AssignmentDetails:
        data = await self.assignment(assignment_id)
        teachers: List[Teacher] = []
        if data.get("classmeetingId"):
            meeting = await self.classmeeting(data["classmeetingId"])
            teachers = [Teacher(id=t.get("id") or 0, name=t.get("name") or "") for t in meeting.get("teachers") or []]
        return AssignmentDetails(
            id=data["assignmentId"],
            name=data.get("assignmentName") or "",
            subject_group=data.get("subjectName") or "",
            teachers=teachers,
            weight=data.get("weight") or 1,
            date=_parse_date(data["dueDate"]) if data.get("dueDate") else None,
            description=data.get("description") or "",
            attachments=await self.attachments(assignment_id) if data.get("attachmentsExists") else [],
        )

    async def announcements(self, take: int = -1, *, timeout: Optional[float] = None) -> List[Announcement]:
        items = await self.announcements_raw()
        if take >= 0:
            items = items[:take]
        return [
            Announcement(
                id=a.get("announcementId") or 0,
                name=a.get("title") or "",
                author=Author(
                    id=(a.get("author") or {}).get("id") or 0,
                    full_name=(a.get("author") or {}).get("name") or "",
                    nickname=(a.get("author") or {}).get("name") or "",
                ),
                content=a.get("description") or "",
                post_date=_parse_datetime(a["postDate"]),
                attachments=[Attachment.from_raw(x) for x in a.get("attachments") or []],
            )
            for a in items
        ]

    async def mail_list(
        self, folder: str = "Inbox", page: int = 1, page_size: int = 20, *, timeout: Optional[float] = None,
    ) -> MailPage:
        data = await self.mail_messages(MAIL_BOXES.get(folder, folder.upper()), page=page, page_size=page_size)
        return MailPage(
            entries=[
                MailEntry(
                    id=int(m["id"]),
                    subject=m.get("subject") or "",
                    author=(m.get("author") or {}).get("name") or "",
                    sent=_parse_datetime(m["sent"]),
                    to_names=m.get("toNames"),
                    read=m.get("read"),
                )
                for m in data.get("items") or []
            ],
            page=data.get("page", page),
            total_items=data.get("totalItems", 0),
        )

    async def mail_unread(self, *, timeout: Optional[float] = None) -> List[int]:
        """ID непрочитанных писем (первые 100 входящих — мобильное API отдаёт флаг в списке)."""
        data = await self.mail_messages("INBOX", page=1, page_size=100)
        return [int(m["id"]) for m in data.get("items") or [] if not m.get("read")]

    async def mail_read(self, message_id: int, *, timeout: Optional[float] = None) -> Message:
        return Message.from_raw(await self.mail_read_raw(message_id))

    async def mail_recipients(self, *, timeout: Optional[float] = None) -> List[MailRecipient]:
        """Адресная книга, развёрнутая в плоский список без повторов."""
        seen: Dict[str, MailRecipient] = {}

        def walk(group: dict) -> None:
            for person in group.get("recipients") or []:
                if person.get("id") and person["id"] not in seen:
                    seen[person["id"]] = MailRecipient.from_raw(person)
            for sub in group.get("subGroups") or []:
                walk(sub)

        for group in await self.address_book():
            walk(group)
        return list(seen.values())

    async def mail_send(
        self,
        subject: str,
        text: str,
        to: List[str],
        *,
        attachments: Sequence[Tuple[str, bytes]] = (),
        notify: bool = False,
        timeout: Optional[float] = None,
    ) -> int:
        """Отправить письмо, при необходимости с файлами ``[(имя, содержимое), …]``, и вернуть его ID.

        Приложение дописывает к тексту подпись — мы не дописываем. Файлы
        загружаются до отправки; если загрузка одного из них не удалась,
        письмо не уходит.
        """
        if not to:
            raise exceptions.NetSchoolError("Не указан получатель письма")
        ids = [await self.upload_attachment(name, data) for name, data in attachments]
        sent = await self.mail_send_raw(subject, text, to, attachment_ids=ids, notify=notify)
        return int(sent["messageId"])

    async def mobile_token(self, *, client_secret: Optional[str] = None) -> MobileToken:
        if self._token is None:
            raise exceptions.SessionExpired("Нет токена мобильного API")
        return self._token


def _lesson_from_mobile(
    meeting: dict, raw_assignments: List[dict], attachments: Dict[int, List[Attachment]],
) -> Lesson:
    start = _parse_datetime(meeting["startTime"])
    end = _parse_datetime(meeting["endTime"])
    return Lesson(
        day=_parse_date(meeting["day"]),
        start=start.time(),
        end=end.time(),
        room=meeting.get("roomName") or "",
        number=meeting.get("scheduleTimeNumber") or meeting.get("order") or 0,
        subject=meeting.get("subjectName") or "",
        classmeeting_id=meeting.get("classmeetingId"),
        is_distance=meeting.get("distanceMeetingId") is not None,
        relay=meeting.get("scheduleTimeRelay"),
        teacher=", ".join(t.get("name") or "" for t in meeting.get("teachers") or [] if t.get("name")),
        topic=meeting.get("lessonTheme") or "",
        absence=meeting.get("attendance") or "",
        extra_activity=meeting.get("extraActivity") is True,
        add_education=meeting.get("addEducation") is True,
        assignments=[_assignment_from_mobile(a, attachments.get(a["assignmentId"], [])) for a in raw_assignments],
    )


def _assignment_from_mobile(data: dict, attachments: List[Attachment]) -> Assignment:
    raw = data.get("result")
    mark: Optional[int] = None
    mark_text: Optional[str] = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        mark = int(raw) if float(raw).is_integer() else None
        mark_text = str(mark) if mark is not None else str(raw)
    elif isinstance(raw, str) and raw.strip():
        mark_text = raw.strip()
        mark = int(mark_text) if mark_text.isdigit() else None
    return Assignment(
        id=data["assignmentId"],
        comment=data.get("comment") or "",
        kind=data.get("assignmentTypeName") or "",
        kind_abbr=data.get("assignmentTypeAbbr") or "",
        content=data.get("assignmentName") or "",
        mark=mark,
        mark_text=mark_text,
        weight=data.get("weight") or 1,
        is_duty=bool(data.get("duty")),
        deadline=_parse_date(data["dueDate"]) if data.get("dueDate") else None,
        mark_set_date=_parse_date(data["resultDate"]) if data.get("resultDate") else None,
        published_at=_parse_datetime(data["assignmentDate"]) if data.get("assignmentDate") else None,
        attachments=attachments,
    )

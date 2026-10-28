"""Асинхронный клиент «Госуслуги Моя школа» (gosuslugi.ru/school).

Данные отдаёт веб-API ``/api/myschool``. Войти можно двумя способами:
по cookies браузера, где пользователь вошёл сам (:meth:`MySchool.login_with_cookies`:
страницы входа портала защищены проверкой браузера), или логином и паролем
Госуслуг по схеме мобильного приложения (:meth:`MySchool.login_via_gosuslugi_mobile`),
тогда запросы авторизуются заголовком ``Authorization: Bearer``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

import httpx

from . import _esia, exceptions
from .models import Homework, Lesson, Mark, Student

BASE_URL = "https://www.gosuslugi.ru"
API = "/api/myschool"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

# Типы объектов витрины ``/v1/datamart``; неизвестный тип сервер отклоняет с 400.
LESSONS = "student_lessons"
MARKS = "student_marks"
HOMEWORKS = "student_homeworks_materials"
EVENTS = "student_events"


def interval_id(day: dt.date) -> str:
    """Идентификатор недели витрины: ISO-неделя и год, ``2026-10-28`` → ``442026``."""
    year, week, _ = day.isocalendar()
    return f"{week}{year}"


def week_intervals(start: dt.date, end: dt.date) -> list[str]:
    """Идентификаторы всех недель, которые пересекает отрезок ``start..end``."""
    ids: list[str] = []
    day = start - dt.timedelta(days=start.weekday())
    while day <= end:
        ident = interval_id(day)
        if ident not in ids:
            ids.append(ident)
        day += dt.timedelta(days=7)
    return ids


class MySchool:
    """Клиент «Моей школы».

    >>> async with MySchool() as ms:
    ...     await ms.login_with_cookies("name=value; name2=value2")
    ...     student = (await ms.students())[0]
    ...     lessons = await ms.lessons(student, dt.date.today())
    """

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30,
        ssl_context: ssl.SSLContext | bool = True,
        proxy: str | None = None,
        not_ready_retries: int = 5,
        not_ready_delay: float = 1.0,
        keepalive_interval: float | None = 600,
        on_session_update: Optional[Callable[[str], Awaitable[None] | None]] = None,
        on_session_expired: Optional[Callable[[], Awaitable[None] | None]] = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            verify=ssl_context,
            proxy=proxy,
            follow_redirects=False,
            headers={"user-agent": _USER_AGENT, "accept": "application/json"},
        )
        self._retries = not_ready_retries
        self._delay = not_ready_delay
        self._authed = False
        self._ssl_context = ssl_context
        self._proxy = proxy
        self._timeout = timeout
        self._bearer: _esia.Tokens | None = None
        self._flow_transport: httpx.AsyncBaseTransport | None = None  # для тестов
        self._keepalive_interval = keepalive_interval
        self._on_update = on_session_update
        self._on_expired = on_session_expired
        self._keepalive_task: asyncio.Task | None = None

    async def __aenter__(self) -> "MySchool":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        self.stop_keepalive()
        await self._http.aclose()

    # ── сессия ──────────────────────────────────────────────

    async def login_with_cookies(self, cookies: str | Mapping[str, str]) -> None:
        """Подставляет cookies сессии Госуслуг и проверяет, что она жива.

        :param cookies: строка заголовка ``Cookie`` (``a=1; b=2``) или словарь.
        :raises SessionExpired: cookies не подошли.
        """
        self._clear_auth()
        for name, value in self._parse_cookies(cookies).items():
            self._http.cookies.set(name, value, domain="www.gosuslugi.ru")
        self._authed = True
        if not await self.is_alive():
            self._authed = False
            raise exceptions.SessionExpired("Госуслуги не приняли cookies: войдите заново")
        self.start_keepalive()

    def _clear_auth(self) -> None:
        self._http.cookies.clear()
        self._http.headers.pop("authorization", None)
        self._bearer = None

    async def login_via_gosuslugi_mobile(
        self,
        esia_login: str | None = None,
        esia_password: str | None = None,
        *,
        otp_callback: Optional[_esia.OtpCallback] = None,
    ) -> None:
        """Вход логином и паролем Госуслуг по схеме мобильного приложения, без браузера.

        OAuth2 + PKCE: портал выдаёт ``client_secret``, ЕСИА принимает логин
        (с подтверждением кодом, если оно включено) и обменивает ``fat_code``
        на ``access_token``, который подставляется в ``Authorization``.

        :param otp_callback: ``async def(mfa_type, mfa_info) -> str`` — код из SMS,
            аутентификатора или «Макса» (``mfa_type`` — ``SMS``/``TOTP``/``MAX``).
            Без него вход с подтверждением кодом бросает :class:`MFAError`.
        :raises InvalidCredentials: неверный логин или пароль.
        :raises AccountLocked: вход заблокирован или нужна капча.
        :raises MFAError: код не введён или неверен.
        :raises ESIAError: ЕСИА ответила неожиданно или шаг нельзя пройти.
        """
        if not esia_login or not esia_password:
            raise exceptions.LoginError("Логин и пароль не могут быть пустыми")
        async with httpx.AsyncClient(
            headers={"user-agent": _esia.USER_AGENT, "accept": "application/json"},
            follow_redirects=False,
            verify=self._ssl_context,
            proxy=self._proxy,
            timeout=self._timeout,
            transport=self._flow_transport,
        ) as flow_http:
            tokens = await _esia.mobile_login(flow_http, esia_login, esia_password, otp_callback)
        await self._use_bearer(tokens)

    async def _use_bearer(self, tokens: "_esia.Tokens") -> None:
        self._clear_auth()
        self._bearer = tokens
        self._http.headers["authorization"] = f"Bearer {tokens.access_token}"
        self._authed = True
        if not await self.is_alive():
            self._authed = False
            raise exceptions.SessionExpired("Госуслуги не приняли токен: войдите заново")
        self.start_keepalive()

    @staticmethod
    def _parse_cookies(raw: str | Mapping[str, str]) -> dict[str, str]:
        if isinstance(raw, Mapping):
            return dict(raw)
        result: dict[str, str] = {}
        for part in raw.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name:
                result[name] = value
        if not result:
            raise exceptions.MySchoolError("В строке cookies нет ни одной пары имя=значение")
        return result

    def export_session(self) -> str:
        """Сессия в виде JSON, чтобы сохранить и позже вернуть через :meth:`import_session`.

        После входа по cookies это словарь cookies (формат не менялся); после
        мобильного входа — объект с ``mode: "bearer"``, токеном и ``refresh_token``.
        Это секрет.
        """
        if self._bearer is not None:
            t = self._bearer
            return json.dumps({
                "mode": "bearer",
                "access_token": t.access_token,
                "refresh_token": t.refresh_token,
                "expires_in": t.expires_in,
                "oid": t.oid,
                "device_id": t.device_id,
                "instance_id": t.instance_id,
            }, ensure_ascii=False)
        return json.dumps(dict(self._http.cookies.items()), ensure_ascii=False)

    async def import_session(self, data: str) -> None:
        """Восстанавливает сессию из :meth:`export_session` (словарь cookies или bearer-сессия)."""
        parsed = json.loads(data)
        mode = parsed.get("mode") if isinstance(parsed, dict) else None
        if mode == "bearer":
            if not parsed.get("access_token"):
                raise exceptions.MySchoolError("В сохранённой сессии нет access_token")
            await self._use_bearer(_esia.Tokens(
                access_token=parsed["access_token"],
                refresh_token=parsed.get("refresh_token", ""),
                expires_in=int(parsed.get("expires_in") or 0),
                oid=parsed.get("oid", ""),
                device_id=parsed.get("device_id", ""),
                instance_id=parsed.get("instance_id", ""),
            ))
        else:
            await self.login_with_cookies(parsed)

    def start_keepalive(self) -> None:
        """Раз в ``keepalive_interval`` секунд обращается к API, чтобы сессия не истекла.

        Срок cookie скользящий и продлевается каждым запросом. Обновлённые cookies
        отдаются в ``on_session_update`` (JSON из :meth:`export_session`), чтобы
        сохранить их; если Госуслуги сессию отозвали, вызывается ``on_session_expired``.
        """
        if not self._keepalive_interval or (
            self._keepalive_task and not self._keepalive_task.done()
        ):
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def stop_keepalive(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None

    async def _notify(self, callback: Optional[Callable[..., Any]], *args: Any) -> None:
        if callback is None:
            return
        result = callback(*args)
        if asyncio.iscoroutine(result):
            await result

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(self._keepalive_interval)
            try:
                await self._request("GET", f"{API}/v1/current_year")
            except exceptions.SessionExpired:
                self._authed = False
                await self._notify(self._on_expired)
                return
            except exceptions.MySchoolError:
                continue  # временный сбой: попробуем в следующий раз
            await self._notify(self._on_update, self.export_session())

    async def is_alive(self) -> bool:
        try:
            await self._request("GET", f"{API}/v1/current_year")
        except exceptions.SessionExpired:
            return False
        return True

    # ── низкоуровневые запросы ──────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise exceptions.ServerUnavailable(f"Госуслуги не ответили: {exc}") from exc
        status = response.status_code
        if status == 401:
            raise exceptions.SessionExpired("Сессия Госуслуг недействительна (401)")
        if status == 403:
            raise exceptions.AccessDenied(f"Доступ запрещён: {path}")
        if status == 404:
            raise exceptions.NotFound(self._error_text(response) or f"Не найдено: {path}")
        if status >= 400:
            raise exceptions.ServerUnavailable(
                f"{path}: HTTP {status} {self._error_text(response)}".strip()
            )
        return response

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        return str(body.get("error_description") or body.get("error") or "") if isinstance(body, dict) else ""

    async def _get_json(self, path: str, **params: Any) -> Any:
        return (await self._request("GET", path, params=params or None)).json()

    # ── ученики ─────────────────────────────────────────────

    async def students(self, role: str = "student") -> list[Student]:
        """Ученики аккаунта: для ``role="student"`` сам ученик, для ``"parent"`` его дети."""
        data = await self._get_json(f"{API}/v2/auth/student", role=role)
        return [Student.model_validate(item) for item in data]

    async def current_year(self) -> str:
        return (await self._get_json(f"{API}/v1/current_year"))["current_year"]

    async def regions(self) -> list[dict]:
        """Регионы, подключённые к «Моей школе»."""
        return await self._get_json(f"{API}/v1/regions")

    # ── витрина данных ──────────────────────────────────────

    async def datamart(
        self,
        obj_type: str,
        student: Student | str,
        intervals: Iterable[str],
    ) -> dict[str, list[dict]]:
        """Записи витрины ``obj_type`` по неделям: ``{interval_id: [запись, ...]}``.

        Сервер отвечает 202, пока готовит данные; запрос повторяется
        ``not_ready_retries`` раз с растущей паузой.

        :raises DataNotReady: данные не появились за время ожидания.
        """
        student_id = student if isinstance(student, str) else student.id
        pending = list(dict.fromkeys(intervals))
        result: dict[str, list[dict]] = {}
        for attempt in range(self._retries + 1):
            body = [{"interval_id": i, "obj_type": obj_type, "student_id": student_id} for i in pending]
            response = await self._request("POST", f"{API}/v1/datamart", json=body)
            if response.status_code == 202 or not response.content:
                payload: list[dict] = []
            else:
                payload = response.json()
            ready = {
                item["interval_id"]: item.get("data") or []
                for item in payload
                if item.get("obj_type") == obj_type and item.get("interval_id") in pending
            }
            result.update(ready)
            pending = [i for i in pending if i not in ready]
            if not pending:
                return result
            if attempt < self._retries:
                await asyncio.sleep(self._delay * (attempt + 1))
        raise exceptions.DataNotReady(f"{obj_type}: нет данных за недели {', '.join(pending)}")

    async def lessons(
        self, student: Student | str, start: dt.date, end: dt.date | None = None
    ) -> list[Lesson]:
        """Уроки в отрезке дат (по умолчанию неделя с ``start``), по времени начала."""
        start, end = self._span(start, end)
        raw = await self.datamart(LESSONS, student, week_intervals(start, end))
        items = [Lesson.model_validate(r) for rows in raw.values() for r in rows]
        items = [x for x in items if x.start and start <= x.start.date() <= end]
        return sorted(items, key=lambda x: x.start)

    async def marks(
        self, student: Student | str, start: dt.date, end: dt.date | None = None
    ) -> list[Mark]:
        """Оценки, выставленные в отрезке дат, по дате."""
        start, end = self._span(start, end)
        raw = await self.datamart(MARKS, student, week_intervals(start, end))
        items = [Mark.model_validate(r) for rows in raw.values() for r in rows]
        return sorted((m for m in items if m.date and start <= m.date <= end), key=lambda m: m.date)

    async def homeworks(
        self, student: Student | str, start: dt.date, end: dt.date | None = None
    ) -> list[Homework]:
        """Домашние задания, срок сдачи которых попадает в отрезок дат."""
        start, end = self._span(start, end)
        student_id = student if isinstance(student, str) else student.id
        intervals = week_intervals(start, end)
        raw = await self.datamart(HOMEWORKS, student_id, intervals)
        done = await self._completed(student_id, intervals)
        items = [Homework.model_validate(r) for rows in raw.values() for r in rows]
        for item in items:
            item.completed = done.get(item.id)
        return sorted(
            (h for h in items if h.plan_ready_date and start <= h.plan_ready_date <= end),
            key=lambda h: h.plan_ready_date,
        )

    async def _completed(self, student_id: str, intervals: list[str]) -> dict[str, bool]:
        data = await self._get_json(
            f"{API}/v2/customservices/{student_id}", object="homework", intervalId=intervals
        )
        return {h["homework_id"]: bool(h.get("completed")) for h in data.get("homeworks", [])}

    @staticmethod
    def _span(start: dt.date, end: dt.date | None) -> tuple[dt.date, dt.date]:
        if end is None:
            start = start - dt.timedelta(days=start.weekday())
            end = start + dt.timedelta(days=6)
        if end < start:
            raise ValueError("Конец отрезка раньше начала")
        return start, end

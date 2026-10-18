"""HTTP-обёртка поверх httpx с таймаутами и повтором при ReadTimeout."""

from __future__ import annotations

import asyncio
import functools
import logging
import ssl
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from netschoolpy.exceptions import ServerUnavailable

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 5   # секунд (прямое соединение)
_TOR_TIMEOUT = 30      # секунд (Tor медленнее)

# SOCKS5-прокси Tor (локальный, поднятый системой)
_TOR_PROXY = "socks5://127.0.0.1:9050"

# Сколько секунд хост после неудачи прямого соединения ходит через Tor.
_TOR_HOST_TTL = 600

# Повторять при 5xx и таймауте можно только запросы, которые ничего не меняют
# на сервере: иначе письмо или ответ на задание уйдёт дважды. auth/getdata —
# POST, но только выдаёт соль для входа.
_SAFE_POST_PATHS = frozenset({"auth/getdata"})

_RUSSIAN_CA = Path(__file__).resolve().parent / "certs" / "russian_trusted_ca.pem"


@functools.cache
def trusted_ssl_context() -> ssl.SSLContext:
    """Проверяющий сертификаты контекст: обычные центры плюс центр Минцифры.

    Часть школьных серверов подписана «Russian Trusted Sub CA», которого нет
    в стандартном наборе.
    """
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover - certifi приходит с httpx
        ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(_RUSSIAN_CA))
    return ctx


class _ExpiringHostSet(set):
    """Множество хостов, где пометка сама снимается через ``ttl`` секунд."""

    def __init__(self, ttl: float) -> None:
        super().__init__()
        self._ttl = ttl
        self._added: dict[str, float] = {}

    def add(self, item: str) -> None:
        self._added[item] = time.monotonic()
        super().add(item)

    def discard(self, item: str) -> None:
        self._added.pop(item, None)
        super().discard(item)

    def clear(self) -> None:
        self._added.clear()
        super().clear()

    def __contains__(self, item: object) -> bool:
        added_at = self._added.get(item)  # type: ignore[arg-type]
        if added_at is not None and time.monotonic() - added_at > self._ttl:
            self.discard(item)  # type: ignore[arg-type]
            return False
        return super().__contains__(item)


# Хосты, для которых прямое соединение не работает → нужен Tor
_tor_hosts: set[str] = _ExpiringHostSet(_TOR_HOST_TTL)


async def _proxy_reachable(proxy_url: str, timeout: float = 1.0) -> bool:
    parsed = urlparse(proxy_url)
    if not parsed.hostname or not parsed.port:
        return False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, parsed.port), timeout,
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


def _is_safe(method: str, path: str) -> bool:
    return method == "GET" or path.strip("/") in _SAFE_POST_PATHS


class HttpSession:
    """Тонкая обёртка вокруг ``httpx.AsyncClient``.

    • При ``ReadTimeout`` и 5xx повторяет только GET: POST мог уже
      выполниться, повтор отправил бы письмо или ответ дважды.
    • При ``ConnectError``/``ConnectTimeout`` — повторяет через Tor
      (socks5://127.0.0.1:9050), если он запущен. Хост ходит через Tor
      ``_TOR_HOST_TTL`` секунд, потом снова пробуем напрямую.
      Полезно для региональных серверов СГО, блокирующих datacenter IP.
    • Если общий таймаут ``timeout`` исчерпан — бросает ``ServerUnavailable``.
    • ``proxy`` — необязательный SOCKS5/HTTP прокси-URL (например,
      ``socks5://127.0.0.1:1080``). Если задан — все запросы идут через него
      (Tor-fallback отключается). Полезно для индивидуального решения с VLESS.
    """

    def __init__(self, base_url: str, *, timeout: int | None = None,
                 proxy: str | None = None):
        self._base_url = base_url.rstrip("/")
        self._external_proxy = proxy
        self._client = self._create_async_client(
            base_url=f"{self._base_url}/webapi",
            headers={
                "user-agent": "NetSchoolPy/1.0",
                "referer": self._base_url,
            },
            proxy=proxy if proxy else None,
            event_hooks={"response": [self._check_status]},
        )
        self._timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT

    @staticmethod
    def _create_async_client(**kwargs: Any) -> httpx.AsyncClient:
        """Создать AsyncClient с поддержкой разных версий httpx.

        В новых версиях используется параметр ``proxy``, в старых —
        ``proxies``. Поддерживаем оба варианта.
        """
        try:
            return httpx.AsyncClient(**kwargs)
        except TypeError as exc:
            proxy = kwargs.get("proxy")
            if proxy is None or "proxy" not in str(exc):
                raise
            legacy_kwargs = dict(kwargs)
            legacy_kwargs.pop("proxy", None)
            legacy_kwargs["proxies"] = proxy
            return httpx.AsyncClient(**legacy_kwargs)

    # ── публичные свойства ───────────────────────────────────

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def client(self) -> httpx.AsyncClient:
        """Прямой доступ к ``httpx.AsyncClient`` (для куки и т.п.)."""
        return self._client

    # ── удобные мутаторы ─────────────────────────────────────

    def set_header(self, key: str, value: str) -> None:
        self._client.headers[key] = value

    def remove_header(self, key: str) -> None:
        self._client.headers.pop(key, None)

    def set_cookie(self, name: str, value: str) -> None:
        self._client.cookies.set(name, value)

    # ── HTTP-методы ──────────────────────────────────────────

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
        follow_redirects: bool = False,
        raw: bool = False,
    ) -> httpx.Response:
        """GET-запрос.

        :param raw: Если ``True`` — путь относительно корня сервера,
            минуя ``/webapi`` префикс.
        """
        if raw:
            path = f"/{path.lstrip('/')}"
        return await self._send(
            "GET", path, params=params,
            timeout=timeout, follow_redirects=follow_redirects,
        )

    async def post(
        self,
        path: str,
        *,
        data: Any | None = None,
        json: Any | None = None,
        files: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        raw: bool = False,
    ) -> httpx.Response:
        """POST-запрос.

        :param raw: Если ``True`` — путь относительно корня сервера,
            минуя ``/webapi`` префикс.
        """
        if raw:
            path = f"/{path.lstrip('/')}"
        return await self._send(
            "POST", path, data=data, json=json, files=files,
            params=params, headers=headers, timeout=timeout,
        )

    async def close(self) -> None:
        await self._client.aclose()
        if hasattr(self, "_tor_client"):
            await self._tor_client.aclose()

    # ── внутренняя механика ──────────────────────────────────

    def _get_active_client(self) -> httpx.AsyncClient:
        """Возвращает Tor-клиент если хост заблокирован, иначе обычный."""
        import httpx as _httpx
        host = self._base_url
        if host in _tor_hosts:
            if not hasattr(self, "_tor_client"):
                log.info("🧅 Tor fallback activated for %s", host)
                self._tor_client = self._create_async_client(
                    base_url=f"{host}/webapi",
                    headers={
                        "user-agent": "NetSchoolPy/1.0",
                        "referer": host,
                    },
                    proxy=_TOR_PROXY,
                    event_hooks={"response": [self._check_status]},
                )
            return self._tor_client
        return self._client

    async def _send(
        self,
        method: str,
        path: str,
        *,
        timeout: int | None = None,
        follow_redirects: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        direct_timeout = timeout if timeout is not None else self._timeout
        max_5xx_retries = 3
        safe = _is_safe(method, path)

        async def _do_request(client: httpx.AsyncClient) -> httpx.Response:
            retry_5xx = 0
            while True:
                try:
                    req = client.build_request(
                        method, path,
                        **{k: v for k, v in kwargs.items() if v is not None},
                    )
                    return await client.send(req, follow_redirects=follow_redirects)
                except httpx.ReadTimeout:
                    if not safe:
                        raise ServerUnavailable(
                            "Сервер не ответил вовремя; запрос мог быть выполнен"
                        ) from None
                    await asyncio.sleep(0.1)
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if (safe and 500 <= status < 600
                            and retry_5xx < max_5xx_retries):
                        retry_5xx += 1
                        await asyncio.sleep(0.2 * retry_5xx)
                        continue
                    raise

        # Если хост уже помечен как требующий Tor — идём сразу через него
        if self._base_url in _tor_hosts and not self._external_proxy:
            try:
                return await asyncio.wait_for(
                    _do_request(self._get_active_client()), _TOR_TIMEOUT
                )
            except asyncio.TimeoutError:
                raise ServerUnavailable("Сервер не ответил (Tor)") from None

        # Если задан внешний прокси — идём только через него (Tor не нужен)
        if self._external_proxy:
            proxy_timeout = _TOR_TIMEOUT
            try:
                return await asyncio.wait_for(
                    _do_request(self._client), proxy_timeout
                )
            except asyncio.TimeoutError:
                raise ServerUnavailable("Сервер не ответил (прокси)") from None
            except (httpx.ConnectError, httpx.ProxyError) as exc:
                raise ServerUnavailable(f"Прокси недоступен: {exc}") from exc

        # Пробуем прямое соединение
        try:
            return await asyncio.wait_for(
                _do_request(self._client), direct_timeout
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            reason: BaseException = exc
        except asyncio.TimeoutError as exc:
            # Таймаут мог наступить, когда запрос уже ушёл на сервер:
            # изменяющий запрос через Tor не повторяем.
            if not safe:
                raise ServerUnavailable(
                    "Сервер не ответил вовремя; запрос мог быть выполнен"
                ) from None
            reason = exc

        # Прямое не сработало — пробуем через Tor, если он запущен
        if not await _proxy_reachable(_TOR_PROXY):
            raise ServerUnavailable(
                f"Сервер не отвечает, а Tor ({_TOR_PROXY}) не запущен"
            ) from reason
        if self._base_url not in _tor_hosts:
            log.warning(
                "🧅 Direct connection failed for %s, retrying via Tor...",
                self._base_url,
            )
            _tor_hosts.add(self._base_url)
        try:
            return await asyncio.wait_for(
                _do_request(self._get_active_client()), _TOR_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise ServerUnavailable("Сервер не ответил (Tor)") from None

    @staticmethod
    async def _check_status(response: httpx.Response) -> None:
        if not response.is_redirect:
            if 500 <= response.status_code < 600:
                log.warning(
                    "Server error %d for %s",
                    response.status_code, response.url,
                )
            if response.is_error:
                # Хук срабатывает до чтения тела: без этого текст ошибки
                # (например, «Неправильный пароль» в 409) недоступен.
                await response.aread()
            response.raise_for_status()

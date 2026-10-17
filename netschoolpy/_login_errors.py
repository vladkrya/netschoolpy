"""Разбор отказов при входе: какое исключение бросить по ответу сервера.

Общий для веб-клиента и мобильного входа. Причину называет класс
исключения, а не слова в тексте, — так её видит вызывающий код.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator, Optional, Union

import httpx

from netschoolpy import exceptions

# Блокировка за частые попытки. Проверяется раньше неверного пароля.
# sgo.e-mordovia.ru после трёх неверных паролей пишет: «Вы совершили 3
# неудачные попытки входа. Следующая попытка может быть совершена не ранее
# чем через минуту» — и не пускает даже с верным паролем.
# «Осталось попыток: 2» блокировкой не считается.
_LOCK_MARKERS = (
    "неудачные попытки", "неудачных попыт", "следующая попытка", "не ранее чем",
    "заблокир", "блокировк", "превышен", "слишком много", "слишком част",
    "повторите попытку через", "попробуйте через", "попыток не осталось",
    "осталось попыток: 0", "капч", "captcha", "locked",
)
# Техработы, о которых сервер сообщает 409 вместо 503.
_MAINTENANCE_MARKERS = ("техническ", "профилактическ", "maintenance")
# Отказ, который новым паролем не исправить.
_REJECT_MARKERS = (
    "нет доступа", "доступ запрещ", "доступ закрыт", "вход запрещ",
    "запрещен вход", "запрещён вход", "нет прав", "не имеет прав",
    "лицензи", "отключен", "неактивн", "деактив",
)

_ESIA_CREDENTIALS = {
    "INVALID_PASSWORD": "Неверный пароль",
    "INVALID_LOGIN": "Неверный логин",
    "ACCOUNT_NOT_FOUND": "Аккаунт не найден",
}
_ESIA_LOCKED = {
    "ACCOUNT_LOCKED": "Аккаунт заблокирован",
    "CAPTCHA_REQUIRED": "Требуется captcha (слишком много попыток)",
}


def conflict_error(
    message: Optional[str],
) -> Union[exceptions.LoginError, exceptions.ServerUnavailable]:
    """Исключение для 409 на ``/webapi/auth/login``.

    Различить причину можно только по тексту ``message``. Если он ни на что
    не похож, считаем, что не подошёл пароль: так школа отвечает почти всегда.
    """
    text = (message or "").strip()
    low = text.lower()
    if any(m in low for m in _LOCK_MARKERS):
        return exceptions.AccountLocked(text)
    if any(m in low for m in _MAINTENANCE_MARKERS):
        return exceptions.ServerUnavailable(text)
    if any(m in low for m in _REJECT_MARKERS):
        return exceptions.LoginRejected(text)
    return exceptions.InvalidCredentials(text or "Неверный логин или пароль")


def conflict_message(response: httpx.Response) -> Optional[str]:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("message") if isinstance(body, dict) else None


def esia_failed_error(code: str) -> exceptions.ESIAError:
    """Исключение для ``{"failed": CODE}`` в ответе Госуслуг на логин."""
    if code in _ESIA_CREDENTIALS:
        return exceptions.ESIAInvalidCredentials(f"Ошибка ESIA: {_ESIA_CREDENTIALS[code]}")
    if code in _ESIA_LOCKED:
        return exceptions.ESIAAccountLocked(f"Ошибка ESIA: {_ESIA_LOCKED[code]}")
    return exceptions.ESIAError(f"Ошибка ESIA: {code}")


def status_error(
    status: int, what: str, text: str = "",
) -> Union[exceptions.LoginRejected, exceptions.ServerUnavailable]:
    """Неуспешный HTTP-статус во время входа (кроме 409 — см. :func:`conflict_error`)."""
    detail = f"{what}: сервер ответил {status}"
    if status >= 500 or status == httpx.codes.TOO_MANY_REQUESTS:
        return exceptions.ServerUnavailable(detail)
    return exceptions.LoginRejected(f"{detail} {text[:200]}".rstrip())


@contextmanager
def server_errors(what: str) -> Iterator[None]:
    """Обрывы, таймауты, 5xx и HTML вместо JSON → :class:`ServerUnavailable`.

    Без этого вход падал голым ``httpx``-исключением или ``JSONDecodeError``
    — по ним не понять, что дело в сервере, а не в пароле.
    """
    try:
        yield
    except httpx.HTTPStatusError as exc:
        raise status_error(exc.response.status_code, what, exc.response.text) from exc
    except httpx.TransportError as exc:
        raise exceptions.ServerUnavailable(f"{what}: {exc!r}") from exc
    except json.JSONDecodeError as exc:
        raise exceptions.ServerUnavailable(
            f"{what}: сервер ответил не JSON — возможно, идут техработы"
        ) from exc

"""Исключения netschoolpy."""


class NetSchoolError(Exception):
    """Базовое исключение библиотеки."""


class LoginError(NetSchoolError):
    """Ошибка авторизации (неверные данные, школа не найдена и т.д.)."""


class MFAError(LoginError):
    """Ошибка двухфакторной аутентификации (неверный код, таймаут и т.д.)."""


class ESIAError(LoginError):
    """Ошибка на стороне Госуслуг (ESIA недоступна, неожиданный ответ)."""


class SchoolNotFound(LoginError):
    """Школа или организация с указанным названием не найдена."""


class InvalidCredentials(LoginError):
    """Сервер ответил, что логин или пароль неверны. Нужен вход заново."""


class AccountLocked(LoginError):
    """Аккаунт заблокирован: слишком много попыток входа или нужна капча."""


class LoginRejected(LoginError):
    """Сервер отказал во входе по другой причине; его текст — в сообщении."""


class ESIAInvalidCredentials(ESIAError, InvalidCredentials):
    """Госуслуги не приняли логин или пароль (``INVALID_PASSWORD`` и т.п.)."""


class ESIAAccountLocked(ESIAError, AccountLocked):
    """Госуслуги заблокировали вход (``ACCOUNT_LOCKED``, ``CAPTCHA_REQUIRED``)."""


class SessionExpired(NetSchoolError):
    """Сессия истекла (HTTP 401). Необходимо повторно авторизоваться."""


class RefreshTokenRejected(LoginRejected, SessionExpired):
    """identity.ir-tech.ru не продлил токен: refresh_token истёк или отозван.

    Это и :class:`SessionExpired` (нужен новый вход), и :class:`LoginError`
    (так эта ошибка ловилась до 9.6.0).
    """


class ServerUnavailable(NetSchoolError):
    """Сервер не ответил, оборвал соединение, вернул 5xx или страницу техработ."""

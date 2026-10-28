"""Исключения myschoolpy."""


class MySchoolError(Exception):
    """Базовое исключение библиотеки."""


class SessionExpired(MySchoolError):
    """Сессия Госуслуг недействительна или истекла (HTTP 401). Нужен новый вход."""


class AccessDenied(MySchoolError):
    """Доступ запрещён (HTTP 403): нет прав на данные этого ученика."""


class NotFound(MySchoolError):
    """Данные не найдены (HTTP 404), например у родителя нет привязанных детей."""


class DataNotReady(MySchoolError):
    """Витрина ещё готовит данные за интервал (HTTP 202) и не успела за время ожидания."""


class ServerUnavailable(MySchoolError):
    """Госуслуги ответили ошибкой сервера или не ответили вовремя."""


class LoginError(MySchoolError):
    """Вход не удался: неверные данные, отказ ЕСИА, непройденное подтверждение."""


class MFAError(LoginError):
    """Ошибка двухфакторной проверки (неверный код, не введён, истекло время)."""


class ESIAError(LoginError):
    """Ошибка на стороне Госуслуг (ЕСИА): неожиданный ответ, шаг нельзя пройти."""


class InvalidCredentials(LoginError):
    """Госуслуги не приняли логин или пароль."""


class AccountLocked(LoginError):
    """Вход заблокирован: слишком много попыток или нужна капча."""


class ESIAInvalidCredentials(ESIAError, InvalidCredentials):
    """ЕСИА ответила ``INVALID_PASSWORD``, ``INVALID_LOGIN`` или ``ACCOUNT_NOT_FOUND``."""


class ESIAAccountLocked(ESIAError, AccountLocked):
    """ЕСИА ответила ``ACCOUNT_LOCKED`` или ``CAPTCHA_REQUIRED``."""

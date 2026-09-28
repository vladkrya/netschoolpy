"""Модели данных netschoolpy.

Pydantic v2 (``model_dump()``, ``model_dump_json()``), но разбор ответов SGO —
тот же, что был проверен в 7.x. В 8.0 модели переписали заново, и они
разошлись и с форматом SGO (``studentId``, ``value``/``title`` в фильтрах
отчётов, ``dueDate``), и с собственным клиентом библиотеки (``diary.schedule``,
``Message.from_raw``, ``ShortSchool.from_raw``). Имена полей 8.0 (``days``,
``date``, ``title``, ``type_name``) оставлены псевдонимами.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class _Model(BaseModel):
    """Неизменяемая модель: как frozen-dataclass в 7.x."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)


SPECIAL_MARK_DESCRIPTIONS: Dict[str, str] = {
    "УП": "Пропуск по уважительной причине",
    "Б": "Пропустил по болезни",
    "НП": "Пропуск по неуважительной причине",
    "ОТ": "Отсутствовал",
    "ОП": "Опоздал",
    "ОСВ": "Освобожден от посещения",
}


def _parse_date(value: Any) -> datetime.date:
    """Парсит дату из ISO-строки (``2024-01-15T00:00:00``)."""
    if isinstance(value, datetime.date):
        return value
    s = str(value).split("T")[0]
    return datetime.date.fromisoformat(s)


def _parse_datetime(value: Any) -> datetime.datetime:
    """Парсит datetime из ISO-строки.

    SGO может возвращать дробные секунды с различным количеством
    цифр (например ``10:51:34.99``), что ломает ``fromisoformat``
    в Python < 3.11.  Нормализуем до 6 цифр (микросекунды).
    """
    if isinstance(value, datetime.datetime):
        return value
    s = str(value)
    # Нормализуем дробную часть секунд до 6 цифр
    import re as _re
    m = _re.match(r'^(.+\.\d{1,6})(\d*)(.*)$', s)
    if m:
        frac = m.group(1)
        # дополняем до 6 цифр после точки
        dot_idx = frac.rfind('.')
        digits_after = frac[dot_idx + 1:]
        frac = frac[:dot_idx + 1] + digits_after.ljust(6, '0')
        s = frac + m.group(3)
    return datetime.datetime.fromisoformat(s)


def _parse_time(value: Any) -> datetime.time:
    """Парсит время из строки ``HH:MM`` или ``HH:MM:SS``."""
    if isinstance(value, datetime.time):
        return value
    parts = str(value).split(":")
    return datetime.time(int(parts[0]), int(parts[1]),
                         int(parts[2]) if len(parts) > 2 else 0)


# ──────────────────────────── Ученик ──────────────────────────────

class Student(_Model):
    """Ученик (ребёнок), привязанный к аккаунту.

    Для родительских аккаунтов может быть несколько учеников.
    """
    id: int
    name: str
    group_name: Optional[str] = None

    @classmethod
    def from_raw(cls, data: dict) -> Student:
        return cls(
            id=data["studentId"],
            name=data.get("nickName") or data.get("name") or "",
            group_name=data.get("className") or data.get("groupName"),
        )

    def __str__(self) -> str:
        return self.name or str(self.id)


# ──────────────────────────── Вложения ────────────────────────────

class Attachment(_Model):
    id: int
    name: str
    description: str = ""

    @classmethod
    def from_raw(cls, data: dict) -> Attachment:
        # В заданиях имя файла в "originalFileName",
        # в почтовых вложениях — в "name".
        name = data.get("originalFileName") or data.get("name") or ""
        return cls(
            id=data["id"],
            name=name,
            description=data.get("description") or "",
        )


# ───────────────────────────── Автор ──────────────────────────────

class Author(_Model):
    id: int
    full_name: str
    nickname: str

    @classmethod
    def from_raw(cls, data: dict) -> Author:
        return cls(
            id=data["id"],
            full_name=data.get("fio", ""),
            nickname=data.get("nickName", ""),
        )


# ─────────────────────────── Объявления ───────────────────────────

class Announcement(_Model):
    name: str
    author: Author
    content: str
    post_date: datetime.datetime
    attachments: List[Attachment] = Field(default_factory=list)
    id: int = 0

    @property
    def title(self) -> str:
        """Имя поля из 8.0."""
        return self.name

    @property
    def description(self) -> str:
        """Имя поля из 8.0."""
        return self.content

    @classmethod
    def from_raw(cls, data: dict) -> Announcement:
        return cls(
            id=data.get("id") or 0,
            name=data["name"],
            author=Author.from_raw(data["author"]),
            content=data.get("description", ""),
            post_date=_parse_datetime(data["postDate"]),
            attachments=[
                Attachment.from_raw(a) for a in data.get("attachments", [])
            ],
        )


# ─────────────────────────── Задания ─────────────────────────────

class Assignment(_Model):
    id: int
    comment: str = ""
    # тип задания (из справочника), напр. «Контрольная работа»; в 8.0 — type_name
    kind: str = Field("", validation_alias=AliasChoices("kind", "type_name"))
    kind_abbr: str = ""  # сокращение типа задания, напр. «К»
    # текст задания; в 8.0 — title
    content: str = Field("", validation_alias=AliasChoices("content", "title"))
    mark: Optional[int] = None
    weight: int = 1  # коэффициент (вес) оценки
    is_duty: bool = False
    deadline: Optional[datetime.date] = None
    mark_text: Optional[str] = None
    special_mark_code: Optional[str] = None
    special_mark_description: Optional[str] = None
    mark_set_date: Optional[datetime.date] = None
    attachments: List[Attachment] = Field(default_factory=list)

    @property
    def title(self) -> str:
        """Имя поля из 8.0."""
        return self.content

    @property
    def type_name(self) -> str:
        """Имя поля из 8.0."""
        return self.kind

    @classmethod
    def from_raw(
        cls,
        data: dict,
        type_mapping: Dict[int, dict] | None = None,
    ) -> Assignment:
        """Собирает ``Assignment`` из «сырого» JSON SGO.

        SGO вкладывает оценку в подобъект ``mark``:
            ``{"mark": {"mark": 5, "dutyMark": false}, "markComment": {"name": "..."}, ...}``
        Здесь это «разворачивается» в плоские поля.

        ``weight`` (коэффициент оценки) берётся прямо из задания.
        ``kind`` / ``kind_abbr`` берутся из справочника ``type_mapping``.
        """
        raw_mark = data.get("mark")
        mark_text: Optional[str] = None
        if isinstance(raw_mark, dict):
            raw_mark_value = raw_mark.get("mark")
            mark_value: Optional[int] = raw_mark_value if isinstance(raw_mark_value, int) else None
            if raw_mark_value is not None and str(raw_mark_value).strip():
                mark_text = str(raw_mark_value).strip()

            # В разных инсталляциях SGO спец-пометки могут лежать
            # в name/abbr/value вместо mark.
            if not mark_text:
                for key in ("abbr", "shortName", "name", "value"):
                    candidate = raw_mark.get(key)
                    if candidate is None:
                        continue
                    text = str(candidate).strip()
                    if text:
                        mark_text = text
                        break
            duty: bool = raw_mark.get("dutyMark", False)
        elif isinstance(raw_mark, int):
            mark_value = raw_mark
            mark_text = str(raw_mark)
            duty = False
        elif isinstance(raw_mark, str):
            mark_value = int(raw_mark) if raw_mark.isdigit() else None
            mark_text = raw_mark.strip() or None
            duty = False
        else:
            mark_value = None
            duty = False

        mark_comment = data.get("markComment")
        comment = mark_comment["name"] if isinstance(mark_comment, dict) and "name" in mark_comment else ""

        if mark_text and mark_text.isdigit() and mark_value is None:
            mark_value = int(mark_text)

        special_mark_code = mark_text.upper() if mark_text and mark_text.upper() in SPECIAL_MARK_DESCRIPTIONS else None
        special_mark_description = SPECIAL_MARK_DESCRIPTIONS.get(special_mark_code) if special_mark_code else None

        mark_set_date: Optional[datetime.date] = None
        for mark_date_key in (
            "markDate",
            "setDate",
            "markSetDate",
            "dateSet",
            "gradeDate",
            "gradeSetDate",
        ):
            raw_mark_date = data.get(mark_date_key)
            if raw_mark_date:
                mark_set_date = _parse_date(raw_mark_date)
                break

        kind_id = data.get("typeId", 0)
        type_info = (type_mapping or {}).get(kind_id, {})
        if isinstance(type_info, str):
            # обратная совместимость: старый формат {id: name}
            kind = type_info
            kind_abbr = ""
        else:
            kind = type_info.get("name", str(kind_id))
            kind_abbr = type_info.get("abbr", "")

        return cls(
            id=data["id"],
            comment=comment,
            kind=kind,
            kind_abbr=kind_abbr,
            content=data.get("assignmentName", ""),
            mark=mark_value,
            weight=data.get("weight", 1),
            is_duty=duty,
            deadline=_parse_date(data["dueDate"]),
            mark_text=mark_text,
            special_mark_code=special_mark_code,
            special_mark_description=special_mark_description,
            mark_set_date=mark_set_date,
            attachments=[Attachment.from_raw(a) for a in data.get("attachments", [])],
        )


# ──────────────────────────── Уроки ──────────────────────────────

class Lesson(_Model):
    # День урока; в 8.0 уроки создавались и без него.
    day: Optional[datetime.date] = None
    # Время из SGO; 8.0 принимал и полный datetime — поддерживаем оба.
    start: Union[datetime.datetime, datetime.time] = datetime.time(0, 0)
    end: Union[datetime.datetime, datetime.time] = datetime.time(0, 0)
    room: str = ""
    number: int = 0
    subject: str = ""
    assignments: List[Assignment] = Field(default_factory=list)

    @classmethod
    def from_raw(cls, data: dict, type_mapping: Dict[int, dict] | None = None) -> Lesson:
        return cls(
            day=_parse_date(data["day"]),
            start=_parse_time(data["startTime"]),
            end=_parse_time(data["endTime"]),
            room=data.get("room") or "",
            number=data["number"],
            subject=data.get("subjectName", ""),
            assignments=[
                Assignment.from_raw(a, type_mapping)
                for a in data.get("assignments", [])
            ],
        )

    def _moment(self, value: Union[datetime.datetime, datetime.time]) -> Optional[datetime.datetime]:
        if isinstance(value, datetime.datetime):
            return value
        if self.day is not None:
            return datetime.datetime.combine(self.day, value)
        return None

    def to_ical_event(self) -> str:
        """Урок как VEVENT.

        Время — «плавающее» (без ``Z``): SGO отдаёт местное время школы, и
        пометка UTC, как в 8.0, сдвигала все уроки в календаре на часовой пояс.
        """
        fmt = "%Y%m%dT%H%M%S"
        start = self._moment(self.start)
        end = self._moment(self.end)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(fmt + "Z")

        description = []
        if self.room:
            description.append(f"Кабинет: {self.room}")
        for a in self.assignments:
            line = f"• {a.title} [{a.type_name}]"
            if a.mark:
                line += f" — Оценка: {a.mark}"
            description.append(line)

        uid_day = start.strftime("%Y%m%d") if start else "nodate"
        lines = [
            "BEGIN:VEVENT",
            f"UID:netschool-lesson-{uid_day}-{self.number}@netschoolpy",
            f"DTSTAMP:{stamp}",
        ]
        if start:
            lines.append(f"DTSTART:{start.strftime(fmt)}")
        if end:
            lines.append(f"DTEND:{end.strftime(fmt)}")
        lines.append(f"SUMMARY:{_ical_escape(f'{self.number}. {self.subject}')}")
        if self.room:
            lines.append(f"LOCATION:{_ical_escape(self.room)}")
        if description:
            lines.append(f"DESCRIPTION:{_ical_escape(chr(10).join(description))}")
        lines.append("END:VEVENT")
        return "\r\n".join(lines)


def _ical_escape(text: str) -> str:
    """Экранирование текста по RFC 5545: иначе перевод строки или запятая
    в названии задания ломали файл календаря."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


# ──────────────────────────── День ───────────────────────────────

class Day(_Model):
    # В 8.0 поле называлось date.
    day: datetime.date = Field(validation_alias=AliasChoices("day", "date"))
    lessons: List[Lesson] = Field(default_factory=list)

    @property
    def date(self) -> datetime.date:
        """Имя поля из 8.0."""
        return self.day

    @classmethod
    def from_raw(cls, data: dict, type_mapping: Dict[int, dict] | None = None) -> Day:
        return cls(
            day=_parse_date(data["date"]),
            lessons=[Lesson.from_raw(l, type_mapping) for l in data.get("lessons", [])],
        )


# ─────────────────────────── Дневник ─────────────────────────────

class Diary(_Model):
    start: datetime.date
    end: datetime.date
    # В 8.0 поле называлось days, а клиент при этом читал schedule —
    # из-за этого term_grades() падал с AttributeError.
    schedule: List[Day] = Field(
        default_factory=list, validation_alias=AliasChoices("schedule", "days")
    )

    @property
    def days(self) -> List[Day]:
        """Имя поля из 8.0."""
        return self.schedule

    @classmethod
    def from_raw(cls, data: dict, type_mapping: Dict[int, dict] | None = None) -> Diary:
        return cls(
            start=_parse_date(data["weekStart"]),
            end=_parse_date(data["weekEnd"]),
            schedule=[Day.from_raw(d, type_mapping) for d in data.get("weekDays", [])],
        )

    def to_ical(self) -> str:
        """Расписание недели в формате iCalendar (.ics)."""
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//netschoolpy//NetSchool Diary//RU",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:Сетевой Город - Дневник",
        ]
        for day in self.schedule:
            for lesson in day.lessons:
                # Урок без даты не привязать к календарю.
                if lesson.day is None:
                    lesson = lesson.model_copy(update={"day": day.day})
                lines.append(lesson.to_ical_event())
        lines.append("END:VCALENDAR")
        return "\r\n".join(lines) + "\r\n"


# ─────────────────────── Итоговые отметки ───────────────────────

class SubjectTotalMark(_Model):
    """Строка из отчета «Итоговые отметки».

    ``period_marks`` содержит оценки по учебным периодам
    (четверти/триместры и т.д.) в порядке следования в отчете.
    """

    order: int
    subject: str
    period_marks: List[Optional[str]]
    year_mark: Optional[str]
    exam_mark: Optional[str]
    final_mark: Optional[str]


class AssignedMark(_Model):
    """Строка из отчета «Успеваемость ученика» (StudentGrades)."""

    subject: str
    assignment_type: str
    topic: str
    lesson_date: datetime.date
    mark_set_date: datetime.date
    mark_text: str
    numeric_mark: Optional[int]
    special_mark_code: Optional[str]
    special_mark_description: Optional[str]


# ─────────────────────────── Школы ───────────────────────────────

class ShortSchool(_Model):
    name: str
    id: int
    short_name: str = ""
    address: str = ""

    @classmethod
    def from_raw(cls, data: dict) -> ShortSchool:
        return cls(
            name=data["name"],
            id=data["id"],
            short_name=data.get("shortName", ""),
            address=data.get("addressString") or data.get("address") or "",
        )


class School(_Model):
    name: str
    about: str
    address: str
    email: str
    site: str
    phone: str
    director: str
    ahc: str
    it_head: str
    uvr: str

    @classmethod
    def from_raw(cls, data: dict) -> School:
        """Собирает ``School`` из JSON ``/schools/{id}/card``.

        SGO хранит поля во вложенных объектах
        ``commonInfo``, ``contactInfo``, ``managementInfo`` —
        здесь они достаются и объединяются.
        """
        common = data.get("commonInfo", {})
        contact = data.get("contactInfo", {})
        mgmt = data.get("managementInfo", {})

        address = contact.get("juridicalAddress") or contact.get("postAddress", "")

        return cls(
            name=common.get("fullSchoolName", ""),
            about=common.get("about", ""),
            address=address,
            email=contact.get("email", ""),
            site=contact.get("web", ""),
            phone=contact.get("phones", ""),
            director=mgmt.get("director", ""),
            ahc=mgmt.get("principalAHC", ""),
            it_head=mgmt.get("principalIT", ""),
            uvr=mgmt.get("principalUVR", ""),
        )


# ─────────────────────────── Способы входа ───────────────────────────

class LoginMethods(_Model):
    """Доступные способы авторизации на сервере SGO.

    Поля:
        password: Вход по логину и паролю SGO.
        esia: Вход через Госуслуги (ЕСИА) доступен.
        esia_main: Госуслуги — основной/обязательный способ входа.
        esia_button: Кнопка «Войти через Госуслуги» отображается.
        signature: Вход по электронной подписи.
        windows_auth: Вход через Windows-аутентификацию.
        sms: Вход по SMS.
        esa: Вход через ЕСА (Единая система авторизации).
        version: Версия сервера «Сетевой Город».
        product_name: Название продукта.

    Свойство :attr:`summary` возвращает человекочитаемое описание.
    """
    password: bool = True
    esia: bool = False
    esia_main: bool = False
    esia_button: bool = False
    signature: bool = False
    windows_auth: bool = False
    sms: bool = False
    esa: bool = False
    version: str = ""
    product_name: str = ""

    @classmethod
    def from_raw(cls, data: dict) -> LoginMethods:
        """Собирает ``LoginMethods`` из JSON ``/logindata``."""
        return cls(
            password=bool(data.get("schoolLogin", False)),
            esia=bool(data.get("esiaLogin", False)),
            esia_main=bool(data.get("esiaMainAuth", False)),
            esia_button=bool(data.get("esiaButton", False)),
            signature=bool(data.get("signatureLogin", False)),
            windows_auth=bool(data.get("windowsAuth", False)),
            sms=bool(data.get("enableSms", False)),
            esa=bool(data.get("esaLogin", False)),
            version=data.get("version", ""),
            product_name=data.get("productName", ""),
        )

    @property
    def summary(self) -> str:
        """Человекочитаемое описание способов входа.

        Примеры::

            "только Госуслуги"
            "логин/пароль + Госуслуги"
            "логин/пароль"
        """
        parts: list[str] = []
        if self.password and not self.esia_main:
            parts.append("логин/пароль")
        if self.esia:
            if self.esia_main:
                parts.append("только Госуслуги")
            else:
                parts.append("Госуслуги")
        if self.signature:
            parts.append("ЭП")
        if self.sms:
            parts.append("SMS")
        if self.esa:
            parts.append("ЕСА")
        if self.windows_auth:
            parts.append("Windows")
        return " + ".join(parts) if parts else "неизвестно"


# ─────────────────────────── Почта / сообщения ─────────────────────

class MailEntry(_Model):
    """Краткая запись о письме (из списка/реестра)."""
    id: int
    subject: str
    author: str
    sent: datetime.datetime
    to_names: Optional[str]

    @classmethod
    def from_raw(cls, data: dict) -> MailEntry:
        return cls(
            id=int(data["id"]),
            subject=data.get("subject", ""),
            author=data.get("author", ""),
            sent=_parse_datetime(data["sent"]),
            to_names=data.get("toNames"),
        )


class MailPage(_Model):
    """Страница списка писем из реестра."""
    entries: List[MailEntry]
    page: int
    total_items: int

    @classmethod
    def from_raw(cls, data: dict) -> MailPage:
        return cls(
            entries=[MailEntry.from_raw(r) for r in data.get("rows", [])],
            page=data.get("page", 1),
            total_items=data.get("totalItems", 0),
        )


class MailRecipient(_Model):
    """Получатель письма / контакт."""
    id: str  # base64-кодированный идентификатор
    name: str
    organization_name: str

    @classmethod
    def from_raw(cls, data: dict) -> MailRecipient:
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            organization_name=data.get("organizationName") or "",
        )


class Message(_Model):
    """Письмо внутренней почты SGO."""
    id: int
    subject: str
    text: str
    sent: datetime.datetime
    author_id: int
    author_name: str
    to_names: str  # кому адресовано (текст)
    is_read: bool
    mailbox: str  # "Inbox", "Sent", "Draft", "Deleted"
    can_reply: bool
    can_forward: bool
    file_attachments: List[Attachment] = Field(default_factory=list)

    @classmethod
    def from_raw(cls, data: dict) -> Message:
        author = data.get("author", {})
        return cls(
            id=data["id"],
            subject=data.get("subject", ""),
            text=data.get("text", ""),
            sent=_parse_datetime(data["sent"]),
            author_id=author.get("id", 0),
            author_name=author.get("name", ""),
            to_names=data.get("toNames", ""),
            is_read=data.get("read", False),
            mailbox=data.get("mailBox", "Inbox"),
            can_reply=data.get("canReplyAll", False) or not data.get("noReply", True),
            can_forward=data.get("canForward", False),
            file_attachments=[
                Attachment.from_raw(a)
                for a in data.get("fileAttachments", [])
            ],
        )


# ─────────────────────── Учебные периоды / оценки ────────────────────

class ReportingPeriod(_Model):
    """Учебный период (четверть, полугодие, год и т.п.).

    Поля:
        id: Идентификатор периода на сервере SGO.
        name: Название, например ``"1 четверть"`` или ``"Произвольный интервал"``.
        start: Дата начала периода (``None``, если период — произвольный интервал).
        end: Дата окончания периода.
    """
    id: int
    name: str
    start: Optional[datetime.date] = None
    end: Optional[datetime.date] = None

    @classmethod
    def from_filter_source(cls, item: dict, *, date_range: Optional[dict] = None) -> ReportingPeriod:
        period_id = int(item["value"])
        start = end = None
        if date_range:
            raw_start = date_range.get("start")
            raw_end = date_range.get("end")
            if raw_start:
                start = _parse_date(raw_start)
            if raw_end:
                end = _parse_date(raw_end)
        return cls(id=period_id, name=item["title"], start=start, end=end)


class SubjectInfo(_Model):
    """Предмет ученика.

    Поля:
        id: Идентификатор предмета в SGO.
        name: Название, например ``"Алгебра"``.
    """
    id: int
    name: str

    @classmethod
    def from_filter_item(cls, item: dict) -> SubjectInfo:
        return cls(id=int(item["value"]), name=item["title"])


class SubjectGrades(_Model):
    """Оценки по одному предмету за период.

    Поля:
        subject: Название предмета.
        marks: Список пар ``(оценка, вес)``.
        average: Простой средний балл.
        weighted_average: Средневзвешенный балл.
    """
    subject: str
    marks: List[Tuple[int, int]]
    average: float
    weighted_average: float

    @classmethod
    def compute(cls, subject: str, marks: List[Tuple[int, int]]) -> SubjectGrades:
        """Подсчитать средний и средневзвешенный баллы из списка ``(оценка, вес)``."""
        if not marks:
            return cls(subject=subject, marks=[], average=0.0, weighted_average=0.0)
        avg = sum(m for m, _ in marks) / len(marks)
        total_w = sum(w for _, w in marks)
        wavg = sum(m * w for m, w in marks) / total_w if total_w else 0.0
        return cls(subject=subject, marks=marks, average=avg, weighted_average=wavg)


class SchoolYear(_Model):
    """Учебный год.

    Поля:
        id: Идентификатор года (``schoolYearId``).
        name: Название, например ``"2025/2026"``.
        start: Дата начала.
        end: Дата окончания.
    """
    id: int
    name: str
    start: datetime.date
    end: datetime.date

    @classmethod
    def from_raw(cls, data: dict) -> SchoolYear:
        gy = data.get("globalYear", data)
        return cls(
            id=data.get("id", gy.get("id", 0)),
            name=gy.get("name", ""),
            start=_parse_date(data.get("startDate", gy.get("startDate", ""))),
            end=_parse_date(data.get("endDate", gy.get("endDate", ""))),
        )



class TotalMarksReportLine(BaseModel):
    """Строка отчёта 'Итоговые оценки'."""
    order: int
    subject: str
    period_marks: list[str]
    year_mark: str | None = None
    exam_mark: str | None = None
    final_mark: str | None = None

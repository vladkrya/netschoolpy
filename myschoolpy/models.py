"""Модели данных «Госуслуги Моя школа» (Pydantic v2)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def parse_datetime(value: Any) -> Optional[dt.datetime]:
    """Разбирает ``2026-09-07 08:00:00.0`` и ISO-строки; пустое значение даёт None."""
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return dt.datetime.fromisoformat(text)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Student(_Model):
    """Ученик из ``/api/myschool/v2/auth/student``."""

    id: str = Field(alias="student_id")
    first_name: str = Field(alias="student_first_name", default="")
    last_name: str = Field(alias="student_last_name", default="")
    middle_name: Optional[str] = Field(alias="student_middle_name", default=None)
    gender: Optional[str] = Field(alias="student_gender", default=None)
    birth_date: Optional[str] = Field(alias="student_birth_date", default=None)
    region: Optional[str] = Field(alias="student_region", default=None)
    region_active: bool = Field(alias="student_region_active", default=True)
    oid: Optional[int] = Field(alias="student_oid", default=None)
    mdm_id: Optional[str] = None
    trusted: bool = True
    disabled: bool = False
    permission_mark: bool = True

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.last_name, self.first_name, self.middle_name) if p)


class Lesson(_Model):
    """Урок расписания (``student_lessons``)."""

    id: str = Field(alias="lesson_id")
    subject_id: Optional[str] = None
    subject_name: str = ""
    start: Optional[dt.datetime] = Field(alias="start_datetime", default=None)
    end: Optional[dt.datetime] = Field(alias="end_datetime", default=None)
    room: Optional[str] = None
    theme: Optional[str] = None
    teacher_id: Optional[str] = None
    teacher_last_name: Optional[str] = Field(alias="lastname", default=None)
    teacher_first_name: Optional[str] = Field(alias="firstname", default=None)
    teacher_middle_name: Optional[str] = Field(alias="patronymic", default=None)
    is_distant: bool = False
    link: Optional[str] = None
    description: Optional[str] = Field(alias="lesson_description", default=None)
    lesson_type: Optional[str] = None
    # Пропуск урока: пусто, если ученик присутствовал.
    skip_type: Optional[str] = Field(alias="type_value", default=None)
    skip_description: Optional[str] = Field(alias="skipping_description", default=None)

    _parse = field_validator("start", "end", mode="before")(parse_datetime)

    @property
    def teacher(self) -> str:
        return " ".join(
            p for p in (self.teacher_last_name, self.teacher_first_name, self.teacher_middle_name) if p
        )


class Mark(_Model):
    """Оценка (``student_marks``)."""

    id: str = Field(alias="marks_id")
    subject_id: Optional[str] = None
    subject_name: str = ""
    value: Optional[str] = Field(alias="mark_value1", default=None)
    value2: Optional[str] = Field(alias="mark_value2", default=None)
    date: Optional[dt.date] = Field(alias="mark_date", default=None)
    lesson_id: Optional[str] = None
    teacher_id: Optional[str] = None
    weight: Optional[float] = None
    comment: Optional[str] = None
    work_type: Optional[str] = Field(alias="work_type_description", default=None)
    work_name: Optional[str] = None
    scale: Optional[str] = Field(alias="scale_code", default=None)
    created: Optional[dt.datetime] = Field(alias="create_datetime", default=None)

    @field_validator("date", mode="before")
    @classmethod
    def _date(cls, v: Any) -> Optional[dt.date]:
        parsed = parse_datetime(v)
        return parsed.date() if parsed else None

    _parse = field_validator("created", mode="before")(parse_datetime)

    @property
    def number(self) -> Optional[int]:
        """Числовое значение пятибалльной оценки, если оно есть."""
        try:
            return int(self.value) if self.value is not None else None
        except ValueError:
            return None


class Homework(_Model):
    """Домашнее задание (``student_homeworks_materials``)."""

    id: str = Field(alias="homeworks_id")
    subject_id: Optional[str] = None
    subject_name: str = ""
    description: str = ""
    issue_date: Optional[dt.date] = None
    plan_ready_date: Optional[dt.date] = None
    issue_lesson_id: Optional[str] = None
    ready_lesson_id: Optional[str] = None
    teacher_id: Optional[str] = None
    materials: list[Any] = Field(default_factory=list)
    # Отметка «выполнено» хранится отдельно (customservices) и подставляется клиентом.
    completed: Optional[bool] = None

    @field_validator("issue_date", "plan_ready_date", mode="before")
    @classmethod
    def _date(cls, v: Any) -> Optional[dt.date]:
        parsed = parse_datetime(v)
        return parsed.date() if parsed else None

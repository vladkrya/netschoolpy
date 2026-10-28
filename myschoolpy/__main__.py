"""Быстрая проверка сессии: ``python -m myschoolpy`` печатает неделю ученика.

Cookies берутся из переменной ``MYSCHOOL_COOKIES`` или из файла, путь к которому
передан в ``MYSCHOOL_COOKIES_FILE`` (строка заголовка ``Cookie`` из DevTools).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

import certifi
import ssl

from . import MySchool, MySchoolError


async def _run() -> None:
    raw = os.environ.get("MYSCHOOL_COOKIES")
    if not raw and os.environ.get("MYSCHOOL_COOKIES_FILE"):
        raw = Path(os.environ["MYSCHOOL_COOKIES_FILE"]).read_text().strip()
    if not raw:
        sys.exit("Задайте MYSCHOOL_COOKIES или MYSCHOOL_COOKIES_FILE")
    role = os.environ.get("MYSCHOOL_ROLE", "student")
    ctx = ssl.create_default_context(cafile=certifi.where())
    async with MySchool(ssl_context=ctx) as ms:
        await ms.login_with_cookies(raw)
        today = dt.date.today()
        for student in await ms.students(role):
            print(f"Ученик: {student.full_name}")
            lessons = await ms.lessons(student, today)
            marks = await ms.marks(student, today - dt.timedelta(days=14), today)
            homeworks = await ms.homeworks(student, today, today + dt.timedelta(days=7))
            print(f"  уроков на неделе: {len(lessons)}")
            print(f"  оценок за 2 недели: {len(marks)}")
            print(f"  домашних заданий на неделю вперёд: {len(homeworks)}")


def main() -> None:
    try:
        asyncio.run(_run())
    except MySchoolError as exc:
        sys.exit(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()

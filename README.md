# netschoolpy

[![CI](https://github.com/vkrya/netschoolpy/actions/workflows/ci.yml/badge.svg)](https://github.com/vkrya/netschoolpy/actions)
[![PyPI](https://img.shields.io/pypi/v/netschoolpy)](https://pypi.org/project/netschoolpy/)
[![Python](https://img.shields.io/pypi/pyversions/netschoolpy)](https://pypi.org/project/netschoolpy/)

Асинхронный клиент для «Сетевого города». Дневник, оценки, домашние задания, объявления — всё программно, без браузера.

Репозиторий: https://github.com/vkrya/netschoolpy

[Поддержать проект (ЮMoney)](https://yoomoney.ru/to/4100118867747459)

Ключевые слова: Сетевой город, SGO, дневник, оценки, домашние задания, Госуслуги, ESIA, QR, API.

## Документация (Docs)

Документация к проекту находится в папке [docs/](docs/). Для локального просмотра:

```bash
pip install mkdocs mkdocs-material
mkdocs serve
```

## Установка

```bash
pip install netschoolpy

# Для отображения QR-кода в терминале (опционально):
pip install netschoolpy[qr]
```

## Способы входа

### По логину/паролю SGO

```python
import asyncio
from netschoolpy import NetSchool

async def main():
    async with NetSchool("https://sgo.example.ru") as ns:
        await ns.login("ИвановИ", "password", "Школа №1")
        diary = await ns.diary()

asyncio.run(main())
```

### Через Госуслуги (логин + пароль ЕСИА)

Программный вход через Госуслуги — без браузера.
Поддерживает SMS, TOTP (приложение-аутентификатор) и MAX (Госключ) в качестве второго фактора.

```python
async with NetSchool("https://sgo.example.ru") as ns:
    await ns.login_via_gosuslugi(
        esia_login="+79001234567",     # телефон, email или СНИЛС
        esia_password="your_password",
        school="Школа №1",             # если к аккаунту привязано несколько организаций
    )
    diary = await ns.diary()
```

При MFA код из SMS/TOTP/MAX будет запрошен через `input()`.
Если `esia_login` / `esia_password` не указаны, они тоже запрашиваются через `input()`.
Если к аккаунту привязано несколько организаций и `school` не указан — будет предложен интерактивный выбор.

### Через Госуслуги (QR-код)

Вход без ввода логина и пароля — нужно отсканировать QR-код
в мобильном приложении «Госуслуги».

```python
async def show_qr(qr_data: str):
    """qr_data — deep-link gosuslugi://auth/signed_token=..."""
    import qrcode
    qrcode.make(qr_data).save("qr.png")
    print("Отсканируйте QR в приложении Госуслуги!")

async with NetSchool("https://sgo.example.ru") as ns:
    await ns.login_via_gosuslugi_qr(
        qr_callback=show_qr,   # вызовется после генерации QR
        qr_timeout=120,         # секунд ожидания сканирования
        school="Школа №1",     # если привязано несколько организаций
    )
    diary = await ns.diary()
```

- `qr_callback` — async/sync функция, получает deep-link
  `gosuslugi://auth/signed_token=...` для кодирования в QR.
  Если не указан — QR печатается в stdout (`pip install qrcode`).
- `qr_timeout` — таймаут ожидания сканирования (по-умолчанию 120 сек).
- `school` — название организации (подстрока). Если привязано несколько — выбирает автоматически. Без параметра — интерактивный выбор.

### По токену / куки (продвинутое)

```python
# По accessToken из localStorage SGO:
await ns.login_with_token("eyJ...")

# По session-store из localStorage:
await ns.login_with_session_store('{"accessToken":"eyJ..."}')

# По Cookie-строке из DevTools:
await ns.login_with_cookies("NSSESSIONID=abc123")
```

## Удержание сессии (keep-alive)

После любого входа автоматически запускается фоновая задача,
которая каждые **5 минут** пингует сервер, не давая сессии истечь.

```python
# Изменить интервал (в секундах):
ns.set_keepalive_interval(120)   # каждые 2 минуты

# Отключить keep-alive:
ns.set_keepalive_interval(0)
```

При вызове `logout()` keep-alive останавливается автоматически.

## Справочник регионов

Библиотека включает справочник из **26 регионов** с готовыми URL.

```python
from netschoolpy import REGIONS, get_url, list_regions

# Все доступные регионы
for name in list_regions():
    print(name, "→", REGIONS[name])

# Быстрый поиск (регистронезависимый, поддерживает подстроки)
url = get_url("Челябинская область")   # "https://sgo.edu-74.ru"
url = get_url("челябинская")           # тоже найдёт

# Использование с клиентом
async with NetSchool(get_url("Краснодарский край")) as ns:
    ...
```

> **Примечание:** Иркутская, Ростовская и Свердловская области имеют
> несколько независимых серверов — для них URL нужно указывать вручную.

## Экспорт/импорт сессии

Чтобы не авторизоваться каждый раз, можно сохранить и восстановить сессию:

```python
from pathlib import Path

# Сохранить после логина:
session_data = ns.export_session()
Path("session.json").write_text(session_data)

# Восстановить при следующем запуске:
async with NetSchool("https://sgo.example.ru") as ns:
    try:
        await ns.import_session(Path("session.json").read_text())
    except netschoolpy.SessionExpired:
        await ns.login(...)  # сессия истекла — логинимся заново
```

## API

```python
async with NetSchool("https://sgo.example.ru") as ns:
    await ns.login(...)

    diary = await ns.diary()                    # дневник (текущая неделя)
    diary = await ns.diary(start, end)          # дневник за период

    overdue = await ns.overdue()                # просроченные задания
    announcements = await ns.announcements()    # объявления
    attachments = await ns.attachments(...)     # вложения к заданию
    info = await ns.school_info()               # информация о школе

    await ns.download_attachment(id, buffer)    # скачать вложение
    data = await ns.download_attachment_as_bytes(id)
    await ns.download_profile_picture(id, buf)  # аватар пользователя

    kinds = await ns.assignment_types()         # типы заданий (ДЗ, К, …)
    bells = await ns.bell_schedule()            # расписание звонков
    limits = await ns.upload_limits()           # лимиты загрузки файлов, КБ
    settings = await ns.my_settings()           # профиль (личные данные!)
```

### Расписание звонков

```python
for bell in await ns.bell_schedule():
    print(bell.weekday, bell.shift, bell.number, bell.start, bell.end)
```

`BellTime.weekday` — ISO (1 — понедельник), `shift` — смена, `number` — номер урока (0 — нулевой урок). Веб-клиент берёт звонки из `schedule/times`: все дни, смены и варианты школы (`variant_id`, `used`). У мобильного API такого маршрута нет, поэтому `MobileNetSchool.bell_schedule(start)` собирает звонки из уроков ученика за неделю, начиная со `start` (по умолчанию текущая неделя).

### Почта

Одинаково в `NetSchool` и `MobileNetSchool`:

```python
from netschoolpy import RecipientGroup

page = await ns.mail_list("Inbox", search="контрольная")   # поиск по теме
page = await ns.mail_list("Inbox", search="Петрова", search_field="author", ascending=True)
msg = await ns.mail_read(message_id)

teachers = await ns.mail_recipients(RecipientGroup.TEACHERS)  # веб: группы адресной книги
mid = await ns.mail_send("Тема", "Текст", [teachers[0].id],
                         attachments=[("дз.pdf", pdf_bytes)])  # вернёт ID письма

draft = await ns.mail_draft(message_id, "reply")   # «Re: …», цитата и адресат
await ns.mail_send(draft.subject, "Ответ" + draft.text, [r.id for r in draft.to])

await ns.mail_mark_read([mid])                     # read=False — непрочитанным
await ns.mail_delete([mid])                        # в «Удалённые»
await ns.mail_restore([mid])                       # обратно
await ns.mail_delete([mid], permanent=True)        # из «Удалённых» — навсегда
```

`RecipientGroup`: `ADMINISTRATION` (по умолчанию), `HEAD_TEACHERS`, `CLASS_TEACHERS`, `TEACHERS`, `CLASSMATES`. Мобильный `mail_recipients()` возвращает всю адресную книгу сразу.

### Отчёты

Любой отчёт со страницы «Отчёты» — тем же путём, что и сайт (очередь задач и SignalR):

```python
for group in await ns.reports():
    print(group.title, [r.id for r in group.reports])

filters = await ns.report_filters("StudentAverageMark")   # что можно выбрать

report = await ns.report(
    "StudentTotal",
    {"TERMID": "2 четверть"},          # value или название пункта
    start=date(2026, 9, 1), end=date(2026, 9, 15),   # для фильтра-интервала
)
report.html                            # как показывает сайт
for row in report.tables()[1]:         # таблицы: строки → тексты ячеек
    print(row)
```

Не указанные фильтры берут значение по умолчанию. `total_marks()` и `assigned_marks()` — готовые разборы отчётов `StudentTotalMarks` и `StudentGrades`.

## Мобильное API (9.0)

`MobileNetSchool` работает через API приложения NetSchool (`/api/mobile/*`). Интерфейс совместим с `NetSchool`: `diary()`, `announcements()`, `mail_list()`, `mail_read()`, `mail_recipients()`, `mail_send()`, `attachments()`, `assignment_details()`, `export_session()` / `import_session()` возвращают те же модели.

Чем оно лучше веб-API:

- токен живёт **60 дней**, keep-alive не нужен;
- есть **время публикации задания** (`Assignment.published_at`), учитель, тема и посещаемость урока (`Lesson.teacher`, `topic`, `absence`);
- итоги по всем предметам одним запросом, аналитика по предмету, лента событий, портфолио.

Для получения токена нужен `client_secret` приложения `parent-mobile`: передайте `client_secret=` или задайте `NETSCHOOL_MOBILE_CLIENT_SECRET`. С библиотекой он не поставляется.

```python
from netschoolpy import MobileNetSchool, NetSchool, published_at

# По логину и паролю — как в приложении
async with MobileNetSchool("https://sgo.e-mordovia.ru") as ns:
    await ns.login("ИвановИ", "password", "МОУ Школа №1")
    diary = await ns.diary()
    for day in diary.schedule:
        for lesson in day.lessons:
            for a in lesson.assignments:
                print(lesson.subject, a.content, a.published_at)

# Из уже вошедшего веб-клиента (в том числе через Госуслуги) — без пароля
mobile = await MobileNetSchool.from_web(web_ns)

# Сохранить и восстановить
saved = mobile.export_session()
await MobileNetSchool(url).import_session(saved)
```

Сырые маршруты возвращают JSON сервера:

```python
await ns.users()                                  # профиль, школы, дети
await ns.education()                              # годы обучения
await ns.terms(); await ns.subjects_raw()         # четверти, предметы
await ns.classmeetings(start, end)                # уроки
await ns.classmeeting(classmeeting_id)
await ns.assignments([id1, id2]); await ns.assignment(id)
await ns.current_term_assignments()               # «Д/З» текущей четверти
await ns.assignment_answer(id)                    # ответ ученика
await ns.attachments_raw(assignment_id)
await ns.period_events(period_days=14)            # новые оценки и задания
await ns.period_events(types=["TermTotalInfo"])   # только итоговые за период
await ns.totals()                                 # итоговые оценки
await ns.subject_performance(subject_id, term_id) # «Итоги по предмету»
await ns.mail_messages("INBOX"); await ns.mail_unread_count()
await ns.mail_read_raw(id)                        # помечает прочитанным
await ns.address_book(); await ns.recipient(user_id)
await ns.mail_blocked_users()
await ns.mail_block(user_id); await ns.mail_unblock(user_id)  # эффект не подтверждён
await ns.announcements_raw(); await ns.push_settings()
await ns.portfolios(); await ns.portfolio(id); await ns.download_portfolio_doc(id, doc_id)
await ns.token_endpoints()
```

Маршруты приложения 2.3.1 (восстановлены по его коду):

```python
await ns.set_answer_text(assignment_id, "Ответ")  # текст ответа учителю; "" — стереть
await ns.upload_answer_file(assignment_id, "решение.pdf", data, description="стр. 1")
await ns.delete_answer_file(assignment_id, attachment_id)
await ns.distance_meetings([distance_meeting_id])     # Lesson.distance_meeting_id
await ns.distance_meeting_join(distance_meeting_id)   # вход на урок через шлюз ВКС
await ns.events(start=date(2025, 1, 1))               # олимпиады и конкурсы
await ns.download_event_file(event_id, file_id)
await ns.parent_reference_types()                     # справки (только родителям)
await ns.order_parent_reference(reference_type_id)    # заказ уходит в школу
await ns.set_push_settings({"Homework": False})       # push приложения, не бота
await ns.address_book_organizations()
```

`set_push_settings` со словарём накладывает его на текущие настройки: сервер заменяет весь набор, и не названные коды иначе пропали бы.

Факультативы определяются по признакам сервера `extraActivity` (внеурочная
деятельность) и `addEducation` (дополнительное образование), без анализа названия:

```python
from netschoolpy import is_elective

is_elective(await ns.classmeeting(classmeeting_id))  # сырой ответ API
for day in (await ns.diary()).schedule:
    for lesson in day.lessons:
        print(lesson.subject, lesson.is_elective)
```

Каталог регионов приложения — без авторизации: `await netschoolpy.list_mobile_regions()`, `await netschoolpy.get_mobile_region(server_id)`. Последняя версия приложения в каталоге — `await netschoolpy.published_app_version()`. Токен можно продлить через `await ns.refresh()`.

## Исключения

```python
import netschoolpy

netschoolpy.NetSchoolError      # базовое
netschoolpy.LoginError          # ошибка авторизации
netschoolpy.MFAError            # ошибка двухфакторной (SMS/TOTP/PUSH)
netschoolpy.ESIAError           # ошибка на стороне Госуслуг
netschoolpy.SchoolNotFound      # школа не найдена
netschoolpy.SessionExpired      # сессия истекла (401)
netschoolpy.ServerUnavailable   # сервер не ответил
```

## Логирование

Библиотека использует стандартный `logging`. Для отладки:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

## Благодарности

«Сетевой город» не публикует документацию к своему API, и многое о нём сначала
узнали другие. Код netschoolpy написан с нуля, но что и где искать —
подсказали эти проекты:

- [nm17/netschoolapi](https://github.com/nm17/netschoolapi) — Даниил Николаев, Иван Горланов, Елисей Власенко, и форк [MrConsoleka/netschoolapi](https://github.com/MrConsoleka/netschoolapi): первая Python-библиотека для «Сетевого города» — вход, дневник, вложения;
- [lentryd/NetSchoolApi](https://github.com/lentryd/NetSchoolApi) (JavaScript): отчёты через очередь задач и SignalR;
- [TheEntropyShard/NetSchoolAPI](https://github.com/TheEntropyShard/NetSchoolAPI) (Java): действия с письмами веб-API, группы адресной книги, список отчётов, типы заданий, лимиты загрузки;
- [chstudios-ru/mobnslib](https://github.com/chstudios-ru/mobnslib): почта мобильного API — удаление, заготовки ответа, получатель по id, блокировка — и типы событий ленты.

Каждый маршрут перед добавлением проверен на живом сервере; там, где проверить
не удалось, это сказано в документации метода.

## Лицензия

© 2026 vkrya.
GNU GPLv3. Подробнее — [LICENSE](LICENSE).

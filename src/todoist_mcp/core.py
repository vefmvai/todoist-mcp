"""Функции-«кнопки» Todoist — чистая логика поверх TodoistClient, без MCP.

Каждая функция принимает клиент и возвращает готовый человекочитаемый текст:
и MCP-сервер (мак), и бот (сервер) просто оборачивают этот текст. Так одно ядро
служит обоим каналам и будущим автоматизациям (ночной обход, поллер комментариев).

Приоритет — по конвенции API Todoist: 4 = самый высокий (p1 в приложении), 1 = обычный.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import plan as plan_module
from .client import TodoistClient, TodoistError

# Поля, которых на бесплатном тарифе нет, и фича, которая их включает.
# Ведут себя по-разному, и оба поведения плохие: дедлайн роняет весь запрос
# (403 Premium only), а длительность Todoist принимает и молча выбрасывает.
# Поэтому недоступное сюда вообще не отправляется — см. _strip_premium.
PREMIUM_FIELDS = {
    "deadline_date": ("deadlines", "дедлайны"),
    "duration": ("durations", "оценка длительности"),
}


# ── помощники ────────────────────────────────────────────────────────────────

def _parse_labels(labels: Any) -> list[str]:
    """Принять список или строку «актив, ждёт» → ['актив','ждёт'] (без @)."""
    if not labels:
        return []
    if isinstance(labels, str):
        parts = labels.split(",")
    else:
        parts = list(labels)
    return [str(x).strip().lstrip("@") for x in parts if str(x).strip()]


def _format_due(due: dict[str, Any] | None) -> str:
    """Срок в человекочитаемом виде и в местном времени.

    У задач со временем Todoist отдаёт `date` в UTC (…Z) плюс поле `timezone`.
    Конвертируем по этому полю (иначе агент видит время по Гринвичу, минус 3 часа
    от московского). Задачи-даты без времени и «плавающее» время отдаём как есть.
    """
    if not due:
        return "без даты"
    raw = due.get("date") or ""
    if "T" not in raw:  # обычная дата без времени
        return raw or "без даты"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return due.get("string") or raw
    if dt.tzinfo is not None:  # UTC → пояс, в котором сейчас живёт владелец
        # COACH_TZ (где Василий сейчас) в приоритете — так вывод совпадает с
        # приложением: Todoist показывает время в текущем поясе аккаунта.
        # Переехал на Бали → COACH_TZ=Asia/Makassar, и время едет за ним,
        # хотя сервер остаётся в Москве. due.timezone — запасной вариант.
        tzname = os.environ.get("COACH_TZ") or due.get("timezone") or "Europe/Moscow"
        try:
            dt = dt.astimezone(ZoneInfo(tzname))
        except Exception:
            pass
    return dt.strftime("%Y-%m-%d %H:%M")


def brief(task: dict[str, Any]) -> str:
    """Однострочное описание задачи для человека."""
    due = _format_due(task.get("due"))
    labels = " ".join(f"@{label}" for label in task.get("labels") or [])
    priority = task.get("priority") or 1  # 4 = p1 в UI
    flag = f" p{5 - priority}" if priority > 1 else ""
    return f"[{task['id']}] {task['content']} — {due}{flag} {labels}".rstrip()


def _strip_premium(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Убрать из тела поля, недоступные на тарифе, и объяснить это словами.

    Вторая защита. Первая — в манифесте: недоступное поле вообще не
    объявляется, и модель физически не может его прислать. Эта нужна для
    прямых вызовов ядра (автоматизации, тесты) и для случая, когда тариф
    сменился посреди жизни процесса.

    Раньше здесь стоял другой приём: послать как есть, поймать 403 и
    переслать без дедлайна. Он лечил только громкий случай — тихий
    (длительность) не лечил вообще, — и стоил лишнего запроса каждый раз.
    """
    current = plan_module.current()
    dropped: list[str] = []
    for field_name, (feature, human) in PREMIUM_FIELDS.items():
        if field_name in payload and not current.has(feature):
            payload.pop(field_name, None)
            if field_name == "duration":
                payload.pop("duration_unit", None)
            dropped.append(human)
    if not dropped:
        return payload, ""
    return payload, f" (на вашем тарифе Todoist недоступно: {', '.join(dropped)} — сделал без этого)"


async def _create_one(client: TodoistClient, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Создать задачу, заранее убрав то, чего тариф не позволяет."""
    payload, note = _strip_premium(payload)
    return await client.post("/tasks", json=payload), note


def _task_payload(args: dict[str, Any]) -> dict[str, Any]:
    """Собрать тело задачи из общих полей (для add/update)."""
    payload: dict[str, Any] = {}
    for key in ("content", "description", "project_id", "section_id", "parent_id"):
        if args.get(key):
            payload[key] = args[key]
    if args.get("due_string"):
        payload["due_string"] = args["due_string"]
        payload["due_lang"] = "ru"
    if args.get("deadline_date"):
        payload["deadline_date"] = args["deadline_date"]  # YYYY-MM-DD
    if args.get("priority"):
        payload["priority"] = int(args["priority"])
    labels = _parse_labels(args.get("labels"))
    if labels:
        payload["labels"] = labels
    if args.get("duration"):
        payload["duration"] = int(args["duration"])
        payload["duration_unit"] = args.get("duration_unit") or "minute"
    return payload


# ── чтение ───────────────────────────────────────────────────────────────────

async def find_tasks(client: TodoistClient, filter: str = "today", limit: int = 30) -> str:
    """Задачи по фильтру Todoist (today, overdue, 7 days, @актив, #Личное, no date…)."""
    query = filter or "today"
    limit = min(int(limit or 30), 100)
    data = await client.get("/tasks/filter", params={"query": query, "limit": limit})
    tasks = data.get("results", []) if isinstance(data, dict) else data
    if not tasks:
        return f"По фильтру «{query}» задач нет."
    return f"Задачи по фильтру «{query}» ({len(tasks)}):\n" + "\n".join(brief(t) for t in tasks)


async def get_task(client: TodoistClient, task_id: str,
                   include_subtasks: bool = True, include_comments: bool = False) -> str:
    """Карточка задачи + при желании подзадачи и комментарии."""
    task = await client.get(f"/tasks/{task_id}")
    lines = [brief(task)]
    # где лежит задача — проект (и секция, если есть)
    place = ""
    if task.get("project_id"):
        try:
            project = await client.get(f"/projects/{task['project_id']}")
            place = project.get("name", task["project_id"])
        except TodoistError:
            place = task["project_id"]
    if task.get("section_id"):
        try:
            section = await client.get(f"/sections/{task['section_id']}")
            place += f" → {section.get('name', task['section_id'])}"
        except TodoistError:
            place += f" → секция {task['section_id']}"
    if place:
        lines.append(f"  проект: {place}")
    if task.get("description"):
        lines.append(f"  описание: {task['description']}")
    if include_subtasks:
        subs = await client.get_paginated("/tasks", params={"parent_id": task_id})
        if subs:
            lines.append(f"  подзадачи ({len(subs)}):")
            lines += [f"    • {brief(s)}" for s in subs]
    if include_comments:
        comments = await client.get_paginated("/comments", params={"task_id": task_id})
        if comments:
            lines.append(f"  комментарии ({len(comments)}):")
            lines += [f"    – [{c.get('id')}] {c.get('content', '')}" for c in comments]
    return "\n".join(lines)


async def get_comments(client: TodoistClient, task_id: str) -> str:
    """Прочитать комментарии задачи. id в квадратных скобках — для delete_comment."""
    comments = await client.get_paginated("/comments", params={"task_id": task_id})
    if not comments:
        return f"У задачи {task_id} комментариев нет."
    return f"Комментарии задачи {task_id} ({len(comments)}):\n" + "\n".join(
        f"– [{c.get('id')}] {c.get('content', '')}" for c in comments
    )


async def get_structure(client: TodoistClient) -> str:
    """Карта: проекты + секции + метки — чтобы класть задачу в правильное место."""
    projects = await client.get_paginated("/projects", params={"limit": 100})
    sections = await client.get_paginated("/sections", params={"limit": 200})
    labels = await client.get_paginated("/labels", params={"limit": 200})
    by_project: dict[str, list[dict[str, Any]]] = {}
    for section in sections:
        by_project.setdefault(section["project_id"], []).append(section)
    lines = ["Проекты и секции (id нужен, чтобы класть задачу в проект/секцию):"]
    for project in projects:
        lines.append(f"  [{project['id']}] {project['name']}")
        for section in by_project.get(project["id"], []):
            lines.append(f"      └ секция [{section['id']}] {section['name']}")
    lines.append("Метки: " + ", ".join(f"@{label['name']}" for label in labels))
    return "\n".join(lines)


async def completed_history(client: TodoistClient, since: str, until: str,
                            project_id: str | None = None, limit: int = 50) -> str:
    """История завершённых задач за период (since/until — YYYY-MM-DD)."""
    params: dict[str, Any] = {
        "since": f"{since}T00:00:00",
        "until": f"{until}T23:59:59",
        "limit": min(int(limit or 50), 200),
    }
    if project_id:
        params["project_id"] = project_id
    data = await client.get("/tasks/completed/by_completion_date", params=params)
    items = data.get("items", data.get("results", [])) if isinstance(data, dict) else data
    if not items:
        return f"С {since} по {until} завершённых задач нет."
    lines = [f"Завершено с {since} по {until} ({len(items)}):"]
    for t in items:
        done = (t.get("completed_at") or "")[:10]
        lines.append(f"  ✓ {t.get('content', '')} — {done}")
    return "\n".join(lines)


# ── запись: задачи ─────────────────────────────────────────────────────────────

async def add_task(client: TodoistClient, content: str, **fields: Any) -> str:
    """Создать задачу или подзадачу (parent_id). due_string — словами по-русски."""
    payload = _task_payload({"content": content, **fields})
    task, note = await _create_one(client, payload)
    kind = "подзадача" if payload.get("parent_id") else "задача"
    return f"Создана {kind}: {brief(task)}{note}"


async def add_tasks_bulk(client: TodoistClient, tasks: list[dict[str, Any]]) -> str:
    """Создать пачку задач/подзадач. Каждый элемент — {content, ...поля}."""
    if not tasks:
        return "Пустой список — нечего создавать."
    lines = []
    count = 0
    for item in tasks:
        content = item.get("content")
        if not content:
            continue
        payload = _task_payload(item)
        task, note = await _create_one(client, payload)
        lines.append(brief(task) + note)
        count += 1
    return f"Создано задач: {count}\n" + "\n".join(lines)


async def update_task(client: TodoistClient, task_id: str, **fields: Any) -> str:
    """Изменить задачу: срок (due_string), приоритет, метки, текст, длительность."""
    payload = _task_payload(fields)
    payload.pop("parent_id", None)  # смену родителя делает move_task
    # Дедлайн на бесплатном тарифе роняет ВЕСЬ запрос (403 Premium only),
    # то есть без этой строки не менялось бы вообще ничего — ни срок, ни текст.
    payload, note = _strip_premium(payload)
    if not payload:
        return "Нечего менять — не передано ни одного поля." + note
    task = await client.post(f"/tasks/{task_id}", json=payload)
    return f"Обновлено: {brief(task)}{note}"


async def complete_task(client: TodoistClient, task_id: str, reopen: bool = False) -> str:
    """Закрыть (или переоткрыть) задачу.

    У повторяющихся задач «закрыть» на деле передвигает срок на следующий раз —
    об этом честно сообщаем, а не врём «закрыта».
    """
    task_id = str(task_id)
    if reopen:
        await client.post(f"/tasks/{task_id}/reopen")
        return f"Задача {task_id} снова открыта."
    recurring = False
    try:
        task = await client.get(f"/tasks/{task_id}")
        recurring = bool((task.get("due") or {}).get("is_recurring"))
    except TodoistError:
        pass
    await client.post(f"/tasks/{task_id}/close")
    if recurring:
        return (f"Повторяющаяся задача {task_id} отмечена — Todoist передвинул её "
                f"на следующий раз (не удалена).")
    return f"Задача {task_id} закрыта."


async def move_task(client: TodoistClient, task_id: str, project_id: str | None = None,
                    section_id: str | None = None, parent_id: str | None = None) -> str:
    """Перенести задачу в проект/секцию или сделать её подзадачей (parent_id)."""
    body: dict[str, Any] = {}
    if project_id:
        body["project_id"] = project_id
    if section_id:
        body["section_id"] = section_id
    if parent_id:
        body["parent_id"] = parent_id
    if not body:
        return "Не указано, куда переносить (project_id / section_id / parent_id)."
    await client.post(f"/tasks/{task_id}/move", json=body)
    return f"Задача {task_id} перенесена."


async def delete_task(client: TodoistClient, task_id: str, confirm: bool = False) -> str:
    """Удалить задачу безвозвратно. Только при confirm=true (каскадом удалит подзадачи)."""
    if not confirm:
        return "Удаление не выполнено: нужен confirm=true. Удаление необратимо."
    await client.delete(f"/tasks/{task_id}")
    return f"Задача {task_id} удалена."


async def quick_add(client: TodoistClient, text: str) -> str:
    """Создать задачу одной строкой на естественном языке (#проект, @метка, p1, «завтра»)."""
    task = await client.post("/tasks/quick", json={"text": text})
    return f"Создано: {brief(task)}"


# ── запись: комментарии и структура ────────────────────────────────────────────

async def add_comment(client: TodoistClient, task_id: str, content: str) -> str:
    """Оставить комментарий к задаче."""
    comment = await client.post("/comments", json={"task_id": str(task_id), "content": content})
    return f"Комментарий добавлен к задаче {task_id} (id {comment.get('id')})."


async def delete_comment(client: TodoistClient, comment_id: str, confirm: bool = False) -> str:
    """Удалить комментарий безвозвратно. Только при confirm=true.

    id комментария берётся из get_comments или get_task(include_comments=true).
    """
    if not confirm:
        return "Удаление не выполнено: нужен confirm=true. Удаление необратимо."
    await client.delete(f"/comments/{comment_id}")
    return f"Комментарий {comment_id} удалён."


async def update_comment(client: TodoistClient, comment_id: str, content: str) -> str:
    """Переписать текст комментария (id виден в get_comments)."""
    comment = await client.post(f"/comments/{comment_id}", json={"content": content})
    return f"Комментарий {comment.get('id', comment_id)} обновлён."


# ── фильтры ───────────────────────────────────────────────────────────────────
#
# У фильтров нет REST-эндпоинта: `GET /filters` отдаёт 404 (проверено 01.08.2026,
# документация утверждает обратное). Живут они только в sync-API — читаются
# через resource_types, а правятся командами filter_add / filter_update /
# filter_delete. Поэтому здесь единственное место, где ядро ходит в /sync.

async def _sync_command(client: TodoistClient, kind: str, args: dict[str, Any]) -> dict[str, Any]:
    """Выполнить одну команду sync-API и вернуть её статус."""
    request_id = str(uuid.uuid4())
    command: dict[str, Any] = {"type": kind, "uuid": request_id, "args": args}
    if kind.endswith("_add"):
        command["temp_id"] = str(uuid.uuid4())
    data = await client.post("/sync", data={"commands": json.dumps([command])})
    status = (data or {}).get("sync_status", {}).get(request_id)
    if status != "ok":
        raise TodoistError(f"Todoist отказал: {status}")
    return data or {}


async def manage_filters(client: TodoistClient, action: str = "list", name: str = "",
                         query: str = "", filter_id: str = "", confirm: bool = False) -> str:
    """Сохранённые фильтры: посмотреть, завести, поправить, удалить.

    action: list | create | update | delete.
    Фильтр — сохранённый запрос вроде «no date & !@когда-нибудь». На бесплатном
    тарифе слотов три, и Todoist их не стережёт: четвёртый создаётся молча
    (проверено 01.08.2026), поэтому счёт ведём сами.
    """
    data = await client.post("/sync", data={"sync_token": "*", "resource_types": '["filters"]'})
    filters = [f for f in (data or {}).get("filters", []) if not f.get("is_deleted")]
    limit = plan_module.current().current.get("max_filters")

    if action == "list":
        if not filters:
            return "Сохранённых фильтров нет."
        lines = [f"Фильтры ({len(filters)}" + (f" из {limit} по тарифу" if limit else "") + "):"]
        lines += [f"  [{f['id']}] {f.get('name')} — {f.get('query')}" for f in filters]
        return "\n".join(lines)

    if action == "create":
        if not name or not query:
            return "Для создания нужны name и query."
        if isinstance(limit, int) and len(filters) >= limit:
            return (f"На тарифе доступно фильтров: {limit}, и все заняты. Todoist это не "
                    f"проверяет — создаст четвёртый молча, но в приложении вы его не увидите. "
                    f"Сначала освободите слот (action=delete).")
        await _sync_command(client, "filter_add", {"name": name, "query": query})
        return f"Фильтр «{name}» создан: {query}"

    if action == "update":
        target = _pick_filter(filters, filter_id, name)
        if target is None:
            return f"Фильтр не найден (искал по id «{filter_id}» и имени «{name}»)."
        args: dict[str, Any] = {"id": target["id"]}
        if query:
            args["query"] = query
        if name and name != target.get("name"):
            args["name"] = name
        if len(args) == 1:
            return "Нечего менять — не передан ни query, ни новое имя."
        await _sync_command(client, "filter_update", args)
        return f"Фильтр «{target.get('name')}» обновлён."

    if action == "delete":
        target = _pick_filter(filters, filter_id, name)
        if target is None:
            return f"Фильтр не найден (искал по id «{filter_id}» и имени «{name}»)."
        if not confirm:
            return (f"Удаление не выполнено: нужен confirm=true. Фильтр "
                    f"«{target.get('name')}» исчезнет безвозвратно.")
        await _sync_command(client, "filter_delete", {"id": target["id"]})
        return f"Фильтр «{target.get('name')}» удалён."

    return f"Неизвестное действие «{action}». Доступно: list, create, update, delete."


def _pick_filter(filters: list[dict[str, Any]], filter_id: str, name: str) -> dict[str, Any] | None:
    """Найти фильтр по id или по имени (регистр не важен)."""
    if filter_id:
        for f in filters:
            if str(f.get("id")) == str(filter_id):
                return f
    if name:
        wanted = name.strip().casefold()
        for f in filters:
            if str(f.get("name", "")).casefold() == wanted:
                return f
    return None


# ── статистика ────────────────────────────────────────────────────────────────

async def productivity_stats(client: TodoistClient) -> str:
    """Сколько закрыто, серии дней и недель, дневная цель.

    Путь — /tasks/completed/stats. Задокументированный /user/productivity-stats
    отдаёт 404 (проверено 01.08.2026).
    """
    data = await client.get("/tasks/completed/stats")
    goals = data.get("goals") or {}
    daily = goals.get("current_daily_streak") or {}
    weekly = goals.get("current_weekly_streak") or {}
    days = data.get("days_items") or []
    lines = [
        f"Закрыто задач всего: {data.get('completed_count', '?')}",
        f"Серия дней подряд: {daily.get('count', 0)}; недель подряд: {weekly.get('count', 0)}",
        f"Дневная цель: {goals.get('daily_goal', '?')} задач",
    ]
    recent = [d for d in days[:7]]
    if recent:
        lines.append("Последние дни:")
        lines += [f"  {d.get('date')}: {d.get('total_completed', 0)}" for d in recent]
    return "\n".join(lines)


async def _find_label(client: TodoistClient, name: str) -> dict[str, Any] | None:
    """Найти метку по имени — регистронезависимо, с «@» или без."""
    wanted = str(name).strip().lstrip("@").casefold()
    labels = await client.get_paginated("/labels", params={"limit": 200})
    for label in labels:
        if str(label.get("name", "")).casefold() == wanted:
            return label
    return None


async def manage_structure(client: TodoistClient, action: str, name: str,
                           project_id: str | None = None, parent_id: str | None = None,
                           new_name: str | None = None, confirm: bool = False) -> str:
    """Создать проект / секцию / метку, переименовать или удалить метку.

    action: create_project | create_section | create_label | rename_label | delete_label.
    У меток Todoist правит по id, поэтому имя внутри превращается в id через GET /labels.
    """
    if action == "create_project":
        body = {"name": name}
        if parent_id:
            body["parent_id"] = parent_id
        proj = await client.post("/projects", json=body)
        return f"Создан проект [{proj['id']}] {proj['name']}."
    if action == "create_section":
        if not project_id:
            return "Для секции нужен project_id."
        sec = await client.post("/sections", json={"name": name, "project_id": project_id})
        return f"Создана секция [{sec['id']}] {sec['name']}."
    if action == "create_label":
        label = await client.post("/labels", json={"name": name.lstrip("@")})
        return f"Создана метка @{label['name']}."
    if action == "rename_label":
        target = (new_name or "").strip().lstrip("@")
        if not target:
            return "Для переименования нужен new_name — новое имя метки."
        label = await _find_label(client, name)
        if not label:
            return f"Метка @{name.lstrip('@')} не найдена."
        old = label["name"]
        updated = await client.post(f"/labels/{label['id']}", json={"name": target})
        return (f"Метка @{old} переименована в @{updated.get('name', target)} — "
                f"во всех задачах она обновилась сама.")
    if action == "delete_label":
        label = await _find_label(client, name)
        if not label:
            return f"Метка @{name.lstrip('@')} не найдена."
        if not confirm:
            return (f"Удаление не выполнено: нужен confirm=true. Метка @{label['name']} "
                    f"исчезнет и снимется со всех задач, где стоит. Это необратимо.")
        await client.delete(f"/labels/{label['id']}")
        return f"Метка @{label['name']} удалена и снята со всех задач."

    # ── проекты и секции: переименовать, убрать в архив, вернуть, удалить ──
    #
    # Архив — это PARA в чистом виде: проект кончился, но выбрасывать его жалко
    # и незачем. Раньше через инструменты это было недоступно вовсе — только
    # создание. Пути проверены прогоном 01.08.2026: archive/unarchive у проектов
    # и секций отвечают 200, удаление — 204.
    if action in ("rename_project", "archive_project", "unarchive_project", "delete_project"):
        if not project_id:
            return f"Для действия «{action}» нужен project_id (посмотреть — get_structure)."
        if action == "rename_project":
            target = (new_name or name or "").strip()
            if not target:
                return "Для переименования нужно новое имя (new_name)."
            proj = await client.post(f"/projects/{project_id}", json={"name": target})
            return f"Проект переименован в «{proj.get('name', target)}»."
        if action == "archive_project":
            await client.post(f"/projects/{project_id}/archive")
            return (f"Проект {project_id} убран в архив. Задачи внутри сохранены, "
                    f"вернуть — action=unarchive_project.")
        if action == "unarchive_project":
            await client.post(f"/projects/{project_id}/unarchive")
            return f"Проект {project_id} возвращён из архива."
        if not confirm:
            return ("Удаление не выполнено: нужен confirm=true. Проект исчезнет вместе "
                    "со всеми задачами внутри, и это необратимо. Обычно нужен архив "
                    "(action=archive_project), а не удаление.")
        await client.delete(f"/projects/{project_id}")
        return f"Проект {project_id} удалён вместе с задачами."

    if action in ("rename_section", "archive_section", "unarchive_section", "delete_section"):
        section_id = _section_id_arg(name, parent_id)
        if not section_id:
            return f"Для действия «{action}» нужен id секции (передайте его в parent_id)."
        if action == "rename_section":
            target = (new_name or "").strip()
            if not target:
                return "Для переименования нужно новое имя (new_name)."
            sec = await client.post(f"/sections/{section_id}", json={"name": target})
            return f"Секция переименована в «{sec.get('name', target)}»."
        if action == "archive_section":
            await client.post(f"/sections/{section_id}/archive")
            return f"Секция {section_id} убрана в архив вместе с задачами."
        if action == "unarchive_section":
            await client.post(f"/sections/{section_id}/unarchive")
            return f"Секция {section_id} возвращена из архива."
        if not confirm:
            return ("Удаление не выполнено: нужен confirm=true. Секция исчезнет вместе "
                    "с задачами внутри. Обычно нужен архив (action=archive_section).")
        await client.delete(f"/sections/{section_id}")
        return f"Секция {section_id} удалена вместе с задачами."

    return (f"Неизвестное действие «{action}». Доступно: create_project, create_section, "
            f"create_label, rename_label, delete_label, rename_project, archive_project, "
            f"unarchive_project, delete_project, rename_section, archive_section, "
            f"unarchive_section, delete_section.")


def _section_id_arg(name: str, parent_id: str | None) -> str | None:
    """id секции берётся из parent_id, а если его нет — из name.

    Кнопка одна на всю структуру, и лишнее поле в схеме стоит места у всех.
    Поэтому id секции едет в parent_id — поле, которое у секций всё равно
    ничего другого не значит.
    """
    candidate = (parent_id or "").strip() or (name or "").strip()
    return candidate or None

"""Функции-«кнопки» Todoist — чистая логика поверх TodoistClient, без MCP.

Каждая функция принимает клиент и возвращает готовый человекочитаемый текст:
и MCP-сервер (мак), и бот (сервер) просто оборачивают этот текст. Так одно ядро
служит обоим каналам и будущим автоматизациям (ночной обход, поллер комментариев).

Приоритет — по конвенции API Todoist: 4 = самый высокий (p1 в приложении), 1 = обычный.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .client import TodoistClient, TodoistError


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


async def _create_one(client: TodoistClient, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Создать задачу; если упала из-за дедлайна (Premium-фича на Free — 403),
    пересоздать без дедлайна и вернуть пометку, а не терять задачу целиком."""
    try:
        return await client.post("/tasks", json=payload), ""
    except TodoistError:
        if "deadline_date" in payload:
            trimmed = {k: v for k, v in payload.items() if k != "deadline_date"}
            task = await client.post("/tasks", json=trimmed)
            return task, " (дедлайн — Premium-фича Todoist, создал без него)"
        raise


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
    if not payload:
        return "Нечего менять — не передано ни одного поля."
    task = await client.post(f"/tasks/{task_id}", json=payload)
    return f"Обновлено: {brief(task)}"


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
    return (f"Неизвестное действие «{action}». Доступно: create_project, create_section, "
            f"create_label, rename_label, delete_label.")

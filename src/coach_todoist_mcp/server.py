"""MCP-сервер (FastMCP, транспорт stdio) — тонкая обёртка над core.py.

Токен берётся из TODOIST_API_TOKEN. Один клиент на процесс (stdio — один процесс).
Приоритет везде по конвенции Todoist: 4 = самый высокий (p1 в приложении), 1 = обычный.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import core
from .client import TodoistClient, TodoistError

mcp = FastMCP("todoist")

_client: TodoistClient | None = None


def _todoist() -> TodoistClient:
    global _client
    if _client is None:
        _client = TodoistClient()  # токен из TODOIST_API_TOKEN
    return _client


async def _run(coro_fn, *args: Any, **kwargs: Any) -> str:
    try:
        return await coro_fn(_todoist(), *args, **kwargs)
    except TodoistError as err:
        return f"Ошибка Todoist: {err}"


# ── чтение ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def find_tasks(filter: str = "today", limit: int = 30) -> str:
    """Показать задачи по фильтру Todoist. Главный способ узнать, что в делах.
    Примеры filter: today | overdue | 7 days | @актив | #Личное | no date & !@когда-нибудь."""
    return await _run(core.find_tasks, filter=filter, limit=limit)


@mcp.tool()
async def get_task(task_id: str, include_subtasks: bool = True, include_comments: bool = False) -> str:
    """Карточка задачи: поля, при желании подзадачи и комментарии."""
    return await _run(core.get_task, task_id=task_id,
                      include_subtasks=include_subtasks, include_comments=include_comments)


@mcp.tool()
async def get_comments(task_id: str) -> str:
    """Прочитать все комментарии задачи (в т.ч. написанные Василием от руки).
    В квадратных скобках — id комментария, он нужен для delete_comment."""
    return await _run(core.get_comments, task_id=task_id)


@mcp.tool()
async def get_structure() -> str:
    """Карта Todoist: проекты, секции и метки — чтобы класть задачу в правильное место."""
    return await _run(core.get_structure)


@mcp.tool()
async def completed_history(since: str, until: str, project_id: str = "", limit: int = 50) -> str:
    """История завершённых задач за период. since/until — даты вида YYYY-MM-DD."""
    return await _run(core.completed_history, since=since, until=until,
                      project_id=project_id or None, limit=limit)


# ── запись: задачи ─────────────────────────────────────────────────────────────

@mcp.tool()
async def add_task(content: str, project_id: str = "", section_id: str = "", parent_id: str = "",
                   due_string: str = "", deadline_date: str = "", priority: int = 0,
                   labels: str = "", duration: int = 0, description: str = "") -> str:
    """Создать задачу или подзадачу. parent_id — сделать подзадачей.
    due_string — словами по-русски («завтра», «в пятницу», «каждый пн»).
    priority: 4 — самый высокий (p1), 1 — обычный. labels — через запятую. duration — в минутах.
    deadline_date — дедлайн YYYY-MM-DD (отдельно от срока-напоминания)."""
    return await _run(core.add_task, content=content, project_id=project_id, section_id=section_id,
                      parent_id=parent_id, due_string=due_string, deadline_date=deadline_date,
                      priority=priority, labels=labels, duration=duration, description=description)


@mcp.tool()
async def add_tasks_bulk(tasks: list[dict]) -> str:
    """Создать пачку задач/подзадач одним вызовом (например, разбить дело на шаги).
    Каждый элемент — объект с полями как у add_task: {content, parent_id?, due_string?, priority?, labels?…}."""
    return await _run(core.add_tasks_bulk, tasks=tasks)


@mcp.tool()
async def update_task(task_id: str, content: str = "", due_string: str = "", priority: int = 0,
                      labels: str = "", description: str = "", duration: int = 0) -> str:
    """Изменить задачу: перенести срок (due_string), приоритет, метки, текст, длительность.
    Для повторяющихся задач срок меняется через due_string."""
    return await _run(core.update_task, task_id=task_id, content=content, due_string=due_string,
                      priority=priority, labels=labels, description=description, duration=duration)


@mcp.tool()
async def complete_task(task_id: str, reopen: bool = False) -> str:
    """Закрыть задачу (или переоткрыть при reopen=true)."""
    return await _run(core.complete_task, task_id=task_id, reopen=reopen)


@mcp.tool()
async def move_task(task_id: str, project_id: str = "", section_id: str = "", parent_id: str = "") -> str:
    """Перенести задачу в другой проект/секцию или сделать её подзадачей (parent_id)."""
    return await _run(core.move_task, task_id=task_id, project_id=project_id or None,
                      section_id=section_id or None, parent_id=parent_id or None)


@mcp.tool()
async def delete_task(task_id: str, confirm: bool = False) -> str:
    """Удалить задачу безвозвратно. Только при confirm=true. Каскадом удалит подзадачи."""
    return await _run(core.delete_task, task_id=task_id, confirm=confirm)


@mcp.tool()
async def quick_add(text: str) -> str:
    """Создать задачу одной строкой на естественном языке (#проект, @метка, p1, «завтра в 15:00»)."""
    return await _run(core.quick_add, text=text)


# ── запись: комментарии и структура ────────────────────────────────────────────

@mcp.tool()
async def add_comment(task_id: str, content: str) -> str:
    """Оставить комментарий к задаче (например, ответить Василию внутри задачи)."""
    return await _run(core.add_comment, task_id=task_id, content=content)


@mcp.tool()
async def delete_comment(comment_id: str, confirm: bool = False) -> str:
    """Удалить комментарий задачи безвозвратно. Только при confirm=true.
    id комментария виден в get_comments и в get_task(include_comments=true)."""
    return await _run(core.delete_comment, comment_id=comment_id, confirm=confirm)


@mcp.tool()
async def manage_structure(action: str, name: str, project_id: str = "", parent_id: str = "",
                           new_name: str = "", confirm: bool = False) -> str:
    """Управление структурой Todoist.
    action: create_project | create_section | create_label | rename_label | delete_label.
    name — имя проекта/секции/метки (метку можно писать с @ или без, регистр не важен).
    Для секции нужен project_id, для вложенного проекта — parent_id.
    rename_label: name — как метка называется сейчас, new_name — как назвать.
    delete_label: срабатывает только с confirm=true — метка снимется со всех задач."""
    return await _run(core.manage_structure, action=action, name=name,
                      project_id=project_id or None, parent_id=parent_id or None,
                      new_name=new_name or None, confirm=confirm)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

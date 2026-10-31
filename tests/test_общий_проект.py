"""Общий проект двух людей: исполнитель обязателен, чужое не трогается.

Без сети: подставной Todoist держит задачи в памяти и отвечает на те пути,
которыми ходит ядро. Имена вымышленные — репозиторий публичный.

Запуск: python -m pytest tests/test_общий_проект.py
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from todoist_mcp import core, people
from todoist_mcp.client import TodoistError

Я, АННА = "1", "2"
ОБЩИЙ, ЛИЧНЫЙ = "p-team", "p-home"


class ПоддельныйTodoist:
    def __init__(self) -> None:
        self.key = "тест"
        self.tasks: dict[str, dict[str, Any]] = {}
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.удалены: list[str] = []
        self._n = 0
        self.projects = [
            {"id": ЛИЧНЫЙ, "name": "Дом", "is_shared": False, "workspace_id": None},
            {"id": ОБЩИЙ, "name": "Работа", "is_shared": True, "workspace_id": "w1"},
        ]
        self.collaborators = {ОБЩИЙ: [{"id": Я, "name": "Борис Петров"},
                                      {"id": АННА, "name": "Анна Петрова"}]}

    def задача(self, content: str, project_id: str = ОБЩИЙ, responsible: str | None = None,
               автор: str = Я, parent_id: str | None = None) -> str:
        self._n += 1
        tid = f"t{self._n}"
        self.tasks[tid] = {"id": tid, "content": content, "project_id": project_id,
                           "responsible_uid": responsible, "added_by_uid": автор,
                           "parent_id": parent_id, "labels": [], "priority": 1, "due": None}
        return tid

    async def get(self, path: str, params: dict | None = None) -> Any:
        params = params or {}
        if path == "/user":
            return {"id": Я}
        if path == "/tasks/filter":
            return {"results": list(self.tasks.values())}
        if path.startswith("/tasks/"):
            tid = path.split("/")[2]
            if tid not in self.tasks:
                raise TodoistError("404")
            return self.tasks[tid]
        if path.startswith("/projects/"):
            pid = path.split("/")[2]
            return next(p for p in self.projects if p["id"] == pid)
        if path.startswith("/sections/"):
            return {"id": path.split("/")[2], "project_id": ОБЩИЙ, "name": "Колонка"}
        raise AssertionError(f"неожиданный GET {path}")

    async def get_paginated(self, path: str, params: dict | None = None, **_: Any) -> list:
        params = params or {}
        if path == "/projects":
            return self.projects
        if path.endswith("/collaborators"):
            return self.collaborators.get(path.split("/")[2], [])
        if path == "/tasks":
            return [t for t in self.tasks.values()
                    if all(str(t.get(k)) == str(v) for k, v in params.items() if k != "limit")]
        if path in ("/sections", "/labels", "/comments"):
            return []
        raise AssertionError(f"неожиданный список {path}")

    async def post(self, path: str, json: dict | None = None, **_: Any) -> Any:
        body = dict(json or {})
        self.posts.append((path, body))
        if path == "/tasks":
            tid = self.задача(body["content"], body.get("project_id") or ЛИЧНЫЙ,
                              body.get("assignee_id"), parent_id=body.get("parent_id"))
            return self.tasks[tid]
        if path == "/tasks/quick":
            # грубый разбор: «#Работа» кладёт в общий проект
            pid = ОБЩИЙ if "#Работа" in body["text"] else ЛИЧНЫЙ
            return self.tasks[self.задача(body["text"], pid)]
        tid = path.split("/")[2]
        if path.endswith("/close"):
            self.tasks.pop(tid)
            return None
        if path.endswith("/move"):
            self.tasks[tid].update({k: v for k, v in body.items() if k == "project_id"})
            return None
        if "assignee_id" in body:
            self.tasks[tid]["responsible_uid"] = body.pop("assignee_id")
        self.tasks[tid].update(body)
        return self.tasks[tid]

    async def delete(self, path: str, **_: Any) -> None:
        tid = path.split("/")[2]
        self.удалены.append(tid)
        self.tasks.pop(tid, None)


@pytest.fixture
def todo() -> ПоддельныйTodoist:
    people.забыть()
    return ПоддельныйTodoist()


def run(coro):
    return asyncio.run(coro)


# ── правило «чья задача» ─────────────────────────────────────────────────────

def test_чья_задача_по_исполнителю_а_без_него_по_автору(todo):
    люди = run(people.загрузить(todo))
    assert люди.чужая({"responsible_uid": АННА, "project_id": ОБЩИЙ})
    assert not люди.чужая({"responsible_uid": Я, "project_id": ОБЩИЙ})
    # поставлена руками в приложении без исполнителя — чья завела
    assert люди.чужая({"responsible_uid": None, "project_id": ОБЩИЙ, "added_by_uid": АННА})
    # в личном проекте других людей нет
    assert not люди.чужая({"responsible_uid": None, "project_id": ЛИЧНЫЙ, "added_by_uid": АННА})


def test_имя_находится_по_имени_началу_и_я(todo):
    люди = run(people.загрузить(todo))
    assert люди.найти(ОБЩИЙ, "Анна") == АННА
    assert люди.найти(ОБЩИЙ, "анна петрова") == АННА
    assert люди.найти(ОБЩИЙ, "Петр") is None  # двое — не однозначно
    assert люди.найти(ОБЩИЙ, "мне") == Я
    assert люди.найти(ОБЩИЙ, "Гоша") is None


def test_не_ответил_todoist_значит_всё_своё(todo):
    async def упал(*a, **k):
        raise TodoistError("сеть")
    todo.get = упал
    люди = run(people.загрузить(todo))
    assert not люди.общие and not люди.чужая({"responsible_uid": АННА, "project_id": ОБЩИЙ})


# ── замок 1: исполнитель ─────────────────────────────────────────────────────

def test_в_общий_проект_без_исполнителя_не_ставится(todo):
    out = run(core.add_task(todo, "Сверстать лендинг", project_id=ОБЩИЙ))
    assert out.startswith("Не сделал") and "обязателен исполнитель" in out
    assert not todo.tasks


def test_с_исполнителем_ставится_и_уходит_как_assignee_id(todo):
    out = run(core.add_task(todo, "Сверстать лендинг", project_id=ОБЩИЙ, assignee="Анна"))
    assert "→ Анна" in out
    path, body = todo.posts[-1]
    # responsible_uid Todoist при записи молча выбрасывает — только assignee_id
    assert path == "/tasks" and body["assignee_id"] == АННА and "responsible_uid" not in body


def test_подзадача_общей_карточки_тоже_требует_исполнителя(todo):
    карточка = todo.задача("Мини-продукт", responsible=Я)
    out = run(core.add_task(todo, "Тексты", parent_id=карточка))
    assert out.startswith("Не сделал")


def test_личный_проект_живёт_как_раньше(todo):
    out = run(core.add_task(todo, "Купить хлеб", project_id=ЛИЧНЫЙ))
    assert out.startswith("Создана") and "→" not in out


def test_исполнитель_в_личном_проекте_отказ(todo):
    assert run(core.add_task(todo, "Купить хлеб", project_id=ЛИЧНЫЙ, assignee="Анна")).startswith("Не сделал")


def test_незнакомое_имя_отказ_со_списком(todo):
    out = run(core.add_task(todo, "Х", project_id=ОБЩИЙ, assignee="Гоша"))
    assert "Гоша" in out and "Анна Петрова" in out


def test_второй_раз_ту_же_задачу_не_ставит(todo):
    todo.задача("Сверстать лендинг", responsible=АННА, автор=АННА)
    out = run(core.add_task(todo, "  сверстать   Лендинг ", project_id=ОБЩИЙ, assignee="я"))
    assert out.startswith("Не создал") and "уже стоит" in out and "→ Анна" in out
    assert len(todo.tasks) == 1


def test_пачка_с_одной_ошибкой_не_создаёт_ничего(todo):
    out = run(core.add_tasks_bulk(todo, [
        {"content": "Раз", "project_id": ОБЩИЙ, "assignee": "я"},
        {"content": "Два", "project_id": ОБЩИЙ},
    ]))
    assert "не создана ни одной" in out and "Два" in out
    assert not todo.tasks


def test_пачка_раскладывает_дела_на_двоих(todo):
    out = run(core.add_tasks_bulk(todo, [
        {"content": "Раз", "project_id": ОБЩИЙ, "assignee": "я"},
        {"content": "Два", "project_id": ОБЩИЙ, "assignee": "Анна"},
    ]))
    assert "Создано задач: 2" in out and "→ я" in out and "→ Анна" in out


def test_исполнителя_снять_нельзя(todo):
    tid = todo.задача("Моё", responsible=Я)
    assert "снять нельзя" in run(core.update_task(todo, tid, assignee="никто"))
    assert todo.tasks[tid]["responsible_uid"] == Я


def test_переназначить_свою_можно(todo):
    tid = todo.задача("Моё", responsible=Я)
    out = run(core.update_task(todo, tid, assignee="Анна"))
    assert out.startswith("Обновлено") and todo.tasks[tid]["responsible_uid"] == АННА


def test_в_общий_проект_переезжает_только_с_исполнителем(todo):
    tid = todo.задача("Из дома", project_id=ЛИЧНЫЙ)
    assert run(core.move_task(todo, tid, project_id=ОБЩИЙ)).startswith("Не сделал")
    out = run(core.move_task(todo, tid, project_id=ОБЩИЙ, assignee="я"))
    assert "исполнитель — я" in out and todo.tasks[tid]["responsible_uid"] == Я


def test_быстрый_ввод_в_общий_проект_откатывается(todo):
    out = run(core.quick_add(todo, "Позвонить #Работа"))
    assert out.startswith("Не создал") and not todo.tasks and todo.удалены


# ── замок 2: чужое ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("действие", [
    lambda c, t: core.complete_task(c, t),
    lambda c, t: core.delete_task(c, t, confirm=True),
    lambda c, t: core.update_task(c, t, due_string="завтра"),
    lambda c, t: core.move_task(c, t, project_id=ЛИЧНЫЙ),
])
def test_чужую_задачу_не_трогает(todo, действие):
    tid = todo.задача("Дело Анны", responsible=АННА, автор=Я)
    out = run(действие(todo, tid))
    assert out.startswith("Не сделал") and "ведёт Анна" in out and "add_comment" in out
    assert todo.tasks[tid]["content"] == "Дело Анны" and tid not in todo.удалены


def test_свою_закрывает(todo):
    tid = todo.задача("Моё", responsible=Я)
    assert "закрыта" in run(core.complete_task(todo, tid))


# ── видимость ────────────────────────────────────────────────────────────────

def test_карта_помечает_общий_проект(todo):
    out = run(core.get_structure(todo))
    assert "Работа — ОБЩИЙ" in out and "Анна Петрова" in out
    assert "Дом — ОБЩИЙ" not in out


def test_карточка_с_подзадачами_разных_людей_общая(todo):
    карточка = todo.задача("Мини-продукт", responsible=Я)
    todo.задача("Тексты", responsible=АННА, parent_id=карточка)
    todo.задача("Сборка", responsible=Я, parent_id=карточка)
    out = run(core.get_task(todo, карточка))
    assert "общая карточка: подзадачи у Анна, я" in out
    assert "→ Анна" in out


def test_чужая_карточка_говорит_об_этом(todo):
    tid = todo.задача("Дело Анны", responsible=АННА)
    assert "чужая задача (ведёт Анна)" in run(core.get_task(todo, tid))


def test_поиск_показывает_исполнителя(todo):
    todo.задача("Без хозяина")
    todo.задача("Её", responsible=АННА)
    out = run(core.find_tasks(todo, "today"))
    assert "Её → Анна" in out and "Без хозяина → без исполнителя" in out

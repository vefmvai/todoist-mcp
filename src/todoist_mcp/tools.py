"""Манифест кнопок — единственное место, где объявлен список инструментов.

Зачем он появился. До 01.08.2026 список объявлялся дважды: в `server.py`
этого пакета и в SDK-обёртке бота. Общими были только `core.py` и `client.py`,
а имя кнопки, её описание и набор полей писались руками по два раза — и уже
разошлись:

  • `update_task` в MCP-сервере не знал про `deadline_date`, у бота знал;
  • `add_tasks_bulk` принимал список объектов у одного и JSON-строку у другого;
  • поле длительности звалось `duration` и `duration_minutes`.

Каждая из трёх поломок по отдельности выглядела как рабочий код: обёртки
никто не сверял, потому что сверять было не с чем.

Теперь кнопка объявляется здесь один раз, а обёртки собираются из манифеста
циклом. Их по-прежнему две, и это не недосмотр: движки принимают разные
типы. FastMCP умеет поле-список объектов, SDK бота — только плоские
(`str`, `int`, `bool`). Разница названа в `Field.kind` и раскрывается
каждой обёрткой по-своему; общее — здесь.

Три пометки на каждой кнопке и поле:

  `summary`  — что делает, человеческим языком. Из этого печатается таблица
               для человека: она ДОЛЖНА собираться отсюда, а не писаться
               руками, иначе у списка кнопок снова станет два дома.
  `feature`  — какая возможность тарифа нужна. Нет её — поле или кнопка
               не объявляется вовсе, и модель физически не может её позвать.
  тумблеры   — см. `toggles.py`: человек решает, что грузить в сессию.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import core, toggles
from .client import TodoistClient
from .plan import Plan

# Типы полей. `json` — список объектов: FastMCP отдаёт его как есть,
# SDK-обёртка принимает строкой и разбирает.
KINDS = ("str", "int", "bool", "json")


@dataclass(frozen=True)
class Field:
    name: str
    kind: str = "str"
    description: str = ""
    required: bool = False
    default: Any = None
    feature: str | None = None  # нужна возможность тарифа

    def available(self, plan: Plan) -> bool:
        return self.feature is None or plan.has(self.feature)


@dataclass(frozen=True)
class Tool:
    name: str
    summary: str          # для человека и для таблицы
    description: str      # для модели
    run: Callable[[TodoistClient, dict[str, Any]], Awaitable[str]]
    fields: tuple[Field, ...] = ()
    feature: str | None = None  # вся кнопка требует возможности тарифа

    def available(self, plan: Plan) -> bool:
        return self.feature is None or plan.has(self.feature)

    def visible_fields(self, plan: Plan) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if f.available(plan))


def _int(args: dict[str, Any], name: str, default: int) -> int:
    try:
        return int(args.get(name) or default)
    except (TypeError, ValueError):
        return default


def _text(args: dict[str, Any], name: str, default: str = "") -> str:
    value = args.get(name)
    return str(value) if value not in (None, "") else default


def _collect(args: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    """Только переданные поля — ядро само разберётся с остальным."""
    out: dict[str, Any] = {}
    for name in names:
        value = args.get(name)
        if value in (None, ""):
            continue
        if name in ("priority", "duration"):
            try:
                out[name] = int(value)
            except (TypeError, ValueError):
                continue
        else:
            out[name] = value
    return out


_TASK_FIELDS = ("project_id", "section_id", "parent_id", "due_string",
                "deadline_date", "priority", "labels", "description", "duration", "assignee")

# Поля, общие для add_task и update_task. Объявляются один раз, потому что
# именно их расхождение и было первой найденной поломкой.
_DUE = Field("due_string", description="Срок словами по-русски: «завтра», «в пятницу», «каждый пн». Снять срок совсем — «без даты»")
_DEADLINE = Field("deadline_date", description="Жёсткий дедлайн, YYYY-MM-DD (отдельно от срока)",
                  feature="deadlines")
_PRIORITY = Field("priority", "int", "Приоритет: 4 — самый высокий (p1 в приложении), 1 — обычный")
_LABELS = Field("labels", description="Метки через запятую, с @ или без")
_DESCRIPTION = Field("description", description="Описание карточки — зачем это и что важно помнить")
_DURATION = Field("duration", "int", "Оценка времени в минутах", feature="durations")
_ASSIGNEE = Field("assignee", description=(
    "Исполнитель — имя участника общего проекта или «я». В общем проекте "
    "обязателен у каждой задачи; в личном не нужен. Участники видны в get_structure"))


TOOLS: tuple[Tool, ...] = (
    # ── чтение ───────────────────────────────────────────────────────────
    Tool(
        name="find_tasks",
        summary="Найти задачи по запросу Todoist",
        description=(
            "Показать задачи по фильтру на языке Todoist "
            "(today, overdue, 7 days, @дом, #Личное, no date & !@отпуск). "
            "Главный способ узнать, что сейчас в делах. В общих проектах строка "
            "показывает исполнителя (→ я, → имя); по нему фильтруют "
            "«assigned to: me», «assigned to: others», «!assigned to: others»."
        ),
        fields=(
            Field("filter", description="Запрос Todoist; по умолчанию today", default="today"),
            Field("limit", "int", "Сколько задач вернуть, максимум 100", default=30),
        ),
        run=lambda c, a: core.find_tasks(c, _text(a, "filter", "today"), _int(a, "limit", 30)),
    ),
    Tool(
        name="get_task",
        summary="Открыть карточку задачи целиком",
        description=(
            "Карточка одной задачи по id: поля, описание, при желании подзадачи "
            "и комментарии. Нужна, когда надо копнуть конкретное дело, а не список."
        ),
        fields=(
            Field("task_id", required=True, description="id задачи"),
            Field("include_subtasks", "bool", "Показать подзадачи", default=True),
            Field("include_comments", "bool", "Показать комментарии", default=False),
        ),
        run=lambda c, a: core.get_task(
            c, _text(a, "task_id"),
            include_subtasks=a.get("include_subtasks", True),
            include_comments=a.get("include_comments", False),
        ),
    ),
    Tool(
        name="get_comments",
        summary="Прочитать комментарии задачи",
        description=(
            "Все комментарии задачи по id. В квадратных скобках — id самого "
            "комментария, он нужен для update_comment и delete_comment."
        ),
        fields=(Field("task_id", required=True, description="id задачи"),),
        run=lambda c, a: core.get_comments(c, _text(a, "task_id")),
    ),
    Tool(
        name="get_structure",
        summary="Карта проектов, секций и меток",
        description="Карта Todoist: проекты, секции и метки — чтобы класть задачу в правильное место.",
        run=lambda c, a: core.get_structure(c),
    ),
    Tool(
        name="completed_history",
        summary="История закрытых задач за период",
        description=(
            "Что реально сделано за период. since и until — даты YYYY-MM-DD, "
            "при желании по одному проекту."
        ),
        fields=(
            Field("since", required=True, description="Начало периода, YYYY-MM-DD"),
            Field("until", required=True, description="Конец периода, YYYY-MM-DD"),
            Field("project_id", description="Ограничить одним проектом"),
            Field("limit", "int", "Сколько записей вернуть", default=50),
        ),
        run=lambda c, a: core.completed_history(
            c, _text(a, "since"), _text(a, "until"),
            project_id=_text(a, "project_id") or None, limit=_int(a, "limit", 50),
        ),
    ),
    Tool(
        name="productivity_stats",
        summary="Сколько закрыто, серии дней и недель, дневная цель",
        description=(
            "Статистика Todoist: всего закрыто задач, текущая серия дней и недель, "
            "дневная цель, закрытия по последним дням. Для обзоров и итогов периода."
        ),
        run=lambda c, a: core.productivity_stats(c),
    ),

    # ── задачи ───────────────────────────────────────────────────────────
    Tool(
        name="add_task",
        summary="Создать задачу или подзадачу",
        description=(
            "Создать задачу, а через parent_id — подзадачу существующей. "
            "Приоритет: 4 — самый высокий (p1 в приложении), 1 — обычный. "
            "В общий проект — только с исполнителем (assignee), и только то, чего "
            "там ещё нет: такая же задача, уже поставленная другим, — отказ."
        ),
        fields=(
            Field("content", required=True, description="Текст задачи"),
            Field("project_id", description="В какой проект положить"),
            Field("section_id", description="В какую секцию (столбец) положить"),
            Field("parent_id", description="id родительской задачи — сделать подзадачей"),
            _ASSIGNEE, _DUE, _DEADLINE, _PRIORITY, _LABELS, _DESCRIPTION, _DURATION,
        ),
        run=lambda c, a: core.add_task(c, _text(a, "content"), **_collect(a, _TASK_FIELDS)),
    ),
    Tool(
        name="add_tasks_bulk",
        summary="Создать пачку задач одним вызовом",
        description=(
            "Создать несколько задач или подзадач за раз — например, разбить дело "
            "на шаги. Каждый элемент — объект с теми же полями, что у add_task. "
            "Пачка проверяется целиком до создания: одна задача без исполнителя "
            "в общем проекте — не создаётся ни одна."
        ),
        fields=(
            Field("tasks", "json", required=True,
                  description='Массив объектов: [{"content":"…","parent_id":"…","due_string":"завтра","assignee":"я"}]'),
        ),
        run=lambda c, a: core.add_tasks_bulk(c, a.get("tasks") or []),
    ),
    Tool(
        name="update_task",
        summary="Изменить задачу: срок, приоритет, метки, текст",
        description=(
            "Изменить существующую задачу: перенести срок (due_string), сменить "
            "приоритет, метки, текст, описание. Смену проекта или родителя делает "
            "move_task. У повторяющихся задач срок меняется через due_string. "
            "Сменить исполнителя — assignee. Задачу другого человека менять нельзя."
        ),
        fields=(
            Field("task_id", required=True, description="id задачи"),
            Field("content", description="Новый текст задачи"),
            _ASSIGNEE, _DUE, _DEADLINE, _PRIORITY, _LABELS, _DESCRIPTION, _DURATION,
        ),
        run=lambda c, a: core.update_task(
            c, _text(a, "task_id"),
            **_collect(a, ("content", "due_string", "deadline_date", "priority",
                           "labels", "description", "duration", "assignee")),
        ),
    ),
    Tool(
        name="complete_task",
        summary="Закрыть задачу или вернуть закрытую",
        description=(
            "Отметить задачу сделанной. Чтобы вернуть ошибочно закрытую — reopen=true. "
            "У повторяющихся задач закрытие передвигает срок на следующий раз. "
            "Задачу другого человека закрывает только он сам или его агент."
        ),
        fields=(
            Field("task_id", required=True, description="id задачи"),
            Field("reopen", "bool", "Вернуть закрытую задачу обратно", default=False),
        ),
        run=lambda c, a: core.complete_task(c, _text(a, "task_id"), reopen=a.get("reopen", False)),
    ),
    Tool(
        name="move_task",
        summary="Перенести задачу в другой проект, секцию или под родителя",
        description=(
            "Перенести задачу в другой проект или секцию либо сделать её подзадачей "
            "(parent_id). Именно для смены места — срок и текст меняет update_task. "
            "В общий проект задача без исполнителя едет только с assignee."
        ),
        fields=(
            Field("task_id", required=True, description="id задачи"),
            Field("project_id", description="Куда перенести — проект"),
            Field("section_id", description="Куда перенести — секция"),
            Field("parent_id", description="Под какую задачу подвесить"),
            _ASSIGNEE,
        ),
        run=lambda c, a: core.move_task(
            c, _text(a, "task_id"),
            project_id=_text(a, "project_id") or None,
            section_id=_text(a, "section_id") or None,
            parent_id=_text(a, "parent_id") or None,
            assignee=_text(a, "assignee") or None,
        ),
    ),
    Tool(
        name="delete_task",
        summary="Удалить задачу безвозвратно",
        description=(
            "Удалить задачу насовсем, каскадом с подзадачами. Необратимо, поэтому "
            "срабатывает только с confirm=true. Обычно задачи закрывают, а не удаляют. "
            "Задачу другого человека удалить нельзя."
        ),
        fields=(
            Field("task_id", required=True, description="id задачи"),
            Field("confirm", "bool", "Подтверждение: без него ничего не произойдёт", default=False),
        ),
        run=lambda c, a: core.delete_task(c, _text(a, "task_id"), confirm=a.get("confirm", False)),
    ),
    Tool(
        name="quick_add",
        summary="Создать задачу одной строкой на естественном языке",
        description=(
            "Быстрый ввод в стиле Todoist, когда всё в одной фразе: "
            "«Позвонить завтра в 15 #Личное @звонки p1». "
            "Для общего проекта не годится — там нужен исполнитель, ставьте через add_task."
        ),
        fields=(Field("text", required=True, description="Строка быстрого ввода"),),
        run=lambda c, a: core.quick_add(c, _text(a, "text")),
    ),

    # ── комментарии ──────────────────────────────────────────────────────
    Tool(
        name="add_comment",
        summary="Оставить комментарий к задаче",
        description="Добавить комментарий к задаче по id: заметка, контекст, договорённость.",
        fields=(
            Field("task_id", required=True, description="id задачи"),
            Field("content", required=True, description="Текст комментария"),
        ),
        run=lambda c, a: core.add_comment(c, _text(a, "task_id"), _text(a, "content")),
    ),
    Tool(
        name="update_comment",
        summary="Переписать текст комментария",
        description=(
            "Изменить существующий комментарий по его id (id виден в get_comments). "
            "Правка вместо удаления и написания заново — история задачи остаётся целой."
        ),
        fields=(
            Field("comment_id", required=True, description="id комментария"),
            Field("content", required=True, description="Новый текст"),
        ),
        run=lambda c, a: core.update_comment(c, _text(a, "comment_id"), _text(a, "content")),
    ),
    Tool(
        name="delete_comment",
        summary="Удалить комментарий безвозвратно",
        description=(
            "Удалить комментарий по его id (виден в get_comments и в карточке задачи). "
            "Необратимо, поэтому срабатывает только с confirm=true."
        ),
        fields=(
            Field("comment_id", required=True, description="id комментария"),
            Field("confirm", "bool", "Подтверждение: без него ничего не произойдёт", default=False),
        ),
        run=lambda c, a: core.delete_comment(
            c, _text(a, "comment_id"), confirm=a.get("confirm", False)),
    ),

    # ── структура и фильтры ──────────────────────────────────────────────
    Tool(
        name="manage_structure",
        summary="Проекты, секции и метки: создать, переименовать, убрать в архив, удалить",
        description=(
            "Управление устройством Todoist. action: create_project | rename_project | "
            "archive_project | unarchive_project | delete_project | create_section | "
            "rename_section | archive_section | unarchive_section | delete_section | "
            "create_label | rename_label | delete_label. "
            "Для действий с проектом нужен project_id; id секции передаётся в parent_id; "
            "метку можно писать с @ или без, регистр не важен. Удаление и снятие метки "
            "срабатывают только с confirm=true. Архив сохраняет задачи, удаление — нет. "
            "Пользуйся осознанно: устройство задаёт человек."
        ),
        fields=(
            Field("action", required=True, description="Что сделать — см. список в описании"),
            Field("name", description="Имя проекта, секции или метки"),
            Field("project_id", description="id проекта — для секций и действий с проектом"),
            Field("parent_id", description="id родительского проекта либо id секции"),
            Field("new_name", description="Новое имя при переименовании"),
            Field("confirm", "bool", "Подтверждение для необратимых действий", default=False),
        ),
        run=lambda c, a: core.manage_structure(
            c, _text(a, "action"), _text(a, "name"),
            project_id=_text(a, "project_id") or None,
            parent_id=_text(a, "parent_id") or None,
            new_name=_text(a, "new_name") or None,
            confirm=a.get("confirm", False),
        ),
    ),
    Tool(
        name="manage_filters",
        summary="Сохранённые фильтры: посмотреть, завести, поправить, удалить",
        description=(
            "Сохранённые запросы Todoist. action: list | create | update | delete. "
            "Фильтр — это запрос вроде «no date & !@отпуск», сохранённый под именем. "
            "Слотов на бесплатном тарифе три, и Todoist их не стережёт — счёт ведётся здесь. "
            "Разовый вопрос к делам фильтра не требует: для него есть find_tasks."
        ),
        fields=(
            Field("action", description="list | create | update | delete", default="list"),
            Field("name", description="Имя фильтра"),
            Field("query", description="Сам запрос на языке Todoist"),
            Field("filter_id", description="id фильтра — точнее, чем имя"),
            Field("confirm", "bool", "Подтверждение для удаления", default=False),
        ),
        run=lambda c, a: core.manage_filters(
            c, _text(a, "action", "list"), _text(a, "name"), _text(a, "query"),
            _text(a, "filter_id"), confirm=a.get("confirm", False),
        ),
    ),
)


def by_name() -> dict[str, Tool]:
    return {tool.name: tool for tool in TOOLS}


def selected(plan: Plan, switches: dict[str, bool] | None = None) -> list[Tool]:
    """Кнопки, которые действительно объявляются: доступные и включённые."""
    return [
        tool for tool in TOOLS
        if tool.available(plan) and toggles.is_on(tool.name, switches)
    ]

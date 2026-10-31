"""Люди в общих проектах: кто я, кто ещё участвует и чья задача.

Зачем. Когда в общем проекте работают двое и у каждого свой агент, оба читают
все задачи подряд. Без знания «чья» каждый начнёт напоминать своему человеку
чужие дела, закрывать их и ставить заново то, что второй уже разложил.

Правило «чья задача» одно на весь пакет и на бота — функция `чужая`:

  исполнитель есть      → задача исполнителя;
  исполнителя нет,
    проект общий        → задача того, кто её завёл (поставили руками в приложении);
    проект личный       → моя, других людей там не бывает.

Исполнитель у задачи Todoist ровно один (`responsible_uid`). «Общая карточка» —
не поле, а вывод: у подзадач разные исполнители (`исполнители_подзадач`).

Кеш на процесс с коротким сроком: бот открывает новый клиент на каждый вызов,
и без кеша каждая строка задачи стоила бы лишних запросов. Участники проекта
меняются редко, десять минут устаревания безопасны.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .client import TodoistClient, TodoistError

СРОК_КЕША = 600  # секунд

# «Я» по-русски во всех формах, в каких его скажет агент: «назначь мне»,
# «исполнитель — я», «возьму себе».
Я = {"я", "мне", "меня", "мой", "моя", "себе", "сам", "сама", "me"}

# Снять исполнителя. В общем проекте это запрещено — слова нужны, чтобы
# отказать словами, а не принять «никто» за имя человека.
НИКТО = {"никто", "никому", "без исполнителя", "снять", "-", "none"}


@dataclass
class Люди:
    """Снимок «кто я и кто где» на момент загрузки."""

    я: str | None = None
    имена: dict[str, str] = field(default_factory=dict)          # uid → полное имя
    общие: dict[str, list[str]] = field(default_factory=dict)    # project_id → uid участников

    def общий(self, project_id: str | None) -> bool:
        return bool(project_id) and str(project_id) in self.общие

    def имя(self, uid: str | None) -> str:
        """Короткое имя для строки задачи: «я» или первое слово полного имени."""
        if not uid:
            return ""
        if uid == self.я:
            return "я"
        полное = self.имена.get(str(uid), "")
        return полное.split()[0] if полное.strip() else f"участник {uid}"

    def хозяин(self, task: dict[str, Any]) -> str | None:
        """uid того, чья задача. None — личный проект без исполнителя: значит моя."""
        if task.get("responsible_uid"):
            return str(task["responsible_uid"])
        if self.общий(task.get("project_id")):
            return str(task.get("added_by_uid") or "") or None
        return None

    def чужая(self, task: dict[str, Any]) -> bool:
        """Задача другого человека. Не знаем, кто я, — считаем всё своим, как раньше."""
        хозяин = self.хозяин(task)
        return bool(self.я and хозяин and хозяин != self.я)

    def участники(self, project_id: str) -> list[str]:
        return list(self.общие.get(str(project_id), []))

    def найти(self, project_id: str, кто: str) -> str | None:
        """Имя, «я» или uid → uid участника проекта. None — такого нет или не однозначно."""
        слово = (кто or "").strip().lower()
        if not слово:
            return None
        if слово in Я:
            return self.я
        uids = self.участники(project_id)
        if слово in uids:
            return слово
        полные = [u for u in uids if self.имена.get(u, "").lower() == слово]
        if len(полные) == 1:
            return полные[0]
        первые = [u for u in uids if (self.имена.get(u, "").lower().split() or [""])[0] == слово]
        if len(первые) == 1:
            return первые[0]
        if len(слово) >= 3:
            начала = [u for u in uids if any(часть.startswith(слово)
                                            for часть in self.имена.get(u, "").lower().split())]
            if len(начала) == 1:
                return начала[0]
        return None

    def список(self, project_id: str) -> str:
        """Участники словами: «я, Анна» — для отказов и карты."""
        return ", ".join(self.имя(u) if u == self.я else self.имена.get(u, u)
                         for u in self.участники(project_id))


_кеш: dict[Any, tuple[float, Люди]] = {}


def _ключ(client: TodoistClient) -> Any:
    return getattr(client, "key", None) or id(client)


def забыть() -> None:
    """Сбросить кеш — для тестов и после правки участников."""
    _кеш.clear()


async def загрузить(client: TodoistClient) -> Люди:
    """Кто я, какие проекты общие, кто в них. Todoist не ответил — пустой снимок.

    Пустой снимок значит «всё моё, общих проектов нет» — то есть поведение
    до появления общих проектов. Строка задачи не должна падать оттого, что
    не загрузились участники.
    """
    ключ = _ключ(client)
    сейчас = time.monotonic()
    if ключ in _кеш and сейчас - _кеш[ключ][0] < СРОК_КЕША:
        return _кеш[ключ][1]
    люди = Люди()
    try:
        люди.я = str((await client.get("/user")).get("id") or "") or None
        проекты = await client.get_paginated("/projects", params={"limit": 200})
        for проект in проекты:
            if not (проект.get("is_shared") or проект.get("workspace_id")):
                continue
            состав = await client.get_paginated(f"/projects/{проект['id']}/collaborators")
            люди.общие[str(проект["id"])] = [str(c["id"]) for c in состав]
            for c in состав:
                люди.имена[str(c["id"])] = c.get("name") or c.get("full_name") or ""
    except (TodoistError, AttributeError, KeyError, TypeError):
        return Люди()
    _кеш[ключ] = (сейчас, люди)
    return люди


def исполнители_подзадач(люди: Люди, подзадачи: list[dict[str, Any]]) -> list[str]:
    """Разные хозяева подзадач по порядку появления. Больше одного — карточка общая."""
    out: list[str] = []
    for t in подзадачи:
        хозяин = люди.хозяин(t)
        if хозяин and хозяин not in out:
            out.append(хозяин)
    return out

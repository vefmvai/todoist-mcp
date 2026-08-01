"""Разбор ответа Todoist: список лежит под `results` либо под `items`.

Куплено прогоном 01.08: ночной снимок собирал закрытые задачи через
`get_paginated` с ключом `results`, а `/tasks/completed/by_completion_date`
отдаёт `items`. В базе оказалось 0 закрытых задач при 1826 по счётчику
самого Todoist — и это выглядело как «человек ничего не закрывал», а не
как поломка.

Тест держит обе формы, чтобы починка пережила следующий каприз API.
Запуск: python -m pytest tests/test_client.py
"""

from __future__ import annotations

import asyncio

import pytest

from todoist_mcp import client as client_module
from todoist_mcp.client import LIST_KEYS, TodoistClient


class ЗаглушкаОтветов:
    """Отдаёт заранее заготовленные страницы вместо сети."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    async def __call__(self, path: str, params: dict | None = None):
        self.calls.append(dict(params or {}))
        return self.pages[len(self.calls) - 1]


def _клиент(pages: list[dict]) -> tuple[TodoistClient, ЗаглушкаОтветов]:
    # Токен латиницей: он уезжает в HTTP-заголовок, а тот только ascii.
    c = TodoistClient(token="test-token")
    stub = ЗаглушкаОтветов(pages)
    c.get = stub  # type: ignore[assignment]
    return c, stub


def _собрать(pages: list[dict], **kwargs) -> list[dict]:
    c, _ = _клиент(pages)
    try:
        return asyncio.run(c.get_paginated("/что-нибудь", **kwargs))
    finally:
        asyncio.run(c.aclose())


def test_список_под_results():
    assert _собрать([{"results": [{"id": "1"}]}]) == [{"id": "1"}]


def test_список_под_items():
    """Форма завершённых задач. Ровно она и терялась."""
    assert _собрать([{"items": [{"id": "9"}]}]) == [{"id": "9"}]


def test_голый_список_проходит_насквозь():
    assert _собрать([[{"id": "3"}]]) == [{"id": "3"}]


def test_незнакомая_форма_даёт_пусто_а_не_падение():
    """Ответ без обоих ключей — пусто. Ронять ночной прогон из-за этого нельзя."""
    assert _собрать([{"неведомое": [{"id": "1"}]}]) == []


def test_явный_ключ_сильнее_умолчания():
    assert _собрать([{"results": [{"id": "1"}], "items": [{"id": "2"}]}], key="items") \
        == [{"id": "2"}]


def test_курсор_доводит_до_конца_и_на_items():
    страницы = [
        {"items": [{"id": "1"}], "next_cursor": "дальше"},
        {"items": [{"id": "2"}]},
    ]
    c, stub = _клиент(страницы)
    try:
        собрано = asyncio.run(c.get_paginated("/что-нибудь"))
    finally:
        asyncio.run(c.aclose())
    assert собрано == [{"id": "1"}, {"id": "2"}]
    assert stub.calls[1]["cursor"] == "дальше", "курсор не поехал во второй запрос"


def test_потолок_режет():
    страницы = [{"results": [{"id": str(i)} for i in range(10)], "next_cursor": "ещё"}]
    assert len(_собрать(страницы, cap=4)) == 4


def test_порядок_кандидатов_объявлен():
    """Сначала частая форма, потом редкая: иначе на каждом ответе лишний промах."""
    assert LIST_KEYS == ("results", "items")
    assert client_module.LIST_KEYS is LIST_KEYS

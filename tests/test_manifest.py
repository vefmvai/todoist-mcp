"""Проверки манифеста, тарифа и тумблеров — без сети и без токена.

Живой smoke-тест (`test_smoke.py`) проверяет, что кнопки работают.
Здесь — что они правильно ОБЪЯВЛЕНЫ: один дом, платное не предлагается,
выключенное не грузится. Именно эти три вещи ломались молча.

Запуск: python -m pytest tests/test_manifest.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from todoist_mcp import core, plan, server, table, toggles, tools

FREE = plan.Plan(name="free", current={"deadlines": False, "durations": False,
                                       "comments": True, "max_filters": 3}, source="api")
PRO = plan.Plan(name="pro", current={"deadlines": True, "durations": True,
                                     "comments": True, "max_filters": 150}, source="api")


# ── один дом ──────────────────────────────────────────────────────────────────

def test_имена_кнопок_не_повторяются():
    names = [t.name for t in tools.TOOLS]
    assert len(names) == len(set(names))


def test_у_каждой_кнопки_есть_и_описание_и_строчка_для_человека():
    for t in tools.TOOLS:
        assert t.summary and t.description, t.name
        assert t.run is not None, t.name


def test_поля_объявлены_известными_типами():
    for t in tools.TOOLS:
        for f in t.fields:
            assert f.kind in tools.KINDS, (t.name, f.name, f.kind)


# ── тариф ─────────────────────────────────────────────────────────────────────

def test_платные_поля_не_объявляются_на_бесплатном():
    add = tools.by_name()["add_task"]
    free_fields = {f.name for f in add.visible_fields(FREE)}
    pro_fields = {f.name for f in add.visible_fields(PRO)}
    assert "deadline_date" not in free_fields and "duration" not in free_fields
    assert {"deadline_date", "duration"} <= pro_fields


def test_правка_задачи_тоже_прячет_платные_поля():
    """Расхождение, с которого начался этап: у update_task не было дедлайна."""
    upd = tools.by_name()["update_task"]
    assert {"deadline_date", "duration"} <= {f.name for f in upd.visible_fields(PRO)}
    assert "deadline_date" not in {f.name for f in upd.visible_fields(FREE)}


def test_неизвестная_возможность_считается_недоступной():
    """Умолчание при незнании — «нельзя»: ошибка в эту сторону дешевле."""
    assert plan.Plan(name="?").has("deadlines") is False


def test_ядро_вырезает_платное_и_говорит_словами(monkeypatch):
    monkeypatch.setattr(plan, "_MEMO", FREE)
    payload, note = core._strip_premium(
        {"content": "x", "deadline_date": "2026-01-01", "duration": 30, "duration_unit": "minute"})
    assert "deadline_date" not in payload and "duration" not in payload
    assert "duration_unit" not in payload
    assert "дедлайны" in note and "недоступно" in note


def test_на_платном_ядро_ничего_не_режет(monkeypatch):
    monkeypatch.setattr(plan, "_MEMO", PRO)
    payload, note = core._strip_premium({"content": "x", "deadline_date": "2026-01-01"})
    assert payload["deadline_date"] == "2026-01-01" and note == ""


def test_переменная_pro_отвечает_да_на_всё(monkeypatch):
    monkeypatch.setenv("TODOIST_PLAN", "pro")
    p = plan.load()
    assert p.source == "env" and p.has("deadlines") and p.has("что_угодно")


def test_переменная_free_отвечает_нет(monkeypatch):
    monkeypatch.setenv("TODOIST_PLAN", "free")
    p = plan.load()
    assert p.source == "env" and not p.has("deadlines")


# ── тумблеры ──────────────────────────────────────────────────────────────────

def test_кнопки_нет_в_файле_значит_включена():
    assert toggles.is_on("add_task", {"find_tasks": False}) is True


def test_выключенная_кнопка_не_объявляется():
    chosen = {t.name for t in tools.selected(FREE, {"delete_task": False})}
    assert "delete_task" not in chosen and "add_task" in chosen


def test_тумблеры_читаются_из_yaml(tmp_path: Path):
    file = tmp_path / "tools.yaml"
    file.write_text("tools:\n  add_task: off\n  find_tasks: on\n", encoding="utf-8")
    assert toggles.load(file) == {"add_task": False, "find_tasks": True}


def test_сломанный_файл_не_оставляет_агента_без_кнопок(tmp_path: Path):
    """Опечатка в необязательном файле не должна обезоруживать агента."""
    file = tmp_path / "tools.yaml"
    file.write_text("tools: [это не словарь\n", encoding="utf-8")
    assert toggles.load(file) == {}
    assert len(tools.selected(FREE, toggles.load(file))) == len(tools.TOOLS)


def test_файла_нет_значит_всё_включено(tmp_path: Path):
    assert toggles.load(tmp_path / "нет-такого.yaml") == {}


# ── сборка сервера ────────────────────────────────────────────────────────────

def _weigh(switches: dict[str, bool], plan_obj: plan.Plan = FREE) -> tuple[int, int]:
    mcp = FastMCP("t")
    names = server.register(mcp, plan_obj, switches)
    listed = asyncio.run(mcp.list_tools())
    payload = [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
               for t in listed]
    return len(names), len(json.dumps(payload, ensure_ascii=False).encode())


def test_выключенная_кнопка_перестаёт_весить():
    """Тумблер, который не экономит место, — украшение."""
    all_count, all_bytes = _weigh({})
    less_count, less_bytes = _weigh({"manage_filters": False, "productivity_stats": False})
    assert less_count == all_count - 2
    assert less_bytes < all_bytes


def test_схема_собирается_из_полей_манифеста():
    mcp = FastMCP("t")
    server.register(mcp, FREE, {})
    listed = {t.name: t for t in asyncio.run(mcp.list_tools())}
    schema = listed["add_task"].inputSchema
    assert schema["properties"]["content"]["type"] == "string"
    assert schema["required"] == ["content"]
    assert "deadline_date" not in schema["properties"]  # платное на Free
    assert listed["add_tasks_bulk"].inputSchema["properties"]["tasks"]["type"] == "array"


def test_описания_полей_доезжают_до_схемы():
    mcp = FastMCP("t")
    server.register(mcp, FREE, {})
    listed = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert listed["get_task"].inputSchema["properties"]["task_id"]["description"]


def test_обработчик_получает_настоящую_сигнатуру():
    handler = server._make_handler(tools.by_name()["find_tasks"],
                                   tools.by_name()["find_tasks"].fields)
    assert list(inspect.signature(handler).parameters) == ["filter", "limit"]


# ── прибор ────────────────────────────────────────────────────────────────────

def test_прибор_молчит_когда_всё_сходится():
    text, code = table.render(FREE, {})
    assert code == 0 and "⚠" not in text


def test_прибор_кричит_когда_todoist_поменял_правила(monkeypatch):
    fake = tools.Tool(name="x", summary="s", description="d",
                      run=lambda c, a: None, feature="телепортация")
    monkeypatch.setattr(tools, "TOOLS", tools.TOOLS + (fake,))
    text, code = table.render(FREE, {})
    assert code == 1 and "телепортация" in text


def test_прибор_предупреждает_когда_тариф_не_спрошен():
    text, code = table.render(plan.Plan(name="?", source="fallback"), {})
    assert code == 1 and "не спрошен" in text


def test_таблица_показывает_все_кнопки_из_манифеста():
    """Таблица печатается из манифеста — новая кнопка попадает в неё сама."""
    text, _ = table.render(FREE, {})
    for t in tools.TOOLS:
        assert t.name in text and t.summary in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

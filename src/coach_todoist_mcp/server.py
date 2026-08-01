"""MCP-сервер (FastMCP, транспорт stdio) — обёртка над манифестом кнопок.

Здесь не объявлено ни одной кнопки. Список живёт в `tools.py`, логика —
в `core.py`, а этот файл только разворачивает манифест в инструменты MCP:
берёт имя, описание и поля, строит из них сигнатуру и отдаёт движку.

Что не объявляется:
  • кнопки и поля, которых нет на тарифе (`plan.py`);
  • кнопки, выключенные человеком (`toggles.py`).

Выключенное не просто не работает — оно не занимает места в контексте,
а место и есть причина, по которой тумблеры существуют.

Токен берётся из TODOIST_API_TOKEN. Один клиент на процесс: stdio — один процесс.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field as PydanticField

from . import plan as plan_module
from . import toggles, tools
from .client import TodoistClient, TodoistError

mcp = FastMCP("todoist")

_client: TodoistClient | None = None

_PYTHON_TYPES: dict[str, Any] = {
    "str": str,
    "int": int,
    "bool": bool,
    "json": list[dict],
}


def _todoist() -> TodoistClient:
    global _client
    if _client is None:
        _client = TodoistClient()  # токен из TODOIST_API_TOKEN
    return _client


def _make_handler(tool: tools.Tool, fields: tuple[tools.Field, ...]):
    """Собрать функцию-обработчик с настоящей сигнатурой из полей манифеста.

    Сигнатура подменяется, а не пишется: FastMCP строит схему инструмента
    по `inspect.signature`, и подменённая сигнатура для него неотличима
    от объявленной руками (проверено 01.08.2026 — схема выходит та же).
    """

    async def handler(**kwargs: Any) -> str:
        try:
            return await tool.run(_todoist(), kwargs)
        except TodoistError as err:
            # Ошибку отдаём словами, а не бросаем: модель должна её увидеть
            # и объяснить человеку, а не молча потерять действие.
            return f"Не получилось (Todoist): {err}"

    parameters = []
    annotations: dict[str, Any] = {"return": str}
    for f in fields:
        annotation = Annotated[
            _PYTHON_TYPES.get(f.kind, str),
            PydanticField(description=f.description or None),
        ]
        parameters.append(
            inspect.Parameter(
                f.name,
                inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty if f.required else f.default,
                annotation=annotation,
            )
        )
        annotations[f.name] = annotation

    handler.__signature__ = inspect.Signature(parameters, return_annotation=str)
    handler.__annotations__ = annotations
    handler.__name__ = tool.name
    handler.__doc__ = tool.description
    return handler


def register(server: FastMCP, plan: plan_module.Plan, switches: dict[str, bool] | None) -> list[str]:
    """Объявить движку кнопки, которые доступны на тарифе и включены."""
    registered: list[str] = []
    for tool in tools.selected(plan, switches):
        handler = _make_handler(tool, tool.visible_fields(plan))
        server.add_tool(handler, name=tool.name, description=tool.description)
        registered.append(tool.name)
    return registered


def main() -> None:
    register(mcp, plan_module.current(), toggles.load())
    mcp.run()


if __name__ == "__main__":
    main()

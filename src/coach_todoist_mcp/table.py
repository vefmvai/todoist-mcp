"""Таблица кнопок: что умеет, что стоит денег, что включено.

Одна команда отвечает сразу на три вопроса, которые иначе задают человеку:

  • что этот сервер вообще умеет — справка;
  • чего нет на моём тарифе и что даст платный — прибор тарифа;
  • что из этого сейчас грузится в сессию — проверка тумблеров.

Таблица **печатается из манифеста**, а не пишется руками. Это не удобство,
а условие: рукописный список кнопок разошёлся бы с настоящим ровно так же,
как разошлись две обёртки до этого этапа.

Прибор ругается на одно — когда возможность, названную в манифесте, Todoist
в своей таблице не показывает вовсе. Значит правила поменялись, и пометка
теперь указывает в пустоту: поле молча считалось бы недоступным навсегда.

Запуск: `python -m coach_todoist_mcp.table`
"""

from __future__ import annotations

import sys

from . import plan as plan_module
from . import toggles, tools

CHECK = "✔"
CROSS = "—"


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - len(text))


def render(plan: plan_module.Plan, switches: dict[str, bool]) -> tuple[str, int]:
    """Собрать таблицу. Возвращает текст и код выхода (1 — есть претензии)."""
    lines: list[str] = []
    lines.append(f"Тариф: {plan.name}   (узнали: {_source_words(plan.source)})")
    lines.append("")

    name_width = max(len(t.name) for t in tools.TOOLS) + 2
    summary_width = max(len(t.summary) for t in tools.TOOLS) + 2
    header = (_pad("кнопка", name_width) + _pad("что делает", summary_width)
              + _pad("тариф", 10) + "грузится")
    lines.append(header)
    lines.append("─" * len(header))

    for tool in tools.TOOLS:
        available = tool.available(plan)
        on = toggles.is_on(tool.name, switches)
        if not available:
            tariff = "платно"
        elif tool.feature:
            tariff = "есть"
        else:
            tariff = "бесплатно"
        loaded = CHECK if (available and on) else CROSS
        lines.append(_pad(tool.name, name_width) + _pad(tool.summary, summary_width)
                     + _pad(tariff, 10) + loaded)
        for f in tool.fields:
            if f.feature and not f.available(plan):
                lines.append(_pad("", name_width) + f"  ↳ поле «{f.name}» — только на платном тарифе")

    shown = [t for t in tools.selected(plan, switches)]
    lines.append("")
    lines.append(f"Грузится кнопок: {len(shown)} из {len(tools.TOOLS)}.")
    if len(shown) < len(tools.TOOLS):
        off = [t.name for t in tools.TOOLS if t not in shown]
        lines.append("Не грузится: " + ", ".join(off))

    # ── что даст переход на платный ───────────────────────────────────────
    wanted = _features_used()
    upgrades = [f for f in wanted if plan.upgrade_gives(f)]
    if upgrades:
        lines.append("")
        lines.append("На платном тарифе добавится: " + ", ".join(sorted(upgrades)))

    # ── претензии прибора ─────────────────────────────────────────────────
    problems: list[str] = []
    if plan.source in ("api", "cache"):
        unknown = [f for f in wanted if f not in plan.current]
        if unknown:
            problems.append(
                "Todoist не знает таких возможностей: " + ", ".join(sorted(unknown))
                + ". Правила изменились — пометка в манифесте указывает в пустоту, "
                  "и поле навсегда считается недоступным."
            )
    else:
        problems.append(
            f"Тариф не спрошен у Todoist (источник: {_source_words(plan.source)}) — "
            f"таблица показывает предположение, а не факт."
        )

    if problems:
        lines.append("")
        for p in problems:
            lines.append("⚠ " + p)

    return "\n".join(lines), (1 if problems else 0)


def _features_used() -> set[str]:
    """Все возможности тарифа, названные в манифесте."""
    used: set[str] = set()
    for tool in tools.TOOLS:
        if tool.feature:
            used.add(tool.feature)
        for f in tool.fields:
            if f.feature:
                used.add(f.feature)
    return used


def _source_words(source: str) -> str:
    return {
        "api": "спросили Todoist",
        "cache": "из вчерашнего ответа Todoist",
        "env": "задан переменной TODOIST_PLAN",
        "fallback": "не удалось узнать, считаем платное недоступным",
    }.get(source, source)


def main() -> None:
    plan = plan_module.load()
    switches = toggles.load()
    text, code = render(plan, switches)
    print(text)
    sys.exit(code)


if __name__ == "__main__":
    main()

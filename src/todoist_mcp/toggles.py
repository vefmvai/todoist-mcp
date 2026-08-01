"""Тумблеры: какие кнопки грузить в сессию, а какие не надо.

Зачем. Описание каждой кнопки уезжает в контекст модели при каждом запуске —
замер 01.08.2026 на живом боте дал ≈570 байт на кнопку, 19 кнопок = 10 844 Б
в каждом разговоре. Одноразовой сессии за компьютером это ничего не стоит,
а агенту с длинной памятью — стоит, и платит он этим местом всегда.

Поэтому набор выбирает тот, кто платит. Не разработчик делением кнопок
на сорта в коде, а человек — переключателем в файле, который он же читает
как справку.

Формат — YAML-словарь «имя кнопки → вкл/выкл»:

    tools:
      add_task: on
      find_tasks: on
      productivity_stats: off

Два правила, и оба важны:

1. **Кнопки нет в файле — она включена.** Обновились, приехала новая
   возможность — она работает. «Молча пропала после обновления» ищут
   неделями, а лишнюю кнопку выключают за десять секунд.
2. **Выключенная кнопка не объявляется вообще** — то есть не стоит ни байта.
   Иначе тумблер был бы украшением.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

DEFAULT_PATH = "~/.config/todoist-mcp/tools.yaml"


def path_from_env(var: str = "TODOIST_TOOLS_FILE", default: str = DEFAULT_PATH) -> Path:
    return Path(os.environ.get(var) or default).expanduser()


def parse(data: Any) -> dict[str, bool]:
    """Достать словарь тумблеров из разобранного YAML.

    Принимается и {tools: {...}}, и просто {...} — человек, правящий файл
    руками, не должен спотыкаться о лишний уровень вложенности.
    """
    if not isinstance(data, dict):
        return {}
    section = data.get("tools") if isinstance(data.get("tools"), dict) else data
    return {
        str(name): bool(value)
        for name, value in section.items()
        if isinstance(value, bool)
    }


def load(path: Path | str | None = None) -> dict[str, bool]:
    """Прочитать тумблеры. Файла нет или он сломан — все кнопки включены.

    Молчаливое «включено всё» здесь правильнее строгости: пустой набор
    кнопок из-за опечатки в необязательном файле — это агент, который
    внезапно ничего не умеет и не говорит почему.
    """
    file = Path(path).expanduser() if path else path_from_env()
    try:
        import yaml
    except ImportError:
        return {}
    try:
        return parse(yaml.safe_load(file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    except Exception:  # yaml.YAMLError и прочее — файл сломан
        return {}


def is_on(name: str, toggles: dict[str, bool] | None) -> bool:
    """Включена ли кнопка. Не упомянута — включена (правило 1)."""
    if not toggles:
        return True
    return toggles.get(name, True)

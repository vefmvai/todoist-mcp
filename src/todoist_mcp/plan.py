"""Тариф Todoist: что доступно на текущем плане, а что только на платном.

Зачем это вообще. У Todoist часть возможностей платные, и ведут себя они
по-разному — одинаково плохо:

  • дедлайн на бесплатном роняет ВЕСЬ запрос: 403 «Premium only feature»,
    задача не создаётся;
  • длительность на бесплатном Todoist принимает и МОЛЧА выбрасывает:
    200 OK, а в ответе `duration: null`.

Второе хуже первого: жалобы нет, и правило «ставь оценку времени каждой
задаче» может годами работать вхолостую (замер 01.08.2026 на живом
аккаунте — 0 задач из 140).

Поэтому тариф спрашивается ЗАРАНЕЕ, и недоступное просто не предлагается
модели. Спрашивается, а не записывается руками: конфиг соврёт в тот день,
когда тариф сменится, а его забудут поправить.

Источник правды — сам Todoist:

    POST /api/v1/sync   resource_types=["user_plan_limits"]
    → {"current": {"plan_name": "free_new_4", "deadlines": false, …},
       "next":    {"plan_name": "pro",        "deadlines": true,  …}}

Ответ кэшируется на сутки: тариф меняется реже, чем запускается процесс.

Аварийный рычаг — переменная TODOIST_PLAN:
    auto (по умолчанию) — спросить Todoist, при неудаче считать платное
                          недоступным;
    free                — не спрашивать, считать платное недоступным;
    pro                 — не спрашивать, считать доступным всё.

Умолчание при любой неудаче — «недоступно». Ошибиться в эту сторону значит
не воспользоваться платной возможностью; ошибиться в другую — уронить
запрос или молча потерять данные.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

SYNC_URL = "https://api.todoist.com/api/v1/sync"
CACHE_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Plan:
    """Снимок тарифа: что есть сейчас и что даст переход на следующий."""

    name: str
    current: dict[str, Any] = field(default_factory=dict)
    upgrade: dict[str, Any] = field(default_factory=dict)
    source: str = "fallback"  # api | cache | env | fallback

    def has(self, feature: str) -> bool:
        """Доступна ли фича сейчас. Неизвестная фича считается недоступной."""
        return bool(self.current.get(feature))

    def upgrade_gives(self, feature: str) -> bool:
        """Появится ли фича на следующем тарифе."""
        return bool(self.upgrade.get(feature)) and not self.has(feature)


def _cache_path() -> Path:
    explicit = os.environ.get("TODOIST_PLAN_CACHE")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "todoist-mcp" / "plan.json"


def _read_cache() -> Plan | None:
    path = _cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - float(raw.get("fetched_at", 0)) > CACHE_TTL_SECONDS:
        return None
    return Plan(
        name=raw.get("name", "?"),
        current=raw.get("current") or {},
        upgrade=raw.get("upgrade") or {},
        source="cache",
    )


def _write_cache(plan: Plan) -> None:
    """Записать кэш. Не смогли — не беда: спросим Todoist в следующий раз."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": time.time(),
                    "name": plan.name,
                    "current": plan.current,
                    "upgrade": plan.upgrade,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def fetch(token: str, *, timeout: float = 15.0) -> Plan:
    """Спросить Todoist о тарифе. Синхронно: зовётся один раз при сборке кнопок."""
    with httpx.Client(timeout=timeout, trust_env=True) as http:
        response = http.post(
            SYNC_URL,
            headers={"Authorization": f"Bearer {token}"},
            data={"sync_token": "*", "resource_types": '["user_plan_limits"]'},
        )
        response.raise_for_status()
        limits = (response.json() or {}).get("user_plan_limits") or {}
    current = limits.get("current") or {}
    return Plan(
        name=str(current.get("plan_name") or "?"),
        current=current,
        upgrade=limits.get("next") or {},
        source="api",
    )


def load(token: str | None = None, *, refresh: bool = False) -> Plan:
    """Тариф: из переменной, из кэша или у Todoist. Никогда не бросает."""
    mode = (os.environ.get("TODOIST_PLAN") or "auto").strip().lower()
    if mode == "pro":
        # Всё доступно. Словарь-«да на всё» вместо перечисления: список фич
        # у Todoist свой, и вести его копию здесь означало бы второй дом.
        return Plan(name="pro (задан TODOIST_PLAN)", current=_YesToAll(), source="env")
    if mode == "free":
        return Plan(name="free (задан TODOIST_PLAN)", source="env")

    if not refresh:
        cached = _read_cache()
        if cached is not None:
            return cached

    token = token or os.environ.get("TODOIST_API_TOKEN", "")
    if not token:
        return Plan(name="неизвестен (нет токена)", source="fallback")
    try:
        plan = fetch(token)
    except Exception:  # сеть, прокси, 5xx — что угодно
        return Plan(name="неизвестен (Todoist не ответил)", source="fallback")
    _write_cache(plan)
    return plan


_MEMO: Plan | None = None


def current(token: str | None = None) -> Plan:
    """Тариф этого процесса. Спрашивается один раз, дальше берётся из памяти.

    Ядро зовёт эту функцию на каждой записи в Todoist, а ходить в сеть на
    каждый вызов незачем: тариф не меняется между двумя нажатиями кнопки.
    """
    global _MEMO
    if _MEMO is None:
        _MEMO = load(token)
    return _MEMO


def forget() -> None:
    """Забыть запомненный тариф — для тестов и для «перепроверь сейчас»."""
    global _MEMO
    _MEMO = None


class _YesToAll(dict):
    """Словарь, который на любой вопрос отвечает «да». Для TODOIST_PLAN=pro."""

    def get(self, key: Any, default: Any = None) -> Any:  # noqa: D102
        return True

    def __getitem__(self, key: Any) -> Any:
        return True

    def __contains__(self, key: object) -> bool:
        return True

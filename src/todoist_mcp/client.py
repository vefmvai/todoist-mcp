"""Тонкий async-клиент к Todoist unified API v1.

Только HTTP-слой: заголовок с токеном, базовый URL, разбор ответа. Вся логика
«кнопок» — в core.py. httpx сам подхватывает HTTPS_PROXY из окружения
(на сервере бот ходит к Todoist через Xray-мост), поэтому прокси тут не настраиваем.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

API_BASE = "https://api.todoist.com/api/v1"

# Под каким ключом Todoist кладёт список в ответ. Их два, и это не наша выдумка:
# `/tasks`, `/projects`, `/comments` отдают `results`, а завершённые задачи —
# `items`. Порядок значим: сначала частый, потом редкий.
LIST_KEYS = ("results", "items")


class TodoistError(RuntimeError):
    """Ошибка обращения к Todoist (не 2xx) с понятным текстом.

    `retryable` отвечает на вопрос, который задаёт вызывающий: «это связь
    моргнула или запрос неправильный?». Без него автоматизации не могут
    отличить одно от другого — и либо повторяют бессмысленное, либо сдаются
    на ровном месте. Куплено прогоном 01.08.2026: обрыв соединения посреди
    работы канала комментариев выглядел как «ничего не изменилось».
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class TodoistClient:
    """Обёртка над httpx.AsyncClient. Использовать как async context manager."""

    def __init__(self, token: str | None = None, timeout: float = 30.0) -> None:
        token = token or os.environ.get("TODOIST_API_TOKEN", "")
        if not token:
            raise TodoistError("Нет токена Todoist: задайте переменную TODOIST_API_TOKEN.")
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            trust_env=True,  # подхватить HTTPS_PROXY на сервере
        )

    async def __aenter__(self) -> "TodoistClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as err:
            body = err.response.text[:300]
            code = err.response.status_code
            raise TodoistError(
                f"Todoist {code} на {method} {path}: {body}",
                # 429 — «слишком часто», 5xx — «мне сейчас плохо»: и то и другое
                # проходит само. 4xx означает, что запрос неправильный, и повтор
                # его правильным не сделает.
                retryable=code == 429 or code >= 500,
            ) from err
        except httpx.HTTPError as err:
            raise TodoistError(
                f"Сеть недоступна на {method} {path}: {err}", retryable=True
            ) from err
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # Короткие удобные обёртки
    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self.request("POST", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, **kwargs)

    async def get_paginated(self, path: str, params: dict[str, Any] | None = None,
                            key: str | tuple[str, ...] = LIST_KEYS,
                            cap: int = 300) -> list[dict[str, Any]]:
        """Собрать все страницы. Список лежит под `results` ЛИБО под `items`.

        Почему кандидаты, а не одно имя. Большинство путей отдаёт `results`,
        а `/tasks/completed/by_completion_date` — `items`. Раньше это знание
        жило в двух местах: параметр `key` по умолчанию стоял на `results`,
        а `core.completed_history` разбирал ответ руками. Ночной снимок про
        вторую форму не знал и молча собирал пустоту: 0 закрытых задач при
        1826 закрытых по счётчику Todoist (найдено прогоном 01.08).

        Пустая таблица выглядит как «данных нет», а означает «мы их не туда
        просили» — поэтому имя ключа перестаёт быть догадкой места вызова.
        """
        params = dict(params or {})
        candidates = (key,) if isinstance(key, str) else tuple(key)
        items: list[dict[str, Any]] = []
        while True:
            data = await self.get(path, params=params)
            if isinstance(data, list):
                return data
            for name in candidates:
                if name in data:
                    items.extend(data[name] or [])
                    break
            cursor = data.get("next_cursor")
            if not cursor or len(items) >= cap:
                return items[:cap]
            params["cursor"] = cursor

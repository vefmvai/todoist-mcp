"""Живой smoke-тест: создаёт временную задачу с подзадачей и комментарием, читает, удаляет.

Нужен реальный токен в TODOIST_API_TOKEN. Создаёт задачи с префиксом __smoke__ в Inbox
и подчищает за собой. Запуск: python tests/test_smoke.py
"""

from __future__ import annotations

import asyncio
import sys

from coach_todoist_mcp import TodoistClient, core
from coach_todoist_mcp import server  # проверяем, что MCP-обёртка (FastMCP) импортируется


async def main() -> int:
    async with TodoistClient() as client:
        # 1. создать родительскую задачу
        parent = await client.post("/tasks", json={"content": "__smoke__ родитель"})
        pid = parent["id"]
        print("created parent:", pid)

        # 2. подзадача через core.add_task(parent_id=...)
        out = await core.add_task(client, content="__smoke__ подзадача", parent_id=pid)
        print("add_task subtask:", out)
        assert "подзадача" in out

        # 3. пачка подзадач
        out = await core.add_tasks_bulk(client, [
            {"content": "__smoke__ шаг 1", "parent_id": pid},
            {"content": "__smoke__ шаг 2", "parent_id": pid, "priority": 4},
        ])
        print("bulk:", out.splitlines()[0])

        # 4. комментарий + чтение
        await core.add_comment(client, task_id=pid, content="__smoke__ коммент")
        comments = await core.get_comments(client, task_id=pid)
        assert "__smoke__ коммент" in comments
        print("comments read: ok")

        # 5. get_task с подзадачами и комментариями
        card = await core.get_task(client, task_id=pid, include_subtasks=True, include_comments=True)
        assert "подзадачи (3)" in card, card
        print("get_task card: ok (3 subtasks visible)")

        # 6. quick_add + завершение с авторством + переоткрытие
        q = await client.post("/tasks/quick", json={"text": "__smoke__ quick завтра p3"})
        qid = q["id"]
        print(await core.complete_task(client, task_id=qid))
        print(await core.complete_task(client, task_id=qid, reopen=True))

        # 7. уборка
        await core.delete_task(client, task_id=pid, confirm=True)
        await core.delete_task(client, task_id=qid, confirm=True)
        print("cleanup: ok")

    print("\nSMOKE PASSED ✅ (tools registered:",
          len(await server.mcp.list_tools()), ")")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

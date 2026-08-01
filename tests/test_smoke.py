"""Живой smoke-тест: создаёт временную задачу с подзадачей и комментарием, читает, удаляет.

Нужен реальный токен в TODOIST_API_TOKEN. Создаёт задачи с префиксом __smoke__ в Inbox
и подчищает за собой. Запуск: python tests/test_smoke.py
"""

from __future__ import annotations

import asyncio
import sys

from todoist_mcp import TodoistClient, core
from todoist_mcp import server  # проверяем, что MCP-обёртка (FastMCP) импортируется


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

        # 4. комментарий + чтение (в выводе должен быть id — им же и удаляем)
        added = await core.add_comment(client, task_id=pid, content="__smoke__ коммент")
        cid = added.rsplit("id ", 1)[1].rstrip(").")
        comments = await core.get_comments(client, task_id=pid)
        assert "__smoke__ коммент" in comments and f"[{cid}]" in comments, comments
        print("comments read: ok (id виден)")

        # 5. get_task с подзадачами и комментариями
        card = await core.get_task(client, task_id=pid, include_subtasks=True, include_comments=True)
        assert "подзадачи (3)" in card, card
        print("get_task card: ok (3 subtasks visible)")

        # 6. удаление комментария (без confirm — отказ, с confirm — удалён)
        assert "confirm=true" in await core.delete_comment(client, comment_id=cid)
        print(await core.delete_comment(client, comment_id=cid, confirm=True))
        assert cid not in await core.get_comments(client, task_id=pid)

        # 7. quick_add + завершение + переоткрытие
        q = await client.post("/tasks/quick", json={"text": "__smoke__ quick завтра p3"})
        qid = q["id"]
        print(await core.complete_task(client, task_id=qid))
        print(await core.complete_task(client, task_id=qid, reopen=True))
        # авторства больше нет — закрытие не должно плодить комментарии
        assert "комментариев нет" in await core.get_comments(client, task_id=qid)

        # 8. метка: создать → переименовать → удалить (имя ищется без учёта регистра и @)
        print(await core.manage_structure(client, "create_label", "__smoke__метка"))
        print(await core.manage_structure(client, "rename_label", "@__SMOKE__МЕТКА",
                                          new_name="__smoke__метка2"))
        assert "confirm=true" in await core.manage_structure(client, "delete_label", "__smoke__метка2")
        print(await core.manage_structure(client, "delete_label", "__smoke__метка2", confirm=True))
        assert "не найдена" in await core.manage_structure(client, "delete_label",
                                                           "__smoke__метка2", confirm=True)

        # 9. уборка
        await core.delete_task(client, task_id=pid, confirm=True)
        await core.delete_task(client, task_id=qid, confirm=True)
        print("cleanup: ok")

    # 10. кнопки объявляются из манифеста, а не руками: регистрируем и считаем.
    # Раньше здесь звался server.mcp напрямую — после этапа 14 он пуст до
    # register(), и молчаливый ноль читался бы как «всё хорошо».
    from todoist_mcp import plan, toggles
    registered = server.register(server.mcp, plan.current(), toggles.load())
    assert registered, "манифест не дал ни одной кнопки"
    print("\nSMOKE PASSED ✅ (кнопок объявлено:", len(registered), ")")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

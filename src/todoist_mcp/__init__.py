"""todoist-mcp — MCP-сервер к Todoist.

Ядро (client + core) переиспользуется ботом напрямую, минуя MCP.
"""

from . import core
from .client import TodoistClient, TodoistError

__all__ = ["core", "TodoistClient", "TodoistError"]
__version__ = "0.2.0"

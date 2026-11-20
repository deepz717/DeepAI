"""
DeepAI core layer — task/agent/message/tool infrastructure.

Import contract: core may import config and ai. It must NOT import app.py.
"""
from core.message import AgentMessage, MessageType, MessagePriority
from core.task import Task, TaskState, TaskManager
from core.context import SessionContext, get_session
from core.memory import MemoryFacade
from core.tool_registry import Tool, ToolRegistry, PermissionLevel, get_registry
from core.agent_manager import AgentManager, get_agent_manager
from core.message_bus import MessageBus, get_bus

__all__ = [
    "AgentMessage", "MessageType", "MessagePriority",
    "Task", "TaskState", "TaskManager",
    "SessionContext", "get_session",
    "MemoryFacade",
    "Tool", "ToolRegistry", "PermissionLevel", "get_registry",
    "AgentManager", "get_agent_manager",
    "MessageBus", "get_bus",
]

from core.agent_manager import (
    AgentManager, get_agent_manager,
    register_service, get_service, list_services,
    get_app_module,
)
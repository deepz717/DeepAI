"""
BaseAgent — the contract every agent obeys.
==========================================
Subclasses must set `name` and implement `handle()`.

Guarantees BaseAgent provides:
  * `self.respond(...)`, `self.fail(...)` helpers that produce
    correctly-formed AgentMessages
  * `self.publish_status(text)` — pushes a short status line onto the
    MessageBus so the GUI can show "💻 Coding Agent → Analyzing Gradle"
    without exposing chain-of-thought
  * `self.get_service(name)` — fetch a shared object registered by app.py
  * exception safety: subclasses wrap risky work in try/except; BaseAgent
    provides the helpers but does NOT auto-swallow — the AgentManager
    already captures raise() and turns it into an ERROR message
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional
from core.agent_manager import get_service
from core.message import AgentMessage, MessagePriority, MessageType
from core.message_bus import get_bus

class BaseAgent(ABC):
    name: str = "base"

    # ---- required interface -------------------------------------------
    @abstractmethod
    def handle(self, msg: AgentMessage) -> AgentMessage:
        """
        Process `msg` and return a response. Never raise.
        Return self.fail(...) on any error.
        """
        raise NotImplementedError

    # ---- message constructors -----------------------------------------
    def respond(
        self,
        content: Any,
        msg: AgentMessage,
        *,
        status: str = "SUCCESS",
        context: Optional[dict] = None,
    ) -> AgentMessage:
        return AgentMessage.result(
            from_agent=self.name,
            to_agent=msg.from_agent or "head_agent",
            content=content,
            task_id=msg.task_id,
            status=status,
            context=context,
        )

    def fail(
        self,
        reason: str,
        msg: AgentMessage,
        *,
        context: Optional[dict] = None,
    ) -> AgentMessage:
        return AgentMessage.error(
            from_agent=self.name,
            to_agent=msg.from_agent or "head_agent",
            content=reason,
            task_id=msg.task_id,
            context=context,
        )

    def need_confirmation(
        self,
        question: str,
        msg: AgentMessage,
        *,
        context: Optional[dict] = None,
    ) -> AgentMessage:
        """Ask the caller (usually the Head Agent) to obtain user consent."""
        return AgentMessage(
            from_agent=self.name,
            to_agent=msg.from_agent or "head_agent",
            type=MessageType.CONFIRM,  # from core.message
            content=question,
            task_id=msg.task_id,
            context=context or {},
        )

    # ---- status / services --------------------------------------------
    def publish_status(self, text: str, msg: Optional[AgentMessage] = None) -> None:
        """Emit a short GUI-safe status line. No reasoning, no CoT."""
        try:
            get_bus().status(
                agent=self.name,
                text=text,
                task_id=(msg.task_id if msg else ""),
            )
        except Exception:
            pass

    def get_service(self, key: str) -> Any:
        """Fetch a shared object registered by app.py, or None."""
        try:
            return get_service(key)
        except Exception:
            return None
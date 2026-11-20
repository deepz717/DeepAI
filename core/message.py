"""
AgentMessage — the structured envelope passed between agents.
=============================================================
Per spec §13. Every inter-agent communication is a message. No ad-hoc
dicts, no positional tuples.

Design notes:
  * Frozen dataclass — messages are immutable once created. Replies are
    new messages, not mutations.
  * `content` is a str for text and a dict for structured payloads.
  * `context` carries any auxiliary data the receiver needs (paths,
    file lists, prior results) without needing global state.
  * `task_id` groups a set of messages that belong to one user request.
  * No circular imports: this module imports only stdlib.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional, Union


class MessageType(str, Enum):
    TASK        = "TASK"        # head/manager → worker: do this
    RESULT      = "RESULT"      # worker → manager/head: here's the outcome
    ERROR       = "ERROR"       # worker → manager: failed, here's why
    QUERY       = "QUERY"       # peer-to-peer question (rare, used sparingly)
    CONFIRM     = "CONFIRM"     # request user confirmation (dangerous action)
    STATUS      = "STATUS"      # short status update for GUI (no CoT)
    CANCEL      = "CANCEL"      # cancel the task identified by task_id


class MessagePriority(str, Enum):
    LOW    = "LOW"
    NORMAL = "NORMAL"
    HIGH   = "HIGH"
    URGENT = "URGENT"


@dataclass(frozen=True)
class AgentMessage:
    """
    One unit of inter-agent communication.

    Required:
        from_agent, to_agent, type, content
    Auto-filled if not supplied:
        message_id, timestamp
    """
    from_agent: str
    to_agent:   str
    type:       MessageType
    content:    Union[str, Dict[str, Any]]

    # Auto-filled
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp:  str = field(default_factory=lambda: datetime.now().isoformat())

    # Optional routing / context
    task_id:    Optional[str] = None
    priority:   MessagePriority = MessagePriority.NORMAL
    context:    Dict[str, Any] = field(default_factory=dict)

    # ---- convenience constructors --------------------------------------
    @classmethod
    def task(
        cls,
        from_agent: str,
        to_agent: str,
        content: Union[str, Dict[str, Any]],
        task_id: Optional[str] = None,
        priority: MessagePriority = MessagePriority.NORMAL,
        context: Optional[Dict[str, Any]] = None,
    ) -> "AgentMessage":
        return cls(
            from_agent=from_agent,
            to_agent=to_agent,
            type=MessageType.TASK,
            content=content,
            task_id=task_id,
            priority=priority,
            context=context or {},
        )

    @classmethod
    def result(
        cls,
        from_agent: str,
        to_agent: str,
        content: Union[str, Dict[str, Any]],
        task_id: Optional[str] = None,
        status: str = "SUCCESS",
        context: Optional[Dict[str, Any]] = None,
    ) -> "AgentMessage":
        ctx = dict(context or {})
        ctx.setdefault("status", status)
        return cls(
            from_agent=from_agent,
            to_agent=to_agent,
            type=MessageType.RESULT,
            content=content,
            task_id=task_id,
            context=ctx,
        )

    @classmethod
    def error(
        cls,
        from_agent: str,
        to_agent: str,
        content: str,
        task_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> "AgentMessage":
        return cls(
            from_agent=from_agent,
            to_agent=to_agent,
            type=MessageType.ERROR,
            content=content,
            task_id=task_id,
            context=context or {},
        )
    
    @classmethod
    def status(
        cls,
        from_agent: str,
        to_agent: str,
        content: str,
        task_id: Optional[str] = None,
    ) -> "AgentMessage":
        return cls(
            from_agent=from_agent,
            to_agent=to_agent,
            type=MessageType.STATUS,
            content=content,
            task_id=task_id,
        )

    # ---- helpers -------------------------------------------------------
    def is_success(self) -> bool:
        return (
            self.type == MessageType.RESULT
            and self.context.get("status", "SUCCESS") == "SUCCESS"
        )

    def as_text(self) -> str:
        """Best-effort text view — used for logging only."""
        if isinstance(self.content, str):
            return self.content
        return str(self.content)

    def to_log_dict(self) -> Dict[str, Any]:
        """Compact dict for logging/GUI; never includes full payload."""
        return {
            "id": self.message_id,
            "from": self.from_agent,
            "to": self.to_agent,
            "type": self.type.value,
            "priority": self.priority.value,
            "task_id": self.task_id,
            "preview": self.as_text()[:120],
            "ts": self.timestamp,
        }


# ---- smoke test --------------------------------------------------------
if __name__ == "__main__":
    m = AgentMessage.task("head", "coding", "analyze gradle", task_id="t1")
    assert m.type == MessageType.TASK
    assert m.task_id == "t1"
    r = AgentMessage.result("coding", "head", "fixed", task_id="t1")
    assert r.is_success()
    e = AgentMessage.error("coding", "head", "still fails", task_id="t1")
    assert not e.is_success()
    print("message.py smoke OK")
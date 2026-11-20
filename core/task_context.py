"""
TaskContext — a scoped, isolated unit of work.
==============================================
Each user request produces exactly one TaskContext. It carries:
  * what the user typed
  * the resolved intent
  * the topic (e.g. "two sum", "palindrome")
  * the target language
  * the resolved_request — a fully-formed sentence the agents receive
  * the previous_code + its complexity/data-structure profile
    (for conversions/explain/optimize)

TaskContext is a plain dataclass — no LLM, no side effects.
The resolver (core/context_resolver.py) produces these.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class Intent(str, Enum):
    # ── Code ─────────────────────────────────────────────
    CODE_NEW         = "CODE_NEW"
    CODE_CONVERSION  = "CODE_CONVERSION"
    CODE_EXPLAIN     = "CODE_EXPLAIN"
    CODE_OPTIMIZE    = "CODE_OPTIMIZE"

    # ── Other ────────────────────────────────────────────
    VISION           = "VISION"
    MEMORY_SAVE      = "MEMORY_SAVE"
    MEMORY_RECALL    = "MEMORY_RECALL"
    RESEARCH         = "RESEARCH"
    SYSTEM           = "SYSTEM"
    GENERAL          = "GENERAL"
    BLENDER          = "BLENDER"      # ← new (Phase 4)


@dataclass
class TaskContext:
    # Identity
    task_id: str
    intent: Intent
    original_request: str
    resolved_request: str

    # Semantics
    topic: Optional[str] = None
    language: Optional[str] = None
    source_language: Optional[str] = None
    target_language: Optional[str] = None

    # Context chain
    parent_task_id: Optional[str] = None
    previous_code: Optional[str] = None
    previous_explanation: Optional[str] = None

    # Optimization context
    previous_complexity: Optional[Dict[str, Any]] = None
    previous_data_structures: Optional[Dict[str, Any]] = None

    # Metadata
    created_at: str = field(
        default_factory=lambda: datetime.now().isoformat()
    )

    def is_followup(self) -> bool:
        return self.parent_task_id is not None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "intent": self.intent.value,
            "original_request": self.original_request,
            "resolved_request": self.resolved_request,
            "topic": self.topic,
            "language": self.language,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "parent_task_id": self.parent_task_id,
            "previous_code": self.previous_code,
            "previous_explanation": self.previous_explanation,
            "previous_complexity": self.previous_complexity,
            "previous_data_structures": self.previous_data_structures,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskContext":
        return cls(
            task_id=d["task_id"],
            intent=Intent(d["intent"]),
            original_request=d["original_request"],
            resolved_request=d["resolved_request"],
            topic=d.get("topic"),
            language=d.get("language"),
            source_language=d.get("source_language"),
            target_language=d.get("target_language"),
            parent_task_id=d.get("parent_task_id"),
            previous_code=d.get("previous_code"),
            previous_explanation=d.get("previous_explanation"),
            previous_complexity=d.get("previous_complexity"),
            previous_data_structures=d.get("previous_data_structures"),
            created_at=d.get("created_at", datetime.now().isoformat()),
        )
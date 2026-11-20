"""
Tool + ToolRegistry — central registry of callable capabilities.
===============================================================
Per spec §11.

A Tool is a named, described, permission-tagged callable. Agents access
tools *only* through the registry. This gives us:
  * a single place to audit what the system can do
  * permission gating for dangerous operations
  * the ability to list tools for the LLM (structured prompt)

PermissionLevel per spec §7:
  READ_ONLY         — safe, no side effects
  SAFE              — opens apps, reads system info
  CONFIRM_REQUIRED  — writes files, sends messages; ask user first
  DANGEROUS         — deletes, shuts down; explicit YES required

The registry does NOT enforce confirmation itself — that's the caller's
job. It only *declares* the level so callers can decide.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class PermissionLevel(str, Enum):
    READ_ONLY        = "READ_ONLY"
    SAFE             = "SAFE"
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
    DANGEROUS        = "DANGEROUS"


@dataclass
class Tool:
    name: str
    description: str
    func: Callable[..., Any]
    permission: PermissionLevel = PermissionLevel.READ_ONLY
    requires_context: bool = False      # if True, caller passes ctx=...
    tags: List[str] = field(default_factory=list)

    def call(self, **kwargs) -> Any:
        return self.func(**kwargs)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        self._lock = threading.RLock()

    # ---- registration -------------------------------------------------
    def register(self, tool: Tool) -> None:
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(f"Tool '{tool.name}' already registered")
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)

    # ---- lookup -------------------------------------------------------
    def get(self, name: str) -> Optional[Tool]:
        with self._lock:
            return self._tools.get(name)

    def exists(self, name: str) -> bool:
        with self._lock:
            return name in self._tools

    def list_tools(self) -> List[Tool]:
        with self._lock:
            return list(self._tools.values())

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._tools.keys())

    def by_permission(self, level: PermissionLevel) -> List[Tool]:
        with self._lock:
            return [t for t in self._tools.values() if t.permission == level]

    def by_tag(self, tag: str) -> List[Tool]:
        with self._lock:
            return [t for t in self._tools.values() if tag in t.tags]

    # ---- execution ----------------------------------------------------
    def call(self, name: str, **kwargs) -> Any:
        """
        Execute a registered tool. Raises KeyError if unknown.
        Callers are responsible for permission checks based on the
        Tool.permission value before calling this.
        """
        tool = self.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        return tool.func(**kwargs)

    # ---- LLM-friendly description ------------------------------------
    def describe_for_llm(self) -> str:
        """Compact textual listing — used in planner prompts."""
        lines = []
        for t in self.list_tools():
            lines.append(f"- {t.name} [{t.permission.value}]: {t.description}")
        return "\n".join(lines)


# ---- singleton --------------------------------------------------------
_registry: Optional[ToolRegistry] = None
_reg_lock = threading.Lock()


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        with _reg_lock:
            if _registry is None:
                _registry = ToolRegistry()
    return _registry


# ---- smoke test --------------------------------------------------------
if __name__ == "__main__":
    r = get_registry()

    def _echo(x: int) -> int:
        return x * 2

    r.register(Tool(
        name="echo",
        description="Double a number",
        func=_echo,
        permission=PermissionLevel.READ_ONLY,
        tags=["test"],
    ))
    assert r.exists("echo")
    assert r.call("echo", x=3) == 6
    assert r.get("echo").permission == PermissionLevel.READ_ONLY
    print("tool_registry.py smoke OK:", r.names())
"""
MemoryFacade — unified read interface over the existing memory systems.
=====================================================================
app.py already has:
    conversation_memory  (ConversationMemory)
    context_memory       (ContextMemory)
    ai_memory            (AIMemory)

We do NOT reimplement these. The facade *borrows* them once they're
registered, and gives agents a clean, read-only-ish surface.

Until Phase 5 registers the real objects, the facade works with
internal no-op stubs so agents can be developed in isolation.

No imports from app.py.
"""
from __future__ import annotations

import threading
from typing import Any, List, Optional, Tuple


class _NullConvMemory:
    def get_recent_context(self, n: int = 15,
                           context_filter: Optional[str] = None) -> str:
        return ""

    def add_turn(self, role: str, content: str,
                 context_type: str = "general") -> None:
        pass


class _NullCtxMemory:
    def get_relevant_context(self, prompt: str) -> Optional[str]:
        return None

    def store(self, category: str, value: Any) -> None:
        pass


class _NullAIMemory:
    def recall(self, key: str) -> Optional[str]:
        return None

    def remember(self, key: str, value: str) -> str:
        return f"(stub) would remember {key}={value}"

    def get_all_memories(self) -> List[Tuple[str, str]]:
        return []

    def get_context_string(self) -> str:
        return ""


class MemoryFacade:
    """
    Thin, thread-safe facade that holds references to the existing
    memory objects once app.py registers them.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conv: Any = _NullConvMemory()
        self._ctx:  Any = _NullCtxMemory()
        self._ltm:  Any = _NullAIMemory()

    # ---- registration (Phase 5 calls these) ---------------------------
    def register_conversation_memory(self, obj: Any) -> None:
        with self._lock:
            self._conv = obj or _NullConvMemory()

    def register_context_memory(self, obj: Any) -> None:
        with self._lock:
            self._ctx = obj or _NullCtxMemory()

    def register_long_term_memory(self, obj: Any) -> None:
        with self._lock:
            self._ltm = obj or _NullAIMemory()

    # ---- convenience accessors ---------------------------------------
    @property
    def conversation(self) -> Any:
        return self._conv

    @property
    def context(self) -> Any:
        return self._ctx

    @property
    def long_term(self) -> Any:
        return self._ltm

    # ---- unified read surface ----------------------------------------
    def recent_context(self, n: int = 15) -> str:
        try:
            return self._conv.get_recent_context(n=n) or ""
        except Exception:
            return ""

    def context_hint(self, prompt: str) -> Optional[str]:
        try:
            return self._ctx.get_relevant_context(prompt)
        except Exception:
            return None

    def recall(self, key: str) -> Optional[str]:
        try:
            return self._ltm.recall(key)
        except Exception:
            return None

    def remember(self, key: str, value: str) -> str:
        try:
            return self._ltm.remember(key, value)
        except Exception:
            return f"(error) could not remember {key}"

    def all_long_term(self) -> List[Tuple[str, str]]:
        try:
            return list(self._ltm.get_all_memories())
        except Exception:
            return []

    def long_term_context_string(self) -> str:
        try:
            return self._ltm.get_context_string() or ""
        except Exception:
            return ""

    # ---- write surface used by agents ---------------------------------
    def record_turn(self, role: str, content: str,
                    context_type: str = "general") -> None:
        try:
            self._conv.add_turn(role, content, context_type)
        except Exception:
            pass

    def store_hint(self, category: str, value: Any) -> None:
        try:
            self._ctx.store(category, value)
        except Exception:
            pass


# ---- singleton --------------------------------------------------------
_mem: Optional[MemoryFacade] = None
_mem_lock = threading.Lock()


def get_memory() -> MemoryFacade:
    global _mem
    if _mem is None:
        with _mem_lock:
            if _mem is None:
                _mem = MemoryFacade()
    return _mem


# ---- smoke test --------------------------------------------------------
if __name__ == "__main__":
    m = get_memory()
    assert m.recent_context() == ""
    assert m.context_hint("anything") is None
    print("memory.py smoke OK (running on null stubs; app.py will register real ones in Phase 5)")
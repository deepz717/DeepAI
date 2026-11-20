"""
SessionContext — replaces module-level global state.
===================================================
Today app.py has this scattered around:

    CURRENT_IMAGES = []
    CURRENT_PDF = None
    CURRENT_PROJECT_PATH = None
    PREVIEW_LABEL = None
    ...

Agents need a clean object instead. SessionContext is that object.

IMPORTANT — Phase 2/5 constraint:
  SessionContext is *not* the source of truth for legacy app.py globals.
  app.py still owns those. SessionContext provides a stable interface
  and can be sync'd from the legacy globals via `sync_from_legacy`.
  No imports from app.py.

Phase 5: dialogue claims for multi-turn flows (chess color selection).
Phase 6: task context + execution queue isolation.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SessionContext:
    """Per-session state that agents may read/write."""
    # vision / images
    current_images: List[str] = field(default_factory=list)

    # pdf
    current_pdf: Optional[str] = None
    pdf_text_cache: Optional[str] = None
    pdf_page_count: int = 0

    # project
    current_project_path: Optional[str] = None
    project_class_paths: Dict[str, str] = field(default_factory=dict)
    project_xml_paths: Dict[str, str] = field(default_factory=dict)

    # chess (loose flag; the ChessGame object itself lives in app.py)
    chess_active: bool = False

    # ── Phase 5: dialogue routing ───────────────────────────────
    # When a specialized agent is mid multi-turn dialogue (chess
    # waiting for a color, PDF wizard, etc.), it claims the next turn
    # here. HeadAgent checks this BEFORE the planner runs and routes
    # the input directly to the claimed agent.
    dialogue_owner: Optional[str] = None
    dialogue_note: Optional[str] = None

    # arbitrary bag for agents
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── Phase 6: task isolation ────────────────────────────────
    # current_task: the TaskContext of the most recent user turn.
    # task_history: last N archived tasks (read-only for context).
    # execution_queue: cleared at the start of every new user turn.
    #                  Nothing executes across turns.
    current_task: Optional[Any] = None
    task_history: List[Any] = field(default_factory=list)
    execution_queue: List[Any] = field(default_factory=list)

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ---- safe mutators -------------------------------------------------
    def add_image(self, path: str, cap: int = 10) -> None:
        with self._lock:
            self.current_images.append(path)
            if len(self.current_images) > cap:
                self.current_images = self.current_images[-cap:]

    def clear_images(self) -> int:
        with self._lock:
            n = len(self.current_images)
            self.current_images.clear()
            return n

    def set_pdf(self, path: Optional[str], text: Optional[str], pages: int) -> None:
        with self._lock:
            self.current_pdf = path
            self.pdf_text_cache = text
            self.pdf_page_count = pages

    def set_project(self, path: Optional[str],
                    class_paths: Optional[Dict[str, str]] = None,
                    xml_paths: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            self.current_project_path = path
            self.project_class_paths = dict(class_paths or {})
            self.project_xml_paths = dict(xml_paths or {})

    # ---- snapshot for prompts -----------------------------------------
    def summary(self) -> str:
        parts = []
        with self._lock:
            if self.current_images:
                parts.append(f"{len(self.current_images)} images loaded")
            if self.current_pdf:
                import os
                parts.append(f"PDF: {os.path.basename(self.current_pdf)} "
                             f"({self.pdf_page_count} pages)")
            if self.current_project_path:
                import os
                parts.append(f"Project: {os.path.basename(self.current_project_path)}")
            if self.chess_active:
                parts.append("Chess game active")
            if self.dialogue_owner:
                parts.append(f"awaiting input for {self.dialogue_owner}")
        return " | ".join(parts) if parts else ""

    # ── Phase 5: dialogue ownership ─────────────────────────────
    def claim_dialogue(self, agent_name: str, note: str = "") -> None:
        with self._lock:
            self.dialogue_owner = agent_name
            self.dialogue_note = note

    def release_dialogue(self) -> None:
        with self._lock:
            self.dialogue_owner = None
            self.dialogue_note = None

    def current_dialogue_owner(self) -> Optional[str]:
        with self._lock:
            return self.dialogue_owner

    # ── Phase 6: task context accessors ─────────────────────────
    def set_current_task(self, task: Any) -> None:
        """
        Replace the current task. The previous task is archived into
        task_history (up to 20 entries). Does NOT clear the execution
        queue — call clear_execution_queue() separately.
        """
        with self._lock:
            if self.current_task is not None:
                self.task_history.append(self.current_task)
                if len(self.task_history) > 20:
                    self.task_history = self.task_history[-20:]
            self.current_task = task

    def get_current_task(self) -> Optional[Any]:
        with self._lock:
            return self.current_task

    def clear_execution_queue(self) -> None:
        with self._lock:
            self.execution_queue.clear()


# ---- module singleton -------------------------------------------------
_ctx: Optional[SessionContext] = None
_ctx_lock = threading.Lock()


def get_session() -> SessionContext:
    global _ctx
    if _ctx is None:
        with _ctx_lock:
            if _ctx is None:
                _ctx = SessionContext()
    return _ctx


def sync_from_legacy(namespace: Dict[str, Any]) -> None:
    """
    Pull current values out of app.py's module globals into the
    SessionContext. Called by app.py in Phase 5. Only reads known keys.
    """
    ctx = get_session()
    with ctx._lock:
        imgs = namespace.get("CURRENT_IMAGES")
        if isinstance(imgs, list):
            ctx.current_images = list(imgs)

        pdf = namespace.get("CURRENT_PDF")
        ctx.current_pdf = pdf if isinstance(pdf, str) else None
        txt = namespace.get("PDF_TEXT_CACHE")
        ctx.pdf_text_cache = txt if isinstance(txt, str) else None
        pages = namespace.get("PDF_PAGE_COUNT")
        ctx.pdf_page_count = int(pages) if isinstance(pages, int) else 0

        proj = namespace.get("CURRENT_PROJECT_PATH")
        ctx.current_project_path = proj if isinstance(proj, str) else None
        cp = namespace.get("PROJECT_CLASS_PATHS")
        ctx.project_class_paths = dict(cp) if isinstance(cp, dict) else {}
        xp = namespace.get("PROJECT_XML_PATHS")
        ctx.project_xml_paths = dict(xp) if isinstance(xp, dict) else {}


# ---- smoke test --------------------------------------------------------
if __name__ == "__main__":
    ctx = get_session()
    ctx.add_image("/tmp/a.png")
    ctx.add_image("/tmp/b.png")
    assert len(ctx.current_images) == 2
    ctx.set_pdf("/tmp/x.pdf", "hello", 3)
    assert ctx.pdf_page_count == 3

    # Phase 5 dialogue claim
    assert ctx.current_dialogue_owner() is None
    ctx.claim_dialogue("chess_agent", "awaiting color")
    assert ctx.current_dialogue_owner() == "chess_agent"
    ctx.release_dialogue()
    assert ctx.current_dialogue_owner() is None

    # Phase 6 task isolation
    class _FakeTask:
        def __init__(self, tid):
            self.task_id = tid

    t1 = _FakeTask("t1")
    t2 = _FakeTask("t2")
    ctx.set_current_task(t1)
    assert ctx.get_current_task() is t1
    assert len(ctx.task_history) == 0
    ctx.set_current_task(t2)
    assert ctx.get_current_task() is t2
    assert len(ctx.task_history) == 1
    assert ctx.task_history[0] is t1

    ctx.execution_queue.append("stale")
    ctx.clear_execution_queue()
    assert len(ctx.execution_queue) == 0

    print("context.py smoke OK:", ctx.summary())
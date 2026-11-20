"""
Task + TaskManager — structured unit of work.
============================================
Per spec §2 and §3.

State transitions guarded so a completed task cannot be reopened.
Context stored as JSON in SQLite — MUST survive round-trip through
create → get → dispatch.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import config


class TaskState(str, Enum):
    PENDING   = "PENDING"
    PLANNING  = "PLANNING"
    RUNNING   = "RUNNING"
    WAITING   = "WAITING"
    REVIEWING = "REVIEWING"
    FAILED    = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


_ALLOWED: Dict[TaskState, set] = {
    TaskState.PENDING:   {TaskState.PLANNING, TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.PLANNING:  {TaskState.RUNNING, TaskState.WAITING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.RUNNING:   {TaskState.WAITING, TaskState.REVIEWING, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.WAITING:   {TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.REVIEWING: {TaskState.COMPLETED, TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.FAILED:    {TaskState.PENDING, TaskState.CANCELLED},
    TaskState.COMPLETED: set(),
    TaskState.CANCELLED: set(),
}


@dataclass
class Task:
    goal: str
    agent: str
    id: str = field(default_factory=lambda: "task_" + uuid.uuid4().hex[:10])
    state: TaskState = TaskState.PENDING
    priority: str = "NORMAL"
    dependencies: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    result: Optional[str] = None
    error: Optional[str] = None
    retries: int = 0
    steps_used: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def can_transition(self, new_state: TaskState) -> bool:
        return new_state in _ALLOWED.get(self.state, set())

    def to_row(self) -> tuple:
        return (
            self.id,
            self.goal,
            self.agent,
            self.state.value,
            self.priority,
            json.dumps(self.dependencies or []),
            json.dumps(self.context or {}),   # never None, never missing
            self.result,
            self.error,
            self.retries,
            self.steps_used,
            self.created_at,
            self.updated_at,
        )

    @classmethod
    def from_row(cls, row: tuple) -> "Task":
        (tid, goal, agent, state, priority, deps_json, ctx_json,
         result, error, retries, steps, created, updated) = row
        # Defensive JSON parsing — never crash on bad rows
        try:
            deps = json.loads(deps_json) if deps_json else []
        except Exception:
            deps = []
        try:
            ctx = json.loads(ctx_json) if ctx_json else {}
        except Exception:
            ctx = {}
        if not isinstance(deps, list):
            deps = []
        if not isinstance(ctx, dict):
            ctx = {}
        return cls(
            id=tid,
            goal=goal,
            agent=agent,
            state=TaskState(state),
            priority=priority,
            dependencies=deps,
            context=ctx,
            result=result,
            error=error,
            retries=retries or 0,
            steps_used=steps or 0,
            created_at=created,
            updated_at=updated,
        )


class TaskManager:
    """Single-process, thread-safe task registry. Backed by SQLite."""

    def __init__(self, db_path: str = config.DB_FILE) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._ensure_table()

    # ---- schema --------------------------------------------------------
    def _ensure_table(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS agent_tasks (
                        id           TEXT PRIMARY KEY,
                        goal         TEXT NOT NULL,
                        agent        TEXT NOT NULL,
                        state        TEXT NOT NULL,
                        priority     TEXT NOT NULL DEFAULT 'NORMAL',
                        dependencies TEXT NOT NULL DEFAULT '[]',
                        context      TEXT NOT NULL DEFAULT '{}',
                        result       TEXT,
                        error        TEXT,
                        retries      INTEGER NOT NULL DEFAULT 0,
                        steps_used   INTEGER NOT NULL DEFAULT 0,
                        created_at   TEXT NOT NULL,
                        updated_at   TEXT NOT NULL
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_state ON agent_tasks(state)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_agent ON agent_tasks(agent)")
                conn.commit()
            finally:
                conn.close()

    # ---- CRUD ----------------------------------------------------------
    def create(self, task: Task) -> Task:
        """Persist the task. The Python object you pass in is returned
        unchanged (context preserved exactly as-is in memory)."""
        if task.context is None:
            task.context = {}
        if task.dependencies is None:
            task.dependencies = []
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                conn.execute(
                    "INSERT INTO agent_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    task.to_row(),
                )
                conn.commit()
            finally:
                conn.close()
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                row = conn.execute(
                    "SELECT * FROM agent_tasks WHERE id = ?", (task_id,)
                ).fetchone()
            finally:
                conn.close()
        return Task.from_row(row) if row else None

    def list_by_state(self, state: TaskState) -> List[Task]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                rows = conn.execute(
                    "SELECT * FROM agent_tasks WHERE state = ? ORDER BY created_at",
                    (state.value,),
                ).fetchall()
            finally:
                conn.close()
        return [Task.from_row(r) for r in rows]

    def list_recent(self, limit: int = 50) -> List[Task]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                rows = conn.execute(
                    "SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        return [Task.from_row(r) for r in rows]

    # ---- transitions ---------------------------------------------------
    def transition(self, task_id: str, new_state: TaskState,
                   *, result: Optional[str] = None,
                   error: Optional[str] = None) -> Task:
        with self._lock:
            task = self.get(task_id)
            if not task:
                raise ValueError(f"Task {task_id} not found")
            if not task.can_transition(new_state):
                raise ValueError(
                    f"Illegal transition {task.state.value} → {new_state.value}"
                )
            task.state = new_state
            task.updated_at = datetime.now().isoformat()
            if result is not None:
                task.result = result
            if error is not None:
                task.error = error

            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                conn.execute(
                    """UPDATE agent_tasks
                          SET state=?, result=?, error=?, updated_at=?
                        WHERE id=?""",
                    (task.state.value, task.result, task.error,
                     task.updated_at, task.id),
                )
                conn.commit()
            finally:
                conn.close()
        return task

    def increment_retry(self, task_id: str) -> int:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                conn.execute(
                    "UPDATE agent_tasks SET retries = retries + 1, "
                    "updated_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), task_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT retries FROM agent_tasks WHERE id = ?", (task_id,)
                ).fetchone()
            finally:
                conn.close()
        return row[0] if row else 0

    def increment_step(self, task_id: str) -> int:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                conn.execute(
                    "UPDATE agent_tasks SET steps_used = steps_used + 1, "
                    "updated_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), task_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT steps_used FROM agent_tasks WHERE id = ?", (task_id,)
                ).fetchone()
            finally:
                conn.close()
        return row[0] if row else 0

    def dependencies_met(self, task: Task) -> bool:
        if not task.dependencies:
            return True
        for dep_id in task.dependencies:
            dep = self.get(dep_id)
            if not dep or dep.state != TaskState.COMPLETED:
                return False
        return True

    def within_limits(self, task: Task) -> bool:
        return (
            task.retries < config.MAX_RETRIES
            and task.steps_used < config.MAX_AGENT_STEPS
        )


# ---- module-level default instance ------------------------------------
_default_manager: Optional[TaskManager] = None
_init_lock = threading.Lock()


def get_task_manager() -> TaskManager:
    global _default_manager
    if _default_manager is None:
        with _init_lock:
            if _default_manager is None:
                _default_manager = TaskManager()
    return _default_manager


# ---- smoke test --------------------------------------------------------
if __name__ == "__main__":
    tm = TaskManager()
    t = tm.create(Task(
        goal="smoke test goal",
        agent="coding",
        context={"action": "check_status", "session_id": "s1"},
    ))
    assert t.state == TaskState.PENDING
    fetched = tm.get(t.id)
    assert fetched.context.get("action") == "check_status", fetched.context
    assert fetched.context.get("session_id") == "s1"

    tm.transition(t.id, TaskState.RUNNING)
    tm.transition(t.id, TaskState.REVIEWING)
    tm.transition(t.id, TaskState.COMPLETED, result="done")
    assert tm.get(t.id).state == TaskState.COMPLETED

    try:
        tm.transition(t.id, TaskState.RUNNING)
        raise SystemExit("❌ illegal transition was allowed!")
    except ValueError:
        pass

    print("task.py smoke OK (context survives round-trip)")
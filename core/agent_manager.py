"""
AgentManager — registration, dispatch, retry, and state tracking.
=================================================================
Per spec §3.

Responsibilities:
  * Register agents by name.
  * Dispatch a Task to the correct agent.
  * Track the in-flight Task's state via TaskManager.
  * Enforce MAX_RETRIES / MAX_AGENT_STEPS.
  * Return AgentMessages. Never raises on agent failure — the failure
    becomes an ERROR message so the Head Agent can decide to replan.
"""
from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Dict, Optional

import config
from core.message import AgentMessage, MessageType
from core.task import Task, TaskState, TaskManager, get_task_manager

logger = logging.getLogger(__name__)


class AgentManager:
    def __init__(self, task_manager: Optional[TaskManager] = None) -> None:
        self._agents: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._tasks = task_manager or get_task_manager()

    # ---- registration -------------------------------------------------
    def register_agent(self, agent: Any) -> None:
        name = getattr(agent, "name", None)
        if not name or not isinstance(name, str):
            raise ValueError("agent must expose a non-empty `name` str attribute")
        if not hasattr(agent, "handle"):
            raise ValueError(
                f"agent '{name}' must expose a handle(message) method"
            )
        with self._lock:
            if name in self._agents:
                raise ValueError(f"Agent '{name}' already registered")
            self._agents[name] = agent
        logger.info("Registered agent: %s", name)

    def unregister_agent(self, name: str) -> None:
        with self._lock:
            self._agents.pop(name, None)

    def get_agent(self, name: str) -> Optional[Any]:
        with self._lock:
            return self._agents.get(name)

    def list_agents(self) -> list:
        with self._lock:
            return sorted(self._agents.keys())

    # ---- dispatch -----------------------------------------------------
    def dispatch(self, task: Task) -> AgentMessage:
        """
        Run a task and return the resulting message. Never raises.
        The task.context passed by the caller is preserved exactly.
        """
        logger.info(
            "Dispatching task %s to %s; context keys=%s",
            task.id, task.agent, list((task.context or {}).keys())
        )

        # 1. Find agent
        agent = self.get_agent(task.agent)
        if agent is None:
            self._safe_transition(task.id, TaskState.FAILED,
                                  error=f"unknown agent '{task.agent}'")
            return AgentMessage.error(
                from_agent="agent_manager",
                to_agent="head_agent",
                content=f"No such agent: {task.agent}",
                task_id=task.id,
            )

        # 2. Caps
        if not self._tasks.within_limits(task):
            self._safe_transition(task.id, TaskState.FAILED,
                                  error="retry or step limit reached")
            return AgentMessage.error(
                from_agent="agent_manager",
                to_agent="head_agent",
                content=f"Task {task.id} exceeded retry/step limits",
                task_id=task.id,
            )

        # 3. Dependencies
        if not self._tasks.dependencies_met(task):
            self._safe_transition(task.id, TaskState.WAITING,
                                  error="dependencies not met")
            return AgentMessage.error(
                from_agent="agent_manager",
                to_agent="head_agent",
                content=f"Task {task.id}: dependencies not met",
                task_id=task.id,
            )

        # 4. Transition to RUNNING
        try:
            self._tasks.transition(task.id, TaskState.RUNNING)
        except ValueError as e:
            return AgentMessage.error(
                from_agent="agent_manager",
                to_agent="head_agent",
                content=f"Task {task.id}: state error {e}",
                task_id=task.id,
            )

        # 5. Count a step
        self._tasks.increment_step(task.id)

        # 6. Build outgoing message — use the ORIGINAL task object's
        #    context (in-memory), never a re-fetched version. This is
        #    the fix: whatever the caller put in `task.context` is what
        #    the agent sees.
        outgoing = AgentMessage.task(
            from_agent="agent_manager",
            to_agent=task.agent,
            content=task.goal,
            task_id=task.id,
            context=dict(task.context or {}),
        )

        logger.info(
            "Sending to %s: content=%r context_keys=%s",
            task.agent, task.goal[:80], list(outgoing.context.keys())
        )

        # 7. Invoke — capture all exceptions
        try:
            incoming = agent.handle(outgoing)
        except Exception as e:
            tb = traceback.format_exc(limit=5)
            logger.error(
                "Agent %s raised on task %s: %s\n%s",
                task.agent, task.id, e, tb,
            )
            self._tasks.increment_retry(task.id)
            self._safe_transition(task.id, TaskState.FAILED, error=str(e))
            return AgentMessage.error(
                from_agent=task.agent,
                to_agent="head_agent",
                content=f"Agent raised: {e}",
                task_id=task.id,
                context={"traceback": tb},
            )

        # 8. Defensive: agent must return an AgentMessage
        if not isinstance(incoming, AgentMessage):
            self._tasks.increment_retry(task.id)
            self._safe_transition(task.id, TaskState.FAILED,
                                  error="agent returned non-AgentMessage")
            return AgentMessage.error(
                from_agent=task.agent,
                to_agent="head_agent",
                content=f"Agent {task.agent} returned {type(incoming).__name__}",
                task_id=task.id,
            )

        # 9. Reflect result in task record
        if incoming.type == MessageType.RESULT and incoming.is_success():
            self._safe_transition(task.id, TaskState.COMPLETED,
                                  result=str(incoming.content)[:2000])
        elif incoming.type == MessageType.ERROR:
            self._tasks.increment_retry(task.id)
            self._safe_transition(task.id, TaskState.FAILED,
                                  error=str(incoming.content)[:2000])
        else:
            self._safe_transition(task.id, TaskState.WAITING)

        return incoming

    # ---- helpers ------------------------------------------------------
    def _safe_transition(self, task_id: str, new_state: TaskState,
                         result: Optional[str] = None,
                         error: Optional[str] = None) -> None:
        try:
            self._tasks.transition(task_id, new_state,
                                   result=result, error=error)
        except Exception as e:
            logger.warning("Could not transition task %s → %s: %s",
                           task_id, new_state.value, e)

    def task_manager(self) -> TaskManager:
        return self._tasks


# ---- singleton --------------------------------------------------------
_am: Optional[AgentManager] = None
_am_lock = threading.Lock()


def get_agent_manager() -> AgentManager:
    global _am
    if _am is None:
        with _am_lock:
            if _am is None:
                _am = AgentManager()
    return _am


# ---- shared-services registry ----------------------------------------
# app.py registers its live objects here in Phase 5.

_services: Dict[str, Any] = {}
_svc_lock = threading.Lock()


def register_service(name: str, obj) -> None:
    with _svc_lock:
        _services[name] = obj
    logger.info("Registered service: %s", name)


def get_service(name: str):
    with _svc_lock:
        return _services.get(name)


def list_services() -> list:
    with _svc_lock:
        return sorted(_services.keys())


# ---- singleton app-module loader -------------------------------------
_app_module_singleton = None
_app_module_lock = threading.Lock()


def get_app_module():
    """Return the one loaded app.py module object for this process."""
    global _app_module_singleton
    registered = get_service("app_module")
    if registered is not None:
        return registered
    with _app_module_lock:
        if _app_module_singleton is not None:
            return _app_module_singleton
        import importlib.util
        import os
        path = os.path.join(os.getcwd(), "app.py")
        spec = importlib.util.spec_from_file_location("_app_module_shared", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _app_module_singleton = mod
        return mod


# ---- smoke test --------------------------------------------------------
if __name__ == "__main__":
    class _EchoAgent:
        name = "echo"
        def handle(self, msg: AgentMessage) -> AgentMessage:
            return AgentMessage.result(
                from_agent=self.name,
                to_agent="head_agent",
                content=f"echo: {msg.content} | ctx={msg.context}",
                task_id=msg.task_id,
            )

    am = AgentManager()
    am.register_agent(_EchoAgent())

    t1 = am.task_manager().create(Task(
        goal="hi", agent="echo",
        context={"action": "check_status", "session_id": "s1"},
    ))
    r1 = am.dispatch(t1)
    assert r1.is_success(), r1
    assert "check_status" in r1.as_text(), f"context lost! got: {r1.as_text()}"

    print("agent_manager.py smoke OK (context preserved end-to-end)")
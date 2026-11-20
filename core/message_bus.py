"""
MessageBus — in-process pub/sub for status updates.
===================================================
Per spec §18: the GUI must show *short* status lines like:

    🧠 Head Agent  → Planning
    📁 File Agent  → Inspecting project
    💻 Coding Agent → Analyzing Gradle

Never chain-of-thought. Only status. The bus carries those.

Design:
  * Subscribe with a callable(payload: dict).
  * Publish is non-blocking.
  * Slow subscribers do not block the publisher — each subscriber gets
    its own bounded queue and a daemon worker thread.
  * Publisher may also publish into the bus's "recent" ring buffer so a
    late-joining subscriber can see the last N statuses.

No imports from app.py. The GUI (app.py) subscribes in Phase 5 by
calling bus.subscribe(handler) and, inside handler, doing
    self.root.after(0, lambda: self.add_to_chat(text, is_user=False))
"""
from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List

logger = logging.getLogger(__name__)


class _Subscriber:
    def __init__(self, handler: Callable[[Dict[str, Any]], None], qsize: int = 200):
        self.handler = handler
        self.queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=qsize)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            payload = self.queue.get()
            if payload is None:   # shutdown sentinel
                return
            try:
                self.handler(payload)
            except Exception as e:
                logger.exception("subscriber handler raised: %s", e)

    def offer(self, payload: Dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            # drop oldest, try again once
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(payload)
            except Exception:
                pass

    def close(self) -> None:
        try:
            self.queue.put_nowait(None)
        except Exception:
            pass


class MessageBus:
    def __init__(self, recent_size: int = 100) -> None:
        self._subs: List[_Subscriber] = []
        self._lock = threading.RLock()
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=recent_size)

    # ---- pub/sub ------------------------------------------------------
    def subscribe(self, handler: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._subs.append(_Subscriber(handler))

    def publish(self, payload: Dict[str, Any]) -> None:
        """
        payload convention (all optional except `text`):
            {
              "text":    "💻 Coding Agent → Analyzing Gradle",
              "agent":   "coding_agent",
              "kind":    "status" | "task" | "result" | "error",
              "task_id": "task_abc",
            }
        """
        with self._lock:
            self._recent.append(payload)
            subs = list(self._subs)
        for s in subs:
            s.offer(payload)

    # ---- convenience --------------------------------------------------
    def status(self, agent: str, text: str,
               task_id: str = "", kind: str = "status") -> None:
        self.publish({
            "text": f"{agent} → {text}",
            "agent": agent,
            "kind": kind,
            "task_id": task_id,
        })

    def recent(self, n: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._recent)[-n:]

    def shutdown(self) -> None:
        with self._lock:
            for s in self._subs:
                s.close()
            self._subs.clear()


# ---- singleton --------------------------------------------------------
_bus: "MessageBus | None" = None
_bus_lock = threading.Lock()


def get_bus() -> MessageBus:
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = MessageBus()
    return _bus


# ---- smoke test --------------------------------------------------------
if __name__ == "__main__":
    import time
    got: List[str] = []
    bus = get_bus()
    bus.subscribe(lambda p: got.append(p["text"]))
    bus.status("🧠 Head Agent", "Planning")
    bus.status("💻 Coding Agent", "Analyzing Gradle")
    time.sleep(0.1)   # let subscriber drain
    assert len(got) == 2, got
    assert bus.recent(5)[-1]["text"].endswith("Analyzing Gradle")
    print("message_bus.py smoke OK:", got)
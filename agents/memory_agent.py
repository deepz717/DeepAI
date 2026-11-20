"""
MemoryAgent — exposes AIMemory through the agent framework.
============================================================
Handles:
  * remember my <key> is <value>
  * remember that my <key> is <value>
  * what is my <key>
  * what do you know about me
  * forget <key>

Reads raw_text from msg.context if present (HeadAgent sets it), so it
always sees the user's exact words — not the planner's summary.

Alias normalization: synonyms like "dob", "birthday", "date of birth"
all map to the same canonical key so recall finds what remember stored.

Why this agent exists:
  The head_agent direct-answer path uses phi3:mini with a generic
  system prompt. When asked to store personal data, the LLM applies
  a safety refusal pattern. MemoryAgent bypasses the LLM entirely
  and calls ai_memory.remember() directly on the local SQLite store.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Optional

from agents.base_agent import BaseAgent
from core.message import AgentMessage


# Canonical key aliases — all map to one normalized form.
KEY_ALIASES = {
    # date of birth
    "dob": "date of birth",
    "d.o.b": "date of birth",
    "d.o.b.": "date of birth",
    "birthday": "date of birth",
    "birth date": "date of birth",
    "birthdate": "date of birth",
    "date of birth": "date of birth",
    # name
    "full name": "name",
    "my name": "name",
    # phone
    "phone": "phone number",
    "phone number": "phone number",
    "mobile": "phone number",
    "mobile number": "phone number",
    # color
    "favorite color": "favorite color",
    "favourite color": "favorite color",
    "fav color": "favorite color",
    "favorite colour": "favorite color",
    # address
    "home address": "address",
    "my address": "address",
}


class MemoryAgent(BaseAgent):
    name = "memory_agent"

    # ------------------------------------------------------------------
    def _memory(self):
        """Live AIMemory service, or a local fallback."""
        live = self.get_service("ai_memory")
        if live is not None:
            return live
        from core.agent_manager import get_app_module
        return get_app_module().AIMemory()

    def _db_file(self) -> str:
        try:
            from core.agent_manager import get_app_module
            return get_app_module().DB_FILE
        except Exception:
            import config
            return config.DB_FILE

    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_key(key: str) -> str:
        """Map synonyms to a canonical key."""
        k = (key or "").strip().lower().rstrip("?.!,")
        k = re.sub(r"\s+", " ", k)   # collapse internal whitespace
        return KEY_ALIASES.get(k, k)

    # ------------------------------------------------------------------
    def handle(self, msg: AgentMessage) -> AgentMessage:
        # Prefer raw user text from context if present (HeadAgent sets it).
        raw = msg.context.get("raw_text")
        if raw:
            text = raw
        else:
            text = msg.content if isinstance(msg.content, str) else str(
                msg.content.get("text", "")
            )
        cmd = text.strip().lower()
        action = (msg.context.get("action") or "").strip().lower()

        self.publish_status(f"memory: {action or cmd[:30]}", msg)

        # action-driven path (planner)
        if action in ("remember", "store", "save_memory", "store_memory"):
            return self._do_remember(text, msg)
        if action in ("recall", "retrieve", "get_memory", "read_memory"):
            return self._do_recall(text, msg)
        if action in ("list_memories", "what_do_you_know",
                      "list_all", "list_memory"):
            return self._do_list(msg)
        if action in ("forget", "delete_memory", "remove_memory"):
            return self._do_forget(text, msg)

        # string-matching fallback (raw chat, no action)
        if cmd.startswith(("remember my ", "remember that my ",
                           "remember that ", "remember ")):
            return self._do_remember(text, msg)
        if cmd.startswith(("what is my ", "what's my ", "whats my ",
                           "recall my ",
                           "so what is my ", "so what's my ",
                           "so whats my ")):
            return self._do_recall(text, msg)
        if cmd.startswith(("so what is ", "so what's ", "so whats ")):
            return self._do_recall(text, msg)
        if cmd in ("what do you know about me",
                   "what do you remember about me",
                   "list my memories",
                   "show my memories",
                   "what do you know"):
            return self._do_list(msg)
        if cmd.startswith(("forget ", "forget my ")):
            return self._do_forget(text, msg)

        return self.fail(
            f"MemoryAgent: could not parse '{cmd}'. Try 'remember my X is Y' "
            f"or 'what is my X' or 'what do you know about me'.",
            msg,
        )

    # ------------------------------------------------------------------
    def _do_remember(self, text: str, msg: AgentMessage) -> AgentMessage:
        # Try several phrasings, longest-specific first.
        patterns = [
            r"remember\s+that\s+my\s+(.+?)\s+is\s+(.+)",
            r"remember\s+my\s+(.+?)\s+is\s+(.+)",
            r"remember\s+that\s+(.+?)\s+is\s+(.+)",
            r"remember\s+(.+?)\s*=\s*(.+)",
        ]
        for pat in patterns:
            m = re.match(pat, text.strip(), re.IGNORECASE)
            if m:
                raw_key = m.group(1).strip().lower()
                value = m.group(2).strip().rstrip(".!?,;")
                if raw_key and value:
                    key = self._normalize_key(raw_key)
                    try:
                        result = self._memory().remember(key, value)
                        return self.respond(result, msg)
                    except Exception as e:
                        return self.fail(f"remember failed: {e}", msg)

        # Fallback: store whole phrase as a note
        m = re.match(r"remember\s+(.+)", text.strip(), re.IGNORECASE)
        if m:
            value = m.group(1).strip().rstrip(".!?,;")
            key = "note_" + str(abs(hash(value)))[:8]
            try:
                result = self._memory().remember(key, value)
                return self.respond(result, msg)
            except Exception as e:
                return self.fail(f"remember failed: {e}", msg)

        return self.fail(
            "MemoryAgent: could not extract key/value. Try "
            "'remember my favorite color is blue'.",
            msg,
        )

    def _do_recall(self, text: str, msg: AgentMessage) -> AgentMessage:
        # Strip leading "so" and trailing "?" variants
        raw = text.strip()
        raw = re.sub(r"^\s*so\s+", "", raw, flags=re.IGNORECASE)

        patterns = [
            r"what'?s?\s+my\s+(.+?)\s*\??$",
            r"what\s+is\s+my\s+(.+?)\s*\??$",
            r"recall\s+my\s+(.+?)\s*\??$",
            r"recall\s+(.+?)\s*\??$",
        ]
        raw_key = None
        for pat in patterns:
            m = re.match(pat, raw, re.IGNORECASE)
            if m:
                raw_key = m.group(1).strip().lower().rstrip("?")
                break
        if raw_key is None:
            raw_key = (msg.context.get("key") or "").strip().lower()
        if not raw_key:
            return self.fail("MemoryAgent: no key provided.", msg)

        key = self._normalize_key(raw_key)

        # Try the canonical key first, then the raw key
        value = None
        try:
            value = self._memory().recall(key)
            if value is None and key != raw_key:
                value = self._memory().recall(raw_key)
        except Exception as e:
            return self.fail(f"recall failed: {e}", msg)

        if value:
            return self.respond(f"📝 Your {key} is {value}", msg)
        return self.respond(
            f"❌ I don't have anything stored for '{key}'.", msg
        )

    def _do_list(self, msg: AgentMessage) -> AgentMessage:
        try:
            memories = self._memory().get_all_memories()
        except Exception as e:
            return self.fail(str(e), msg)
        if not memories:
            return self.respond(
                "📭 I don't remember anything yet. "
                "Say 'remember my X is Y' to teach me.",
                msg,
            )
        lines = [f"• {k}: {v}" for k, v in memories[:20]]
        return self.respond(
            "📝 **Things I remember:**\n\n" + "\n".join(lines),
            msg,
        )

    def _do_forget(self, text: str, msg: AgentMessage) -> AgentMessage:
        m = re.match(r"forget\s+(?:my\s+)?(.+)", text.strip(), re.IGNORECASE)
        raw_key = m.group(1).strip().lower().rstrip(".!?,;") if m else None
        if not raw_key:
            return self.fail("MemoryAgent: no key to forget.", msg)
        key = self._normalize_key(raw_key)

        try:
            db_file = self._db_file()
            conn = sqlite3.connect(db_file, timeout=30)
            try:
                cur = conn.cursor()
                # Delete both the canonical and raw key, in case
                # older entries used the non-normalized form.
                cur.execute(
                    "DELETE FROM long_term_memory WHERE key IN (?, ?)",
                    (key, raw_key),
                )
                conn.commit()
                deleted = cur.rowcount
            finally:
                conn.close()

            if deleted:
                try:
                    mem = self._memory()
                    if hasattr(mem, "session_memory"):
                        mem.session_memory.pop(key, None)
                        mem.session_memory.pop(raw_key, None)
                except Exception:
                    pass
                return self.respond(f"🗑️ Forgot '{key}'.", msg)
            return self.respond(
                f"❌ I don't have anything stored for '{key}'.", msg
            )
        except Exception as e:
            return self.fail(f"forget failed: {e}", msg)
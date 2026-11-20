"""
CodingAgent — read/search/analyze code. Write is confirm-gated.
===============================================================
Actions:
  inspect_project       — summarize project structure
  read_file             — return file content
  search_project        — regex/grep across a project tree
  find_symbol           — locate class/def/function declaration
  write_file            — CONFIRM_REQUIRED
  general_code_question — free-form code Q&A (conceptual only)

Accepts BOTH raw text and structured actions.

NOTE: Code *synthesis* is code_agent's job. coding_agent handles
reading, searching, and inspecting existing code.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from ai.ollama import get_llm
from agents.base_agent import BaseAgent
from core.message import AgentMessage


class CodingAgent(BaseAgent):
    name = "coding_agent"

    def __init__(self) -> None:
        self._mod = None
        self._llm = get_llm()

    # ------------------------------------------------------------------
    def _app(self):
        from core.agent_manager import get_app_module
        return get_app_module()

    # ------------------------------------------------------------------
    def handle(self, msg: AgentMessage) -> AgentMessage:
        # Prefer raw user text from context (HeadAgent sets it).
        raw = msg.context.get("raw_text")
        if raw:
            text = raw
        else:
            text = msg.content if isinstance(msg.content, str) else str(
                msg.content.get("text", "")
            )
        text = text.strip()
        action = (msg.context.get("action") or "").strip().lower()

        verb = self._canonical(text.lower(), action)

        self.publish_status(f"coding: {verb or action or 'unknown'}", msg)

        try:
            if verb == "inspect_project":
                return self._inspect_project(msg)
            if verb == "read_file":
                return self._read_file(msg, text)
            if verb == "search_project":
                return self._search_project(msg, text)
            if verb == "find_symbol":
                return self._find_symbol(msg, text)
            if verb == "write_file":
                return self._write_file(msg, text)
            if verb == "wrong_agent_code_synthesis":
                return self.respond(
                    "ℹ️ Code generation is handled by code_agent. "
                    "Try: 'write a two sum code in python'.",
                    msg,
                )
            return self._general_question(msg, text)
        except Exception as e:
            return self.fail(f"CodingAgent error: {e}", msg)

    # ------------------------------------------------------------------
    @staticmethod
    def _canonical(cmd: str, action: str) -> str:
        cmd = (cmd or "").strip().lower()
        action = (action or "").strip().lower()

        exact = {
            "inspect_project":       "inspect_project",
            "analyze_project":       "inspect_project",
            "inspect project":       "inspect_project",
            "analyze project":       "inspect_project",
            "read_file":             "read_file",
            "read file":             "read_file",
            "search_project":        "search_project",
            "search_code":           "search_project",
            "search project":        "search_project",
            "find_symbol":           "find_symbol",
            "find symbol":           "find_symbol",
            "write_file":            "write_file",
            "write file":            "write_file",
            "modify_file":           "write_file",
            "general_code_question": "general_code_question",
            "code_question":         "general_code_question",
            "write_code":            "wrong_agent_code_synthesis",
            "write code":            "wrong_agent_code_synthesis",
            "code_request":          "wrong_agent_code_synthesis",
            "generate_code":         "wrong_agent_code_synthesis",
        }
        if cmd in exact:
            return exact[cmd]
        if action in exact:
            return exact[action]

        if cmd.startswith("inspect project") or cmd.startswith("analyze project"):
            return "inspect_project"
        if cmd.startswith("read file "):
            return "read_file"
        if cmd.startswith("search project for "):
            return "search_project"
        if cmd.startswith("find symbol "):
            return "find_symbol"
        if cmd.startswith("write file "):
            return "write_file"

        return "general_code_question"

    # ------------------------------------------------------------------
    def _inspect_project(self, msg: AgentMessage) -> AgentMessage:
        app = self._app()
        project = msg.context.get("project_path") or getattr(
            app, "CURRENT_PROJECT_PATH", None
        )
        if not project:
            return self.fail("No project path provided and none loaded.", msg)
        analyzer = app.SmartProjectAnalyzer()
        stats = analyzer.analyze_project_level1(project)
        if not stats:
            return self.fail(f"Analyzer returned nothing for {project}", msg)
        lines = [f"📁 {os.path.basename(project)}",
                 f"  Files: {stats['total_files']:,}"]
        if stats.get("languages"):
            top = sorted(stats["languages"].items(), key=lambda x: -x[1])[:5]
            lines.append("  Languages: " + ", ".join(
                f"{k}({v})" for k, v in top))
        if stats.get("frameworks"):
            lines.append("  Frameworks: " + ", ".join(
                sorted(stats["frameworks"])))
        android = stats.get("android", {})
        lines.append(f"  Activities: {android.get('activities', 0)}, "
                     f"Fragments: {android.get('fragments', 0)}, "
                     f"Layouts: {android.get('layouts', 0)}")
        return self.respond("\n".join(lines), msg)

    def _read_file(self, msg: AgentMessage, text: str) -> AgentMessage:
        path = msg.context.get("path") or self._extract_arg(text, "read file ")
        if not path or not os.path.isfile(path):
            return self.fail(f"File not found: {path}", msg)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception as e:
            return self.fail(f"Read failed: {e}", msg)
        if len(content) > 8000:
            content = content[:8000] + "\n... [truncated]"
        return self.respond(content, msg)

    def _search_project(self, msg: AgentMessage, text: str) -> AgentMessage:
        needle = (msg.context.get("query")
                  or self._extract_arg(text, "search project for "))
        if not needle:
            return self.fail("No search term provided.", msg)
        root = msg.context.get("project_path")
        if not root:
            app = self._app()
            root = getattr(app, "CURRENT_PROJECT_PATH", None) or os.getcwd()
        try:
            pattern = re.compile(needle, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(needle), re.IGNORECASE)
        hits: List[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")
                           and d not in {"node_modules", "__pycache__",
                                         ".git", "build", "venv", ".venv"}]
            for fname in filenames:
                if not fname.endswith((".py", ".java", ".kt", ".js", ".ts",
                                       ".txt", ".md", ".json", ".xml")):
                    continue
                fp = os.path.join(dirpath, fname)
                try:
                    with open(fp, "r", encoding="utf-8",
                              errors="ignore") as fh:
                        for n, line in enumerate(fh, 1):
                            if pattern.search(line):
                                rel = os.path.relpath(fp, root)
                                hits.append(f"{rel}:{n}: "
                                            f"{line.strip()[:160]}")
                                if len(hits) >= 40:
                                    raise StopIteration
                except StopIteration:
                    break
                except Exception:
                    continue
            if len(hits) >= 40:
                break
        if not hits:
            return self.respond(f"No matches for '{needle}'.", msg)
        return self.respond(
            f"🔍 {len(hits)} match(es) for '{needle}':\n"
            + "\n".join(hits[:40]),
            msg,
        )

    def _find_symbol(self, msg: AgentMessage, text: str) -> AgentMessage:
        name = (msg.context.get("symbol")
                or self._extract_arg(text, "find symbol "))
        if not name:
            return self.fail("No symbol name provided.", msg)
        root = msg.context.get("project_path")
        if not root:
            app = self._app()
            root = getattr(app, "CURRENT_PROJECT_PATH", None) or os.getcwd()
        pat = re.compile(
            rf"^\s*(class|def|function|interface|enum)\s+"
            rf"{re.escape(name)}\b"
        )
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")
                           and d not in {"node_modules", "__pycache__",
                                         ".git", "build", "venv", ".venv"}]
            for fname in filenames:
                if not fname.endswith((".py", ".java", ".kt", ".js", ".ts")):
                    continue
                fp = os.path.join(dirpath, fname)
                try:
                    with open(fp, "r", encoding="utf-8",
                              errors="ignore") as fh:
                        for n, line in enumerate(fh, 1):
                            if pat.match(line):
                                found.append(
                                    f"{os.path.relpath(fp, root)}:{n}: "
                                    f"{line.strip()[:160]}"
                                )
                except Exception:
                    continue
        if not found:
            return self.respond(f"No declaration found for '{name}'.", msg)
        return self.respond(f"📍 {name}:\n" + "\n".join(found[:20]), msg)

    # ------------------------------------------------------------------
    def _general_question(self, msg: AgentMessage, text: str) -> AgentMessage:
        answer = self._llm.generate(
            text,
            system=(
                "You are a senior software engineer answering a "
                "conceptual coding question concisely and factually. "
                "Do not include philosophical commentary. "
                "If the user is asking you to write code, say so — code "
                "generation is handled by a different assistant."
            ),
            temperature=0.2,
            max_tokens=500,
        )
        if not answer:
            return self.fail("LLM produced no answer.", msg)
        return self.respond(answer, msg)

    # ------------------------------------------------------------------
    def _write_file(self, msg: AgentMessage, text: str) -> AgentMessage:
        if not msg.context.get("confirmed"):
            path = msg.context.get("path") or self._extract_arg(
                text, "write file ")
            return self.need_confirmation(
                f"⚠️ Write to file?\n  path: {path}\n"
                "Reply YES to confirm. This will overwrite the file.",
                msg,
                context={"action": "write_file", "path": path},
            )
        path = msg.context.get("path")
        content = msg.context.get("content", "")
        if not path:
            return self.fail("write_file requires path in context.", msg)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)),
                        exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return self.respond(
                f"✅ Wrote {len(content)} bytes to {path}", msg)
        except Exception as e:
            return self.fail(f"write failed: {e}", msg)

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_arg(text: str, prefix: str) -> str:
        lowered = text.lower()
        idx = lowered.find(prefix.lower())
        if idx == -1:
            return ""
        return text[idx + len(prefix):].strip()
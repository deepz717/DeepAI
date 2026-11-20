"""
FileAgent — wraps PDF, project, file-index, and file-open helpers.
==================================================================
Handles BOTH:
  * raw user text (msg.content), e.g. "open resume"
  * structured action (msg.context["action"]), e.g. "open_resume"
"""
from __future__ import annotations

import os
from typing import Any, Optional

from agents.base_agent import BaseAgent
from core.message import AgentMessage


class FileAgent(BaseAgent):
    name = "file_agent"

    def __init__(self) -> None:
        self._mod = None

    # ------------------------------------------------------------------
    def _app(self):
        from core.agent_manager import get_app_module
        return get_app_module()

    # ------------------------------------------------------------------
    def handle(self, msg: AgentMessage) -> AgentMessage:
        # Prefer raw user text from context
        raw = msg.context.get("raw_text")
        if raw:
            text = raw
        else:
            text = msg.content if isinstance(msg.content, str) else str(
                msg.content.get("text", "")
            )
        cmd = text.strip().lower()
        action = (msg.context.get("action") or "").strip().lower()

        verb = self._canonical(cmd, action)

        self.publish_status(
            f"file: {verb or action or cmd or 'unknown'}", msg
        )

        try:
            app = self._app()
        except Exception as e:
            return self.fail(str(e), msg)

        # ---- PDF ----
        if verb == "summarize_pdf":
            pdf_path = self.get_service("current_pdf") or getattr(app, "CURRENT_PDF", None)
            if not pdf_path:
                return self.fail("No PDF loaded. Open a PDF first.", msg)
            text_cache = getattr(app, "PDF_TEXT_CACHE", None)
            pages = getattr(app, "PDF_PAGE_COUNT", 0)
            if not text_cache:
                return self.fail("PDF text cache is empty.", msg)
            return self.respond(
                app.pdf_rag.instant_summary(pdf_path, text_cache, pages),
                msg,
            )

        if verb == "search_pdf":
            # extract query
            m = cmd.split("search pdf for ")
            q = m[1].strip() if len(m) > 1 else (
                msg.context.get("query", "").strip()
            )
            if not q:
                return self.fail("No search term provided.", msg)
            pdf_path = self.get_service("current_pdf") or getattr(app, "CURRENT_PDF", None)
            if not pdf_path:
                return self.fail("No PDF loaded.", msg)
            return self.respond(app.pdf_rag.search_pdf(pdf_path, q), msg)

        # ---- project ----
        if verb == "show_java_classes":
            proj = (self.get_service("current_project_path")
                    or getattr(app, "CURRENT_PROJECT_PATH", None))
            if not proj:
                return self.fail("No project loaded.", msg)
            classes = app.ProjectRAG.search_classes(proj, "")
            names = [c[0] for c in classes[:40]]
            return self.respond(
                "📋 Classes:\n" + "\n".join(f"  • {n}" for n in names),
                msg,
            )

        if verb == "which_file_contains":
            needle = ""
            if "which file contains " in cmd:
                needle = cmd.split("which file contains ", 1)[1].strip()
            needle = needle or msg.context.get("query", "").strip()
            if not needle:
                return self.fail("No search term provided.", msg)
            proj = (self.get_service("current_project_path")
                    or getattr(app, "CURRENT_PROJECT_PATH", None))
            if not proj:
                return self.fail("No project loaded.", msg)
            results = app.ProjectRAG.search_classes(proj, needle)
            if not results:
                return self.respond(f"No files contain '{needle}'.", msg)
            lines = [f"  📄 {r[0]}" for r in results[:15]]
            return self.respond(
                f"🔍 Files containing '{needle}':\n" + "\n".join(lines),
                msg,
            )

        # ---- index ----
        if verb == "index_stats":
            return self.respond(app.file_indexer.get_index_stats(), msg)

        if verb == "rebuild_index":
            return self.respond(app.file_indexer.rebuild_index(), msg)

        if verb == "find":
            q = ""
            if cmd.startswith("find "):
                q = cmd[5:].strip()
            q = q or msg.context.get("query", "").strip()
            if not q:
                return self.fail("No search term provided.", msg)
            results = app.file_indexer.smart_search(q)
            if not results:
                return self.respond(f"No files matched '{q}'.", msg)
            lines = [f"📄 {r[1]}" for r in results[:15]]
            return self.respond(
                f"🔍 Found {len(results)} files:\n" + "\n".join(lines),
                msg,
            )

        # ---- smart open ----
        if verb == "open_resume":
            return self.respond(app.SmartFileOpen.open_resume(), msg)

        if verb == "open_latest_pdf":
            result = app.SmartFileOpen.open_latest("~/Downloads", "pdf")
            return self.respond(
                f"📄 Opened: {result}" if result else "No PDF found.",
                msg,
            )

        return self.fail(
            f"FileAgent: unrecognized command '{cmd}' / "
            f"action '{action}'.",
            msg,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _canonical(cmd: str, action: str) -> str:
        cmd = (cmd or "").strip().lower()
        action = (action or "").strip().lower()
        hay = f"{cmd} || {action}"

        exact = {
            # PDF
            "summarize pdf":         "summarize_pdf",
            "summarize_pdf":         "summarize_pdf",
            "tell me about this pdf": "summarize_pdf",
            "tell_me_about_this_pdf": "summarize_pdf",
            "summarize this pdf":    "summarize_pdf",
            "search_pdf":            "search_pdf",
            "search pdf":            "search_pdf",
            # project
            "show java classes":     "show_java_classes",
            "show_java_classes":     "show_java_classes",
            "show classes":          "show_java_classes",
            "which_file_contains":   "which_file_contains",
            # index
            "index stats":           "index_stats",
            "index_stats":           "index_stats",
            "file index stats":      "index_stats",
            "rebuild index":         "rebuild_index",
            "rebuild_index":         "rebuild_index",
            "find":                  "find",
            "find_file":             "find",
            # smart open
            "open resume":           "open_resume",
            "open_resume":           "open_resume",
            "find_resume":           "open_resume",     # planner sometimes uses this
            "find my resume":        "open_resume",
            "open my resume":        "open_resume",
            "show my resume":        "open_resume",
            "open latest pdf":       "open_latest_pdf",
            "open_latest_pdf":       "open_latest_pdf",
        }
        if cmd in exact:
            return exact[cmd]
        if action in exact:
            return exact[action]

        # substring heuristics
        if "summarize" in hay and "pdf" in hay:
            return "summarize_pdf"
        if "search" in hay and "pdf" in hay:
            return "search_pdf"
        if "java" in hay and "class" in hay:
            return "show_java_classes"
        if "which file" in hay and "contain" in hay:
            return "which_file_contains"
        if "index stats" in hay or ("index" in hay and "stat" in hay):
            return "index_stats"
        if "rebuild" in hay and "index" in hay:
            return "rebuild_index"
        if "resume" in hay and ("open" in hay or "find" in hay or "show" in hay):
            return "open_resume"
        if "latest" in hay and "pdf" in hay:
            return "open_latest_pdf"
        if cmd.startswith("find ") or action == "find":
            return "find"
        if cmd.startswith("search pdf for "):
            return "search_pdf"
        if cmd.startswith("which file contains "):
            return "which_file_contains"

        return ""
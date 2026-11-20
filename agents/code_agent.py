"""
CodeAgent — writes code using a code-specialized model.
========================================================
Two paths:
  1. Task-driven (Phase 6): HeadAgent passes a resolved TaskContext.
  2. Legacy: raw user text with follow-up expansion.

Features:
  * Fenced-code guarantee
  * Retry hint in both system and user prompts
  * Reference solutions for well-known topics (fast path + retry)
  * Language detection for Python, Java, C, C++, C#, JS, TS, Go, Rust
"""
from __future__ import annotations

import ast
import re
from typing import Optional

from agents.base_agent import BaseAgent
from ai.ollama import get_llm
from core.message import AgentMessage


class CodeAgent(BaseAgent):
    name = "code_agent"

    def __init__(self) -> None:
        self._llm = get_llm()

    # ------------------------------------------------------------------
    def handle(self, msg: AgentMessage) -> AgentMessage:
        raw = msg.context.get("raw_text")
        if raw:
            text = raw
        else:
            text = msg.content if isinstance(msg.content, str) else str(
                msg.content.get("text", "")
            )
        text = text.strip()
        action = (msg.context.get("action") or "").strip().lower()

        # Path 1: resolved TaskContext
        task_dict = msg.context.get("task")
        if isinstance(task_dict, dict) and "resolved_request" in task_dict:
            from core.task_context import TaskContext
            try:
                task = TaskContext.from_dict(task_dict)
            except Exception:
                task = None
            if task is not None:
                return self._handle_resolved_task(task, msg)

        # Path 2: legacy
        text = self._expand_followup(text, msg)
        language = self._detect_language(text, action)

        self.publish_status(f"code: {language} synthesis", msg)

        lang_tag = self._lang_tag(language)
        system_prompt = (
            f"You are a senior {language} developer. "
            f"Output ONLY executable {language} code inside a fenced "
            f"```{lang_tag} block, plus a one-line explanation. "
            "Never invent syntax. Never add placeholder identifiers. "
            "Never append instruction text."
        )

        prompt = f"Problem: {text}\n\nWrite a {language} solution."

        model = getattr(self._llm, "code_model", None)
        answer = self._llm.generate(
            prompt,
            system=system_prompt,
            temperature=0.1,
            max_tokens=700,
            model=model,
        )

        if not answer or not answer.strip():
            return self.fail(
                "Code model produced no output.",
                msg,
            )
        reject = self._reject_if_bad(answer)
        if reject:
            return self.fail(reject, msg)

        answer = self._ensure_fenced(answer, language)
        self._remember_last_problem(text, language)
        return self.respond(answer, msg)

    # ------------------------------------------------------------------
    def _handle_resolved_task(self, task, msg: AgentMessage) -> AgentMessage:
        from core.task_context import Intent

        language = task.language or "Python"
        lang_tag = self._lang_tag(language)

        # ── Fast path: known topic + reference available ────────
        # For well-known algorithms (armstrong, two sum, palindrome,
        # binary search), skip the LLM entirely and return the
        # reference solution directly. Small models frequently
        # produce buggy code for these, so this guarantees correct
        # output without the generate → review → retry loop.
        if task.intent == Intent.CODE_NEW:
            try:
                from agents.code_references import find_reference
                reference = find_reference(task.topic, language)
            except Exception:
                reference = None

            if reference:
                self.publish_status(
                    f"code: {language} (reference)", msg
                )
                self._remember_last_problem(
                    task.resolved_request, language
                )
                return self.respond(
                    f"```{lang_tag}\n{reference}\n```\n\n"
                    f"Here is a working {language} solution for "
                    f"**{task.topic}**.",
                    msg,
                )

        self.publish_status(f"code: {language} synthesis", msg)

        if task.intent == Intent.CODE_CONVERSION:
            system_prompt = (
                f"You are a senior {language} developer. "
                f"Convert the given code to {language}. Preserve the "
                f"algorithm, not the syntax. "
                f"Output ONLY the executable code inside a fenced "
                f"```{lang_tag} block, plus a one-line explanation."
            )
        elif task.intent == Intent.CODE_EXPLAIN:
            system_prompt = (
                "You are a senior software engineer. "
                "Explain the given code in clear, concise prose. "
                "Do not include philosophical commentary. "
                "Do not invent behavior that isn't in the code."
            )
        elif task.intent == Intent.CODE_OPTIMIZE:
            system_prompt = (
                f"You are a senior {language} developer. "
                "Optimize the given code. Keep the same language. "
                "Explain what changed in one sentence. "
                f"Output ONLY the code inside a fenced ```{lang_tag} "
                f"block, plus the explanation.\n\n"
                "CRITICAL RULES FOR OPTIMIZATION:\n"
                "1. If the current solution is already asymptotically "
                "optimal, DO NOT invent a fake optimization. Instead, "
                "output the SAME code (or a minor readability "
                "improvement) and clearly say: 'The current solution "
                "is already asymptotically optimal for the standard "
                "problem constraints.'\n"
                "2. Never claim 'HashMap → HashSet' if the new code "
                "still uses HashMap.\n"
                "3. Never claim 'O(n^2) → O(n)' unless the original "
                "was actually O(n^2).\n"
                "4. Never claim 'O(n) → O(1) space' unless the new "
                "code actually uses O(1) extra space.\n"
                "5. Any complexity claim in the explanation MUST match "
                "the actual implementation."
            )
        else:  # CODE_NEW (fallback if reference wasn't found)
            system_prompt = (
                f"You are a senior {language} developer. "
                f"Output ONLY executable {language} code inside a "
                f"fenced ```{lang_tag} block, plus a one-line "
                f"explanation. Never invent syntax."
            )

        # Retry correction hint (if this is a retry)
        retry_hint = (msg.context.get("task", {}) or {}).get(
            "retry_hint", ""
        )
        if retry_hint:
            system_prompt += (
                "\n\nPrevious attempt failed review. "
                f"Correction required: {retry_hint}"
            )

            # For known topics, give the model a concrete reference
            # solution in the target language.
            reference = None
            try:
                from agents.code_references import find_reference
                reference = find_reference(task.topic, language)
            except Exception:
                reference = None

            if reference:
                prompt = (
                    f"[CORRECTION REQUIRED] {retry_hint}\n\n"
                    f"Here is a known-correct reference solution in "
                    f"{language} for this problem. Adapt it if needed "
                    f"and return a working solution:\n\n"
                    f"```{lang_tag}\n{reference}\n```\n\n"
                    f"Original task: {task.resolved_request}\n\n"
                    f"Produce a corrected {language} solution that "
                    f"addresses the review feedback. Return only the "
                    f"code inside a fenced ```{lang_tag} block plus a "
                    f"one-line explanation."
                )
            else:
                prompt = (
                    f"[CORRECTION REQUIRED] {retry_hint}\n\n"
                    f"Original task: {task.resolved_request}\n\n"
                    f"Carefully fix the specific issue mentioned "
                    f"above and return a corrected solution."
                )
        else:
            prompt = task.resolved_request

        model = getattr(self._llm, "code_model", None)
        answer = self._llm.generate(
            prompt,
            system=system_prompt,
            temperature=0.1,
            max_tokens=700,
            model=model,
        )

        if not answer or not answer.strip():
            return self.fail(
                "Code model produced no output. The problem may be "
                "too vague or the model timed out. Try asking again "
                "with a specific problem name.",
                msg,
            )
        
        reject = self._reject_if_bad(answer)
        if reject:
            return self.fail(reject, msg)

        answer = self._ensure_fenced(answer, language)
        self._remember_last_problem(prompt, language)
        return self.respond(answer, msg)

    # ------------------------------------------------------------------
    @staticmethod
    def _lang_tag(language: str) -> str:
        l = language.lower()
        if "c++" in l:
            return "cpp"
        if "c#" in l:
            return "csharp"
        return l

    @staticmethod
    def _ensure_fenced(answer: str, language: str) -> str:
        if not answer or not answer.strip():
            return ""
        if "```" in answer:
            return answer
        lang_tag = CodeAgent._lang_tag(language)
        return f"```{lang_tag}\n{answer.strip()}\n```"

    def _expand_followup(self, text: str, msg: AgentMessage) -> str:
        low = text.strip().lower()
        followup_patterns = (
            "now in ", "in ", "and in ", "also in ",
            "do it in ", "write it in ", "convert to ",
            "what about ", "same in ",
        )
        if not any(low.startswith(p) or low == p.rstrip()
                   for p in followup_patterns) and len(low.split()) > 3:
            return text

        try:
            from core.context import get_session
            sess = get_session()
            with sess._lock:
                last = sess.extra.get("code_agent_last_problem")
                last_lang = sess.extra.get("code_agent_last_language")
        except Exception:
            last = None
            last_lang = None

        if not last:
            return text

        return (
            f"Write the code to solve the following problem, but in a "
            f"different language than before ({last_lang} was last):\n"
            f"Original problem: {last}\n"
            f"User follow-up: {text}"
        )

    @staticmethod
    def _remember_last_problem(problem_text: str, language: str) -> None:
        try:
            from core.context import get_session
            sess = get_session()
            with sess._lock:
                sess.extra["code_agent_last_problem"] = problem_text[:400]
                sess.extra["code_agent_last_language"] = language
        except Exception:
            pass

    @staticmethod
    def _detect_language(text: str, action: str) -> str:
        low = (text or "").lower()
        act = (action or "").lower()

        if "javascript" in low or " js " in low or " node " in low \
                or "nodejs" in low or "javascript" in act or "js" in act:
            return "JavaScript"
        if "typescript" in low or " ts " in low or "typescript" in act:
            return "TypeScript"
        if "c++" in low or "cpp" in act or "cplusplus" in low \
                or "c plus plus" in low:
            return "C++"
        if "c#" in low or "csharp" in act or "c sharp" in low \
                or "csharp" in low:
            return "C#"
        if re.search(r"\bjava\b", low) or (
            "java" in act and "javascript" not in low
        ):
            return "Java"
        if re.search(r"\bgolang\b", low) or re.search(r"\bgo\b", low):
            return "Go"
        if re.search(r"\brust\b", low):
            return "Rust"
        if re.search(r"\bin\s+c\b", low) or \
                re.search(r"\bc\s+code\b", low) or \
                low.endswith(" in c") or \
                low.endswith(" c code") or \
                "c language" in low:
            return "C"
        return "Python"

    @staticmethod
    def _reject_if_bad(answer: str) -> Optional[str]:
        garbage_markers = (
            "_excluded_", "__builtins__", "`__builtins__`",
            "divmod(__import__", "(-4*((i-",
            "SystemExit(\"\\nexception occurred",
            "__main__clueless",
        )
        if any(m in answer for m in garbage_markers):
            return ("Generated code failed sanity check. "
                    "Try a more specific prompt.")
        fenced_blocks = re.findall(
            r"```[^\n]*\n(.*?)```", answer, re.DOTALL)
        if "```" in answer and not fenced_blocks:
            return ("Generated code looks malformed. "
                    "Try a more specific prompt.")
        code_lines = (
            line for block in fenced_blocks for line in block.splitlines()
        ) if fenced_blocks else iter(answer.splitlines())
        if any(len(line) > 500 for line in code_lines):
            return ("Generated code looks malformed. "
                    "Try a more specific prompt.")
        for m in re.finditer(r"```(?:python|py)\n(.*?)```",
                             answer, re.DOTALL):
            block = m.group(1)
            try:
                ast.parse(block)
            except SyntaxError as e:
                return (f"Generated Python failed syntax check: "
                        f"line {e.lineno}, {e.msg}. "
                        f"Try a more specific prompt.")
        return None

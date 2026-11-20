"""
ReviewerAgent — the multi-stage quality gate.
=============================================
Stages: Language, Syntax, Type Safety, Compilation, Logic, Runtime,
Explanation, Complexity, Examples, Optimization.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ai.ollama import get_llm
from agents.base_agent import BaseAgent
from agents.code_validator import validate_code_answer
from agents.optimization_validator import validate_optimization
from core.message import AgentMessage
from core.task_context import TaskContext, Intent

logger = logging.getLogger(__name__)

SCHEMA_HINT = {
    "verdict": "PASS | FAIL | UNCERTAIN",
    "reason": "one sentence",
    "suggestion": "one sentence or empty string",
}


class ReviewerAgent(BaseAgent):
    name = "reviewer_agent"

    _TOOL_RESULT_AGENTS = {
        "system_agent", "file_agent", "vision_agent",
        "chess_agent", "research_agent", "memory_agent",
        "code_agent",
        "blender_agent",
    }

    def __init__(self) -> None:
        self._llm = get_llm()

    # ================================================================
    def review_for_task(self, task: TaskContext,
                        agent_result: str) -> Dict[str, Any]:
        if not agent_result or len(agent_result.strip()) < 3:
            return self._make_verdict(
                "FAIL", "Empty or trivial result.",
                language="SKIP", syntax="FAIL", type_safety="SKIP",
                compilation="SKIP", logic="SKIP", runtime="SKIP",
                explanation="SKIP", complexity="SKIP",
                examples="SKIP", optimization="SKIP",
            )

        if task.intent == Intent.CODE_EXPLAIN:
            stripped = agent_result.strip()
            if len(stripped) < 20:
                return self._make_verdict(
                    "FAIL", "Explanation too short.",
                    language="SKIP", syntax="SKIP", type_safety="SKIP",
                    compilation="SKIP", logic="SKIP", runtime="SKIP",
                    explanation="FAIL", complexity="SKIP",
                    examples="SKIP", optimization="SKIP",
                )
            return self._make_verdict(
                "PASS", "Explanation provided.",
                language="SKIP", syntax="SKIP", type_safety="SKIP",
                compilation="SKIP", logic="SKIP", runtime="SKIP",
                explanation="PASS", complexity="SKIP",
                examples="SKIP", optimization="SKIP",
            )

        if task.intent in (Intent.CODE_NEW, Intent.CODE_CONVERSION,
                            Intent.CODE_OPTIMIZE):
            return self._review_code_task(task, agent_result)

        # Tool-result fast path
        stripped = agent_result.strip()
        markers = ("📊", "📸", "🖼️", "📄", "📋", "🔍", "♟️", "🎲",
                   "✅", "❌", "💾", "📁", "🔎", "📝", "🗑️", "📭",
                   "ℹ️", "⚠️", "💡", "📚", "🏆", "ℹ")
        if any(m in stripped for m in markers):
            return self._make_verdict(
                "PASS", "Valid tool result.",
                language="SKIP", syntax="SKIP", type_safety="SKIP",
                compilation="SKIP", logic="SKIP", runtime="SKIP",
                explanation="SKIP", complexity="SKIP",
                examples="SKIP", optimization="SKIP",
            )

        lower = stripped.lower()
        refusal_starts = ("i can't ", "i cannot ", "i'm sorry",
                          "sorry,", "i am sorry", "unable to")
        if len(stripped) < 200 and not lower.startswith(refusal_starts):
            return self._make_verdict(
                "PASS", "Short confirmation.",
                language="SKIP", syntax="SKIP", type_safety="SKIP",
                compilation="SKIP", logic="SKIP", runtime="SKIP",
                explanation="SKIP", complexity="SKIP",
                examples="SKIP", optimization="SKIP",
            )

        if not self._llm.is_alive():
            return self._make_verdict(
                "UNCERTAIN", "LLM unavailable.",
                language="SKIP", syntax="SKIP", type_safety="SKIP",
                compilation="SKIP", logic="SKIP", runtime="SKIP",
                explanation="SKIP", complexity="SKIP",
                examples="SKIP", optimization="SKIP",
            )
        prompt = (
            "You are a strict reviewer. Decide if the AGENT RESULT "
            "plausibly satisfies the RESOLVED TASK.\n\n"
            f"TASK:\n{task.resolved_request}\n\n"
            f"INTENT: {task.intent.value}\n\n"
            f"AGENT RESULT:\n{agent_result[:1500]}\n\n"
            "Reply with JSON only."
        )
        parsed = self._llm.structured_output(
            prompt, schema_hint=SCHEMA_HINT,
            system="You are a strict, terse reviewer. Output only JSON.",
        )
        if not isinstance(parsed, dict):
            return self._make_verdict(
                "UNCERTAIN", "Could not parse verdict.",
                language="SKIP", syntax="SKIP", type_safety="SKIP",
                compilation="SKIP", logic="SKIP", runtime="SKIP",
                explanation="SKIP", complexity="SKIP",
                examples="SKIP", optimization="SKIP",
            )
        v = str(parsed.get("verdict", "UNCERTAIN")).upper()
        if v not in ("PASS", "FAIL", "UNCERTAIN"):
            v = "UNCERTAIN"
        return self._make_verdict(
            v, str(parsed.get("reason", ""))[:240],
            language="SKIP", syntax="SKIP", type_safety="SKIP",
            compilation="SKIP", logic="SKIP", runtime="SKIP",
            explanation="SKIP", complexity="SKIP",
            examples="SKIP", optimization="SKIP",
        )

    # ================================================================
    def _review_code_task(self, task: TaskContext,
                          agent_result: str) -> Dict[str, Any]:
        expected_lang = (
            task.target_language or task.language or "Python"
        )
        report = validate_code_answer(
            answer=agent_result,
            topic=task.topic,
            target_language=expected_lang,
        )

        stages = [
            report["language"], report["syntax"],
            report["type_safety"], report["compilation"],
            report["logic"], report["runtime"],
            report["explanation"], report["complexity"],
            report["examples"],
        ]
        if "FAIL" in stages:
            overall = "FAIL"
        elif all(s == "SKIP" for s in stages):
            overall = "UNCERTAIN"
        else:
            overall = "PASS"

        suggestion = ""
        if overall == "FAIL":
            suggestion = self._build_correction(report)

        # Optimization check
        opt_status = "SKIP"
        opt_report = None
        if task.intent == Intent.CODE_OPTIMIZE and task.previous_code:
            opt_report = validate_optimization(
                original_code=task.previous_code,
                new_code=agent_result,
                original_expl=task.previous_explanation or "",
                new_expl=agent_result,
            )
            opt_status = opt_report["verdict"]

            if opt_status == "NOT_NEEDED":
                # Treat NOT_NEEDED as a PASS — do not trigger retry.
                overall = "PASS"
                report["reason"] = opt_report["reason"]
            elif opt_status == "FAIL":
                overall = "FAIL"
                suggestion = (
                    "The proposed optimization does not actually "
                    "improve complexity. Either provide a genuine "
                    "asymptotic improvement, or state that the "
                    "current solution is already optimal."
                )
                report["reason"] = opt_report["reason"]

        return self._make_verdict(
            overall,
            report["reason"] or "Code looks valid.",
            language=report["language"],
            syntax=report["syntax"],
            type_safety=report["type_safety"],
            compilation=report["compilation"],
            logic=report["logic"],
            runtime=report["runtime"],
            explanation=report["explanation"],
            complexity=report["complexity"],
            examples=report["examples"],
            optimization=opt_status,
            optimization_report=opt_report,
            suggestion=suggestion,
        )

    @staticmethod
    def _build_correction(report: Dict[str, str]) -> str:
        parts = []
        if report.get("language") == "FAIL":
            parts.append("Use the requested language.")
        if report.get("syntax") == "FAIL":
            parts.append("Fix the syntax error.")
        if report.get("type_safety") == "FAIL":
            parts.append("Fix the type mismatch.")
        if report.get("compilation") == "FAIL":
            parts.append("Fix the compilation error.")
        if report.get("logic") == "FAIL":
            parts.append("The algorithm does not solve the problem.")
        if report.get("runtime") == "FAIL":
            parts.append("Fix the runtime issue.")
        if report.get("explanation") == "FAIL":
            parts.append("Align the explanation with the code.")
        if report.get("complexity") == "FAIL":
            parts.append("Correct the complexity claim.")
        if report.get("examples") == "FAIL":
            parts.append("Verify the example output.")
        if not parts:
            parts.append("Regenerate the answer.")
        return " ".join(parts)

    # ================================================================
    @staticmethod
    def _make_verdict(overall: str, reason: str,
                      language: str = "SKIP",
                      syntax: str = "SKIP",
                      type_safety: str = "SKIP",
                      compilation: str = "SKIP",
                      logic: str = "SKIP",
                      runtime: str = "SKIP",
                      explanation: str = "SKIP",
                      complexity: str = "SKIP",
                      examples: str = "SKIP",
                      optimization: str = "SKIP",
                      optimization_report: Optional[Dict] = None,
                      suggestion: str = "") -> Dict[str, Any]:
        return {
            "verdict": overall,
            "language": language,
            "syntax": syntax,
            "type_safety": type_safety,
            "compilation": compilation,
            "logic": logic,
            "runtime": runtime,
            "explanation": explanation,
            "complexity": complexity,
            "examples": examples,
            "optimization": optimization,
            "optimization_report": optimization_report,
            "reason": reason,
            "suggestion": suggestion,
        }

    # ================================================================
    def review(self, original_task_goal: str, agent_name: str,
               agent_result: str, success_flag: bool) -> Dict[str, Any]:
        if not success_flag:
            return self._make_verdict(
                "FAIL", f"{agent_name} returned an error.",
                suggestion="Replan or retry.",
            )
        if not agent_result or len(agent_result.strip()) < 3:
            return self._make_verdict("FAIL", "Empty or trivial.")
        if agent_name == "code_agent":
            report = validate_code_answer(
                answer=agent_result, topic=None, target_language=None,
            )
            stages = [
                report["language"], report["syntax"],
                report["type_safety"], report["compilation"],
                report["logic"], report["runtime"],
                report["explanation"], report["complexity"],
                report["examples"],
            ]
            if "FAIL" in stages:
                overall = "FAIL"
            elif all(s == "SKIP" for s in stages):
                overall = "UNCERTAIN"
            else:
                overall = "PASS"
            return self._make_verdict(
                overall, report["reason"] or "Code looks valid.",
                language=report["language"], syntax=report["syntax"],
                type_safety=report["type_safety"],
                compilation=report["compilation"],
                logic=report["logic"], runtime=report["runtime"],
                explanation=report["explanation"],
                complexity=report["complexity"],
                examples=report["examples"],
            )
        if agent_name in self._TOOL_RESULT_AGENTS:
            markers = ("📊", "📸", "🖼️", "📄", "📋", "🔍", "♟️", "🎲",
                       "✅", "❌", "💾", "📁", "🔎", "📝", "🗑️", "📭",
                       "ℹ️", "⚠️", "💡", "📚", "🏆", "ℹ")
            if any(m in agent_result for m in markers):
                return self._make_verdict(
                    "PASS", f"{agent_name} valid tool result.")
        if not self._llm.is_alive():
            return self._make_verdict("UNCERTAIN", "LLM unavailable.")
        prompt = (
            f"Task: {original_task_goal}\n"
            f"Agent: {agent_name}\n"
            f"Result: {agent_result[:1500]}\nReply with JSON only."
        )
        parsed = self._llm.structured_output(
            prompt, schema_hint=SCHEMA_HINT,
            system="You are a strict reviewer. Output only JSON.",
        )
        if not isinstance(parsed, dict):
            return self._make_verdict("UNCERTAIN", "Could not parse.")
        v = str(parsed.get("verdict", "UNCERTAIN")).upper()
        if v not in ("PASS", "FAIL", "UNCERTAIN"):
            v = "UNCERTAIN"
        return self._make_verdict(v, str(parsed.get("reason", ""))[:240])

    def handle(self, msg: AgentMessage) -> AgentMessage:
        goal = msg.context.get("original_task_goal", "")
        result = msg.context.get("agent_result", "")
        agent_name = msg.context.get("agent_name", "unknown")
        success = bool(msg.context.get("success_flag", True))
        verdict = self.review(goal, agent_name, result, success)
        return self.respond(verdict, msg)
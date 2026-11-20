"""
PlannerAgent — converts a natural-language request into a task graph.
====================================================================
Uses LocalLLM.structured_output() to obtain a JSON object of the form:

    {
      "tasks": [
        {
          "id": "t1",
          "agent": "file_agent",
          "action": "summarize_pdf",
          "description": "...",
          "dependencies": [],
          "priority": "NORMAL"
        },
        ...
      ],
      "reasoning_summary": "short plain-English summary (<= 200 chars)"
    }

Design rules:
  * CONVERSATIONAL SHORT-CIRCUIT: greetings, thanks, and small
    talk NEVER reach the LLM. plan() returns a synthetic
    direct-answer plan immediately. This is a regression fix —
    a greeting like "hello" used to be misrouted by the model to
    memory_agent/remember, which then failed.
  * If the model returns invalid JSON after retries, we try ONE more
    attempt with a minimal prompt. If that also fails, we return None.
  * HeadAgent is expected to catch None and use make_fallback_plan().
  * The planner may only route to agents in ALLOWED_AGENTS.
  * Hard cap: MAX_TASKS (currently 3). The LLM is told to prefer ONE.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ai.ollama import get_llm
from core.message import AgentMessage
from agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

# Hard cap on tasks per plan. The LLM is asked to prefer 1.
MAX_TASKS = 3

# Agents the planner may route to.
ALLOWED_AGENTS = {
    "chess_agent",
    "file_agent",
    "system_agent",
    "vision_agent",
    "coding_agent",
    "research_agent",
    "memory_agent",
    "code_agent",
    "blender_agent",
    "head_agent",
}

# JSON shape we ask for.
SCHEMA_HINT = {
    "tasks": [
        {
            "id": "string, short, e.g. t1",
            "agent": "one of: " + ", ".join(sorted(ALLOWED_AGENTS)),
            "action": "string, short verb-noun, e.g. summarize_pdf",
            "description": "string, one sentence",
            "dependencies": ["list of task ids that must complete first"],
            "priority": "LOW | NORMAL | HIGH",
        }
    ],
    "reasoning_summary": "string, one sentence, no chain-of-thought",
}


# ----------------------------------------------------------------------
# Routing hints — critical for correct agent selection.
# ----------------------------------------------------------------------
ROUTING_RULES = """
ROUTING RULES — read carefully:

  system_agent handles:
    * system status, CPU, RAM, memory usage, disk space, system information
    * battery status
    * volume mute / unmute / up / down
    * shutdown, restart, sleep, lock pc, cancel shutdown
    * empty recycle bin
    * open <folder> (downloads, documents, desktop, pictures, videos)
    * open <app> (chrome, notepad, calculator, settings, spotify, etc.)
    ⚠️ NOTE: "memory usage", "RAM usage", "how much memory" →
       system_agent with action="system_info". This is HARDWARE RAM,
       NOT long-term memory storage.

  memory_agent handles:
    * "remember my X is Y", "remember that X is Y", "remember X = Y"
      → action="remember"
    * "what is my X", "what's my X", "recall my X", "so what is my X"
      → action="recall"
    * "what do you know about me", "list my memories"
      → action="list_memories"
    * "forget X", "delete memory X"
      → action="forget"
    ⚠️ CRITICAL: "remember" and "recall" are SEPARATE operations.
       Do NOT combine them into one plan for a single user message.
       - If the user says "remember ...", use ONLY action="remember".
       - If the user asks "what is my ...", use ONLY action="recall".
       - Never emit BOTH remember and recall for one user request.
    ⚠️ Any request to store or recall PERSONAL FACTS about the user
       (their birthday, name, address, favorite color, pet's name,
       phone number, etc.) MUST route to memory_agent.
    ⚠️ DO NOT route "remember ..." or "what is my ..." requests to
       head_agent — head_agent may refuse on privacy grounds.

  file_agent handles:
    * PDF: summarize pdf, tell me about this pdf, search pdf for <x>
    * project analysis, class listing, "which file contains X"
    * file index: find <name>, index stats, rebuild index
    * open resume, open latest pdf

  chess_agent handles:
    * "play chess", "start chess", "new game", "start a game of chess"
      → action="start_game"
    * moves (e4, Nf3, etc.), castling (O-O, O-O-O)
    * coach, hint, best move, why, rate move, blunder check, opening,
      explain position, show threats, show board, resign
    * save chess, load chess, export pgn, import pgn
    * stop chess, quit chess, end chess
    ⚠️ NOTE: "play chess" or any variant MUST route to chess_agent
       with action="start_game". Do NOT route chess to other agents.

  vision_agent handles:
    * describe screen, analyze screen, what's on my screen
    * screenshot, screen capture
      → action="describe_screen"
    * describe image, analyze image, what's in this image,
      text in image, look at image
      → action="describe_image"
    ⚠️ NOTE: If the user mentions "image" (not "screen"), use
       describe_image even if no image is uploaded — the agent
       will fall back to screen analysis automatically.

  code_agent handles:
    * "write a two sum code in python"
    * "write a function to reverse a string"
    * "implement binary search"
    * "give me code for X"
    * "show me a python script that does Y"
    * "write a javascript function that Z"
      → action="generate_code"
    ⚠️ code_agent is for *writing new code*.
       For reading / searching / analyzing existing code, use coding_agent.

  blender_agent handles:
    * "create a cube", "add a sphere", "add a light", "add a camera"
    * "inspect scene", "list scene objects", "blender status"
    * "create a scene with ...", "build a room with ...", any request
      that composes multiple 3D objects or inspects the persistent scene
    ⚠️ Deterministic shortcuts (create cube, add light, inspect scene)
       are handled BEFORE the planner; only complex multi-object
       requests reach this agent via the planner.

  research_agent handles:
    * search web, search online, look up, find online, google <x>

  head_agent handles (direct LLM, no tools):
    * arithmetic, definitions, explanations, general conversation
    * creative writing, translations, summaries of provided text
    * anything that does not need a tool
    ⚠️ DO NOT use head_agent for "remember" or "recall" requests —
       those go to memory_agent.
    ⚠️ CASUAL CONVERSATION (greetings, thanks, small talk):
       If the user's message is ONLY a greeting or pleasantry with
       no actionable content — e.g. "hello", "hi", "hey",
       "good morning", "how are you", "thanks", "ok", "cool" —
       you MUST NOT create a memory task. You MUST NOT create ANY
       task that requires a tool. Greetings are not tasks.
       If the message is pure conversation, choose:
           {"tasks":[{"id":"t1","agent":"head_agent",
                      "action":"direct_answer",
                      "description":"<the user's message>",
                      "dependencies":[],"priority":"NORMAL"}],
            "reasoning_summary":"conversational"}
       Never infer that a greeting should be remembered, stored,
       or recorded. Memory tasks require an EXPLICIT request to
       remember / recall / store / forget.
"""


class PlannerAgent(BaseAgent):
    name = "planner_agent"

    def __init__(self) -> None:
        self._llm = get_llm()

    # ------------------------------------------------------------------
    def plan(self, user_request: str,
             context_hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Return a validated plan dict or None on failure.
        Never raises. Tries a full prompt, then a minimal prompt.
        """
        # ---- Conversational short-circuit (regression fix) ----
        # Greetings and small talk are answered directly. We never
        # ask the LLM to plan a task for pure conversation, because
        # small models reliably hallucinate a memory task. This runs
        # before any LLM call — cheap and deterministic.
        from core.context_resolver import is_conversational
        if is_conversational(user_request):
            return {
                "tasks": [{
                    "id": "t1",
                    "agent": "head_agent",
                    "action": "direct_answer",
                    "description": user_request[:200],
                    "dependencies": [],
                    "priority": "NORMAL",
                }],
                "reasoning_summary": "conversational",
            }

        # ---- Attempt 1: full prompt with routing rules ----
        prompt = self._build_prompt(user_request, context_hint)
        result = self._llm.structured_output(
            prompt,
            schema_hint=SCHEMA_HINT,
            system=(
                "You are a task planner for a local AI assistant. "
                "You output ONLY valid JSON matching the requested shape. "
                "You never include explanations, code fences, or prose. "
                "Keep reasoning_summary under 200 characters. "
                "Use the routing rules provided to select the correct agent. "
                "Prefer ONE task. Use multiple tasks only when necessary. "
                "Never plan a memory task for a greeting or small talk."
            ),
            max_retries=2,
        )
        plan = self._validate(result) if isinstance(result, dict) else None
        if plan is not None:
            return plan

        logger.warning("Planner: full prompt failed, trying minimal prompt")

        # ---- Attempt 2: minimal prompt, easy JSON ----
        minimal_prompt = (
            f"User request: {user_request}\n\n"
            "Return JSON only, in this exact shape:\n"
            '{"tasks":[{"id":"t1","agent":"<agent>","action":"<verb>",'
            '"description":"<one sentence>","dependencies":[],'
            '"priority":"NORMAL"}],"reasoning_summary":"<one sentence>"}\n\n'
            "Available agents: chess_agent, file_agent, system_agent, "
            "vision_agent, coding_agent, research_agent, memory_agent, "
            "code_agent, blender_agent, head_agent. "
            "Use head_agent for general questions and greetings."
        )
        result2 = self._llm.structured_output(
            minimal_prompt,
            max_retries=2,
        )
        plan = self._validate(result2) if isinstance(result2, dict) else None
        if plan is not None:
            return plan

        logger.error("Planner: both attempts failed for %r", user_request)
        return None

    # ------------------------------------------------------------------
    def handle(self, msg: AgentMessage) -> AgentMessage:
        text = msg.content if isinstance(msg.content, str) else str(
            msg.content.get("text", "")
        )
        plan = self.plan(text, context_hint=msg.context.get("context_hint"))
        if plan is None:
            return self.fail("Planner could not produce a valid plan.", msg)
        return self.respond(plan, msg)

    # ------------------------------------------------------------------
    @staticmethod
    def _build_prompt(user_request: str, context_hint: Optional[str]) -> str:
        hint = f"\nKnown context: {context_hint}\n" if context_hint else ""
        return (
            "User request:\n"
            f"{user_request}\n"
            f"{hint}\n"
            f"{ROUTING_RULES}\n"
            "\n"
            "RULES FOR PLANNING:\n"
            "  * Use the SMALLEST number of tasks that fully solves the "
            "request.\n"
            "  * Prefer ONE task. Only use multiple tasks if there are "
            "truly independent sub-goals (e.g. 'compare X and Y' might "
            "need two).\n"
            "  * Do NOT create a chain of tasks that all depend on the "
            "first one. That is a single task with a longer description.\n"
            "  * Hard maximum: 3 tasks.\n"
            "  * If a single task suffices, return exactly one.\n"
            "  * Greetings and small talk are NOT tasks. See the "
            "CASUAL CONVERSATION rule under head_agent.\n"
            "\n"
            "Examples:\n"
            "  'what is 25 * 4' → 1 task: head_agent / direct_answer\n"
            "  'hello' → 1 task: head_agent / direct_answer\n"
            "  'thanks' → 1 task: head_agent / direct_answer\n"
            "  'system status' → 1 task: system_agent / system_info\n"
            "  'describe the image' → 1 task: vision_agent / describe_image\n"
            "  'what texts do you see in the image' → 1 task: "
            "vision_agent / describe_image\n"
            "  'find my resume and open it' → 1 task: "
            "file_agent / find_resume\n"
            "  'play chess' → 1 task: chess_agent / start_game\n"
            "  'remember my birthday is June 5' → 1 task: "
            "memory_agent / remember\n"
            "  'what is my birthday' → 1 task: "
            "memory_agent / recall\n"
            "  'so what is my dob' → 1 task: memory_agent / recall\n"
            "  'what do you know about me' → 1 task: "
            "memory_agent / list_memories\n"
            "\n"
            "Return JSON only."
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _validate(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Enforce shape, allowlist agents, cap task count."""
        tasks = raw.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return None
        if len(tasks) > MAX_TASKS:
            tasks = tasks[:MAX_TASKS]

        clean: List[Dict[str, Any]] = []
        seen_ids = set()

        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                return None
            tid = str(t.get("id", f"t{i+1}")).strip() or f"t{i+1}"
            if tid in seen_ids:
                tid = f"{tid}_{i+1}"
            seen_ids.add(tid)

            agent = str(t.get("agent", "")).strip()
            if agent not in ALLOWED_AGENTS:
                logger.info("Planner: rejecting unknown agent %r", agent)
                return None

            deps = t.get("dependencies", [])
            if not isinstance(deps, list):
                deps = []

            clean.append({
                "id": tid,
                "agent": agent,
                "action": str(t.get("action", "handle"))[:64],
                "description": str(t.get("description", ""))[:300],
                "dependencies": [str(d) for d in deps],
                "priority": str(t.get("priority", "NORMAL")).upper(),
            })

        # ensure every dependency points to an existing task
        ids = {c["id"] for c in clean}
        for c in clean:
            c["dependencies"] = [d for d in c["dependencies"] if d in ids]

        return {
            "tasks": clean,
            "reasoning_summary": str(raw.get("reasoning_summary", ""))[:200],
        }


# ----------------------------------------------------------------------
# Fallback plan — used by HeadAgent when plan() returns None
# ----------------------------------------------------------------------
def make_fallback_plan(user_request: str) -> Dict[str, Any]:
    """
    When the LLM planner can't produce valid JSON, return a minimal
    one-task plan that routes to head_agent for a direct answer.
    Never fails — this is the safety net so HeadAgent always has a
    tasks list to iterate.
    """
    return {
        "tasks": [{
            "id": "t1",
            "agent": "head_agent",
            "action": "direct_answer",
            "description": user_request[:200],
            "dependencies": [],
            "priority": "NORMAL",
        }],
        "reasoning_summary": "Planner fell back to direct answer.",
    }


# ---- smoke test ------------------------------------------------------
if __name__ == "__main__":
    p = PlannerAgent()
    if not get_llm().is_alive():
        print("planner_agent: Ollama not running — skipping live test")
        raise SystemExit(0)
    for req in (
        "play chess",
        "show me my memory usage",
        "remember my birthday is June 5",
        "what is my birthday",
        "so what is my dob",
        "what do you know about me",
        "what is 25 * 4",
        "hello",
        "thanks",
    ):
        plan = p.plan(req)
        print(f"--- {req} ---")
        print(json.dumps(plan, indent=2)[:600] if plan else "None (will use fallback)")

    print()
    print("--- fallback test ---")
    print(json.dumps(make_fallback_plan("hello"), indent=2))
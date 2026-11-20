"""
Context resolver — turns (user_input, session) into a TaskContext.
==================================================================
The resolver runs BEFORE planning. It:
  1. Checks for known shortcut-table commands → SYSTEM intent.
  2. Applies deterministic rules to classify intent.
  3. Uses conversation context (last_task) for follow-up detection.
  4. Builds a TaskContext that fully describes what the user wants.

Deterministic rules cover:
  * "now in <lang>"                   → CODE_CONVERSION
  * "write <X> code in <lang>"        → CODE_NEW
  * "explain it/this/that"            → CODE_EXPLAIN
  * "optimize it/this"                → CODE_OPTIMIZE
  * "remember ..." / "save ..."       → MEMORY_SAVE
  * "what is my X" / "recall my X"    → MEMORY_RECALL
  * "search the web for ..."          → RESEARCH
  * "what is the latest ..."          → RESEARCH
  * "analyze this screenshot/image"   → VISION
  * blender vocabulary                → BLENDER

Also exposes is_conversational(), used by HeadAgent and the planner
to bypass planning for pure casual conversation (greetings, thanks,
acknowledgements). This is a regression fix: greetings must never
reach the planner, and must never produce a memory_agent task.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from core.task_context import TaskContext, Intent


# ─────────────────────────────────────────────────────────────
# Language normalization
# ─────────────────────────────────────────────────────────────
_LANGUAGE_PATTERNS = [
    # Order matters — C++ / C# / JS before C and Java
    ("JavaScript", r"\bjavascript\b|\bjs\b|\bnode\.?js\b|\bnode\b"),
    ("TypeScript", r"\btypescript\b"),
    ("C++",        r"\bc\+\+\b|\bcpp\b"),
    ("C#",         r"\bc#\b|\bcsharp\b|\bc\s*sharp\b"),
    ("Python",     r"\bpython\b|\bpy\b"),
    ("Java",       r"\bjava\b(?!script)"),
    ("Go",         r"\bgolang\b|\bgo\b"),
    ("Rust",       r"\brust\b"),
    ("C",          r"\bc\b(?!\+\+|#|ode)"),
]


def _detect_language(text: str) -> Optional[str]:
    """Return a canonical language name, or None."""
    low = text.lower()
    for canonical, pat in _LANGUAGE_PATTERNS:
        if re.search(pat, low):
            return canonical
    return None


# ─────────────────────────────────────────────────────────────
# Known fast-path commands — exact prefixes only.
# These correspond to entries in HeadAgent.SHORTCUT_TABLE.
# Do NOT add wildcards like "what is " or bare "open ".
# ─────────────────────────────────────────────────────────────
_FAST_PATH_PREFIXES = (
    # system status & diagnostics
    "system status", "check system", "cpu", "ram", "battery",
    # volume
    "mute", "unmute", "volume up", "volume down",
    "increase volume", "decrease volume",
    # power / lock
    "shutdown", "restart", "reboot", "sleep pc", "lock pc",
    "cancel shutdown", "power off",
    # recycle
    "recycle bin", "empty recycle bin", "empty trash",
    # open folder
    "open downloads", "open documents", "open desktop",
    "open pictures", "open videos",
    # open app — explicit list
    "open chrome", "open notepad", "open calculator", "open calc",
    "open settings", "open spotify", "open vscode", "open code",
    "open discord", "open steam", "open vlc", "open whatsapp",
    "open telegram", "open word", "open excel", "open powerpoint",
    "open outlook", "open firefox", "open edge", "open brave",
    "open paint", "open cmd", "open terminal", "open explorer",
    "open task manager", "open control panel", "open snipping tool",
    "open google", "open microsoft edge", "open command prompt",
    "open vs code", "open github desktop",
    # chess
    "play chess", "start chess", "stop chess", "quit chess",
    "coach", "hint", "best move", "why", "rate move",
    "blunder check", "opening", "explain position", "show threats",
    "show board", "resign", "save chess", "load chess", "export pgn",
    # vision
    "describe screen", "describe image", "screenshot",
    # file
    "open resume", "open latest pdf", "index stats", "rebuild index",
    "summarize pdf", "tell me about this pdf",
    # research
    "search web for", "look up", "google",
    # blender (Phase 4)
    "blender",
)


def _is_fast_path(low: str) -> bool:
    """
    True only for known shortcut-table commands.
    NEVER wildcards.
    """
    return any(low.startswith(p) for p in _FAST_PATH_PREFIXES)


# Intents that indicate a prior task produced code we can build on.
_PRIOR_CODE_INTENTS = (
    Intent.CODE_NEW,
    Intent.CODE_CONVERSION,
    Intent.CODE_EXPLAIN,
    Intent.CODE_OPTIMIZE,
)


# ─────────────────────────────────────────────────────────────
# Conversational-input detection (regression fix)
# ─────────────────────────────────────────────────────────────
#
# The planner must never be invoked for pure casual conversation.
# A greeting like "hello" is not a task; it is a request for a
# direct reply. This helper is deliberately conservative — it only
# returns True for short, unambiguous conversational inputs.
#
# IMPORTANT: anything that looks like an actionable request
# (contains a verb like "remember", "search", "open", etc.) is
# classified as NON-conversational so the normal router handles it.
_CONVERSATIONAL_EXACT = {
    "hello", "hi", "hey", "yo", "sup", "howdy",
    "good morning", "good afternoon", "good evening", "good night",
    "how are you", "how are you doing", "how's it going",
    "hows it going", "what's up", "whats up", "wassup",
    "thanks", "thank you", "thanks!", "thank you!",
    "thx", "ty", "cheers",
    "ok", "okay", "k", "cool", "nice", "great", "awesome",
    "got it", "understood", "alright", "all right",
    "bye", "goodbye", "see you", "see ya",
    "yes", "no", "yeah", "nope", "yep",
}

# Actionable markers — if ANY of these substrings appear, the
# input is NOT conversational.
_ACTIONABLE_MARKERS = (
    "remember", "recall", "save ", "forget",
    "search", "look up", "google", "find ",
    "open ", "launch", "start ", "run ",
    "shutdown", "restart", "reboot", "sleep ",
    "mute", "unmute", "volume",
    "play chess", "chess", "move ",
    "describe ", "screenshot", "analyze ",
    "summarize", "tell me about",
    "write ", "implement ", "code ", "program ",
    "convert ", "optimize ", "explain ",
    "blender", "create ", "add ", "make ",
    "remind", "schedule",
    "system status", "cpu", "ram", "battery",
    "index ",
    "what is my ", "what's my ", "whats my ",
)


def is_conversational(text: str) -> bool:
    """
    True if `text` is pure casual conversation with no actionable
    content. Used by HeadAgent to bypass the planner for greetings.

    Conservative:
      * If any actionable marker appears → False.
      * If the input is longer than 6 words and not an exact match
        to a known conversational phrase → False.
      * Otherwise True iff the normalized input is in the exact set
        or starts with a known conversational opener.
    """
    if not text or not isinstance(text, str):
        return False

    low = text.strip().lower()
    if not low:
        return False

    # Strip trailing punctuation for matching
    low_clean = re.sub(r"[.!?]+$", "", low).strip()

    # Actionable marker check — bail out immediately
    for marker in _ACTIONABLE_MARKERS:
        if marker in low_clean:
            return False

    # Exact match
    if low_clean in _CONVERSATIONAL_EXACT:
        return True

    # Starts-with checks for compound greetings / pleasantries
    starters = (
        "hello ", "hi ", "hey ",
        "good morning", "good afternoon", "good evening",
        "thanks ", "thank you ",
    )
    if any(low_clean.startswith(s) for s in starters):
        # Guard: reject if it's more than a short pleasantry
        if len(low_clean.split()) <= 6:
            return True

    return False


# ─────────────────────────────────────────────────────────────
# Intent classification
# ─────────────────────────────────────────────────────────────
def _classify(user_input: str, last_task: Optional[TaskContext]) -> Intent:
    """
    Deterministic intent classifier.
    Returns Intent.GENERAL when nothing matches.
    """
    low = user_input.strip().lower()
    low_norm = re.sub(r"\s+", " ", low).rstrip(".!?,;:")

    # ── Fast-path commands ──────────────────────────────────
    if _is_fast_path(low_norm):
        return Intent.SYSTEM

    # ── "now in X" / "same in X" / "convert to X" ───────────
    m_conv = re.match(
        r"^(?:now\s+|same\s+|also\s+|and\s+|ok\s+)?"
        r"(?:in|to|convert\s+to|do\s+it\s+in|write\s+it\s+in)\s+(.+)$",
        low_norm,
    )
    if m_conv:
        target_lang = _detect_language(m_conv.group(1))
        if target_lang and last_task is not None and \
           last_task.intent in _PRIOR_CODE_INTENTS:
            return Intent.CODE_CONVERSION

    # ── "explain it / this / that" ──────────────────────────
    if re.match(
        r"^explain\s+(it|this|that|the\s+(code|solution|algorithm|program))$",
        low_norm,
    ):
        if last_task is not None and last_task.intent in _PRIOR_CODE_INTENTS:
            return Intent.CODE_EXPLAIN

    # ── "optimize it / this / the code" ─────────────────────
    if re.match(
        r"^optimize\s+(it|this|that|the\s+(code|solution|algorithm|program))$",
        low_norm,
    ):
        if last_task is not None and last_task.intent in _PRIOR_CODE_INTENTS:
            return Intent.CODE_OPTIMIZE

    # ── "write <X> code in <lang>" ──────────────────────────
    if re.match(
        r"^(?:please\s+)?write\s+(?:me\s+)?(?:a\s+|an\s+|the\s+)?"
        r".+?\s+(?:code|program|script|function|solution|algorithm)"
        r"(?:\s+in\s+.+)?$",
        low_norm,
    ):
        return Intent.CODE_NEW

    if re.match(
        r"^(?:please\s+)?write\s+(?:me\s+)?(?:a\s+|an\s+|the\s+)?"
        r".+?\s+in\s+(?:python|java|javascript|typescript|c\+\+|c#|c|go|rust)$",
        low_norm,
    ):
        return Intent.CODE_NEW

    if low_norm.startswith(("write ", "implement ", "code ", "solve ",
                             "program ", "give me code ", "show me code ")):
        return Intent.CODE_NEW

    # ── VISION ─────────────────────────────────────────────
    if re.search(
        r"\b(screenshot|screen shot|this image|the image|"
        r"this picture|analyze (the )?(image|screenshot)|"
        r"what('s| is) in (this|the) (image|picture|photo))\b",
        low_norm,
    ):
        return Intent.VISION

    # ── MEMORY ─────────────────────────────────────────────
    if low_norm.startswith(("remember ", "save ", "don't forget ",
                             "dont forget ", "note that ", "keep in mind ")):
        return Intent.MEMORY_SAVE
    if low_norm.startswith(("what is my ", "what's my ", "whats my ",
                             "recall my ", "recall ")):
        return Intent.MEMORY_RECALL
    if re.search(
        r"\b(what did i (tell|say)|what do you (remember|know) about my)\b",
        low_norm,
    ):
        return Intent.MEMORY_RECALL

    # ── RESEARCH ───────────────────────────────────────────
    if low_norm.startswith(("search the web for ", "search online for ",
                             "look up ", "google ", "search for ")):
        return Intent.RESEARCH
    if re.search(
        r"\b(latest|current|newest|today|right now|as of now|"
        r"what happened|news about)\b",
        low_norm,
    ):
        return Intent.RESEARCH

    # ── BLENDER (Phase 4) ──────────────────────────────────
    # Only trigger on explicit Blender vocabulary so we don't steal
    # requests that belong to other agents.
    if re.search(
        r"\b(blender|bpy)\b"
        r"|\b(?:create|add|make)\s+(?:a\s+|an\s+|the\s+)?"
        r"(cube|sphere|plane|floor|light|camera)\b"
        r"|\b(?:create|make)\s+(?:a\s+|an\s+|the\s+)?"
        r"(?:simple\s+)?table\b"
        r"|\binspect\s+scene\b"
        r"|\bscene\s+objects\b",
        low_norm,
    ):
        return Intent.BLENDER

    return Intent.GENERAL


# ─────────────────────────────────────────────────────────────
# Topic extraction
# ─────────────────────────────────────────────────────────────
def _extract_topic(user_input: str) -> Optional[str]:
    low = user_input.strip().lower()
    low_norm = re.sub(r"\s+", " ", low)

    m = re.match(
        r"^(?:please\s+)?write\s+(?:me\s+)?(?:a\s+|an\s+|the\s+)?"
        r"(.+?)\s+(?:code|program|script|function|solution|algorithm)"
        r"(?:\s+in\s+.+)?$",
        low_norm,
    )
    if m:
        topic = m.group(1).strip()
        topic = re.sub(r"\s+in\s+\S+\s*$", "", topic).strip()
        return topic

    m2 = re.match(
        r"^(?:please\s+)?write\s+(?:me\s+)?(?:a\s+|an\s+|the\s+)?"
        r"(.+?)\s+in\s+\S+\s*$",
        low_norm,
    )
    if m2:
        return m2.group(1).strip()

    if low_norm.startswith(("implement ", "solve ", "code ", "program ")):
        return re.sub(
            r"^(implement|solve|code|program)\s+", "", low_norm
        ).strip()

    return None


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────
def resolve_request(user_input: str,
                    last_task: Optional[TaskContext] = None) -> TaskContext:
    """
    Turn (user_input, last_task) into a fully-formed TaskContext.
    Never raises.
    """
    task_id = "task_" + uuid.uuid4().hex[:10]
    intent = _classify(user_input, last_task)

    # ── CODE_NEW ────────────────────────────────────────────
    if intent == Intent.CODE_NEW:
        topic = _extract_topic(user_input)
        language = _detect_language(user_input) or "Python"
        if topic:
            resolved = (f"Write a {language} solution to the problem: "
                        f"{topic}.")
        else:
            resolved = f"Write a {language} solution to: {user_input}"
        return TaskContext(
            task_id=task_id,
            intent=intent,
            original_request=user_input,
            resolved_request=resolved,
            topic=topic,
            language=language,
        )

    # ── CODE_CONVERSION ─────────────────────────────────────
    if intent == Intent.CODE_CONVERSION and last_task is not None:
        target_lang = _detect_language(user_input) or "Python"
        if last_task.topic:
            topic = last_task.topic
        elif last_task.original_request:
            topic = last_task.original_request.strip()[:100]
        else:
            topic = "the previous problem"

        prev_lang = (last_task.language or last_task.target_language
                     or "unknown")

        prev_code = last_task.previous_code or ""
        if prev_code.strip():
            resolved = (
                f"Convert the previous code from {prev_lang} to "
                f"{target_lang}. The original problem was: {topic}\n\n"
                f"Here is the {prev_lang} code to convert:\n\n"
                f"```{prev_lang.lower()}\n{prev_code}\n```\n\n"
                f"Produce an equivalent {target_lang} solution."
            )
        else:
            resolved = (
                f"Write a {target_lang} solution to the following "
                f"problem: {topic}"
            )

        return TaskContext(
            task_id=task_id,
            intent=intent,
            original_request=user_input,
            resolved_request=resolved,
            topic=topic,
            language=target_lang,
            source_language=prev_lang,
            target_language=target_lang,
            parent_task_id=last_task.task_id,
            previous_code=last_task.previous_code,
        )

    # ── CODE_EXPLAIN ────────────────────────────────────────
    if intent == Intent.CODE_EXPLAIN and last_task is not None:
        if last_task.topic:
            topic = last_task.topic
        elif last_task.original_request:
            topic = last_task.original_request.strip()[:100]
        else:
            topic = "the current solution"
        lang = (last_task.language or last_task.target_language
                or "unknown")
        prev_code = last_task.previous_code or ""
        if prev_code.strip():
            resolved = (
                f"Explain the following {lang} solution in detail. "
                f"The original problem was: {topic}\n\n"
                f"```{lang.lower()}\n{prev_code}\n```"
            )
        else:
            resolved = (
                f"Explain a solution to the following problem in "
                f"{lang}: {topic}"
            )
        return TaskContext(
            task_id=task_id,
            intent=intent,
            original_request=user_input,
            resolved_request=resolved,
            topic=topic,
            language=lang,
            parent_task_id=last_task.task_id,
            previous_code=last_task.previous_code,
        )

    if intent == Intent.CODE_OPTIMIZE and last_task is not None:
        if last_task.topic:
            topic = last_task.topic
        elif last_task.original_request:
            topic = last_task.original_request.strip()[:100]
        else:
            topic = "the current solution"
        lang = (last_task.language or last_task.target_language
                or "unknown")
        prev_code = last_task.previous_code or ""
        if prev_code.strip():
            resolved = (
                f"Optimize the following {lang} solution. Keep the "
                f"language the same. The original problem was: "
                f"{topic}\n\n"
                f"```{lang.lower()}\n{prev_code}\n```"
            )
        else:
            resolved = (
                f"Optimize a {lang} solution for: {topic}"
            )
        return TaskContext(
            task_id=task_id,
            intent=intent,
            original_request=user_input,
            resolved_request=resolved,
            topic=topic,
            language=lang,
            parent_task_id=last_task.task_id,
            previous_code=last_task.previous_code,
        )

    # ── Fallback (VISION, MEMORY_*, RESEARCH, SYSTEM, GENERAL) ──
    return TaskContext(
        task_id=task_id,
        intent=intent,
        original_request=user_input,
        resolved_request=user_input,
        language=_detect_language(user_input),
    )
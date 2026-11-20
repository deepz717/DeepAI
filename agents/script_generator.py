"""
ScriptGenerator — LLM → Blender Python source.
================================================
Uses the existing LocalLLM (qwen3:8b). Never spawns its own client.
Never executes generated code.

Flow:
    user_text + scene_summary
        → generate_script(...)
            → LLM (with hardened system prompt)
            → strip code fences
            → validate_script(...)
            → if valid: return (True, source, "")
            → if invalid: retry with validator reason in prompt
            → after BLENDER_SCRIPT_MAX_RETRIES: return (False, "", reason)

Public API:
    generate_script(user_text, scene_summary="", *, llm=None)
        -> (ok, source_or_empty, reason, raw)
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

import config
from agents.blender.script_validator import validate_script

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are DeepAI's Blender Python generator.

You produce ONLY Python source code that runs inside Blender's `bpy`
environment. You never produce prose, markdown, comments outside the
code, shell commands, system commands, network calls, or filesystem
access.

HARD RULES — violations cause the script to be rejected:

  1. Only these imports are permitted:
        import bpy
        import mathutils
        import math
     No other imports. Never `from X import Y`. Never `import ... as`.

  2. Never use: os, sys, subprocess, shutil, socket, requests,
     urllib, pathlib, ctypes, pickle, importlib, or any other
     standard-library or third-party module.

  3. Never use: __import__, eval, exec, compile, open, input,
     globals, locals, vars, getattr, setattr, delattr, breakpoint.

  4. Never access dunder attributes: __class__, __bases__,
     __subclasses__, __globals__, __code__, __dict__, and similar.

  5. Never call `bpy.ops.wm.quit_blender` or any function that
     terminates Blender.

  6. The scene is loaded automatically. Do not call
     `bpy.ops.wm.read_factory_settings(...)` unless the user
     explicitly asked to reset the scene.

  7. Preserve existing objects. Only modify or delete objects when
     the user's request requires it.

  8. Perform ONLY the requested scene operation. Do not add extra
     props, lights, cameras, materials, or objects the user did not
     ask for.

  9. Location and dimension values must be plain numeric literals or
     values built from `math` / `mathutils`. Never read values from
     external sources.

  10. Output only the code, no markdown fences, no explanatory text.

Allowed API surface examples (not exhaustive):
    bpy.ops.mesh.primitive_cube_add(...)
    bpy.ops.mesh.primitive_uv_sphere_add(...)
    bpy.ops.mesh.primitive_plane_add(...)
    bpy.ops.mesh.primitive_cylinder_add(...)
    bpy.ops.mesh.primitive_cone_add(...)
    bpy.ops.mesh.primitive_torus_add(...)
    bpy.ops.object.select_all(action='DESELECT')
    bpy.ops.object.delete()
    bpy.ops.transform.translate(...)
    bpy.ops.transform.rotate(...)
    bpy.ops.transform.resize(...)
    bpy.data.objects[name]
    obj.location, obj.rotation_euler, obj.scale
    bpy.context.scene.objects
    mathutils.Vector(...), math.radians(...)
"""


def _strip_fences(text: str) -> str:
    """
    Remove ```python ... ``` or ``` ... ``` fences.
    Leaves source otherwise unchanged.
    """
    if not text:
        return ""
    text = text.strip()
    m = re.match(
        r"^```(?:python|py)?\s*\n(.*?)\n```\s*$",
        text, re.DOTALL | re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    # Fallback: strip a leading fence line and a trailing fence line
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _build_user_prompt(
    user_text: str,
    scene_summary: str,
    previous_error: str = "",
) -> str:
    parts = [f"User request: {user_text.strip()}"]
    if scene_summary:
        parts.append(
            "Current scene (do not reset unless asked):\n"
            f"{scene_summary.strip()[:1200]}"
        )
    if previous_error:
        parts.append(
            "Previous attempt was REJECTED by the AST validator. "
            f"Reason: {previous_error}\n"
            "Produce a corrected script that avoids this issue."
        )
    parts.append(
        "Return ONLY the Blender Python code. No markdown fences, "
        "no prose."
    )
    return "\n\n".join(parts)


def generate_script(
    user_text: str,
    scene_summary: str = "",
    *,
    llm=None,
) -> Tuple[bool, str, str, str]:
    """
    Return (ok, source, reason, raw_model_output).

    `llm` is injectable for tests. When None, the shared LocalLLM is
    used with its default text_model (qwen3:8b).
    """
    if not getattr(config, "BLENDER_SCRIPT_ENABLED", True):
        return False, "", (
            "Phase 6 script generation is disabled "
            "(BLENDER_SCRIPT_ENABLED=false)"
        ), ""

    if llm is None:
        from ai.ollama import get_llm
        llm = get_llm()

    max_retries = max(0, int(getattr(
        config, "BLENDER_SCRIPT_MAX_RETRIES", 2
    )))
    temperature = float(getattr(
        config, "BLENDER_SCRIPT_TEMPERATURE", 0.1
    ))
    max_tokens = int(getattr(
        config, "BLENDER_SCRIPT_MAX_TOKENS", 900
    ))

    last_reason = ""
    last_raw = ""
    previous_error = ""

    attempts = max_retries + 1
    for attempt in range(1, attempts + 1):
        prompt = _build_user_prompt(
            user_text, scene_summary, previous_error,
        )
        raw = ""
        try:
            raw = llm.generate(
                prompt,
                system=SYSTEM_PROMPT,
                temperature=temperature,
                max_tokens=max_tokens,
                allow_thinking=False,
            )
        except Exception as e:
            last_reason = f"LLM call failed: {e}"
            logger.warning(
                "script_generator: LLM call failed on attempt %d: %s",
                attempt, e,
            )
            continue

        last_raw = raw or ""
        source = _strip_fences(last_raw)

        if not source:
            last_reason = "model returned no code"
            previous_error = last_reason
            continue

        ok, reason = validate_script(source)
        if ok:
            return True, source, "", last_raw

        last_reason = f"validator: {reason}"
        previous_error = reason
        logger.warning(
            "script_generator: attempt %d rejected — %s",
            attempt, reason,
        )

    return False, "", last_reason or "generation failed", last_raw
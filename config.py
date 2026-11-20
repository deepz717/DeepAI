"""
DeepAI Assistant — Centralized Configuration
=============================================
Bottom of the import graph. Nothing here imports from ai/, tools/,
core/, agents/, or app.py.

Import order contract (do not violate):
    config  ->  ai  ->  tools  ->  core  ->  agents  ->  app
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil

# ─────────────────────────────────────────────────────────────
# Base directory (the folder containing this file)
# ─────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent

# Read the project's optional .env before evaluating configuration values.
# Process environment variables always win, and no integration is enabled by
# the loader itself. Invalid entries are retained as diagnostics so a caller
# can fail clearly instead of silently changing a feature flag.
CONFIG_ERRORS = []


def _load_project_dotenv(path=None):
    env_path = Path(path) if path is not None else BASE_DIR / ".env"
    if not env_path.is_file():
        return ()
    loaded = []
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        CONFIG_ERRORS.append("could not read %s: %s" % (env_path, exc))
        return ()
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            CONFIG_ERRORS.append("invalid .env entry at %s:%d" % (env_path, line_number))
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '\"'):
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return tuple(loaded)


DOTENV_LOADED_KEYS = _load_project_dotenv()


def _discover_blender_executable():
    """Resolve Blender without assuming a vendor installation directory."""
    configured = os.environ.get("BLENDER_EXECUTABLE")
    if configured is not None:
        return os.path.expandvars(os.path.expanduser(configured.strip()))
    for name in ("blender", "blender.exe"):
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve())
    if os.name == "nt":
        try:
            import winreg
            views = (0, getattr(winreg, "KEY_WOW64_64KEY", 0),
                     getattr(winreg, "KEY_WOW64_32KEY", 0))
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for view in dict.fromkeys(views):
                    flags = winreg.KEY_READ | view
                    for key_name in (
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\blender.exe",
                        r"SOFTWARE\BlenderFoundation\Blender",
                    ):
                        try:
                            with winreg.OpenKey(hive, key_name, 0, flags) as key:
                                if key_name.endswith("blender.exe"):
                                    values = [winreg.QueryValue(key, None)]
                                else:
                                    values = []
                                    count = winreg.QueryInfoKey(key)[0]
                                    for index in range(count):
                                        try:
                                            sub_name = winreg.EnumKey(key, index)
                                            with winreg.OpenKey(key, sub_name, 0, flags) as sub:
                                                for value_name in ("InstallDir", "Path"):
                                                    try:
                                                        values.append(winreg.QueryValueEx(sub, value_name)[0])
                                                    except OSError:
                                                        pass
                                        except OSError:
                                            continue
                            for value in values:
                                candidate = Path(os.path.expandvars(str(value))).expanduser()
                                if candidate.is_dir():
                                    candidate /= "blender.exe"
                                if candidate.is_file():
                                    return str(candidate.resolve())
                        except OSError:
                            continue
        except (ImportError, OSError):
            pass
        # The official Windows installer commonly uses a versioned child
        # folder but may not register an App Paths key. Search installation
        # roots reported by Windows rather than assuming a Blender version
        # or a fixed Program Files string.
        roots = []
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(env_name)
            if root:
                roots.append(Path(root) / "Blender Foundation")
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            roots.append(Path(local_app_data) / "Programs" / "Blender Foundation")
        for root in roots:
            try:
                candidates = list(root.glob("Blender */blender.exe"))
                candidates += list(root.glob("*/blender.exe"))
                candidates += [root / "blender.exe"]
                candidates = sorted({item.resolve() for item in candidates
                                     if item.is_file()}, reverse=True)
                if candidates:
                    return str(candidates[0])
            except OSError:
                continue
    return ""


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    lowered = value.strip().casefold()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    CONFIG_ERRORS.append("%s must be true/false, 1/0, yes/no, or on/off" % name)
    return bool(default)


def _env_int(name, default, minimum=None, maximum=None):
    value = os.environ.get(name)
    if value is None:
        return int(default)
    try:
        result = int(value.strip())
        if minimum is not None and result < minimum:
            raise ValueError
        if maximum is not None and result > maximum:
            raise ValueError
        return result
    except (TypeError, ValueError):
        CONFIG_ERRORS.append("%s has invalid integer value %r" % (name, value))
        return int(default)


def _env_float(name, default, minimum=None):
    value = os.environ.get(name)
    if value is None:
        return float(default)
    try:
        result = float(value.strip())
        if not (result == result and abs(result) != float("inf")):
            raise ValueError
        if minimum is not None and result < minimum:
            raise ValueError
        return result
    except (TypeError, ValueError):
        CONFIG_ERRORS.append("%s has invalid numeric value %r" % (name, value))
        return float(default)

# ─────────────────────────────────────────────────────────────
# Ollama / Models
# ─────────────────────────────────────────────────────────────
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
TEXT_MODEL = os.environ.get("DEEPAI_TEXT_MODEL", "qwen3:8b")
CHAT_MODEL = os.environ.get("DEEPAI_CHAT_MODEL", "phi3:mini")
VISION_MODEL = os.environ.get("DEEPAI_VISION_MODEL", "llava:latest")
CODE_MODEL = os.environ.get("DEEPAI_CODE_MODEL", "qwen2.5-coder:1.5b")
OLLAMA_KEEPALIVE = "1m"

REQUEST_TIMEOUT = int(os.environ.get("DEEPAI_REQUEST_TIMEOUT", "180"))
VISION_TIMEOUT = int(os.environ.get("DEEPAI_VISION_TIMEOUT", "180"))
OLLAMA_PING_TIMEOUT = int(os.environ.get("DEEPAI_PING_TIMEOUT", "5"))

# File indexing can read file names and metadata across several user folders.
# Keep automatic scanning opt-in; manual `rebuild index` remains available.
AUTO_INDEX_FILES = _env_bool("DEEPAI_AUTO_INDEX_FILES", False)

# Health-check cache TTL (seconds). See ai/ollama.py: is_alive().
OLLAMA_ALIVE_TTL = 30

# Structured-output behaviour (JSON validation, not schema validation)
STRUCTURED_MAX_RETRIES = 3
STRUCTURED_TEMPERATURE = 0.0

# ─────────────────────────────────────────────────────────────
# Database files — MUST remain in the project root.
# DO NOT move these to data/ without a dedicated migration plan.
# The existing DBs at ./deepai_data.db and ./file_index.db are the
# current source of truth.
# ─────────────────────────────────────────────────────────────
DB_FILE = os.environ.get("DEEPAI_DB_FILE", "./deepai_data.db")
FILE_INDEX_DB = os.environ.get("DEEPAI_FILE_INDEX_DB", "./file_index.db")

# ─────────────────────────────────────────────────────────────
# Existing asset directories — keep as relative paths (unchanged
# from the original app) so current behavior is preserved.
# ─────────────────────────────────────────────────────────────
SCREENSHOTS_DIR = os.environ.get("DEEPAI_SCREENSHOTS_DIR", "screenshots")
UPLOADS_DIR = os.environ.get("DEEPAI_UPLOADS_DIR", "uploads")
CHESS_SAVE_DIR = os.environ.get("DEEPAI_CHESS_SAVE_DIR", "chess_saves")

# Other JSON/text files (unchanged)
CHAT_HISTORY = "chat_history.txt"
MEMORY_FILE = "deepai_memory.json"
CONVERSATION_FILE = "conversation_history.json"
APP_CACHE_FILE = "deepai_app_cache.json"
ALIASES_FILE = "deepai_aliases.json"

# ─────────────────────────────────────────────────────────────
# Agent architecture
# ─────────────────────────────────────────────────────────────
USE_AGENTIC = True
USE_AGENTIC_SERVICES = True
MAX_AGENT_STEPS = 10
MAX_RETRIES = 3
AGENT_TIMEOUT = 180
MAX_CONVERSATION_TURNS = 100

# ─────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────
WINDOW_WIDTH = 1400
WINDOW_HEIGHT = 800

# ─────────────────────────────────────────────────────────────
# Voice / Wake word
# ─────────────────────────────────────────────────────────────
WAKE_WORD = "hey deepai"
WAKE_WORD_ENABLED = True

# ─────────────────────────────────────────────────────────────
# Blender integration (Phase 1)
# ─────────────────────────────────────────────────────────────
BLENDER_ENABLED = _env_bool("BLENDER_ENABLED", False)
BLENDER_EXECUTABLE_EXPLICIT = "BLENDER_EXECUTABLE" in os.environ
BLENDER_EXECUTABLE = _discover_blender_executable()
BLENDER_WORKSPACE = os.environ.get("BLENDER_WORKSPACE", "./workspace/blender")
BLENDER_TIMEOUT = int(os.environ.get("BLENDER_TIMEOUT", "120"))
BLENDER_MAX_RETRIES = int(os.environ.get("BLENDER_MAX_RETRIES", "3"))
BLENDER_AGENT_MODEL = os.environ.get("BLENDER_AGENT_MODEL", CODE_MODEL)

# ─────────────────────────────────────────────────────────────
# Blender Task Planner (Phase 5)
# ─────────────────────────────────────────────────────────────
BLENDER_PLANNER_ENABLED = (
    os.environ.get("BLENDER_PLANNER_ENABLED", "true").lower() == "true"
)
BLENDER_PLANNER_MAX_RETRIES = int(os.environ.get("BLENDER_PLANNER_MAX_RETRIES", "2"))
BLENDER_PLANNER_MAX_ACTIONS = int(os.environ.get("BLENDER_PLANNER_MAX_ACTIONS", "40"))
BLENDER_PLANNER_TEMPERATURE = float(
    os.environ.get("BLENDER_PLANNER_TEMPERATURE", "0.1")
)
BLENDER_PLANNER_MAX_TOKENS = int(os.environ.get("BLENDER_PLANNER_MAX_TOKENS", "900"))

# ─────────────────────────────────────────────────────────────
# Blender Phase 6 — Safe LLM-generated Blender Python
# ─────────────────────────────────────────────────────────────
BLENDER_SCRIPT_ENABLED = (
    os.environ.get("BLENDER_SCRIPT_ENABLED", "true").lower() == "true"
)
BLENDER_SCRIPT_MAX_RETRIES = int(os.environ.get("BLENDER_SCRIPT_MAX_RETRIES", "2"))
BLENDER_SCRIPT_TIMEOUT = int(os.environ.get("BLENDER_SCRIPT_TIMEOUT", "120"))
BLENDER_SCRIPT_MAX_BYTES = int(os.environ.get("BLENDER_SCRIPT_MAX_BYTES", "12000"))
BLENDER_SCRIPT_MAX_AST_NODES = int(
    os.environ.get("BLENDER_SCRIPT_MAX_AST_NODES", "1500")
)
BLENDER_SCRIPT_TEMPERATURE = float(os.environ.get("BLENDER_SCRIPT_TEMPERATURE", "0.1"))
BLENDER_SCRIPT_MAX_TOKENS = int(os.environ.get("BLENDER_SCRIPT_MAX_TOKENS", "900"))

# ─────────────────────────────────────────────────────────────
# Blender Phase 7 — Scene Intelligence
# ─────────────────────────────────────────────────────────────
BLENDER_SCENE_MAX_OBJECTS = int(os.environ.get("BLENDER_SCENE_MAX_OBJECTS", "200"))
BLENDER_SCENE_MAX_MATERIALS = int(os.environ.get("BLENDER_SCENE_MAX_MATERIALS", "100"))
BLENDER_SCENE_MAX_COLLECTIONS = int(
    os.environ.get("BLENDER_SCENE_MAX_COLLECTIONS", "50")
)
BLENDER_SCENE_MAX_LIGHTS = int(os.environ.get("BLENDER_SCENE_MAX_LIGHTS", "50"))
BLENDER_SCENE_MAX_CAMERAS = int(os.environ.get("BLENDER_SCENE_MAX_CAMERAS", "20"))
BLENDER_SCENE_MAX_STRING_LEN = int(
    os.environ.get("BLENDER_SCENE_MAX_STRING_LEN", "120")
)
BLENDER_SCENE_MAX_CONTEXT_BYTES = int(
    os.environ.get("BLENDER_SCENE_MAX_CONTEXT_BYTES", "16000")
)
BLENDER_SCENE_MAX_OUTPUT_BYTES = int(
    os.environ.get("BLENDER_SCENE_MAX_OUTPUT_BYTES", str(1_000_000))
)
BLENDER_PROMPT_MAX_MATERIALS = int(
    os.environ.get("BLENDER_PROMPT_MAX_MATERIALS", "24")
)
BLENDER_FOLLOWUP_ANSWER_ENABLED = (
    os.environ.get("BLENDER_FOLLOWUP_ANSWER_ENABLED", "true").lower()
    == "true"
)
BLENDER_ANIMATION_MAX_FRAME = int(
    os.environ.get("BLENDER_ANIMATION_MAX_FRAME", "100000")
)
BLENDER_ANIMATION_DEFAULT_FPS = int(
    os.environ.get("BLENDER_ANIMATION_DEFAULT_FPS", "24")
)
# Phase 9.2 - defensive per-property keyframe point cap. Enforced
# by h_insert_keyframe: an insert that would push an object's
# action past this many points on the requested property is
# rejected with KEYFRAME_LIMIT_EXCEEDED.
BLENDER_KEYFRAME_MAX_POINTS = int(
    os.environ.get("BLENDER_KEYFRAME_MAX_POINTS", "100000")
)

# Phase 9.3 - animation sequencing and render workload limits.
BLENDER_ANIMATION_MAX_SEQUENCE_PAIRS = int(
    os.environ.get("BLENDER_ANIMATION_MAX_SEQUENCE_PAIRS", "16")
)
BLENDER_ANIMATION_MAX_RENDER_FRAMES = int(
    os.environ.get("BLENDER_ANIMATION_MAX_RENDER_FRAMES", "100")
)
BLENDER_RENDER_OUTPUT_ROOT = os.environ.get(
    "BLENDER_RENDER_OUTPUT_ROOT",
    os.path.join("workspace", "blender", "renders"),
)
# Single authoritative per-action timeout for render_animation.
# Used by both the bridge server (server.py) and the client
# (agents/blender_agent.py) so the two sides cannot disagree.
BLENDER_RENDER_ACTION_TIMEOUT_S = float(
    os.environ.get("BLENDER_RENDER_ACTION_TIMEOUT_S", "900.0")
)

# ─────────────────────────────────────────────────────────────
# Direct-answer path (Qwen3 over-generation regression)
# ─────────────────────────────────────────────────────────────
DIRECT_ANSWER_MAX_TOKENS = int(os.environ.get("DEEPAI_DIRECT_MAX_TOKENS", "220"))
DIRECT_ANSWER_TEMPERATURE = float(os.environ.get("DEEPAI_DIRECT_TEMPERATURE", "0.4"))
# ─────────────────────────────────────────────────────────────
# Blender Phase 8 — Materials & Lighting
# ─────────────────────────────────────────────────────────────
BLENDER_MATERIAL_MAX = int(os.environ.get("BLENDER_MATERIAL_MAX", "100"))
BLENDER_MATERIAL_USERS_MAX = int(os.environ.get("BLENDER_MATERIAL_USERS_MAX", "20"))
BLENDER_LIGHT_MAX = int(os.environ.get("BLENDER_LIGHT_MAX", "50"))
BLENDER_LIGHT_ENERGY_MAX = float(
    os.environ.get("BLENDER_LIGHT_ENERGY_MAX", "1_000_000.0")
)
BLENDER_LIGHT_ENERGY_MIN = float(os.environ.get("BLENDER_LIGHT_ENERGY_MIN", "0.0"))
BLENDER_LIGHT_COLOR_MAX = float(os.environ.get("BLENDER_LIGHT_COLOR_MAX", "10.0"))
BLENDER_MATERIAL_VALUE_MIN = 0.0
BLENDER_MATERIAL_VALUE_MAX = 1.0
BLENDER_COLOR_COMPONENT_MAX = float(
    os.environ.get("BLENDER_COLOR_COMPONENT_MAX", "10.0")
)
# ─────────────────────────────────────────────────────────────
# Blender Live Bridge (Stage 2)
# ─────────────────────────────────────────────────────────────
BLENDER_BRIDGE_ENABLED = _env_bool("BLENDER_BRIDGE_ENABLED", True)
BLENDER_BRIDGE_HOST = os.environ.get("BLENDER_BRIDGE_HOST", "127.0.0.1")
BLENDER_BRIDGE_PORT = _env_int("BLENDER_BRIDGE_PORT", 8766, 1, 65535)
if BLENDER_BRIDGE_PORT == 8765:
    raise RuntimeError("Production bridge is forbidden in the staged project; use isolated port 8766")
BLENDER_BRIDGE_TIMEOUT = _env_float("BLENDER_BRIDGE_TIMEOUT", 15.0, 0.1)
BLENDER_BRIDGE_MAX_BYTES = int(
    os.environ.get("BLENDER_BRIDGE_MAX_BYTES", str(64 * 1024))
)
BLENDER_BRIDGE_PROTOCOL_VERSION = 1
BLENDER_BRIDGE_HEALTH_TTL = _env_float("BLENDER_BRIDGE_HEALTH_TTL", 15.0, 0.0)

# ─────────────────────────────────────────────────────────────
# Blender auto-launch (DeepX Connect button)
# ─────────────────────────────────────────────────────────────
BLENDER_AUTOSTART_TIMEOUT_S = _env_float("BLENDER_AUTOSTART_TIMEOUT_S", 60.0, 0.0)
BLENDER_AUTOSTART_POLL_MS = _env_int("BLENDER_AUTOSTART_POLL_MS", 500, 1, 60000)
# ─────────────────────────────────────────────────────────────
# Identity
# ─────────────────────────────────────────────────────────────
PROJECT_NAME = "DeepAI Assistant"
VERSION = "v23.1-agentic-phase7"


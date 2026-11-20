"""
SystemAgent — wraps SystemControl + SystemMonitor with permission gating.
=========================================================================
Permission gate:
  * READ_ONLY        — battery status, system status
  * SAFE             — open folder, open app, mute/unmute volume
  * CONFIRM_REQUIRED — shutdown, restart, sleep, empty recycle bin,
                        run_command

Accepts BOTH raw text and structured action. Distinguishes
  open_folder:<x>
  open_app:<x>
  run_command:<shell>
so they don't collide.

App launching delegates to SystemControl.open_folder, which:
  1. Tries os.startfile for folders, protocols, and known apps.
  2. Falls back to a properly-detached subprocess for unknown names.

All subprocess launches use CREATE_NO_WINDOW | DETACHED_PROCESS with
stdin/stdout/stderr redirected to DEVNULL, so a missing app can never
hang the GUI.
"""
from __future__ import annotations

import subprocess
from agents.base_agent import BaseAgent
from core.message import AgentMessage, MessageType


# Confirmation-required actions (canonical verbs).
_CONFIRM_REQUIRED = {
    "shutdown pc":       "This will shut down your PC in 5 seconds.",
    "restart pc":        "This will restart your PC in 5 seconds.",
    "sleep pc":          "This will put your PC to sleep.",
    "empty recycle bin": "This will permanently empty the recycle bin.",
}


def _detached_flags() -> int:
    """Return creation flags that detach the child process on Windows."""
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS
    return flags


def _safe_popen(cmdline: str) -> None:
    """
    Launch a shell command detached, with no console, no inherited pipes.
    Never blocks the caller.
    """
    subprocess.Popen(
        cmdline,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_detached_flags(),
        close_fds=True,
    )


class SystemAgent(BaseAgent):
    name = "system_agent"

    def __init__(self) -> None:
        self._mod = None

    # ------------------------------------------------------------------
    def _app(self):
        from core.agent_manager import get_app_module
        return get_app_module()

    # ------------------------------------------------------------------
    def handle(self, msg: AgentMessage) -> AgentMessage:
        raw = msg.context.get("raw_text")
        if raw:
            text = raw
        else:
            text = msg.content if isinstance(msg.content, str) else str(
                msg.content.get("text", "")
            )
        cmd = text.strip().lower()
        action = (msg.context.get("action") or "").strip().lower()
        confirmed = bool(msg.context.get("confirmed", False))

        verb = self._canonical(cmd, action)

        self.publish_status(
            f"system: {verb or action or cmd or 'unknown'}", msg
        )

        try:
            app = self._app()
        except Exception as e:
            return self.fail(str(e), msg)

        # ---- run_command:<shell string> ----
        if verb.startswith("run_command:"):
            cmdline = verb.split(":", 1)[1].strip()
            if not confirmed:
                return self.need_confirmation(
                    f"⚠️ This will run the shell command:\n"
                    f"    {cmdline}\n"
                    f"Type YES to confirm.",
                    msg,
                    context={
                        "action": verb,
                        "requires_confirmation": True,
                    },
                )
            try:
                _safe_popen(cmdline)
                return self.respond(
                    f"⚡ Executing: {cmdline}", msg
                )
            except Exception as e:
                return self.fail(f"Command failed: {e}", msg)

        # ---- open_app:<name> ----
        if verb.startswith("open_app:"):
            name = verb.split(":", 1)[1].strip().lower()
            return self._open_app(name, app, msg)

        # ---- open_folder:<name> ----
        if verb.startswith("open_folder:"):
            name = verb.split(":", 1)[1].strip().lower()
            return self._open_folder(name, app, msg)

        # ---- CONFIRM gate ----
        if verb in _CONFIRM_REQUIRED and not confirmed:
            return self.need_confirmation(
                f"⚠️ {_CONFIRM_REQUIRED[verb]}\nType YES to confirm.",
                msg,
                context={
                    "action": verb,
                    "requires_confirmation": True,
                },
            )

        # ---- READ_ONLY ----
        if verb == "battery status":
            return self.respond(app.SystemControl.battery_status(), msg)
        if verb == "system status":
            return self.respond(app.SystemMonitor.get_status(), msg)

        # ---- SAFE (volume) ----
        if verb == "mute volume":
            return self.respond(
                app.SystemControl.volume_control("mute"), msg)
        if verb == "unmute volume":
            return self.respond(
                app.SystemControl.volume_control("unmute"), msg)
        if verb == "increase volume":
            return self.respond(
                app.SystemControl.volume_control("up"), msg)
        if verb == "decrease volume":
            return self.respond(
                app.SystemControl.volume_control("down"), msg)
        if verb == "get volume":
            return self.respond(app.SystemControl.volume_control("get"), msg)
        if verb.startswith("set volume:"):
            return self.respond(app.SystemControl.volume_control(
                "set:" + verb.split(":", 1)[1]), msg)

        # ---- CONFIRMED destructive actions ----
        if verb == "shutdown pc" and confirmed:
            return self.respond(app.SystemControl.shutdown(), msg)
        if verb == "restart pc" and confirmed:
            return self.respond(app.SystemControl.restart(), msg)
        if verb == "lock pc":
            return self.respond(app.SystemControl.lock(), msg)
        if verb == "sleep pc" and confirmed:
            return self.respond(app.SystemControl.sleep(), msg)
        if verb == "cancel shutdown":
            return self.respond(
                app.SystemControl.cancel_shutdown(), msg)
        if verb == "empty recycle bin" and confirmed:
            return self.respond(
                app.SystemControl.empty_recycle_bin(), msg)

        return self.fail(
            f"SystemAgent: unrecognized command '{cmd}' / "
            f"action '{action}'.",
            msg,
        )

    # ------------------------------------------------------------------
    def _open_app(self, name: str, app, msg: AgentMessage) -> AgentMessage:
        """
        Route app launching through SystemControl.open_folder. That method
        handles folders, protocol URIs, known apps (via os.startfile),
        and unknown names (via a detached subprocess). It never hangs.
        """
        try:
            result = app.SystemControl.open_folder(name)
            return self.respond(result, msg)
        except Exception as e:
            return self.fail(f"Could not open {name}: {e}", msg)

    def _open_folder(self, name: str, app, msg: AgentMessage
                     ) -> AgentMessage:
        try:
            result = app.SystemControl.open_folder(name)
            return self.respond(result, msg)
        except Exception as e:
            return self.fail(f"Could not open {name}: {e}", msg)

    # ------------------------------------------------------------------
    @staticmethod
    def _canonical(cmd: str, action: str) -> str:
        cmd = (cmd or "").strip().lower()
        action = (action or "").strip().lower()

        for text in (cmd, action):
            match = __import__("re").search(r"\b(?:set\s+)?volume\s+(?:to\s+)?(\d{1,3})\s*%(?!\d)", text)
            if match:
                return "set volume:" + match.group(1)
            if text in {"volume", "volume status", "current volume", "what is my volume"}:
                return "get volume"

        # ---- prefixed forms ----
        for a in (action, cmd):
            if a.startswith("open_app:"):
                return "open_app:" + a.split(":", 1)[1].strip()
            if a.startswith("open_folder:"):
                return "open_folder:" + a.split(":", 1)[1].strip()
            if a.startswith("run_command:"):
                return "run_command:" + a.split(":", 1)[1].strip()

        # ---- "open <x>" pattern ----
        folders = {"downloads", "documents", "desktop",
                   "pictures", "videos"}

        for a in (cmd, action):
            if a.startswith("open "):
                arg = a[5:].strip()
                if arg in folders:
                    return f"open_folder:{arg}"
                if arg:
                    # Known or unknown, always try to launch
                    return f"open_app:{arg}"

        # ---- exact-match table ----
        exact = {
            "system status":        "system status",
            "check system":         "system status",
            "show system status":   "system status",
            "cpu":                  "system status",
            "ram":                  "system status",
            "memory usage":         "system status",
            "system_info":          "system status",
            "system_information":   "system status",
            "system_status":        "system status",
            "check_status":         "system status",
            "get_system_status":    "system status",
            "show_system_status":   "system status",
            "battery":              "battery status",
            "battery status":       "battery status",
            "battery_status":       "battery status",
            "get_battery":          "battery status",
            "mute":                 "mute volume",
            "mute volume":          "mute volume",
            "volume_mute":          "mute volume",
            "unmute":               "unmute volume",
            "unmute volume":        "unmute volume",
            "volume_unmute":        "unmute volume",
            "volume up":            "increase volume",
            "increase volume":      "increase volume",
            "volume_up":            "increase volume",
            "volume down":          "decrease volume",
            "decrease volume":      "decrease volume",
            "volume_down":          "decrease volume",
            "volume status":        "get volume",
            "lock":                 "lock pc",
            "lock pc":              "lock pc",
            "shutdown pc":          "shutdown pc",
            "shutdown":             "shutdown pc",
            "shutdown_pc":          "shutdown pc",
            "restart pc":           "restart pc",
            "restart":              "restart pc",
            "reboot":               "restart pc",
            "restart_pc":           "restart pc",
            "sleep":                "sleep pc",
            "sleep pc":             "sleep pc",
            "cancel shutdown":      "cancel shutdown",
            "cancel_shutdown":      "cancel shutdown",
            "recycle bin":          "empty recycle bin",
            "empty recycle bin":    "empty recycle bin",
            "empty_recycle_bin":    "empty recycle bin",
            "empty trash":          "empty recycle bin",
        }
        if cmd in exact:
            return exact[cmd]
        if action in exact:
            return exact[action]

        # ---- substring heuristics ----
        hay = f"{cmd} || {action}"
        if "shutdown" in hay or "power off" in hay:
            return "shutdown pc"
        if "restart" in hay or "reboot" in hay:
            return "restart pc"
        if "cancel" in hay and "shut" in hay:
            return "cancel shutdown"
        if "sleep" in hay:
            return "sleep pc"
        if "lock" in hay:
            return "lock pc"
        if "recycle" in hay or "trash" in hay:
            return "empty recycle bin"
        if "battery" in hay:
            return "battery status"
        if "unmute" in hay:
            return "unmute volume"
        if "mute" in hay:
            return "mute volume"
        if "volume" in hay and ("up" in hay or "increase" in hay):
            return "increase volume"
        if "volume" in hay and ("down" in hay or "decrease" in hay):
            return "decrease volume"
        if any(k in hay for k in ("system", "cpu", "ram", "disk",
                                   "memory usage")):
            return "system status"

        return ""

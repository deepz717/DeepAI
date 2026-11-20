"""
VisionAgent — wraps your vision helpers via LocalLLM.vision.
============================================================
Handles:
  * describe screen / analyze screen / what's on my screen
  * describe image / analyze image / what's in this image
  * screenshot analysis

Self-healing: if the caller asks for image analysis but no image is
loaded, fall back to a screen analysis (and say so in the reply).
That way queries like "what text do you see in the image" work
whether or not the user has uploaded an image.
"""
from __future__ import annotations

import os
from typing import Optional

from agents.base_agent import BaseAgent
from ai.ollama import get_llm
from core.message import AgentMessage


class VisionAgent(BaseAgent):
    name = "vision_agent"

    def __init__(self) -> None:
        self._mod = None

    # ------------------------------------------------------------------
    def _app(self):
        from core.agent_manager import get_app_module
        return get_app_module()

    # ------------------------------------------------------------------
    def handle(self, msg: AgentMessage) -> AgentMessage:
        text = msg.content if isinstance(msg.content, str) else str(
            msg.content.get("text", "")
        )
        cmd = text.strip().lower()
        action = (msg.context.get("action") or "").strip().lower()

        verb = self._canonical(cmd, action)

        self.publish_status(
            f"vision: {verb or action or cmd or 'unknown'}", msg
        )

        try:
            app = self._app()
        except Exception as e:
            return self.fail(str(e), msg)

        # ---- describe_image path (with screen fallback) ----
        if verb == "describe_image":
            images = getattr(app, "CURRENT_IMAGES", None) or []
            if images:
                return self._analyze_uploaded_image(images[-1], msg)
            # No image loaded — fall back to screen analysis.
            self.publish_status(
                "vision: no image loaded, using screen", msg
            )
            fallback = self._analyze_screen(msg)
            # Annotate the fallback so the reviewer knows what happened
            if fallback.is_success():
                annotated = (
                    "ℹ️ No uploaded image was found, so I analyzed your "
                    "current screen instead:\n\n" + fallback.as_text()
                )
                return self.respond(annotated, msg)
            return fallback

        # ---- describe_screen path ----
        if verb == "describe_screen":
            return self._analyze_screen(msg)

        return self.fail(
            f"VisionAgent: unrecognized command '{cmd}' / "
            f"action '{action}'. Try 'describe screen' or "
            f"'describe image'.",
            msg,
        )

    # ------------------------------------------------------------------
    def _analyze_screen(self, msg: AgentMessage) -> AgentMessage:
        try:
            import pyautogui
            shot = pyautogui.screenshot()
            result = get_llm().vision(
                shot, "Describe this screen concisely, including any "
                      "visible text and code."
            )
            if result:
                return self.respond(f"📸 {result}", msg)
            return self.fail("Vision model returned no output.", msg)
        except Exception as e:
            return self.fail(f"Screen capture failed: {e}", msg)

    def _analyze_uploaded_image(self, image_path: str,
                                msg: AgentMessage) -> AgentMessage:
        try:
            from PIL import Image
            img = Image.open(image_path)
            result = get_llm().vision(
                img, "Describe this image concisely, including any "
                     "visible text or code."
            )
            if result:
                return self.respond(f"🖼️ {result}", msg)
            return self.fail("Vision model returned no output.", msg)
        except Exception as e:
            return self.fail(f"Image analysis failed: {e}", msg)

    # ------------------------------------------------------------------
    @staticmethod
    def _canonical(cmd: str, action: str) -> str:
        cmd = (cmd or "").strip().lower()
        action = (action or "").strip().lower()
        hay = f"{cmd} || {action}"

        exact = {
            "describe screen":        "describe_screen",
            "analyze screen":         "describe_screen",
            "what's on my screen":    "describe_screen",
            "whats on my screen":     "describe_screen",
            "screenshot":             "describe_screen",
            "take screenshot":        "describe_screen",
            "screen analysis":        "describe_screen",
            "describe_screen":        "describe_screen",
            "analyze_screen":         "describe_screen",
            "screenshot_analysis":    "describe_screen",
            "describe image":         "describe_image",
            "analyze image":          "describe_image",
            "what's in this image":   "describe_image",
            "whats in this image":    "describe_image",
            "look at this image":     "describe_image",
            "describe_image":         "describe_image",
            "analyze_image":          "describe_image",
            "image_analysis":         "describe_image",
        }
        if cmd in exact:
            return exact[cmd]
        if action in exact:
            return exact[action]

        if any(k in hay for k in (
            "screenshot", "screen_capture", "capture screen",
            "describe_screen", "analyze_screen", "screen_analysis",
            "what's on my screen", "whats on my screen",
        )):
            return "describe_screen"
        if any(k in hay for k in (
            "describe_image", "analyze_image", "image_analysis",
            "describe image", "analyze image", "this image",
            "look at image",
        )):
            return "describe_image"
        if any(k in hay for k in ("screen", "screenshot")):
            return "describe_screen"
        if any(k in hay for k in ("image", "picture", "photo")):
            return "describe_image"

        return ""
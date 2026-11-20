"""Deterministic Blender tools — no LLM involved.

Each tool returns a dict from the shared executor.
"""
from tools.blender import scene_tools

__all__ = ["scene_tools"]
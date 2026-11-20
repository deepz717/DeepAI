from agents.base_agent import BaseAgent
from agents.chess_agent import ChessAgent
from agents.file_agent import FileAgent
from agents.system_agent import SystemAgent
from agents.vision_agent import VisionAgent
from agents.planner_agent import PlannerAgent
from agents.coding_agent import CodingAgent
from agents.research_agent import ResearchAgent
from agents.reviewer_agent import ReviewerAgent
from agents.head_agent import HeadAgent
from agents.memory_agent import MemoryAgent
from agents.code_agent import CodeAgent                    # ← new

__all__ = [
    "BaseAgent",
    "ChessAgent",
    "FileAgent",
    "SystemAgent",
    "VisionAgent",
    "PlannerAgent",
    "CodingAgent",
    "ResearchAgent",
    "ReviewerAgent",
    "HeadAgent",
    "MemoryAgent",
    "CodeAgent",
    "register_all_agents",
]


def register_all_agents(manager) -> None:
    for cls in (
        ChessAgent,
        FileAgent,
        SystemAgent,
        VisionAgent,
        CodingAgent,
        ResearchAgent,
        MemoryAgent,
        CodeAgent,
    ):
        try:
            manager.register_agent(cls())
        except ValueError:
            pass

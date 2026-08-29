"""ForgeLoop: a compact coding agent using native model tool calling."""

from .agent import AgentResult, CodingAgent
from .client import DeepSeekClient
from .config import Settings
from .tools import ToolRegistry, Workspace

__all__ = [
    "AgentResult",
    "CodingAgent",
    "DeepSeekClient",
    "Settings",
    "ToolRegistry",
    "Workspace",
]

__version__ = "0.1.0"


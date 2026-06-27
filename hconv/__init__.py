"""harness-convert: relocate a coding-agent session across harnesses.

Common interface (common.py) every harness maps to + an N^2 enrichment map
(enrich.py) for the surplus. Adapters (adapters/) read/write each harness.
"""
from .common import (AssistantMessage, Session, ToolCall, ToolResult,
                     UserMessage, synthesize_missing_results)
from .adapter import Adapter, convert, get, known, register
from . import adapters  # noqa: F401  (registers claude + codex)

__all__ = ["Session", "UserMessage", "AssistantMessage", "ToolCall",
           "ToolResult", "synthesize_missing_results", "Adapter",
           "convert", "get", "known", "register"]

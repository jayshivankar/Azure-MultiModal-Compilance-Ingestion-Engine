"""
Graph State Schema — Brand Guardian AI (v2.0 Multi-Agent)

The VideoAuditState is the single shared "memory" that flows through
every node in the LangGraph workflow.

Key additions over v1:
  • messages       – stores the BasMessage conversation history so
                     agents can share context via MessagesState semantics.
  • agent_logs     – append-only list of human-readable status strings
                     streamed to the frontend via SSE.
  • audio_findings – intermediate findings from the Audio/Transcript agent.
  • visual_findings– intermediate findings from the Visual/OCR agent.
  • critic_cycles  – number of re-evaluation loops the Critic agent ran.
"""

from operator import add
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langchain_core.messages import BaseMessage


# ---------------------------------------------------------------------------
# Sub-schemas
# ---------------------------------------------------------------------------

class ComplianceIssue(TypedDict):
    category: str            # e.g. "FTC_DISCLOSURE", "BRAND_VOICE"
    description: str         # Human-readable description of the violation
    severity: str            # "CRITICAL" | "WARNING"
    source: str              # "audio" | "visual" | "combined"
    timestamp: Optional[str] # Approximate video timestamp, if known


# ---------------------------------------------------------------------------
# Primary State
# ---------------------------------------------------------------------------

class VideoAuditState(TypedDict):
    """Single source of truth for the entire LangGraph execution."""

    # ── Input ────────────────────────────────────────────────────────────────
    video_url: str
    video_id: str

    # ── Extraction results (populated by Indexer Node) ────────────────────
    local_file_path: Optional[str]
    video_metadata: Dict[str, Any]   # {duration_seconds, platform}
    transcript: Optional[str]        # Full speech → text
    ocr_text: List[str]              # All on-screen text elements

    # ── Agent message history (MessagesState semantics) ───────────────────
    # operator.add ensures nodes append to this list, never replace it.
    messages: Annotated[List[BaseMessage], add]

    # ── Live agent status feed (read by SSE endpoint) ─────────────────────
    # Each node appends a short status string, e.g. "🎙 Audio Agent: analysing transcript…"
    agent_logs: Annotated[List[str], add]

    # ── Intermediate per-agent findings ───────────────────────────────────
    audio_findings: List[ComplianceIssue]
    visual_findings: List[ComplianceIssue]

    # ── Merged, validated compliance results (Critic output) ──────────────
    compliance_results: Annotated[List[ComplianceIssue], add]

    # ── Critic loop tracking ──────────────────────────────────────────────
    critic_cycles: int               # incremented each Critic pass

    # ── Final deliverables ────────────────────────────────────────────────
    final_status: str                # "PASS" | "FAIL"
    final_report: str                # Markdown summary for the frontend

    # ── System errors (append-only, never halt execution) ─────────────────
    errors: Annotated[List[str], add]
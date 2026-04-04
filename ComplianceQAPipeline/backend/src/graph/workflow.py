"""
Workflow Definition — Brand Guardian AI (v2.0 Multi-Agent)

Graph topology:
                        ┌─────────┐
                        │  START  │
                        └────┬────┘
                             │
                        ┌────▼────┐
                        │ Indexer │  (download + Azure VI)
                        └────┬────┘
                             │
                        ┌────▼──────┐
                        │ Supervisor│  (routing + log)
                        └────┬──────┘
                    ┌────────┴────────┐
                    │                 │
              ┌─────▼──────┐  ┌──────▼──────┐
              │ Audio Agent│  │ Visual Agent│
              └─────┬──────┘  └──────┬──────┘
                    └────────┬────────┘
                             │
                        ┌────▼────┐
                        │ Critic  │◄─── (re-route if needs_revision)
                        └────┬────┘
                             │
                          ┌──▼──┐
                          │ END │
                          └─────┘

Conditional routing:
  • After Supervisor  → agents run in parallel (audio + visual)
  • After Critic      → if needs_revision AND cycles < MAX → back to supervisor
                        else → END
"""

from langgraph.graph import END, START, StateGraph

from ComplianceQAPipeline.backend.src.graph.state import VideoAuditState
from ComplianceQAPipeline.backend.src.graph.nodes import (
    audio_agent_node,
    critic_agent_node,
    index_video_node,
    supervisor_node,
    visual_agent_node,
    MAX_CRITIC_CYCLES,
)


# ---------------------------------------------------------------------------
# Conditional edge: should Critic loop or end?
# ---------------------------------------------------------------------------

def route_after_critic(state: VideoAuditState) -> str:
    """
    Returns "supervisor" to re-run agents if the Critic requested revision
    AND we haven't hit the max cycle limit yet.
    Returns "end" otherwise.
    """
    cycles = state.get("critic_cycles", 0)
    # Critic sets audio_findings=[] / visual_findings=[] when it wants a revision.
    # We detect this by checking whether final_status has been set.
    has_final_verdict = bool(state.get("final_status") and state.get("final_report"))

    if not has_final_verdict and cycles < MAX_CRITIC_CYCLES:
        return "supervisor"   # loop back
    return "end"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def create_graph():
    """Builds and compiles the multi-agent LangGraph workflow."""

    workflow = StateGraph(VideoAuditState)

    # ── Register nodes ──────────────────────────────────────────────────────
    workflow.add_node("indexer",       index_video_node)
    workflow.add_node("supervisor",    supervisor_node)
    workflow.add_node("audio_agent",   audio_agent_node)
    workflow.add_node("visual_agent",  visual_agent_node)
    workflow.add_node("critic",        critic_agent_node)

    # ── Static edges ────────────────────────────────────────────────────────
    workflow.add_edge(START,        "indexer")
    workflow.add_edge("indexer",    "supervisor")

    # Supervisor → both agents (parallel fan-out)
    workflow.add_edge("supervisor", "audio_agent")
    workflow.add_edge("supervisor", "visual_agent")

    # Both agents → critic (fan-in)
    workflow.add_edge("audio_agent",  "critic")
    workflow.add_edge("visual_agent", "critic")

    # ── Conditional edge from Critic ─────────────────────────────────────
    workflow.add_conditional_edges(
        "critic",
        route_after_critic,
        {
            "supervisor": "supervisor",  # revision loop
            "end": END,                  # pipeline complete
        },
    )

    # ── Compile ──────────────────────────────────────────────────────────────
    app = workflow.compile()

    return app


# Expose compiled graph for import by the API and CLI
app = create_graph()
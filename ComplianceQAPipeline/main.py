"""
CLI Entry Point — Brand Guardian AI (v2.0 Multi-Agent)

Runs a quick end-to-end simulation of the multi-agent compliance
audit pipeline directly from the terminal, without starting the
FastAPI server. Useful for local testing and CI smoke-tests.

Usage:
    python ComplianceQAPipeline/main.py
"""

import json
import logging
import uuid

from dotenv import load_dotenv

load_dotenv(override=True)

from ComplianceQAPipeline.backend.src.graph.workflow import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("brand-guardian-cli")


def run_cli_simulation():
    """Runs a full multi-agent compliance audit and prints the report."""

    session_id = str(uuid.uuid4())
    logger.info(f"Starting CLI Audit Session: {session_id}")

    # Must include ALL required fields from the new VideoAuditState
    initial_inputs = {
        "video_url":          "https://www.youtube.com/watch?v=VW2nUA7yrJw",
        "video_id":           f"vid_{session_id[:8]}",
        "compliance_results": [],
        "agent_logs":         [],
        "messages":           [],
        "audio_findings":     [],
        "visual_findings":    [],
        "errors":             [],
        "critic_cycles":      0,
    }

    print("\n" + "═" * 60)
    print("  BRAND GUARDIAN AI  ·  Multi-Agent Compliance Pipeline")
    print("═" * 60)
    print(f"\n📋 Session:   {session_id}")
    print(f"🔗 Video URL: {initial_inputs['video_url']}")
    print(f"🆔 Video ID:  {initial_inputs['video_id']}")
    print("\n⏳ Executing multi-agent graph …\n")

    try:
        final_state = app.invoke(initial_inputs)

        print("\n" + "═" * 60)
        print("  COMPLIANCE AUDIT REPORT")
        print("═" * 60)

        status = final_state.get("final_status", "UNKNOWN")
        status_icon = "✅ PASS" if status == "PASS" else "❌ FAIL"
        print(f"\nFinal Status:  {status_icon}")
        print(f"Video ID:      {final_state.get('video_id', '—')}")
        print(f"Critic Cycles: {final_state.get('critic_cycles', 0)}")

        # ── Agent Logs ──────────────────────────────────────────
        logs = final_state.get("agent_logs", [])
        if logs:
            print("\n── Agent Activity Log ──")
            for line in logs:
                print(f"  {line}")

        # ── Findings ────────────────────────────────────────────
        results = final_state.get("compliance_results", [])
        print(f"\n── Compliance Findings ({len(results)} issue(s)) ──")
        if results:
            for issue in results:
                sev = issue.get("severity", "?")
                cat = issue.get("category", "?")
                src = issue.get("source", "?")
                desc = issue.get("description", "")
                ts   = issue.get("timestamp")
                ts_str = f"  @ {ts}" if ts else ""
                print(f"  [{sev}] [{src.upper()}] {cat}{ts_str}")
                print(f"    → {desc}")
        else:
            print("  No violations detected.")

        # ── Summary ─────────────────────────────────────────────
        print("\n── Executive Summary ──")
        print(f"  {final_state.get('final_report', 'No report generated.')}")

        # ── Errors ──────────────────────────────────────────────
        errors = final_state.get("errors", [])
        if errors:
            print("\n── System Errors ──")
            for err in errors:
                print(f"  ⚠️  {err}")

        print("\n" + "═" * 60 + "\n")

    except Exception as e:
        logger.error(f"Pipeline execution failed: {str(e)}")
        raise


if __name__ == "__main__":
    run_cli_simulation()

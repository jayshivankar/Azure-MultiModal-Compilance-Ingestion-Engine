"""
LangGraph Nodes — Brand Guardian AI (v2.1 Multi-Agent)

Node architecture:
  ┌─────────────┐      ┌──────────────────┐      ┌──────────────────┐
  │  Indexer    │ ───► │   Supervisor     │ ────►│   Audio Agent    │
  └─────────────┘      │  (router LLM)    │      │ (transcript+tools│
                       └──────────────────┘      └────────┬─────────┘
                                │                         │
                                │                ┌────────▼─────────┐
                                └───────────────►│   Visual Agent   │
                                                 │  (OCR + tools)   │
                                                 └────────┬─────────┘
                                                          │
                                                 ┌────────▼─────────┐
                                                 │  Critic Agent    │
                                                 │ (validate/loop)  │
                                                 └──────────────────┘

Key design decisions:
  • Indexer uses Azure Blob Storage → SAS URL → VI submission to avoid
    ConnectionResetError on direct large-file uploads.
  • All LLM clients are created once per node invocation (stateless nodes).
  • Structured output (.with_structured_output) is used for Critic.
  • JSON extraction uses regex to handle markdown-wrapped LLM responses.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import AzureChatOpenAI
from pydantic import BaseModel, Field

from ComplianceQAPipeline.backend.src.graph.state import ComplianceIssue, VideoAuditState
from ComplianceQAPipeline.backend.src.graph.tools import COMPLIANCE_TOOLS
from ComplianceQAPipeline.backend.src.services.video_indexer import VideoIndexerService

logger = logging.getLogger("brand-guardian-nodes")

MAX_CRITIC_CYCLES = 2   # Maximum Critic re-evaluation loops before forcing verdict
MAX_TOOL_STEPS   = 3    # Maximum ReAct tool-call iterations per agent


# ===========================================================================
# Pydantic models for structured LLM output (Critic)
# ===========================================================================

class StructuredIssue(BaseModel):
    category: str    = Field(description="Short compliance category label, e.g. FTC_DISCLOSURE")
    description: str = Field(description="Detailed explanation of the violation")
    severity: str    = Field(description="CRITICAL or WARNING")
    source: str      = Field(description="audio, visual, or combined")
    timestamp: str | None = Field(default=None, description="Approx video timestamp if known")


class StructuredVerdict(BaseModel):
    status: str               = Field(description="PASS or FAIL")
    issues: List[StructuredIssue] = Field(default_factory=list, description="All confirmed compliance issues")
    final_report: str         = Field(description="Executive-summary paragraph for the dashboard")
    needs_revision: bool      = Field(
        default=False,          # ← default prevents ValidationError when LLM omits this field
        description="True if findings lack citations and must be re-evaluated"
    )


# ===========================================================================
# Helper: build LLM client
# ===========================================================================

def _build_llm(temperature: float = 0.0) -> AzureChatOpenAI:
    return AzureChatOpenAI(
        azure_deployment=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
        temperature=temperature,
    )


# ===========================================================================
# Helper: robust JSON extraction from LLM text
# ===========================================================================

def _extract_json(text: str) -> str:
    """
    Extracts a JSON array from an LLM response, handling cases where the
    model wraps the output in markdown code fences (```json ... ```).

    Returns the raw JSON string, or '[]' if nothing is found.
    """
    if not text:
        return "[]"

    # Try to extract JSON between code fences first
    fence_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    # Try to find a bare JSON array in the response
    array_match = re.search(r"\[.*\]", text, re.DOTALL)
    if array_match:
        return array_match.group(0).strip()

    # No JSON array found — return empty array
    logger.warning(f"[JSON extractor] Could not find JSON array in: {text[:200]!r}")
    return "[]"


# ===========================================================================
# Helper: run a ReAct tool-calling loop for a sub-agent
# ===========================================================================

def _run_tool_agent(
    llm: AzureChatOpenAI,
    system_prompt: str,
    user_content: str,
    context_label: str,
) -> tuple[str, List[Any]]:
    """
    Executes a tool-calling ReAct loop and returns:
      (final_text_response, all_messages)

    The LLM is given COMPLIANCE_TOOLS. If it calls a tool, we execute it
    and feed the result back, up to MAX_TOOL_STEPS iterations.
    """
    llm_with_tools = llm.bind_tools(COMPLIANCE_TOOLS)
    messages: List[Any] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]

    tool_map = {t.name: t for t in COMPLIANCE_TOOLS}

    for step in range(MAX_TOOL_STEPS):
        response: AIMessage = llm_with_tools.invoke(messages)
        messages.append(response)

        if not response.tool_calls:
            # No more tool calls — final answer
            logger.info(f"[{context_label}] Finished after {step} tool call(s).")
            return response.content, messages

        # Execute each tool call and append results
        for tc in response.tool_calls:
            tool_name = tc["name"]
            tool_args = tc["args"]
            logger.info(f"[{context_label}] Calling tool '{tool_name}' with args: {tool_args}")
            try:
                tool_fn = tool_map.get(tool_name)
                result = tool_fn.invoke(tool_args) if tool_fn else f"Tool '{tool_name}' not found."
            except Exception as e:
                result = f"Tool execution error: {str(e)}"

            messages.append(
                ToolMessage(content=str(result), tool_call_id=tc["id"])
            )

    # Exceeded max steps — ask for final answer without tools
    logger.warning(f"[{context_label}] Reached MAX_TOOL_STEPS={MAX_TOOL_STEPS}. Forcing final answer.")
    final_llm = _build_llm()
    final = final_llm.invoke(
        messages + [HumanMessage(content="Summarise your findings as a final JSON answer now.")]
    )
    messages.append(final)
    return final.content, messages


# ===========================================================================
# NODE 1: Indexer
# ===========================================================================

def index_video_node(state: VideoAuditState) -> Dict[str, Any]:
    """
    Downloads the YouTube video, stages it in Azure Blob Storage,
    submits a SAS URL to Azure Video Indexer, polls until done,
    then extracts transcript + OCR.

    Using Blob Storage as an intermediary solves the ConnectionResetError
    that occurs when posting large video files directly to api.videoindexer.ai.
    """
    video_url   = state.get("video_url", "")
    video_id    = state.get("video_id", "vid_demo")

    log = [f"📥 Indexer: Starting ingestion for video_id={video_id}"]
    logger.info(f"--- [Node: Indexer] {video_url} ---")

    local_filename = f"temp_{video_id}.mp4"
    blob_name      = f"{video_id}.mp4"

    try:
        vi_service = VideoIndexerService()

        # ── Step 1: Download from YouTube (max 720p) ──────────────────────
        log.append("⬇️ Indexer: Downloading from YouTube (max 720p) …")
        if "youtube.com" in video_url or "youtu.be" in video_url:
            local_path = vi_service.download_youtube_video(
                video_url, output_path=local_filename
            )
        else:
            raise ValueError("Only YouTube URLs are supported by this pipeline.")

        # ── Step 2: Upload to Azure Blob Storage (chunked, reliable) ─────
        log.append("📦 Indexer: Staging video in Azure Blob Storage …")
        vi_service.upload_to_blob(local_path, blob_name)

        # ── Step 3: Generate SAS URL (4-hour expiry) ──────────────────────
        log.append("🔗 Indexer: Generating SAS URL for Video Indexer …")
        sas_url = vi_service.generate_sas_url(blob_name)

        # ── Step 4: Clean up local temp file ──────────────────────────────
        if os.path.exists(local_path):
            os.remove(local_path)
            logger.info(f"[Indexer] Deleted local temp file: {local_path}")

        # ── Step 5: Submit SAS URL to Video Indexer (no file upload) ─────
        log.append("☁️ Indexer: Submitting to Azure Video Indexer via URL …")
        azure_video_id = vi_service.upload_video(sas_url, video_name=video_id)

        # ── Step 6: Poll for completion ────────────────────────────────────
        log.append("⏳ Indexer: Waiting for Azure to finish indexing (2-5 min) …")
        raw_insights = vi_service.wait_for_processing(azure_video_id)

        # ── Step 7: Extract insights ───────────────────────────────────────
        log.append("🔍 Indexer: Extracting transcript and OCR data …")
        clean_data = vi_service.extract_data(raw_insights)

        # ── Step 8: Clean up blob ──────────────────────────────────────────
        vi_service.delete_blob(blob_name)

        log.append("✅ Indexer: Extraction complete. Routing to Supervisor.")
        logger.info("--- [Node: Indexer] Extraction complete ---")

        return {**clean_data, "agent_logs": log}

    except Exception as e:
        logger.error(f"[Node: Indexer] Failed: {e}")
        # Best-effort cleanup of local file
        if os.path.exists(local_filename):
            try:
                os.remove(local_filename)
            except OSError:
                pass
        return {
            "transcript": "",
            "ocr_text": [],
            "video_metadata": {},
            "agent_logs": log + [f"❌ Indexer Error: {str(e)}"],
            "errors": [str(e)],
            "final_status": "FAIL",
        }


# ===========================================================================
# NODE 2: Supervisor
# ===========================================================================

def supervisor_node(state: VideoAuditState) -> Dict[str, Any]:
    """
    Lightweight routing node. Validates that we have extractable content
    and emits a structured log entry so the frontend can show progress.

    The actual routing (to audio/visual agents) is handled by conditional
    edges in workflow.py based on the presence of transcript / ocr_text.
    """
    transcript = state.get("transcript", "")
    ocr_text   = state.get("ocr_text", [])

    logs = ["🧠 Supervisor: Analysing extracted content and routing to specialist agents …"]

    if transcript:
        logs.append("🎙 Supervisor: Transcript detected → routing to Audio Agent")
    if ocr_text:
        logs.append("👁 Supervisor: OCR data detected → routing to Visual Agent")
    if not transcript and not ocr_text:
        logs.append("⚠️ Supervisor: No content available for analysis.")

    return {
        "agent_logs": logs,
        "audio_findings": [],
        "visual_findings": [],
        "critic_cycles": 0,
    }


# ===========================================================================
# NODE 3: Audio Agent (Transcript Analysis)
# ===========================================================================

def audio_agent_node(state: VideoAuditState) -> Dict[str, Any]:
    """
    Analyses the audio transcript for spoken-word compliance violations.

    This agent has access to COMPLIANCE_TOOLS and uses a ReAct loop
    to look up brand guidelines and/or web sources before issuing findings.
    """
    transcript = state.get("transcript", "")
    logs = ["🎙 Audio Agent: Beginning transcript analysis …"]

    if not transcript:
        logs.append("⚠️ Audio Agent: No transcript available, skipping.")
        return {"audio_findings": [], "agent_logs": logs}

    llm = _build_llm()

    system_prompt = """You are a specialised Brand Compliance Audio Analyst.

Your role is to audit spoken content (transcripts) from video ads or branded content
against official brand guidelines and regulations.

INSTRUCTIONS:
1. Read the transcript carefully.
2. Use the `search_azure_knowledge_base` tool to look up relevant brand/regulatory rules.
3. If the KB doesn't answer your question, use `search_public_web` as a fallback.
4. Identify ALL spoken violations: inappropriate claims, missing disclosures, off-brand language, etc.
5. Respond with a JSON array of findings in this format:
[
  {
    "category": "FTC_DISCLOSURE",
    "description": "...",
    "severity": "CRITICAL",
    "source": "audio",
    "timestamp": "00:15"
  }
]
If no violations found, respond with an empty JSON array: []
Do NOT include markdown code blocks — return raw JSON only."""

    user_content = f"TRANSCRIPT TO AUDIT:\n\n{transcript}"

    logs.append("🔎 Audio Agent: Querying knowledge base and performing analysis …")
    try:
        response_text, messages = _run_tool_agent(llm, system_prompt, user_content, "AudioAgent")
        # Parse JSON
        cleaned = _extract_json(response_text)
        findings: List[ComplianceIssue] = json.loads(cleaned) if cleaned else []
        logs.append(f"✅ Audio Agent: Found {len(findings)} issue(s) in transcript.")
        return {
            "audio_findings": findings,
            "messages": messages,
            "agent_logs": logs,
        }
    except json.JSONDecodeError as e:
        logger.error(f"[Audio Agent] JSON parse error: {e}. Raw: {response_text[:300]}")
        logs.append(f"⚠️ Audio Agent: Could not parse structured output, findings may be incomplete.")
        return {"audio_findings": [], "agent_logs": logs, "errors": [f"Audio Agent JSON parse error: {e}"]}
    except Exception as e:
        logger.error(f"[Audio Agent] Error: {e}")
        logs.append(f"❌ Audio Agent Error: {str(e)}")
        return {"audio_findings": [], "agent_logs": logs, "errors": [str(e)]}


# ===========================================================================
# NODE 4: Visual Agent (OCR Analysis)
# ===========================================================================

def visual_agent_node(state: VideoAuditState) -> Dict[str, Any]:
    """
    Analyses on-screen text (OCR) for visual compliance violations.

    Looks for: unauthorised logos, misleading visual claims, missing
    legal caveats visible on screen, copyright symbols, etc.
    """
    ocr_text = state.get("ocr_text", [])
    logs = ["👁 Visual Agent: Beginning OCR text analysis …"]

    if not ocr_text:
        logs.append("⚠️ Visual Agent: No OCR text available, skipping.")
        return {"visual_findings": [], "agent_logs": logs}

    llm = _build_llm()

    system_prompt = """You are a specialised Brand Compliance Visual Analyst.

Your role is to audit on-screen text elements (extracted via OCR) from branded video content.

INSTRUCTIONS:
1. Review the OCR text carefully.
2. Use `search_azure_knowledge_base` to look up brand guidelines on visual elements.
3. If needed, use `search_public_web` for external verification.
4. Identify ALL visual violations: unauthorised logos/marks, misleading on-screen claims,
   missing legal disclaimers, incorrect trademark symbols, competitor branding, etc.
5. Respond with a JSON array in this exact format:
[
  {
    "category": "TRADEMARK_MISUSE",
    "description": "...",
    "severity": "CRITICAL",
    "source": "visual",
    "timestamp": null
  }
]
If no violations found, respond with: []
Do NOT include markdown code blocks — return raw JSON only."""

    ocr_joined = "\n".join(f"• {t}" for t in ocr_text)
    user_content = f"ON-SCREEN TEXT (OCR) TO AUDIT:\n\n{ocr_joined}"

    logs.append("🔎 Visual Agent: Querying knowledge base and performing analysis …")
    try:
        response_text, messages = _run_tool_agent(llm, system_prompt, user_content, "VisualAgent")
        cleaned = _extract_json(response_text)
        findings: List[ComplianceIssue] = json.loads(cleaned) if cleaned else []
        logs.append(f"✅ Visual Agent: Found {len(findings)} issue(s) in visual content.")
        return {
            "visual_findings": findings,
            "messages": messages,
            "agent_logs": logs,
        }
    except json.JSONDecodeError as e:
        logger.error(f"[Visual Agent] JSON parse error: {e}. Raw: {response_text[:300]}")
        logs.append("⚠️ Visual Agent: Could not parse structured output.")
        return {"visual_findings": [], "agent_logs": logs, "errors": [f"Visual Agent JSON parse error: {e}"]}
    except Exception as e:
        logger.error(f"[Visual Agent] Error: {e}")
        logs.append(f"❌ Visual Agent Error: {str(e)}")
        return {"visual_findings": [], "agent_logs": logs, "errors": [str(e)]}


# ===========================================================================
# NODE 5: Critic Agent (Validation + Final Verdict)
# ===========================================================================

def critic_agent_node(state: VideoAuditState) -> Dict[str, Any]:
    """
    Evaluates the combined findings from Audio and Visual agents.

    The Critic:
      1. Reviews all findings for factual accuracy and citation quality.
      2. Deduplicates overlapping issues.
      3. Uses `.with_structured_output(StructuredVerdict)` to return a
         guaranteed-valid Pydantic model — no regex or manual JSON parsing.
      4. If needs_revision=True AND critic_cycles < MAX_CRITIC_CYCLES,
         workflow.py routes back to the agents for another pass.
      5. Otherwise, produces the final compliance verdict.
    """
    audio_findings  = state.get("audio_findings", [])
    visual_findings = state.get("visual_findings", [])
    transcript      = state.get("transcript", "")[:1500]     # truncate for context window
    critic_cycles   = state.get("critic_cycles", 0)
    logs = [f"⚖️ Critic Agent: Evaluating findings (review cycle {critic_cycles + 1}/{MAX_CRITIC_CYCLES}) …"]

    all_findings = audio_findings + visual_findings

    llm = _build_llm()
    structured_llm = llm.with_structured_output(StructuredVerdict)

    system_prompt = """You are a Senior Brand Compliance Auditor performing a final quality review.

You will receive:
  • Audio Agent findings (from transcript analysis)
  • Visual Agent findings (from OCR analysis)
  • A snippet of the original transcript

Your tasks:
  1. Verify each finding is legitimate and supported by evidence in the content.
  2. Deduplicate issues that appear in both audio and visual findings.
  3. Flag `needs_revision = true` ONLY IF findings lack specific citations or
     evidence from the content — meaning the sub-agents need another loop.
  4. Set `needs_revision = false` if findings are well-supported (even if empty).
  5. Write a clear executive summary for the `final_report` field.
  6. Set status 'FAIL' if ANY CRITICAL issue is present, else 'PASS'."""

    user_content = f"""
AUDIO AGENT FINDINGS:
{json.dumps(audio_findings, indent=2) if audio_findings else "None"}

VISUAL AGENT FINDINGS:
{json.dumps(visual_findings, indent=2) if visual_findings else "None"}

TRANSCRIPT SNIPPET (first 1500 chars):
{transcript}
"""

    try:
        verdict: StructuredVerdict = structured_llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ])

        confirmed_issues: List[ComplianceIssue] = [
            {
                "category": issue.category,
                "description": issue.description,
                "severity": issue.severity,
                "source": issue.source,
                "timestamp": issue.timestamp,
            }
            for issue in verdict.issues
        ]

        if verdict.needs_revision and critic_cycles < MAX_CRITIC_CYCLES:
            logs.append(
                f"🔄 Critic: Findings need revision — sending back to agents "
                f"(cycle {critic_cycles + 1}/{MAX_CRITIC_CYCLES})."
            )
            return {
                "critic_cycles": critic_cycles + 1,
                "agent_logs": logs,
                # Clear intermediate findings to force fresh analysis
                "audio_findings": [],
                "visual_findings": [],
            }
        else:
            if verdict.needs_revision:
                logs.append("⚠️ Critic: Max revision cycles reached. Accepting current findings.")
            else:
                logs.append(f"✅ Critic: Findings validated. Final status: {verdict.status}")

            return {
                "compliance_results": confirmed_issues,
                "final_status": verdict.status,
                "final_report": verdict.final_report,
                "critic_cycles": critic_cycles + 1,
                "agent_logs": logs + ["🏁 Pipeline complete. Report ready."],
            }

    except Exception as e:
        logger.error(f"[Critic Agent] Error: {e}")
        logs.append(f"❌ Critic Agent Error: {str(e)}")
        return {
            "compliance_results": all_findings,
            "final_status": "FAIL" if all_findings else "PASS",
            "final_report": "Critic evaluation failed. Raw agent findings returned as-is.",
            "critic_cycles": critic_cycles + 1,
            "agent_logs": logs,
            "errors": [str(e)],
        }
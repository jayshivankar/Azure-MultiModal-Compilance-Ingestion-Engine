"""
FastAPI Server — Brand Guardian AI (v2.0)

New additions over v1:
  • Rate limiting via slowapi + Redis (5 audit submissions / min / IP)
  • Enhanced Pydantic input validation (YouTube URL domain check, length cap)
  • SSE streaming endpoint: GET /api/audit/{session_id}/stream
    Streams agent_logs from the job record as Server-Sent Events so the
    frontend can display the live "thought process" without polling.
"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, AsyncGenerator, Literal

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from ComplianceQAPipeline.backend.src.api.telemetry import setup_telemetry
from ComplianceQAPipeline.backend.src.graph.workflow import app as compliance_graph

load_dotenv(override=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("brand-guardian-api")

# Suppress noisy Azure SDK and telemetry logs
for _noisy in (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.monitor.opentelemetry",
    "azure.identity",
    "urllib3.connectionpool",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BACKEND_API_DIR   = Path(__file__).resolve().parent
PROJECT_DIR       = BACKEND_API_DIR.parents[2]
FRONTEND_DIR      = PROJECT_DIR / "frontend"
FRONTEND_ENTRYPOINT = FRONTEND_DIR / "index.html"

# ---------------------------------------------------------------------------
# Rate limiter (Redis backend preferred in ECS multi-container deployments)
# ---------------------------------------------------------------------------
_redis_url = os.getenv("REDIS_URL")

if _redis_url:
    from redis import Redis
    _storage_uri = _redis_url
else:
    _storage_uri = "memory://"

limiter = Limiter(key_func=get_remote_address, storage_uri=_storage_uri)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
DEFAULT_ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

def _get_allowed_origins() -> list[str]:
    raw = os.getenv("ALLOWED_ORIGINS", "")
    env_origins = [o.strip() for o in raw.split(",") if o.strip()]
    return env_origins or DEFAULT_ALLOWED_ORIGINS

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
AuditJobState    = Literal["QUEUED", "PROCESSING", "COMPLETED", "FAILED"]
AuditResultState = Literal["PASS", "FAIL", "UNKNOWN"]

# ---------------------------------------------------------------------------
# In-memory job store (thread-safe)
# ---------------------------------------------------------------------------
audit_jobs: dict[str, dict[str, Any]] = {}
audit_jobs_lock = Lock()

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _create_job_record(session_id: str, video_id: str, video_url: str) -> dict[str, Any]:
    ts = _utc_now()
    return {
        "session_id": session_id,
        "video_id": video_id,
        "video_url": video_url,
        "job_status": "QUEUED",
        "final_status": "UNKNOWN",
        "final_report": "Audit request accepted. Processing in the background.",
        "compliance_results": [],
        "agent_logs": [],
        "errors": [],
        "created_at": ts,
        "updated_at": ts,
    }

def _store_job(job: dict[str, Any]) -> dict[str, Any]:
    with audit_jobs_lock:
        audit_jobs[job["session_id"]] = job
        return dict(job)

def _update_job(session_id: str, **updates: Any) -> dict[str, Any]:
    with audit_jobs_lock:
        if session_id not in audit_jobs:
            raise KeyError(f"Unknown session: {session_id}")
        audit_jobs[session_id].update(updates)
        audit_jobs[session_id]["updated_at"] = _utc_now()
        return dict(audit_jobs[session_id])

def _get_job(session_id: str) -> dict[str, Any] | None:
    with audit_jobs_lock:
        j = audit_jobs.get(session_id)
        return dict(j) if j else None

def _append_log(session_id: str, lines: list[str]) -> None:
    """Thread-safe append to the job's agent_logs list."""
    with audit_jobs_lock:
        if session_id in audit_jobs:
            audit_jobs[session_id]["agent_logs"].extend(lines)
            audit_jobs[session_id]["updated_at"] = _utc_now()

# ---------------------------------------------------------------------------
# Background audit runner
# ---------------------------------------------------------------------------

def _run_audit_job(session_id: str, initial_inputs: dict[str, Any]) -> None:
    logger.info("Starting background audit session=%s", session_id)
    _update_job(session_id, job_status="PROCESSING",
                final_report="Audit job is running …")

    try:
        # Single-pass: use stream_mode="updates" for live log draining.
        # We collect the final accumulated state as we go — no second invoke needed.
        final_state: dict[str, Any] = {}

        for event in compliance_graph.stream(initial_inputs, stream_mode="updates"):
            for node_name, node_output in event.items():
                # Drain agent logs into the job record (read by SSE endpoint)
                new_logs = node_output.get("agent_logs", [])
                if new_logs:
                    _append_log(session_id, new_logs)

                # Propagate intermediate errors
                errs = node_output.get("errors", [])
                if errs:
                    with audit_jobs_lock:
                        audit_jobs[session_id]["errors"].extend(errs)

                # Merge node output into running final_state
                final_state.update(node_output)

        errors       = _get_job(session_id).get("errors", []) or []
        final_status = final_state.get("final_status", "UNKNOWN")
        # Mark as FAILED if: explicit errors, or indexer set final_status=FAIL
        job_status: AuditJobState = (
            "FAILED" if (errors or final_status == "FAIL") else "COMPLETED"
        )
        _update_job(
            session_id,
            job_status=job_status,
            final_status=final_status,
            final_report=final_state.get("final_report", "No report generated."),
            compliance_results=final_state.get("compliance_results", []),
            errors=errors,
        )
        logger.info("Background audit done session=%s job_status=%s", session_id, job_status)
    except Exception as exc:
        logger.exception("Background audit failed session=%s", session_id)
        _update_job(
            session_id,
            job_status="FAILED",
            final_status="FAIL",
            final_report="Audit failed before completion. Check system errors.",
            errors=[str(exc)],
        )

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_telemetry()
    logger.info("Brand Guardian AI v2.0 starting up (rate limiter: %s)",
                "Redis" if _redis_url else "in-memory")
    yield
    logger.info("Brand Guardian AI shutting down")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Brand Guardian AI",
    description="Multi-agent multimodal compliance audit platform powered by Azure AI and LangGraph.",
    version="2.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")

# ---------------------------------------------------------------------------
# Pydantic I/O models
# ---------------------------------------------------------------------------

ALLOWED_YT_DOMAINS = ("youtube.com", "youtu.be", "www.youtube.com", "m.youtube.com")

class AuditRequest(BaseModel):
    video_url: HttpUrl = Field(..., description="Public YouTube URL to audit.")

    @field_validator("video_url")
    @classmethod
    def must_be_youtube(cls, v: HttpUrl) -> HttpUrl:
        host = (v.host or "").lower()
        if not any(host == d or host.endswith("." + d) for d in ALLOWED_YT_DOMAINS):
            raise ValueError(
                f"Only YouTube URLs are accepted. Got host: {host!r}"
            )
        url_str = str(v)
        if len(url_str) > 200:
            raise ValueError("URL is too long (max 200 characters).")
        return v


class ComplianceIssueResponse(BaseModel):
    category: str
    severity: str
    description: str
    source: str   = "unknown"
    timestamp: str | None = None


class AuditSubmissionResponse(BaseModel):
    session_id: str
    video_id: str
    job_status: AuditJobState
    final_status: AuditResultState
    final_report: str
    status_url: str
    stream_url: str
    created_at: str
    updated_at: str


class AuditStatusResponse(BaseModel):
    session_id: str
    video_id: str
    video_url: str
    job_status: AuditJobState
    final_status: AuditResultState
    final_report: str
    compliance_results: list[ComplianceIssueResponse]
    agent_logs: list[str] = Field(default_factory=list)
    errors: list[str]     = Field(default_factory=list)
    created_at: str
    updated_at: str


class AppConfigResponse(BaseModel):
    app_name: str
    app_version: str
    environment: str
    allowed_origins: list[str]
    features: dict[str, Any]

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def serve_frontend() -> FileResponse:
    if not FRONTEND_ENTRYPOINT.exists():
        raise HTTPException(status_code=404, detail="Frontend assets not found.")
    return FileResponse(FRONTEND_ENTRYPOINT)


@app.get("/api/health")
def health_check() -> dict[str, Any]:
    return {
        "status": "healthy",
        "service": "Brand Guardian AI",
        "version": "2.0.0",
        "environment": os.getenv("APP_ENV", "development"),
    }


@app.get("/api/config", response_model=AppConfigResponse)
def app_config() -> AppConfigResponse:
    return AppConfigResponse(
        app_name=app.title,
        app_version=app.version,
        environment=os.getenv("APP_ENV", "development"),
        allowed_origins=_get_allowed_origins(),
        features={
            "azure_monitor":         bool(os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")),
            "knowledge_base_index":  os.getenv("AZURE_SEARCH_INDEX_NAME", "not-configured"),
            "frontend":              FRONTEND_ENTRYPOINT.exists(),
            "audit_mode":            "async-streaming",
            "rate_limiter":          "redis" if _redis_url else "in-memory",
            "multi_agent":           True,
            "tavily_search":         bool(os.getenv("TAVILY_API_KEY")),
        },
    )


@app.post(
    "/api/audit",
    response_model=AuditSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit("5/minute")
async def audit_video(
    request: Request,
    audit_request: AuditRequest,
    background_tasks: BackgroundTasks,
) -> AuditSubmissionResponse:
    """
    Submit a YouTube URL for compliance auditing.
    Rate-limited to 5 requests/minute per IP.
    Returns immediately with a session_id for polling / streaming.
    """
    session_id    = str(uuid.uuid4())
    video_id_short = f"vid_{session_id[:8]}"

    logger.info(
        "Audit request video=%s session=%s ip=%s",
        audit_request.video_url,
        session_id,
        get_remote_address(request),
    )

    initial_inputs = {
        "video_url":          str(audit_request.video_url),
        "video_id":           video_id_short,
        "compliance_results": [],
        "agent_logs":         [],
        "messages":           [],
        "audio_findings":     [],
        "visual_findings":    [],
        "errors":             [],
        "critic_cycles":      0,
    }

    job = _create_job_record(session_id, video_id_short, str(audit_request.video_url))
    _store_job(job)
    background_tasks.add_task(_run_audit_job, session_id, initial_inputs)

    return AuditSubmissionResponse(
        session_id=session_id,
        video_id=video_id_short,
        job_status="QUEUED",
        final_status="UNKNOWN",
        final_report=job["final_report"],
        status_url=f"/api/audit/{session_id}",
        stream_url=f"/api/audit/{session_id}/stream",
        created_at=job["created_at"],
        updated_at=job["updated_at"],
    )


@app.get("/api/audit/{session_id}", response_model=AuditStatusResponse)
async def get_audit_status(session_id: str) -> AuditStatusResponse:
    job = _get_job(session_id)
    if not job:
        raise HTTPException(status_code=404, detail="Audit session not found.")
    return AuditStatusResponse(**job)


@app.get("/api/audit/{session_id}/stream")
async def stream_audit_logs(session_id: str) -> StreamingResponse:
    """
    Server-Sent Events stream for live agent thought-process updates.

    The frontend connects once via EventSource; this endpoint tails
    the job's agent_logs list and yields new entries as SSE data frames.
    Closes automatically when the job reaches a terminal state.

    Event format:
      data: {"type": "log", "message": "🎙 Audio Agent: ..."}
      data: {"type": "complete", "job_status": "COMPLETED"}
    """
    job = _get_job(session_id)
    if not job:
        raise HTTPException(status_code=404, detail="Audit session not found.")

    async def _event_generator() -> AsyncGenerator[str, None]:
        seen_index = 0
        terminal_states = {"COMPLETED", "FAILED"}

        while True:
            current_job = _get_job(session_id)
            if not current_job:
                yield _sse_event({"type": "error", "message": "Session not found."})
                break

            # Drain new log entries
            logs = current_job.get("agent_logs", [])
            new_logs = logs[seen_index:]
            for line in new_logs:
                yield _sse_event({"type": "log", "message": line})
            seen_index += len(new_logs)

            job_status = current_job.get("job_status", "QUEUED")
            if job_status in terminal_states:
                yield _sse_event({
                    "type": "complete",
                    "job_status": job_status,
                    "final_status": current_job.get("final_status", "UNKNOWN"),
                })
                break

            await asyncio.sleep(1.5)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disables Nginx buffering for SSE
        },
    )


def _sse_event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
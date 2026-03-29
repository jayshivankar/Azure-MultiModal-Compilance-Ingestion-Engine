import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl
from ComplianceQAPipeline.backend.src.graph.workflow import app as compliance_graph
from ComplianceQAPipeline.backend.src.api.telemetry import setup_telemetry


load_dotenv(override=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("brand-guardian-api")

BACKEND_API_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_API_DIR.parents[2]
FRONTEND_DIR = PROJECT_DIR / "frontend"
FRONTEND_ENTRYPOINT = FRONTEND_DIR / "index.html"

DEFAULT_ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

AuditJobState = Literal["QUEUED", "PROCESSING", "COMPLETED", "FAILED"]
AuditResultState = Literal["PASS", "FAIL", "UNKNOWN"]

audit_jobs: dict[str, dict[str, Any]] = {}
audit_jobs_lock = Lock()


def _get_allowed_origins() -> list[str]:
    raw_origins = os.getenv("ALLOWED_ORIGINS", "")
    env_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return env_origins or DEFAULT_ALLOWED_ORIGINS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_job_record(session_id: str, video_id: str, video_url: str) -> dict[str, Any]:
    timestamp = _utc_now()
    return {
        "session_id": session_id,
        "video_id": video_id,
        "video_url": video_url,
        "job_status": "QUEUED",
        "final_status": "UNKNOWN",
        "final_report": "Audit request accepted. Processing will continue in the background.",
        "compliance_results": [],
        "errors": [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _store_job(job_record: dict[str, Any]) -> dict[str, Any]:
    with audit_jobs_lock:
        audit_jobs[job_record["session_id"]] = job_record
        return dict(job_record)


def _update_job(session_id: str, **updates: Any) -> dict[str, Any]:
    with audit_jobs_lock:
        if session_id not in audit_jobs:
            raise KeyError(f"Unknown audit session: {session_id}")
        audit_jobs[session_id].update(updates)
        audit_jobs[session_id]["updated_at"] = _utc_now()
        return dict(audit_jobs[session_id])


def _get_job(session_id: str) -> dict[str, Any] | None:
    with audit_jobs_lock:
        job = audit_jobs.get(session_id)
        return dict(job) if job else None


def _run_audit_job(session_id: str, initial_inputs: dict[str, Any]) -> None:
    logger.info("Starting background audit for session=%s", session_id)
    _update_job(
        session_id,
        job_status="PROCESSING",
        final_report="Audit job is running. The frontend will keep polling for updates.",
    )

    try:
        final_state = compliance_graph.invoke(initial_inputs)
        errors = final_state.get("errors", [])
        final_status = final_state.get("final_status", "UNKNOWN")
        job_status: AuditJobState = "FAILED" if final_status == "FAIL" and errors else "COMPLETED"
        _update_job(
            session_id,
            job_status=job_status,
            video_id=final_state.get("video_id", initial_inputs["video_id"]),
            final_status=final_status,
            final_report=final_state.get("final_report", "No report generated."),
            compliance_results=final_state.get("compliance_results", []),
            errors=errors,
        )
        logger.info("Background audit finished for session=%s with job_status=%s", session_id, job_status)
    except Exception as exc:
        logger.exception("Background audit failed for session=%s", session_id)
        _update_job(
            session_id,
            job_status="FAILED",
            final_status="FAIL",
            final_report="Audit failed before completion. Review the captured system errors.",
            errors=[str(exc)],
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_telemetry()
    logger.info("Brand Guardian API starting up")
    yield
    logger.info("Brand Guardian API shutting down")


app = FastAPI(
    title="Brand Guardian AI",
    description="End-to-end multimodal compliance audit application for brand and regulatory reviews.",
    version="2.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")


class AuditRequest(BaseModel):
    video_url: HttpUrl = Field(..., description="Public YouTube URL to audit.")


class ComplianceIssueResponse(BaseModel):
    category: str
    severity: str
    description: str
    timestamp: str | None = None


class AuditSubmissionResponse(BaseModel):
    session_id: str
    video_id: str
    job_status: AuditJobState
    final_status: AuditResultState
    final_report: str
    status_url: str
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
    errors: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class AppConfigResponse(BaseModel):
    app_name: str
    app_version: str
    environment: str
    allowed_origins: list[str]
    features: dict[str, Any]


@app.get("/", include_in_schema=False)
def serve_frontend() -> FileResponse:
    if not FRONTEND_ENTRYPOINT.exists():
        raise HTTPException(status_code=404, detail="Frontend assets were not found.")
    return FileResponse(FRONTEND_ENTRYPOINT)


@app.get("/api/health")
def health_check() -> dict[str, Any]:
    return {
        "status": "healthy",
        "service": "Brand Guardian AI",
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
            "azure_monitor": bool(os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")),
            "knowledge_base_index": os.getenv("AZURE_SEARCH_INDEX_NAME", "not-configured"),
            "frontend": FRONTEND_ENTRYPOINT.exists(),
            "frontend_path": str(FRONTEND_DIR.relative_to(PROJECT_DIR)) if FRONTEND_DIR.exists() else "missing",
            "audit_mode": "async-polling",
        },
    )


@app.post("/api/audit", response_model=AuditSubmissionResponse, status_code=status.HTTP_202_ACCEPTED)
async def audit_video(request: AuditRequest, background_tasks: BackgroundTasks) -> AuditSubmissionResponse:
    session_id = str(uuid.uuid4())
    video_id_short = f"vid_{session_id[:8]}"

    logger.info("Received audit request for %s (session=%s)", request.video_url, session_id)

    initial_inputs = {
        "video_url": str(request.video_url),
        "video_id": video_id_short,
        "compliance_results": [],
        "errors": [],
    }

    job_record = _create_job_record(session_id, video_id_short, str(request.video_url))
    _store_job(job_record)
    background_tasks.add_task(_run_audit_job, session_id, initial_inputs)

    return AuditSubmissionResponse(
        session_id=session_id,
        video_id=video_id_short,
        job_status="QUEUED",
        final_status="UNKNOWN",
        final_report=job_record["final_report"],
        status_url=f"/api/audit/{session_id}",
        created_at=job_record["created_at"],
        updated_at=job_record["updated_at"],
    )


@app.get("/api/audit/{session_id}", response_model=AuditStatusResponse)
async def get_audit_status(session_id: str) -> AuditStatusResponse:
    job_record = _get_job(session_id)
    if not job_record:
        raise HTTPException(status_code=404, detail="Audit session not found.")
    return AuditStatusResponse(**job_record)
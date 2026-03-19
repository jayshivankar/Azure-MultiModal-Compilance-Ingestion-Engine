import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl
from ComplianceQAPipeline.backend.src.api.telemetry import setup_telemetry 
from ComplianceQAPipeline.backend.src.graph.workflow import app as compliance_graph


# Load environment variables from .env file
load_dotenv(override=True)

# Configure logging
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

def _get_allowed_origins() -> list[str]:
    raw_origins = os.getenv("ALLOWED_ORIGINS", "")
    env_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return env_origins or DEFAULT_ALLOWED_ORIGINS

@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_telemetry()
    logger.info("Brand Guardian API starting up")
    yield
    logger.info("Brand Guardian API shutting down")

# Initialize FastAPI app with lifespan for telemetry setup
app = FastAPI(
    title="Brand Guardian AI",
    description="End-to-end multimodal compliance audit application for brand and regulatory reviews.",
    version="2.1.0",
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

class AuditResponse(BaseModel):
    session_id: str
    video_id: str
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    final_report: str
    compliance_results: list[ComplianceIssueResponse]
    errors: list[str] = Field(default_factory=list)

class AppConfigResponse(BaseModel):
    app_name: str
    app_version: str
    environment: str
    allowed_origins: list[str]
    features: dict[str, Any]


@app.get("/", response_class=FileResponse, include_in_schema=False)
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
        },
    )


@app.post("/api/audit", response_model=AuditResponse)
async def audit_video(request: AuditRequest) -> AuditResponse:
    session_id = str(uuid.uuid4())
    video_id_short = f"vid_{session_id[:8]}"

    logger.info("Received audit request for %s (session=%s)", request.video_url, session_id)

    initial_inputs = {
        "video_url": str(request.video_url),
        "video_id": video_id_short,
        "compliance_results": [],
        "errors": [],
    }
    try:
        final_state = await compliance_graph.ainvoke(initial_inputs)   # changed from invoke to ainvoke for async execution
        return AuditResponse(
            session_id=session_id,
            video_id=final_state.get("video_id", video_id_short),
            status=final_state.get("final_status", "UNKNOWN"),
            final_report=final_state.get("final_report", "No report generated."),
            compliance_results=final_state.get("compliance_results", []),
            errors=final_state.get("errors", []),
        )
    except Exception as exc:
        logger.exception("Audit failed for session=%s", session_id)
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Workflow execution failed.",
                "session_id": session_id,
                "error": str(exc),
            },
        ) from exc
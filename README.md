# Azure MultiModal Compliance Ingestion Engine (Brand Guardian AI)

A production-oriented **multimodal compliance auditing pipeline** that ingests a public YouTube video, extracts transcript + OCR signals with Azure Video Indexer, retrieves policy guidance from a vectorized knowledge base in Azure AI Search, and generates a structured compliance decision with Azure OpenAI.

This repository currently contains a complete end-to-end implementation under `ComplianceQAPipeline/`, including:

* A FastAPI backend (`/api/audit`, `/api/health`, `/api/config`)
* A LangGraph workflow for orchestration (indexing -> auditing)
* Azure Video Indexer integration for media understanding
* RAG-based compliance reasoning using Azure AI Search + Azure OpenAI
* A lightweight frontend dashboard for running audits and reviewing findings
* A document indexing script for building the compliance knowledge base

---

## Table of Contents

* [1) What this project does](#1-what-this-project-does)
* [2) High-level architecture](#2-high-level-architecture)
* [3) Repository layout](#3-repository-layout)
* [4) Core workflow in detail](#4-core-workflow-in-detail)
* [5) API reference](#5-api-reference)
* [6) Configuration](#6-configuration)
* [7) Local setup and run](#7-local-setup-and-run)
* [8) Knowledge base indexing](#8-knowledge-base-indexing)
* [9) Frontend dashboard](#9-frontend-dashboard)
* [10) Troubleshooting](#10-troubleshooting)
* [11) Operational notes](#11-operational-notes)
* [12) Security and responsible use](#12-security-and-responsible-use)
* [13) Roadmap suggestions](#13-roadmap-suggestions)

---

## 1) What this project does

At a practical level, this project answers one question:

> **"Given a video advertisement or influencer content, does it violate known brand/regulatory rules?"**

It does so by combining four stages:

1. **Ingestion**: Download a YouTube video and send it to Azure Video Indexer.
2. **Extraction**: Pull transcript and on-screen text (OCR) from processed video insights.
3. **Retrieval**: Search a compliance knowledge base (PDFs indexed into Azure AI Search).
4. **Reasoning**: Prompt Azure OpenAI to return a structured compliance verdict.

Output includes:

* `PASS` / `FAIL` status
* List of detected compliance issues (`category`, `severity`, `description`, optional `timestamp`)
* Human-readable summary report
* Captured system errors (if any)

---

## 2) High-level architecture

```text
YouTube URL
   |
   v
[VideoIndexerService]
   |- downloads video (yt-dlp)
   |- uploads video to Azure Video Indexer
   |- polls processing status
   |- extracts transcript + OCR + metadata
   v
[LangGraph state]
   v
[Auditor Node]
   |- retrieves top-k policy chunks from Azure AI Search
   |- prompts Azure OpenAI with rules + transcript + OCR
   |- parses strict JSON response
   v
Final audit response (status/report/issues/errors)
   |
   +--> FastAPI endpoint (/api/audit)
   +--> Frontend dashboard render
```

**Workflow:**
`START -> indexer -> auditor -> END`

---

## 3) Repository layout

```text
.
├── README.md
├── pyproject.toml
└── ComplianceQAPipeline/
    ├── main.py
    ├── pyproject.toml
    ├── installAzureCli.sh
    ├── frontend/
    │   ├── index.html
    │   ├── app.js
    │   └── styles.css
    └── backend/
        ├── data/
        ├── scripts/
        │   └── index_documents.py
        └── src/
            ├── api/
            │   ├── server.py
            │   └── telemetry.py
            ├── graph/
            │   ├── state.py
            │   ├── nodes.py
            │   └── workflow.py
            └── services/
                └── video_indexer.py
```

---

## 4) Core workflow in detail

### Node 1 — Indexer

* Downloads YouTube video (`yt-dlp`)
* Uploads to Azure Video Indexer
* Polls processing status
* Extracts transcript + OCR

Returns FAIL if ingestion fails.

---

### Node 2 — Auditor

* Retrieves policy chunks from Azure AI Search
* Sends structured prompt to Azure OpenAI
* Parses strict JSON response

Returns:

* `compliance_results`
* `final_status`
* `final_report`

---

## 5) API reference

### GET `/api/health`

```json
{
  "status": "healthy",
  "service": "Brand Guardian AI",
  "environment": "development"
}
```

---

### GET `/api/config`

Returns runtime configuration.

---

### POST `/api/audit`

**Request**

```json
{
  "video_url": "https://www.youtube.com/watch?v=..."
}
```

**Response includes**

* session_id
* video_id
* status
* final_report
* compliance_results
* errors

---

## 6) Configuration

Create `.env`:

```bash
APP_ENV=development
LOG_LEVEL=INFO
ALLOWED_ORIGINS=http://localhost:8000

AZURE_OPENAI_ENDPOINT=...
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_CHAT_DEPLOYMENT=...

AZURE_SEARCH_ENDPOINT=...
AZURE_SEARCH_API_KEY=...
AZURE_SEARCH_INDEX_NAME=...

AZURE_VI_ACCOUNT_ID=...
AZURE_VI_LOCATION=...
AZURE_SUBSCRIPTION_ID=...
AZURE_RESOURCE_GROUP=...
AZURE_VI_NAME=...
```

Optional:

```bash
APPLICATIONINSIGHTS_CONNECTION_STRING=...
```

---

## 7) Local setup and run

```bash
pip install -e .
uvicorn ComplianceQAPipeline.backend.src.api.server:app --reload
```

Open:

* http://localhost:8000
* http://localhost:8000/docs

---

## 8) Knowledge base indexing

```bash
python ComplianceQAPipeline/backend/scripts/index_documents.py
```

---

## 9) Frontend dashboard

* Health check panel
* Audit submission
* Results + issues display

---

## 10) Troubleshooting

* No transcript → indexing failed
* Slow processing → Video Indexer delay
* Weak results → improve KB docs
* JSON errors → tighten prompt

---

## 11) Operational notes

* Long videos increase latency
* Polling interval: 30s
* Fail-fast design

---

## 12) Security

* Never commit secrets
* Validate content rights
* Use human review for critical decisions

---

## 13) Roadmap

* Async jobs
* DB persistence
* Schema validation
* Multi-source ingestion



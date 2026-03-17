import uuid      
import logging     
from fastapi import FastAPI, HTTPException  


from pydantic import BaseModel  

from typing import List, Optional  



from dotenv import load_dotenv
load_dotenv(override=True)  


# INITIALIZE TELEMETRY
from backend.src.api.telemetry import setup_telemetry
setup_telemetry()  


# IMPORT WORKFLOW GRAPH 
from backend.src.graph.workflow import app as compliance_graph


# CONFIGURE LOGGING 
logging.basicConfig(level=logging.INFO)  


logger = logging.getLogger("api-server")  


# FASTAPI APPLICATION 
app = FastAPI(
    title="Brand Guardian AI API",
    description="API for auditing video content against brand compliance rules.",
    version="1.0.0"
)


# DEFINE DATA MODELS 

class AuditRequest(BaseModel):
    """
    Defines the expected structure of incoming API requests.
    
    Pydantic validates that:
    - The request contains a 'video_url' field
    - The value is a string (not int, list, etc.)
    
    Example valid request:
    {
        "video_url": "https://youtu.be/abc123"
    }
    
    """
    video_url: str 


class ComplianceIssue(BaseModel):
    """
    Defines the structure of a single compliance violation.
    
    Used inside AuditResponse to represent each violation found.
    """
    category: str     
    severity: str      
    description: str   


class AuditResponse(BaseModel):
    """
    Defines the structure of API responses.
    
    FastAPI uses this to:
    1. Validate the response before sending (catches bugs)
    2. Auto-generate API documentation (shows users what to expect)
    3. Provide type hints for frontend developers
    
    Example response:
    {
        "session_id": "ce6c43bb-c71a-4f16-a377-8b493502fee2",
        "video_id": "vid_ce6c43bb",
        "status": "FAIL",
        "final_report": "Video contains 2 critical violations...",
        "compliance_results": [
            {
                "category": "Misleading Claims",
                "severity": "CRITICAL",
                "description": "Absolute guarantee at 00:32"
            }
        ]
    }
    """
    session_id: str                         
    video_id: str                            
    status: str                               
    final_report: str                        
    compliance_results: List[ComplianceIssue] 


# MAIN ENDPOINT 
@app.post("/audit", response_model=AuditResponse)
async def audit_video(request: AuditRequest):
    """
    Process:
    1. Generate unique session ID
    2. Prepare input for LangGraph workflow
    3. Invoke the graph (Indexer → Auditor)
    4. Return formatted results
    """
    
    session_id = str(uuid.uuid4())  
    # Creates unique ID like: "ce6c43bb-c71a-4f16-a377-8b493502fee2"
    
    video_id_short = f"vid_{session_id[:8]}"  
    # Takes first 8 characters: "vid_ce6c43bb"
    
    logger.info(f"Received Audit Request: {request.video_url} (Session: {session_id})")
    # Example output: "Received Audit Request: https://youtu.be/abc (Session: ce6c43bb...)"

    initial_inputs = {
        "video_url": request.video_url,  
        "video_id": video_id_short,      
        "compliance_results": [],        
        "errors": []                     
    }

    try:
        final_state = compliance_graph.invoke(initial_inputs)
        return AuditResponse(
            session_id=session_id,
            video_id=final_state.get("video_id"),  
            status=final_state.get("final_status", "UNKNOWN"),  
            final_report=final_state.get("final_report", "No report generated."),
            compliance_results=final_state.get("compliance_results", [])
        )
        # FastAPI automatically converts this Pydantic object to JSON

    except Exception as e:
        logger.error(f"Audit Failed: {str(e)}")  
        raise HTTPException(
            status_code=500, 
            detail=f"Workflow Execution Failed: {str(e)}"
        )
       

#  HEALTH CHECK ENDPOINT 
@app.get("/health")
def health_check():
    """
    Simple endpoint to verify the API is running.
    
    """
    return {"status": "healthy", "service": "Brand Guardian AI"}

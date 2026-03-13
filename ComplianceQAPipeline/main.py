"""
Main Execution Entry Point for Brand Guardian AI.

This file is the "control center" that starts and manages the entire 
compliance audit workflow. Think of it as the master switch that:
1. Sets up the audit request
2. Runs the AI workflow
3. Displays the final compliance report
"""

# Standard library imports for basic Python functionality
import uuid      
import json      
import logging  
from dotenv import load_dotenv

load_dotenv(override=True)  # override=True means .env values take priority over system variables

# Import the main workflow graph 
from backend.src.graph.workflow import app

# Configure logging 
logging.basicConfig(
    level=logging.INFO,        
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'  
  
)
logger = logging.getLogger("brand-guardian-runner")


def run_cli_simulation():
    """
    Simulates a Video Compliance Audit request.
    
    This function orchestrates the entire audit process:
    - Creates a unique session ID
    - Prepares the video URL and metadata
    - Runs it through the AI workflow
    - Displays the compliance results
    """
    
    # STEP 1: GENERATE SESSION ID 
    # Creates a unique identifier for this audit session
    
    session_id = str(uuid.uuid4())  
    logger.info(f"Starting Audit Session: {session_id}") 

    # STEP 2: DEFINE INITIAL STATE
    
    initial_inputs = {
        "video_url": "https://youtu.be/dT7S75eYhcQ",
                
        "video_id": f"vid_{session_id[:8]}",   # Example: "vid_ce6c43bb"
        
        "compliance_results": [],
    
        "errors": []
    }

    #  DISPLAY SECTION: INPUT SUMMARY 
    print("\n--- 1.nput Payload: INITIALIZING WORKFLOW ---")
    
    print(f"I {json.dumps(initial_inputs, indent=2)}")

    # STEP 3: EXECUTE GRAPH 
    # runs the entire workflow
    try:
        
        final_state = app.invoke(initial_inputs)
        
        print("\n--- 2. WORKFLOW EXECUTION COMPLETE ---")
        
        # Display a formatted compliance report
        
        print("\n=== COMPLIANCE AUDIT REPORT ===")
        
        
        # Displays the video ID that was audited
        print(f"Video ID:    {final_state.get('video_id')}")
        
        # Shows PASS or FAIL status
        print(f"Status:      {final_state.get('final_status')}")
        
        # VIOLATIONS SECTION 
        print("\n[ VIOLATIONS DETECTED ]")
        
        # Extract the list of compliance violations
        # Default to empty list if no results
        results = final_state.get('compliance_results', [])
        
        if results:
            # Loop through each violation and display it
            for issue in results:
                # Each issue is a dict with: severity, category, description
                # Example output: "- [CRITICAL] Misleading Claims: Absolute guarantee detected"
                print(f"- [{issue.get('severity')}] {issue.get('category')}: {issue.get('description')}")
        else:
            # No violations found (clean video)
            print("No violations found.")

        #  SUMMARY SECTION 
        print("\n[ FINAL SUMMARY ]")
        
        print(final_state.get('final_report'))

    except Exception as e:
        logger.error(f"Workflow Execution Failed: {str(e)}")
        raise e



if __name__ == "__main__":
    run_cli_simulation() 



'''
Ingestion:  (YouTube -> Azure)

Indexing:  (Speech-to-Text + OCR)

Retrieval:  (Found the rules about "Claims")

Reasoning:  (Applied rules to the specific claims in the video)
'''




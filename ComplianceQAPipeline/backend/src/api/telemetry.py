import os           
import logging      
from azure.monitor.opentelemetry import configure_azure_monitor  

logger = logging.getLogger("brand-guardian-telemetry")
# Example log output: "brand-guardian-telemetry - INFO - Azure Monitor enabled"


def setup_telemetry():
    """
    Initializes Azure Monitor OpenTelemetry.
    
    What does "hooks into FastAPI automatically" mean?
    - Once configured, it auto-captures every API request/response
    - No need to manually log each endpoint
    - Tracks response times, error rates, dependencies (like Azure Search calls)
    """
    
    #  RETRIEVE CONNECTION STRING 
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    
    # STEP 2: CHECK IF CONFIGURED 
    if not connection_string:
        logger.warning("No Instrumentation Key found. Telemetry is DISABLED.")
        return 
    
    #  CONFIGURE AZURE MONITOR
    try:
        configure_azure_monitor(
            connection_string=connection_string, 
            logger_name="brand-guardian-tracer"  
        )
        logger.info(" Azure Monitor Tracking Enabled & Connected!")
        
    except Exception as e:
        logger.error(f"Failed to initialize Azure Monitor: {e}")
        
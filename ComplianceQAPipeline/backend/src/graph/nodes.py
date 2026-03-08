# ---- Installing necessary libraries ----
import os
import logging 
import json
import re

from langchain_core.prompts import ChatPromptTemplate
from langchain_community.vectorstores import AzureSearch
from langchain_openai import AzureOpenAIEmbeddings,AzureChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from backend.src.graph.state import *
from backend.src.services.video_indexer import *

# -- setting up logger for the graph nodes --
logger = logging.getLogger('ComplianceQAPipeline')
logging.basicConfig(level = logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 1st node: Video indexer
def video_index_node(state:VideoAuditState) -> Dict[str,Any]:
    """ Ingests the video from the url, stores it in blob storage
           Extracts data from the video using Azure Video Indexer and updates the state with the results. """
    video_url = state.get('video_url')
    video_id_input = state.get('video_id','vid_demo')

    logger.info(f"--- Node : Video Indexer is querying the video url {video_url} ")

    local_filename = "youtube_video.mp4"
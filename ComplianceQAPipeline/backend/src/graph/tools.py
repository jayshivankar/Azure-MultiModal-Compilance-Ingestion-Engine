"""
Agentic Tools — Brand Guardian AI

Two tools are registered here for use by the Audio and Visual sub-agents:

  1. search_azure_knowledge_base
     Performs semantic (vector) similarity search against the Azure AI
     Search index that contains your brand and regulatory PDF guidelines.

  2. search_public_web
     Falls back to a Tavily web search when the internal knowledge base
     doesn't contain a definitive ruling (e.g. on new slang, recent FTC
     guidance updates, or competitor brand assets).

Both tools are decorated with @tool so they can be bound to any
LangChain-compatible LLM via `llm.bind_tools([...])`.
"""

import logging
import os
from typing import Annotated

from langchain_core.tools import tool
from langchain_openai import AzureOpenAIEmbeddings
from langchain_community.vectorstores import AzureSearch
from tavily import TavilyClient

logger = logging.getLogger("brand-guardian-tools")


# ---------------------------------------------------------------------------
# Helpers (lazy-initialised singletons)
# ---------------------------------------------------------------------------

_vector_store: AzureSearch | None = None
_tavily_client: TavilyClient | None = None


def _get_vector_store() -> AzureSearch:
    """Returns a cached AzureSearch vector store instance."""
    global _vector_store
    if _vector_store is None:
        embeddings = AzureOpenAIEmbeddings(
            azure_deployment=os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"),
            openai_api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
        )
        _vector_store = AzureSearch(
            azure_search_endpoint=os.getenv("AZURE_SEARCH_ENDPOINT"),
            azure_search_key=os.getenv("AZURE_SEARCH_API_KEY"),
            index_name=os.getenv("AZURE_SEARCH_INDEX_NAME"),
            embedding_function=embeddings.embed_query,
        )
        logger.info("[Tools] Azure AI Search vector store initialised.")
    return _vector_store


def _get_tavily() -> TavilyClient:
    """Returns a cached Tavily client."""
    global _tavily_client
    if _tavily_client is None:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "TAVILY_API_KEY is not set. Add it to your .env file."
            )
        _tavily_client = TavilyClient(api_key=api_key)
        logger.info("[Tools] Tavily client initialised.")
    return _tavily_client


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@tool
def search_azure_knowledge_base(
    query: Annotated[str, "Natural-language search query to look up brand or regulatory guidelines."],
) -> str:
    """
    Search the internal brand/regulatory knowledge base stored in Azure AI Search.

    Use this tool FIRST when you need to verify whether specific content
    (audio or visual) violates official brand guidelines, FTC rules, or
    any other policy documents in the enterprise knowledge base.

    Returns the top-3 most relevant policy passages as a single string.
    """
    try:
        vs = _get_vector_store()
        docs = vs.similarity_search(query, k=3)
        if not docs:
            return "No relevant policy documents found in the knowledge base for this query."
        passages = [f"[Policy {i+1}]\n{doc.page_content}" for i, doc in enumerate(docs)]
        return "\n\n".join(passages)
    except Exception as e:
        logger.error(f"[Tools] Azure Search error: {e}")
        return f"Knowledge base lookup failed: {str(e)}"


@tool
def search_public_web(
    query: Annotated[str, "Public web search query for compliance verification or background research."],
) -> str:
    """
    Search the public internet via Tavily to supplement the internal knowledge base.

    Use this tool ONLY when:
      • The internal knowledge base does not contain an authoritative answer.
      • You need to verify recent regulatory updates (e.g. new FTC guidance).
      • You need context on slang, cultural references, or competitor marks
        not covered by existing policy documents.

    Returns a concise AI-generated answer with source citations.
    """
    try:
        client = _get_tavily()
        result = client.search(
            query=query,
            search_depth="basic",
            max_results=3,
            include_answer=True,
        )
        answer = result.get("answer", "")
        sources = result.get("results", [])
        source_lines = [
            f"• {s.get('title', 'Untitled')} — {s.get('url', '')}"
            for s in sources[:3]
        ]
        output = f"**Summary:** {answer}\n\n**Sources:**\n" + "\n".join(source_lines)
        return output
    except Exception as e:
        logger.error(f"[Tools] Tavily search error: {e}")
        return f"Web search failed: {str(e)}"


# Expose as a list for easy binding: llm.bind_tools(COMPLIANCE_TOOLS)
COMPLIANCE_TOOLS = [search_azure_knowledge_base, search_public_web]

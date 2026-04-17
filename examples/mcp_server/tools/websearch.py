
from typing import List
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_chroma import Chroma
from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.chat_models import ChatOllama
from langchain_community.document_loaders import PyPDFDirectoryLoader
from langchain_openai import ChatOpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter
from openai import api_key
from tavily import TavilyClient

from langsmith import Client, tracing_context, traceable
import os
import logging
from dotenv import load_dotenv

load_dotenv()

tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
logger = logging.getLogger(__name__)

@tool
@traceable(name="web_search_fn")
def web_search(query: str) -> str:

    """Search the web for recent information."""
    logger.info("web_search invoke %s", query)
    print(f"[PRINT] web_search called with query={query}")
    results = tavily.search(query=query, max_results=5)
    items = results.get("results", [])
    if not items:
        return "No relevant web results found."

    answer= "\n\n".join(
        f"Title: {item.get('title')}\n"
        f"URL: {item.get('url')}\n"
        f"Content: {item.get('content')}"
        for item in items
    )
    logger.info("web_search answer %s", answer)
    return answer



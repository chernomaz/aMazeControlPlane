import logging
import os

from dotenv import load_dotenv
from langchain_core.tools import tool
from langsmith import traceable
from tavily import TavilyClient

load_dotenv()

logger = logging.getLogger(__name__)


def _tavily() -> TavilyClient:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY is not set")
    return TavilyClient(api_key=key)


@tool
@traceable(name="web_search_fn")
def web_search(query: str) -> str:
    """Search the web for recent information."""
    logger.info("web_search invoke %s", query)
    print(f"[PRINT] web_search called with query={query}")
    results = _tavily().search(query=query, max_results=5)
    items = results.get("results", [])
    if not items:
        return "No relevant web results found."

    answer = "\n\n".join(
        f"Title: {item.get('title')}\n"
        f"URL: {item.get('url')}\n"
        f"Content: {item.get('content')}"
        for item in items
    )
    logger.info("web_search answer %s", answer)
    return answer

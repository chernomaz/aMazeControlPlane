import os
import sys
import logging
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
load_dotenv()

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langsmith import traceable, tracing_context
from tavily import TavilyClient

logger = logging.getLogger(__name__)


# ---------- LLM ----------

llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY"),
)

# ---------- Agent ----------

def main():
    # Accept prompt from env var, CLI arg (3rd positional after script + policy), or stdin
    prompt = os.environ.get("AGENT_PROMPT", "").strip()
    if not prompt and len(sys.argv) > 3:
        prompt = sys.argv[3].strip()
    if not prompt:
        prompt = input("You: ").strip()
    if prompt.lower() in {"exit", "quit"}:
        return

    agent = create_agent(
        model=llm,
        tools=[pdf_search, web_search, dummy_email, file_read],
        system_prompt=(
            "You are a helpful research assistant. "
            "Use pdf_search for questions about local PDF documents. "
            "Use web_search for current or external information. "
            "Use dummy_email to retrieve emails for alice, bob, or carol. "
            "Use file_read to read the contents of a local file by path. "
            "Always cite which tool and source you used."
        ),
    )

    with tracing_context(enabled=True):
        result = agent.invoke({"messages": [{"role": "user", "content": prompt}]})

    print("\nAssistant:")
    print(result["messages"][-1].content)
    print()


if __name__ == "__main__":
    main()

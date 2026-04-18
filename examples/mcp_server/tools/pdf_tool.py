
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

DATA_DIR = os.getenv("DATA_DIR", "/home/ubuntu/data/learn/pdf_reader/")
CHROMA_DIR = os.getenv("CHROMA_DIR", "/home/ubuntu/data/learn/chroma_db/")
COLLECTION_NAME = "pdf_docs"
logger = logging.getLogger(__name__)



def load_pdf_documents(data_dir: str):
    loader = PyPDFDirectoryLoader(data_dir, recursive=True)
    return loader.load()


def split_documents(documents):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=150
    )
    return splitter.split_documents(documents)


def get_embeddings():
    return OllamaEmbeddings(model="nomic-embed-text")
    #return OpenAIEmbeddings(model="text-embedding-3-large")


def build_vectorstore_once() -> Chroma:
    embeddings = get_embeddings()

    vectorstore = Chroma(
        collection_name=COLLECTION_NAME,
        persist_directory=CHROMA_DIR,
        embedding_function=embeddings,
    )

    # Check whether collection already has documents
    existing = vectorstore.get(limit=1)

    if existing["ids"]:
        print("Using existing Chroma index...")
        return vectorstore

    print("No existing index found. Building vector store from PDFs...")

    docs = load_pdf_documents(DATA_DIR)

    for doc in docs:
        doc.metadata["tenant_id"] = "companyA"   # optional custom metadata

    chunks = split_documents(docs)
    vectorstore.add_documents(chunks)

    print("Vector store created successfully.")
    return vectorstore
# ---------- Build / load PDF vector store ----------



_vectorstore = None
_retriever = None


def _get_retriever():
    global _vectorstore, _retriever
    if _retriever is None:
        _vectorstore = build_vectorstore_once()
        _retriever = _vectorstore.as_retriever(search_kwargs={"k": 4})
    return _retriever


# ---------- Tools ----------
@tool
@traceable(name="pdf_search")
def pdf_search(query: str) -> str:
    """Search the local PDF collection and return relevant passages with sources."""
    logger.info("pdf_search invoke %s", query)
    docs = _get_retriever().invoke(query)
    if not docs:
        return "No relevant PDF content found."

    parts: List[str] = []
    for d in docs:
        src = d.metadata.get("source", "unknown")
        page = d.metadata.get("page", "unknown")
        parts.append(
            f"Source: {src}, page: {page}\n"
            f"Content: {d.page_content}"
        )
    answer= "\n\n".join(parts)
    logger.info("pdf_search found %s", answer)
    return answer



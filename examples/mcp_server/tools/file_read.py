import os
import logging
from langchain_core.tools import tool
from langsmith import traceable

logger = logging.getLogger(__name__)


@tool
@traceable(name="file_read")
def file_read(path: str) -> str:
    """Read the contents of a local file and return it as a string."""
    logger.info("file_read invoke path=%s", path)
    if not os.path.exists(path):
        return f"Error: file not found: {path}"
    if not os.path.isfile(path):
        return f"Error: path is not a file: {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        logger.info("file_read read %d chars from %s", len(content), path)
        return content
    except Exception as e:
        logger.error("file_read error: %s", e)
        return f"Error reading file: {e}"

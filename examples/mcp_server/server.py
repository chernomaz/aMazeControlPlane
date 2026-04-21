import inspect
import importlib.util
import logging
import os
from pathlib import Path

from fastmcp import FastMCP
from langchain_core.tools import BaseTool

mcp = FastMCP("demo-mcp")

BASE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = BASE_DIR / "tools"


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] trace_id=%(trace_id)s %(message)s",
    )


class TraceIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "trace_id"):
            record.trace_id = "-"
        return True


setup_logging()
for h in logging.getLogger().handlers:
    h.addFilter(TraceIdFilter())

logger = logging.getLogger("mcp_server")


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to create spec for {path}")

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def discover_tools(mod):
    tools = []
    for name, obj in inspect.getmembers(mod):
        if isinstance(obj, BaseTool):
            logger.info(
                "Discovered tool object: %s (%s)",
                name,
                type(obj).__name__,
                extra={"trace_id": "-"},
            )
            tools.append(obj)
    return tools


def register_langchain_tool(tool: BaseTool):
    if getattr(tool, "coroutine", None):
        fn = tool.coroutine
    elif getattr(tool, "func", None):
        fn = tool.func
    else:
        raise ValueError(f"Tool {tool.name} has no callable")

    logger.info(
        "Registering tool %s -> function %s",
        tool.name,
        fn.__name__,
        extra={"trace_id": "-"},
    )

    mcp.add_tool(fn)


def load_tools():
    logger.info("Looking for tools in %s", TOOLS_DIR, extra={"trace_id": "-"})
    logger.info("Tools dir exists=%s", TOOLS_DIR.exists(), extra={"trace_id": "-"})

    files = [f for f in TOOLS_DIR.glob("*.py") if f.name != "__init__.py"]
    logger.info("Found %d python files", len(files), extra={"trace_id": "-"})

    for f in files:
        logger.info("Loading module from %s", f, extra={"trace_id": "-"})
        try:
            mod = load_module(f)
        except Exception as exc:
            logger.warning(
                "Skipping %s — failed to load: %s",
                f.name,
                exc,
                extra={"trace_id": "-"},
            )
            continue

        tools = discover_tools(mod)

        if not tools:
            logger.warning("No tools found in %s", f.name, extra={"trace_id": "-"})

        for t in tools:
            try:
                register_langchain_tool(t)
            except Exception as exc:
                logger.warning(
                    "Skipping tool %s — registration failed: %s",
                    t.name,
                    exc,
                    extra={"trace_id": "-"},
                )


load_tools()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info("Starting MCP server on port %d", port, extra={"trace_id": "-"})
    mcp.run(transport="streamable-http", host="127.0.0.1", port=port)

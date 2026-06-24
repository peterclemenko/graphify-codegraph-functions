import logging
from typing import Any, Dict, List, Optional
from mcp.server.fastmcp import FastMCP
from .core.handlers import CodeGraphHandlers
from .core.telemetry import trace_async

logger = logging.getLogger(__name__)

mcp = FastMCP("gitnexus-sidecar")
_handlers: Optional[CodeGraphHandlers] = None

def init_mcp_handlers(handlers_instance: CodeGraphHandlers) -> None:
    """Initialize FastMCP with the shared handlers instance."""
    global _handlers
    _handlers = handlers_instance
    logger.info("FastMCP handlers successfully initialized.")

@mcp.tool()
@trace_async("mcp.tool.gitnexus_impact")
async def gitnexus_impact(symbol_name: str, max_depth: int = 3) -> Dict[str, Any]:
    """Evaluate the dependency impact (blast radius) of a given symbol."""
    if not _handlers:
        return {"error": "Handlers context not initialized."}
    return await _handlers.handle_impact(symbol_name, max_depth)

@mcp.tool()
@trace_async("mcp.tool.gitnexus_detect_changes")
async def gitnexus_detect_changes(diffs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve changes in workspace files using LSP and report structural impact."""
    if not _handlers:
        return {"error": "Handlers context not initialized."}
    return await _handlers.handle_detect_changes(diffs)

@mcp.tool()
@trace_async("mcp.tool.gitnexus_context")
async def gitnexus_context(symbol_name: str) -> Dict[str, Any]:
    """Retrieve local database context and neighbors around a symbol."""
    if not _handlers:
        return {"error": "Handlers context not initialized."}
    return await _handlers.handle_context(symbol_name)

@mcp.tool()
@trace_async("mcp.tool.gitnexus_query")
async def gitnexus_query(query: str, parameters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Run a raw Cypher query against the active graph database."""
    if not _handlers:
        return []
    return await _handlers.execute_raw_query(query, parameters)

@mcp.tool()
@trace_async("mcp.tool.gitnexus_rename")
async def gitnexus_rename(uri: str, line: int, character: int, new_name: str) -> Dict[str, Any]:
    """Rename a symbol in the project files via LSP and update database schema."""
    if not _handlers:
        return {"error": "Handlers context not initialized."}
    return await _handlers.handle_rename(uri, line, character, new_name)

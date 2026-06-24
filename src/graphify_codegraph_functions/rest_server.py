import logging
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Depends, status
from pydantic import BaseModel, Field
from .core.handlers import CodeGraphHandlers

logger = logging.getLogger(__name__)

# Pydantic Request Models
class ImpactRequest(BaseModel):
    symbol_name: str = Field(..., min_length=1, description="Name of the symbol to evaluate")
    max_depth: int = Field(3, ge=1, le=10, description="Max depth of the dependency graph traversal")

class DiffItem(BaseModel):
    uri: str = Field(..., description="The document uri / path")
    content: str = Field(..., description="The complete text content of the file")
    line: Optional[int] = Field(None, ge=0, description="Optional line position for precise hover resolving")
    character: Optional[int] = Field(None, ge=0, description="Optional character position for precise hover resolving")

class DetectChangesRequest(BaseModel):
    diffs: List[DiffItem] = Field(..., description="List of modified files and their contents")

class RenameRequest(BaseModel):
    uri: str = Field(..., description="URI of the file containing the symbol")
    line: int = Field(..., ge=0, description="Line number (0-indexed)")
    character: int = Field(..., ge=0, description="Character index (0-indexed)")
    new_name: str = Field(..., min_length=1, description="The new symbol name")

class ContextRequest(BaseModel):
    symbol_name: str = Field(..., min_length=1, description="The target symbol name")


app = FastAPI(
    title="Graphify CodeGraph REST Sidecar",
    description="High-performance event-driven dependency graph sidecar service.",
    version="1.0.0"
)

def get_handlers() -> CodeGraphHandlers:
    if not hasattr(app.state, "handlers") or app.state.handlers is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Application handlers context is not fully initialized."
        )
    return app.state.handlers


@app.post("/api/v1/impact", status_code=status.HTTP_200_OK)
async def post_impact(payload: ImpactRequest, handlers: CodeGraphHandlers = Depends(get_handlers)):
    """Evaluate the blast radius / dependency impact of a given code symbol."""
    try:
        result = await handlers.handle_impact(payload.symbol_name, payload.max_depth)
        return result
    except Exception as e:
        logger.exception("Error in POST /api/v1/impact")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while computing the symbol impact."
        )


@app.post("/api/v1/detect-changes", status_code=status.HTTP_200_OK)
async def post_detect_changes(payload: DetectChangesRequest, handlers: CodeGraphHandlers = Depends(get_handlers)):
    """Resolve changes in workspace files using LSP and report structural impact."""
    try:
        diff_dicts = [
            {
                "uri": item.uri,
                "content": item.content,
                "line": item.line,
                "character": item.character
            } for item in payload.diffs
        ]
        result = await handlers.handle_detect_changes(diff_dicts)
        return result
    except Exception as e:
        logger.exception("Error in POST /api/v1/detect-changes")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred during structural change detection."
        )


@app.post("/api/v1/rename", status_code=status.HTTP_200_OK)
async def post_rename(payload: RenameRequest, handlers: CodeGraphHandlers = Depends(get_handlers)):
    """Perform symbol renaming using LSP and update database schemas."""
    try:
        result = await handlers.handle_rename(payload.uri, payload.line, payload.character, payload.new_name)
        return result
    except Exception as e:
        logger.exception("Error in POST /api/v1/rename")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred during symbol renaming."
        )


@app.post("/api/v1/context", status_code=status.HTTP_200_OK)
async def post_context(payload: ContextRequest, handlers: CodeGraphHandlers = Depends(get_handlers)):
    """Retrieve database attributes, direct connections, and process root ancestors."""
    try:
        result = await handlers.handle_context(payload.symbol_name)
        return result
    except Exception as e:
        logger.exception("Error in POST /api/v1/context")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while fetching symbol context."
        )

# Instrument FastAPI app if package is present
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app)
    logger.info("FastAPI application auto-instrumented with OpenTelemetry.")
except ImportError:
    logger.warning("opentelemetry-instrumentation-fastapi not installed. Skipping auto-instrumentation.")


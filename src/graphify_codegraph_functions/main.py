import asyncio
import logging
import os
import sys
from typing import List, Optional

import uvicorn
from uvicorn.config import Config
from uvicorn.server import Server

# Import application components
from .database.neo4j_impl import Neo4jDatabase
from .database.falkor_impl import FalkorDatabase
from .lsp.client import LSPClient
from .lsp.resolver import LSPResolver
from .core.handlers import CodeGraphHandlers
from .rest_server import app as fastapi_app
from .nats_client import NatsClient
from .mcp_server import mcp, init_mcp_handlers
from .core.config import load_config

# Configure logging to stderr to prevent polluting stdout (used by FastMCP stdio protocol)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("graphify.main")

async def run_fastapi(host: str, port: int) -> None:
    """Run the FastAPI web application server asynchronously."""
    config = Config(
        app=fastapi_app,
        host=host,
        port=port,
        log_level="info",
        log_config=None,
        use_colors=False
    )
    server = Server(config)
    logger.info(f"Starting FastAPI server on {host}:{port}")
    await server.serve()

async def run_mcp() -> None:
    """Run the FastMCP server using stdio transport."""
    logger.info("Starting FastMCP stdio server")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, mcp.run)

async def main() -> None:
    logger.info("Bootstrapping Graphify CodeGraph sidecar context...")

    # 1. Load Configurations (config.json + env vars)
    config = load_config()
    graph_engine = config["graph_engine"]
    neo4j_uri = config["neo4j_uri"]
    neo4j_user = config["neo4j_user"]
    neo4j_password = config["neo4j_password"]
    
    falkordb_host = config["falkordb_host"]
    falkordb_port = config["falkordb_port"]
    falkordb_graph = config["falkordb_graph"]
    falkordb_password = config["falkordb_password"]

    nats_url = config["nats_url"]
    nats_servers: List[str] = [s.strip() for s in nats_url.split(",") if s.strip()]

    # LSP configuration for multilspy
    lsp_language = config["lsp_language"]
    lsp_root_path = config["lsp_root_path"]

    # FastAPI settings
    api_host = config["api_host"]
    api_port = config["api_port"]

    # 2. Initialize Database Layer
    db = None
    try:
        if graph_engine == "neo4j":
            logger.info("Initializing Neo4j database client...")
            db = Neo4jDatabase(uri=neo4j_uri, user=neo4j_user, secret=neo4j_password)
            await db.connect()
        elif graph_engine == "falkordb":
            logger.info("Initializing FalkorDB client...")
            db = FalkorDatabase(host=falkordb_host, port=falkordb_port, graph_name=falkordb_graph, password=falkordb_password)
            await db.connect()
        else:
            logger.warning(f"Unsupported GRAPH_ENGINE '{graph_engine}'. Initializing without database.")
    except Exception as e:
        logger.warning(f"Failed to connect to database. Falling back to local standalone mode: {e}")
        db = None

    # 3. Initialize LSP Layer
    lsp_resolver = None
    try:
        logger.info(f"Initializing multilspy client for language: {lsp_language} in {lsp_root_path}")
        lsp_client = LSPClient(language=lsp_language, project_path=lsp_root_path)
        await lsp_client.start()
        lsp_resolver = LSPResolver(client=lsp_client)
    except Exception as e:
        logger.warning(f"Failed to initialize LSP client. Operating without LSP resolution: {e}")

    # 4. Instantiate Shared Handlers
    handlers = CodeGraphHandlers(db=db, lsp=lsp_resolver)

    # 5. Inject Handlers into Interfaces
    fastapi_app.state.handlers = handlers
    init_mcp_handlers(handlers)

    # 6. Initialize NATS Client
    nats_client = NatsClient(servers=nats_servers, handlers=handlers)
    await nats_client.connect()

    # 7. Initialize Incremental Sync Watchdog
    from .core.sync import IncrementalSyncManager
    sync_manager = IncrementalSyncManager(db=db, lsp=lsp_resolver)

    # 8. Concurrently Execute API, NATS, MCP, and local filesystem Watchdog
    try:
        await asyncio.gather(
            run_fastapi(api_host, api_port),
            run_mcp(),
            sync_manager.run_local_watchdog()
        )
    except asyncio.CancelledError:
        logger.info("Services execution cancelled. Cleaning up context...")
    finally:
        # Gracefully shut down active connections
        if nats_client:
            await nats_client.close()
        if lsp_resolver and lsp_resolver.client:
            await lsp_resolver.client.stop()
        if db:
            await db.close()
        logger.info("Graphify Sidecar services successfully terminated.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process interrupted by user.")

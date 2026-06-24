import asyncio
import logging
from typing import Any

from falkordb.asyncio import FalkorDB as AsyncFalkorDB

from .base import GraphDatabaseBridge, GraphDatabaseError

logger = logging.getLogger(__name__)

class FalkorDBDatabaseBridge(GraphDatabaseBridge):
    """FalkorDB graph database implementation of the GraphDatabaseBridge protocol."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        graph_id: str = "codegraph",
        graph_name: str | None = None,
        password: str | None = None,
    ) -> None:
        """Initialize the FalkorDB database bridge.

        Args:
            host: The Redis/FalkorDB server host.
            port: The Redis/FalkorDB server port.
            graph_id: Target graph identifier.
            graph_name: Alias for graph_id to preserve backward compatibility.
            password: Optional Redis authentication password.
        """
        self.host = host
        self.port = port
        self.graph_id = graph_name or graph_id
        self.password = password
        self.db: AsyncFalkorDB | None = None
        self.graph: Any = None

    async def connect(self) -> None:
        """Establish connection and select the target graph asynchronously."""
        try:
            self.db = AsyncFalkorDB(
                host=self.host,
                port=self.port,
                password=self.password
            )
            # Support both synchronous and asynchronous select_graph depending on falkordb-py version
            select_res = self.db.select_graph(self.graph_id)
            if hasattr(select_res, "__await__") or asyncio.iscoroutine(select_res):
                self.graph = await select_res
            else:
                self.graph = select_res
                
            # Verify connectivity by pinging the database connection
            if hasattr(self.db, "connection") and hasattr(self.db.connection, "ping"):
                ping_res = self.db.connection.ping()
                if hasattr(ping_res, "__await__") or asyncio.iscoroutine(ping_res):
                    await ping_res
            logger.info(f"Successfully connected to FalkorDB at {self.host}:{self.port} (Graph: {self.graph_id})")
        except Exception as e:
            logger.error(f"Failed to connect to FalkorDB at {self.host}:{self.port}: {e}")
            raise GraphDatabaseError(f"FalkorDB connection failed: {e}") from e

    async def close(self) -> None:
        """Close connection to FalkorDB database."""
        if self.db:
            try:
                if hasattr(self.db, "connection") and hasattr(self.db.connection, "close"):
                    close_res = self.db.connection.close()
                    if hasattr(close_res, "__await__") or asyncio.iscoroutine(close_res):
                        await close_res
                elif hasattr(self.db, "close"):
                    close_res = self.db.close()
                    if hasattr(close_res, "__await__") or asyncio.iscoroutine(close_res):
                        await close_res
                logger.info("FalkorDB connection closed.")
            except Exception as e:
                logger.warning(f"Error closing FalkorDB connection: {e}")

    async def execute_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute an openCypher query asynchronously and parse results into Neo4j format.

        Args:
            query: The openCypher query string to execute.
            params: Optional query parameters to bind.

        Returns:
            A list of dictionary records mapping header names to row values.

        Raises:
            GraphDatabaseError: Wrapped engine query exception.
        """
        if not self.graph:
            raise GraphDatabaseError("FalkorDB graph client is not connected.")

        from ..core.telemetry import tracer
        from opentelemetry.trace import StatusCode

        with tracer.start_as_current_span("falkordb.query") as span:
            span.set_attribute("db.system", "falkordb")
            span.set_attribute("db.statement", query)
            if params:
                span.set_attribute("db.params", str(params))

            try:
                # Execute query on the async graph object
                result = await self.graph.query(query, params or {})
                
                records = []
                if hasattr(result, "result_set") and result.result_set:
                    headers = result.header if hasattr(result, "header") else []
                    for row in result.result_set:
                        record = {}
                        for i, val in enumerate(row):
                            header = headers[i] if i < len(headers) else f"col_{i}"
                            record[header] = self._parse_value(val)
                        records.append(record)
                        
                span.set_status(StatusCode.OK)
                return records
            except Exception as e:
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
                logger.error(f"FalkorDB query failed: {e}")
                raise GraphDatabaseError(f"FalkorDB query execution failed: {e}") from e

    def _parse_value(self, val: Any) -> Any:
        """Recursively parses a value returning Neo4j-compatible property representation."""
        if hasattr(val, "properties"):
            return val.properties
        if hasattr(val, "relation_properties"):
            return val.relation_properties
        return val

# Alias for backwards compatibility with existing code
FalkorDatabase = FalkorDBDatabaseBridge

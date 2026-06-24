import logging
from typing import Any

from neo4j import AsyncGraphDatabase
from neo4j.exceptions import Neo4jError

from .base import GraphDatabaseBridge, GraphDatabaseError

logger = logging.getLogger(__name__)

class Neo4jDatabaseBridge(GraphDatabaseBridge):
    """Neo4j graph database wrapper implementing the GraphDatabaseBridge protocol."""

    def __init__(self, uri: str, user: str, password: str | None = None, secret: str | None = None) -> None:
        """Initialize the Neo4j database bridge and construct the async driver.

        Args:
            uri: The Neo4j connection URI (e.g., bolt://localhost:7687).
            user: The database username.
            password: The database password.
            secret: Alias for password to maintain backward compatibility.
        """
        self.uri = uri
        self.user = user
        self.password = password or secret or ""
        
        # Construct the official async driver instance
        self.driver = AsyncGraphDatabase.driver(
            self.uri,
            auth=(self.user, self.password)
        )

    async def connect(self) -> None:
        """Verify connectivity to the Neo4j database."""
        try:
            await self.driver.verify_connectivity()
            logger.info("Successfully connected to Neo4j database and verified connectivity.")
        except Neo4jError as e:
            logger.error(f"Failed to verify connectivity to Neo4j at {self.uri}: {e}")
            raise GraphDatabaseError(f"Failed to connect to Neo4j: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected connection error to Neo4j at {self.uri}: {e}")
            raise GraphDatabaseError(f"Unexpected error connecting to Neo4j: {e}") from e

    async def close(self) -> None:
        """Close the async driver instance and release resources."""
        if self.driver:
            await self.driver.close()
            logger.info("Neo4j database connection closed.")

    async def execute_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute an openCypher query asynchronously using a read or write transaction.

        Args:
            query: The openCypher query string to execute.
            params: Optional query parameters to bind.

        Returns:
            A list of dictionaries representing the query records.

        Raises:
            GraphDatabaseError: Wrapped exception for database or driver failures.
        """
        from ..core.telemetry import tracer
        from opentelemetry.trace import StatusCode

        # Determine transaction type based on mutations
        query_upper = query.upper()
        write_keywords = {"MERGE", "SET", "CREATE", "DELETE", "REMOVE", "DROP"}
        is_write = any(keyword in query_upper for keyword in write_keywords)

        span_name = "neo4j.write_transaction" if is_write else "neo4j.read_transaction"

        with tracer.start_as_current_span(span_name) as span:
            span.set_attribute("db.system", "neo4j")
            span.set_attribute("db.statement", query)
            if params:
                span.set_attribute("db.params", str(params))

            try:
                async with self.driver.session() as session:
                    async def transaction_work(tx) -> list[dict[str, Any]]:
                        result = await tx.run(query, params or {})
                        records = []
                        async for record in result:
                            records.append(record.data())
                        return records

                    if is_write:
                        data = await session.execute_write(transaction_work)
                    else:
                        data = await session.execute_read(transaction_work)

                    span.set_status(StatusCode.OK)
                    return data

            except Neo4jError as e:
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
                logger.error(f"Neo4j query execution failed: {e}")
                raise GraphDatabaseError(f"Neo4j transaction failure: {e}") from e
            except Exception as e:
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
                logger.error(f"Unexpected error executing Neo4j query: {e}")
                raise GraphDatabaseError(f"Unexpected error executing database query: {e}") from e

# Alias for backwards compatibility with existing code
Neo4jDatabase = Neo4jDatabaseBridge

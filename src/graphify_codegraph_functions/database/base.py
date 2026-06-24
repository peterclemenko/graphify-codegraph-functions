from typing import Any, Protocol

class GraphDatabaseError(Exception):
    """Exception raised for errors during graph database operations.

    This exception wraps underlying database driver failures to provide a
    unified error interface across different backing engines.
    """
    pass

class GraphDatabaseBridge(Protocol):
    """Asynchronous protocol enforcing a uniform interface for executing openCypher queries
    across different backing graph engines.
    """

    async def execute_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute an openCypher query asynchronously and return the results.

        Args:
            query: The openCypher query string to be executed.
            params: Optional dictionary containing query parameters to be bound.

        Returns:
            A list of records, where each record is represented as a dictionary of key-value pairs.

        Raises:
            GraphDatabaseError: If the query execution fails due to underlying driver or database issues.
        """
        ...

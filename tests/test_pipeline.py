import pytest
from unittest.mock import AsyncMock, MagicMock
from graphify_codegraph_functions.core.pipeline import run_framework_root_tagging, precompute_execution_flows
from graphify_codegraph_functions.database.base import GraphDatabaseBridge

@pytest.fixture
def mock_db():
    db = MagicMock(spec=GraphDatabaseBridge)
    db.execute_query = AsyncMock(return_value=[])
    return db

@pytest.mark.asyncio
async def test_run_framework_root_tagging_success(mock_db):
    # Setup mock return value for tagged nodes count
    mock_db.execute_query.return_value = [{"tagged_count": 5}]
    
    await run_framework_root_tagging(mock_db)
    
    # Assert query was executed
    mock_db.execute_query.assert_called_once()
    query_arg = mock_db.execute_query.call_args[0][0]
    assert "MATCH (e:Entity {file_type: 'code'})" in query_arg
    assert "SET e:ProcessRoot:ApiEntryPoint" in query_arg

@pytest.mark.asyncio
async def test_precompute_execution_flows_success(mock_db):
    # Setup mock return value for created paths count
    mock_db.execute_query.return_value = [{"path_edges_count": 12}]
    
    await precompute_execution_flows(mock_db)
    
    # Assert query was executed
    mock_db.execute_query.assert_called_once()
    query_arg = mock_db.execute_query.call_args[0][0]
    assert "MATCH path = (root:ProcessRoot)-[:CALLS*1..6]->(leaf:Entity {file_type: 'code'})" in query_arg
    assert "MERGE (curr)-[r:NEXT_STEP {process: root.name, sequence_order: idx}]->(next)" in query_arg

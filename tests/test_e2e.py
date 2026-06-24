import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from graphify_codegraph_functions.core.handlers import CodeGraphHandlers
from graphify_codegraph_functions.database.base import GraphDatabaseBridge
from graphify_codegraph_functions.lsp.resolver import LSPResolver
from graphify_codegraph_functions.rest_server import fastapi_app
from graphify_codegraph_functions.nats_client import NatsClient

@pytest.fixture
def mock_db():
    db = MagicMock(spec=GraphDatabaseBridge)
    db.execute_query = AsyncMock(return_value=[
        {"name": "DependencyNode", "file_type": "code", "path_length": 1, "risk_factor": "LOCALIZED"}
    ])
    return db

@pytest.fixture
def mock_lsp():
    lsp = MagicMock(spec=LSPResolver)
    lsp.open_document = AsyncMock()
    lsp.hover = AsyncMock(return_value={"contents": "HoveredSymbol"})
    lsp.document_symbols = AsyncMock(return_value=[{"name": "SymbolInFile", "kind": 12}])
    return lsp

@pytest.fixture
def handlers(mock_db, mock_lsp):
    return CodeGraphHandlers(db=mock_db, lsp=mock_lsp)

@pytest.fixture
def client(handlers):
    fastapi_app.state.handlers = handlers
    return TestClient(fastapi_app)

@pytest.mark.asyncio
async def test_e2e_rest_impact_flow(client, mock_db):
    """E2E Test: REST POST /api/v1/impact triggers database Cypher execution."""
    response = client.post("/api/v1/impact", json={"symbol_name": "RootNode", "max_depth": 3})
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["symbol"] == "RootNode"
    assert res_data["impacted_nodes"][0]["name"] == "DependencyNode"
    mock_db.execute_query.assert_called_once()

@pytest.mark.asyncio
async def test_e2e_nats_wildcard_routing(handlers, mock_db):
    """E2E Test: NATS wildcard client correctly parses and executes commands."""
    nats_client = NatsClient(servers=["nats://127.0.0.1:4222"], handlers=handlers)
    
    # Mock NATS connection objects
    mock_nc = MagicMock()
    mock_nc.publish = AsyncMock()
    nats_client.nc = mock_nc
    
    # Mock message
    mock_msg = MagicMock()
    mock_msg.subject = "gitnexus.commands.impact"
    mock_msg.reply = "reply.subject"
    mock_msg.data = json.dumps({"symbol_name": "RootNode", "max_depth": 3}).encode("utf-8")
    
    # Trigger message handler
    await nats_client._on_message(mock_msg)
    
    # Verify response was published back
    mock_nc.publish.assert_called_once()
    publish_args = mock_nc.publish.call_args[0]
    assert publish_args[0] == "reply.subject"
    
    res_data = json.loads(publish_args[1].decode("utf-8"))
    assert res_data["symbol"] == "RootNode"
    assert res_data["impacted_nodes"][0]["name"] == "DependencyNode"

@pytest.mark.asyncio
async def test_e2e_graph_enricher(handlers, mock_db, mock_lsp):
    """E2E Test: GraphEnricher successfully resolves and commits semantic relationships."""
    from graphify_codegraph_functions.core.enricher import GraphEnricher

    # Mock DB returns a list of entities to enrich
    mock_db.execute_query.side_effect = [
        [
            {"id": "node-1", "name": "ClassA", "file_path": "foo.py", "start_line": 5, "end_line": 15}
        ],
        [], # batch IMPLEMENTS commit query return
        [], # batch REFERENCES_TYPE commit query return
        []  # batch OVERRIDES commit query return
    ]

    # Mock LSP definition, references, implementation responses
    mock_lsp.definition = AsyncMock(return_value={"uri": "file://foo.py", "range": {"start": {"line": 4}}})
    mock_lsp.implementation = AsyncMock(return_value=[{"uri": "file://foo.py", "range": {"start": {"line": 4}}}])

    enricher = GraphEnricher(db=mock_db, lsp=mock_lsp)
    res = await enricher.enrich()

    assert res["status"] == "success"
    assert res["implements_edges_count"] == 1
    assert res["references_type_edges_count"] == 1
    assert res["overrides_edges_count"] == 1

@pytest.mark.asyncio
async def test_e2e_incremental_sync(mock_db, mock_lsp):
    """E2E Test: IncrementalSyncManager correctly handles file updates and updates LSP/DB."""
    import os
    from graphify_codegraph_functions.core.sync import IncrementalSyncManager

    # Mock file contents on disk
    mock_file = "temp_auth.py"
    with open(mock_file, "w", encoding="utf-8") as f:
        f.write("def login(): pass")

    mock_db.execute_query.side_effect = [
        [], # Purge query execution return
        [], # Batch nodes insertion query return
        []  # Batch edges insertion query return
    ]

    mock_lsp.change_document = AsyncMock()
    mock_lsp.definition = AsyncMock(return_value=[])

    sync_manager = IncrementalSyncManager(db=mock_db, lsp=mock_lsp)
    try:
        await sync_manager.sync_file(mock_file)
        
        # Verify purge was executed
        assert mock_db.execute_query.call_count >= 1
        purge_call = mock_db.execute_query.call_args_list[0][0]
        assert "DETACH DELETE" in purge_call[0]
        assert purge_call[1]["file_path"] == "temp_auth.py"

        # Verify LSP change was sent
        mock_lsp.change_document.assert_called_once_with("file://" + os.path.abspath(mock_file), "def login(): pass")
    finally:
        if os.path.exists(mock_file):
            os.remove(mock_file)



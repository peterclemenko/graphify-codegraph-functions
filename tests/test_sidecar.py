import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from graphify_codegraph_functions.core.handlers import CodeGraphHandlers, handle_impact, handle_context, handle_detect_changes, handle_query, handle_rename, handle_list_repos, handle_cypher_escape
from graphify_codegraph_functions.database.base import GraphDatabaseBridge
from graphify_codegraph_functions.lsp.client import LSPProcessClient
from graphify_codegraph_functions.lsp.resolver import LSPResolver
from graphify_codegraph_functions.rest_server import fastapi_app, get_handlers
from graphify_codegraph_functions.mcp_server import mcp, gitnexus_impact, gitnexus_rename, init_mcp_handlers

@pytest.fixture
def mock_db():
    db = MagicMock(spec=GraphDatabaseBridge)
    db.execute_query = AsyncMock(return_value=[])
    return db

@pytest.fixture
def mock_lsp():
    lsp = MagicMock(spec=LSPResolver)
    lsp.open_document = AsyncMock()
    lsp.hover = AsyncMock(return_value={"contents": "test_symbol\nhover info"})
    lsp.rename = MagicMock()
    # lsp.rename returns a coroutine when called
    async def mock_rename(uri, line, character, new_name):
        return {"documentChanges": []}
    lsp.rename.side_effect = mock_rename
    lsp.document_symbols = AsyncMock(return_value=[{"name": "test_symbol", "kind": 12, "range": {}}])
    return lsp

@pytest.fixture
def handlers(mock_db, mock_lsp):
    return CodeGraphHandlers(db=mock_db, lsp=mock_lsp)

@pytest.fixture
def client(handlers):
    fastapi_app.state.handlers = handlers
    return TestClient(fastapi_app)

@pytest.mark.asyncio
async def test_handlers_fallback_mode():
    """Test handlers operate correctly without database or LSP (standalone fallback)."""
    fallback_handlers = CodeGraphHandlers(db=None, lsp=None)
    
    impact = await fallback_handlers.handle_impact("SymbolA")
    assert impact["fallback"] is True
    assert impact["impacted_nodes"] == []

    changes = await fallback_handlers.handle_detect_changes([{"uri": "file://foo.py", "content": "pass"}])
    assert changes["fallback"] is True
    assert changes["changed_symbols"] == []

    rename = await fallback_handlers.handle_rename("file://foo.py", 0, 0, "NewName")
    assert rename["fallback"] is True
    assert rename["workspace_edit"] is None

@pytest.mark.asyncio
async def test_handlers_impact_success(handlers, mock_db):
    mock_db.execute_query.return_value = [
        {"name": "SymbolB", "file_type": "code", "path_length": 1, "risk_factor": "LOCALIZED"},
        {"name": "SymbolC", "file_type": "code", "path_length": 2, "risk_factor": "HIGH"}
    ]
    
    impact = await handlers.handle_impact("SymbolA", max_depth=2)
    assert impact["fallback"] is False
    assert len(impact["impacted_nodes"]) == 2
    names = [node["name"] for node in impact["impacted_nodes"]]
    assert "SymbolB" in names
    assert "SymbolC" in names
    mock_db.execute_query.assert_called_once()

def test_api_impact_endpoint(client, handlers, mock_db):
    mock_db.execute_query.return_value = [{"name": "SymbolB", "file_type": "code", "path_length": 1, "risk_factor": "LOCALIZED"}]
    
    response = client.post("/api/v1/impact", json={"symbol_name": "SymbolA", "max_depth": 2})
    assert response.status_code == 200
    assert response.json()["impacted_nodes"][0]["name"] == "SymbolB"

def test_api_rename_endpoint(client, handlers, mock_lsp, mock_db):
    mock_db.execute_query.return_value = [{"name": "NewName"}]
    
    response = client.post("/api/v1/rename", json={
        "uri": "file://foo.py",
        "line": 5,
        "character": 10,
        "new_name": "NewName"
    })
    assert response.status_code == 200
    assert response.json()["workspace_edit"] == {"documentChanges": []}

@pytest.mark.asyncio
async def test_mcp_tool_invocation(handlers):
    init_mcp_handlers(handlers)
    res = await gitnexus_impact("SymbolA")
    assert "impacted_nodes" in res


@pytest.mark.asyncio
async def test_module_handle_impact(mock_db):
    mock_db.execute_query.return_value = [
        {
            "id": "dep-1",
            "name": "CallerA",
            "file_type": "code",
            "start_line": 10,
            "end_line": 20,
            "depth": 1,
            "path_names": ["CallerA", "SymbolA"]
        }
    ]
    res = await handle_impact(mock_db, "SymbolA", max_depth=3)
    assert res["symbol"] == "SymbolA"
    assert len(res["dependents"]) == 1
    dep = res["dependents"][0]
    assert dep["name"] == "CallerA"
    assert dep["type"] == "code"
    assert dep["line_scope"] == {"start_line": 10, "end_line": 20}
    assert dep["depth"] == 1
    assert dep["impact_confidence"] == 0.75  # 1.0 - (1 * 0.25)
    assert dep["path"] == ["CallerA", "SymbolA"]


@pytest.mark.asyncio
async def test_module_handle_context(mock_db):
    mock_db.execute_query.side_effect = [
        # Target node query
        [{"e": {"docstring": "Sample docstring", "start_line": 5, "end_line": 15, "file_path": "foo.py", "file_type": "code", "gitnexus_risk_factor": "MEDIUM"}}],
        # Incoming query
        [{"name": "CallerB", "relation_type": "CALLS", "file_type": "code"}],
        # Outgoing query
        [{"name": "CalleeB", "relation_type": "CALLS", "file_type": "code"}],
        # Rationales query
        [{"text": "Architecture requirement #1", "rationale": None, "name": None}]
    ]
    res = await handle_context(mock_db, "SymbolA")
    assert res["symbol"] == "SymbolA"
    assert res["attributes"]["docstring"] == "Sample docstring"
    assert res["attributes"]["start_line"] == 5
    assert res["attributes"]["end_line"] == 15
    assert res["attributes"]["file_path"] == "foo.py"
    assert res["incoming_callers"][0]["name"] == "CallerB"
    assert res["outgoing_targets"][0]["name"] == "CalleeB"
    assert "Architecture requirement #1" in res["architecture_rationales"]


@pytest.mark.asyncio
async def test_module_handle_detect_changes(mock_db):
    mock_lsp = MagicMock(spec=LSPProcessClient)
    from unittest.mock import patch
    with patch("graphify_codegraph_functions.core.handlers.resolve_symbol_at_position") as mock_resolve:
        mock_resolve.return_value = {
            "fully_qualified_name": "ResolvedSymbolA"
        }
        mock_db.execute_query.return_value = [
            {
                "id": "dep-2",
                "name": "CallerC",
                "file_type": "code",
                "start_line": 30,
                "end_line": 40,
                "depth": 1,
                "path_names": ["CallerC", "ResolvedSymbolA"]
            }
        ]
        
        diffs = [{"file_path": "bar.py", "line": 5, "character": 10}]
        res = await handle_detect_changes(mock_db, mock_lsp, diffs)
        
        assert "ResolvedSymbolA" in res["resolved_symbols"]
        assert res["total_impacted_nodes"] == 1
        assert res["blast_radius"][0]["symbol"] == "ResolvedSymbolA"


@pytest.mark.asyncio
async def test_module_handle_query(mock_db):
    mock_db.execute_query.return_value = [
        {
            "id": "entity-1",
            "name": "UserQueryNode",
            "file_type": "code",
            "docstring": "Detailed flow",
            "root_name": "AuthFlow",
            "root_id": "root-auth"
        },
        {
            "id": "entity-2",
            "name": "AnotherNode",
            "file_type": "code",
            "docstring": "No flow",
            "root_name": None,
            "root_id": None
        }
    ]
    res = await handle_query(mock_db, "UserQuery")
    assert res["query"] == "UserQuery"
    assert res["total_matches"] == 2
    assert len(res["flows"]["AuthFlow"]) == 1
    assert res["flows"]["AuthFlow"][0]["name"] == "UserQueryNode"
    assert len(res["flows"]["standalone"]) == 1
    assert res["flows"]["standalone"][0]["name"] == "AnotherNode"


@pytest.mark.asyncio
async def test_module_handle_rename(mock_db):
    mock_lsp = MagicMock(spec=LSPProcessClient)
    from unittest.mock import patch, mock_open
    with patch("graphify_codegraph_functions.core.handlers.resolve_symbol_at_position") as mock_resolve, \
         patch("builtins.open", mock_open(read_data="def old_sym(): pass")) as m_open, \
         patch("os.path.exists", return_value=True):
        
        mock_resolve.return_value = {"fully_qualified_name": "old_sym"}
        
        mock_lsp.send_request = AsyncMock(return_value={
            "changes": {
                "file://dummy.py": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 4},
                            "end": {"line": 0, "character": 11}
                        },
                        "newText": "new_sym"
                    }
                ]
            }
        })
        
        mock_db.execute_query.return_value = [{"name": "new_sym"}]
        
        res = await handle_rename(mock_db, mock_lsp, "dummy.py", 0, 4, "new_sym")
        assert res["db_synced"] is True
        assert res["workspace_edit"] is not None
        mock_lsp.send_request.assert_called_once()


@pytest.mark.asyncio
async def test_module_handle_list_repos(mock_db):
    res = await handle_list_repos(mock_db)
    assert "repositories" in res
    assert len(res["repositories"]) == 1
    assert res["repositories"][0]["lsp_language"] == "python"


@pytest.mark.asyncio
async def test_module_handle_cypher_escape(mock_db):
    mock_db.execute_query.return_value = [{"res": "success"}]
    res = await handle_cypher_escape(mock_db, "MATCH (n) RETURN n LIMIT 1", {})
    assert res == [{"res": "success"}]




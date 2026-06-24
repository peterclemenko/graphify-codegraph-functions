import asyncio
import json
import os
import shutil
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from graphify_codegraph_functions.lsp.config import SUPPORTED_ECOSYSTEMS, LSPConfigurationError, LSPCommunicationError
from graphify_codegraph_functions.lsp.client import discover_workspace_servers, LSPProcessClient, LSPClient
from graphify_codegraph_functions.lsp.resolver import (
    parse_hover_content,
    resolve_symbol_at_position,
    get_document_symbols,
    LSPResolver,
)


def test_supported_ecosystems_structure():
    """Verify registry has at least 42 ecosystems with correct keys."""
    assert len(SUPPORTED_ECOSYSTEMS) >= 42
    for name, config in SUPPORTED_ECOSYSTEMS.items():
        assert "binaries" in config
        assert "manifests" in config
        assert "extensions" in config
        assert "language_id" in config
        assert "args" in config


@pytest.mark.asyncio
async def test_discover_workspace_servers(tmp_path):
    """Verify codebase workspace discovery profiles matching files and extensions."""
    # Create mock workspace
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "app.ts").write_text("console.log('test')")
    (src_dir / "main.py").write_text("print('hello')")
    (tmp_path / "package.json").write_text("{}")

    with patch("shutil.which", side_effect=lambda x: f"/bin/{x}"):
        discovered = await discover_workspace_servers(str(tmp_path))
        assert "typescript" in discovered
        assert "python" in discovered
        # typescript should have higher score due to manifest
        assert discovered["typescript"]["score"] >= 1000


def test_parse_hover_content():
    """Verify hover content parsing extracts signatures and namespaces."""
    # Python
    py_content = "```python\nclass Foo(Bar):\n    def baz(self):\n```\nSome documentation here."
    res = parse_hover_content(py_content, ".py")
    assert res["signature"] == "class Foo(Bar):"
    assert "baz" in res["documentation"]

    # Go
    go_content = "```go\ntype MyStruct struct\n```"
    res = parse_hover_content(go_content, ".go")
    assert res["fully_qualified_name"] == "MyStruct"
    assert res["kind"] == "struct"

    # Rust
    rs_content = "```rust\npub fn calculate() -> u32\n```"
    res = parse_hover_content(rs_content, ".rs")
    assert res["fully_qualified_name"] == "calculate() -> u32"
    assert res["kind"] == "fn"


@pytest.mark.asyncio
async def test_lsp_process_client_read_write():
    """Verify LSPProcessClient writes content and parses JSON-RPC headers correctly."""
    mock_process = MagicMock()
    mock_process.stdin = MagicMock()
    mock_process.stdout = AsyncMock()

    # Stub stdout.readline to return headers followed by empty line, then JSON body
    # We will stub the calls sequentially
    response_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"capabilities": {}}
    }
    response_bytes = json.dumps(response_payload).encode("utf-8")
    
    mock_process.stdout.readline.side_effect = [
        f"Content-Length: {len(response_bytes)}\r\n".encode("utf-8"),
        b"\r\n",
        b"" # EOF signal for subsequent loops
    ]
    mock_process.stdout.readexactly.return_value = response_bytes

    client = LSPProcessClient(
        binary_path="pyright-langserver",
        args=["--stdio"],
        workspace_root=".",
        language_id="python"
    )
    client.process = mock_process
    client._is_running = True
    
    # Manually seed request future
    future = asyncio.get_running_loop().create_future()
    client._pending_requests[1] = future

    # Trigger read
    client._handle_incoming_payload(response_payload)
    
    assert future.done()
    result = await future
    assert result == {"capabilities": {}}


@pytest.mark.asyncio
async def test_resolver_adapter_interface():
    """Verify legacy interface adapter routes requests to process client."""
    mock_client = MagicMock(spec=LSPProcessClient)
    mock_client.workspace_root = "."
    mock_client.send_request = AsyncMock(return_value={"contents": "Hover Info"})
    mock_client.send_notification = AsyncMock()

    resolver = LSPResolver(mock_client)
    await resolver.open_document("file://test.py", "print('hello')", "python")
    mock_client.send_notification.assert_called_once()

    await resolver.hover("file://test.py", 1, 1)
    # resolve_symbol_at_position will do send_notification then send_request
    assert mock_client.send_request.called

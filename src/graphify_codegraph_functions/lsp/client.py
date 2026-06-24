import asyncio
import fnmatch
import json
import logging
import os
import shutil
from typing import Any, Dict, List, Optional
from .config import SUPPORTED_ECOSYSTEMS, LSPConfigurationError, LSPCommunicationError
from ..core.telemetry import trace_async

logger = logging.getLogger(__name__)


@trace_async("lsp.discover")
async def discover_workspace_servers(workspace_root: str) -> dict[str, dict]:
    """Profiles the target codebase at workspace_root and finds compatible language servers.
    
    Iteratively evaluates SUPPORTED_ECOSYSTEMS, checks for binaries, and ranks based on manifest or extensions.
    """
    manifest_counts = {}
    extension_counts = {}

    for eco in SUPPORTED_ECOSYSTEMS:
        manifest_counts[eco] = 0
        extension_counts[eco] = 0

    # Traverse directory tree
    for root, dirs, files in os.walk(workspace_root):
        # Skip common directories to avoid slow scans
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "venv", ".venv", "__pycache__", "build", "dist")]

        for file in files:
            # Check manifests
            for eco, info in SUPPORTED_ECOSYSTEMS.items():
                for manifest_pattern in info.get("manifests", []):
                    if fnmatch.fnmatch(file, manifest_pattern):
                        manifest_counts[eco] += 1

            # Check extensions
            _, ext = os.path.splitext(file)
            if ext:
                for eco, info in SUPPORTED_ECOSYSTEMS.items():
                    if ext in info.get("extensions", []):
                        extension_counts[eco] += 1

    discovered = {}
    for eco, info in SUPPORTED_ECOSYSTEMS.items():
        matched_binary = None
        for binary in info.get("binaries", []):
            resolved = shutil.which(binary)
            if resolved:
                matched_binary = resolved
                break

        if not matched_binary:
            continue

        score = (manifest_counts[eco] * 1000) + extension_counts[eco]
        if score > 0:
            discovered[eco] = {
                "binary": matched_binary,
                "args": info.get("args", []),
                "language_id": info.get("language_id"),
                "score": score
            }

    return discovered


class LSPProcessClient:
    """An asynchronous process client that runs an LSP server binary via subprocess and uses JSON-RPC over stdin/stdout."""

    def __init__(self, binary_path: str, args: List[str], workspace_root: str, language_id: str):
        self.binary_path = binary_path
        self.args = args
        self.workspace_root = os.path.abspath(workspace_root)
        self.language_id = language_id
        self.process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._req_id = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._is_running = False
        self.recycle_count = 0

    @trace_async("lsp.client.start")
    async def start(self) -> None:
        """Spawns the server process and triggers the initialize/initialized handshake."""
        logger.info(f"Spawning LSP process: {self.binary_path} with args {self.args}")
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.binary_path,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.workspace_root
            )
        except Exception as e:
            raise LSPConfigurationError(f"Failed to start LSP process {self.binary_path}: {e}")

        self._is_running = True
        self._reader_task = asyncio.create_task(self._read_loop())

        # Mandatory initial handshake
        init_params = {
            "processId": os.getpid(),
            "rootPath": self.workspace_root,
            "rootUri": f"file://{self.workspace_root}",
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "synchronization": {
                        "dynamicRegistration": True,
                        "willSave": True,
                        "willSaveWaitUntil": True,
                        "didSave": True
                    }
                },
                "workspace": {
                    "workspaceFolders": True
                }
            },
            "workspaceFolders": [
                {
                    "uri": f"file://{self.workspace_root}",
                    "name": os.path.basename(self.workspace_root)
                }
            ]
        }

        try:
            await self.send_request("initialize", init_params)
            await self.send_notification("initialized", {})
            logger.info("LSP initialization handshake complete.")
        except Exception as e:
            logger.error(f"LSP handshake failed: {e}")
            await self.stop()
            raise LSPCommunicationError(f"Handshake failed: {e}")

    async def _read_loop(self) -> None:
        """Non-blocking read loop parsing LSP RPC headers and routing responses."""
        try:
            while self._is_running and self.process and self.process.stdout:
                content_length = None
                while True:
                    line_bytes = await self.process.stdout.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode("utf-8").strip()
                    if not line:
                        break
                    if line.lower().startswith("content-length:"):
                        parts = line.split(":")
                        if len(parts) >= 2:
                            content_length = int(parts[1].strip())

                if content_length is None:
                    if not line_bytes:
                        break
                    continue

                body_bytes = await self.process.stdout.readexactly(content_length)
                if not body_bytes:
                    break

                try:
                    payload = json.loads(body_bytes.decode("utf-8"))
                    self._handle_incoming_payload(payload)
                except Exception as e:
                    logger.error(f"Error parsing JSON payload: {e}")
        except asyncio.IncompleteReadError:
            logger.warning("LSP stream ended abruptly.")
        except Exception as e:
            logger.error(f"Error in LSP read loop: {e}")
        finally:
            await self._handle_process_exit()

    def _handle_incoming_payload(self, payload: dict) -> None:
        msg_id = payload.get("id")
        if msg_id is not None and msg_id in self._pending_requests:
            future = self._pending_requests.pop(msg_id)
            if not future.done():
                if "error" in payload:
                    future.set_exception(LSPCommunicationError(payload["error"]))
                else:
                    future.set_result(payload.get("result"))
        else:
            method = payload.get("method")
            if method:
                logger.debug(f"Received LSP notification/request: {method}")

    async def _handle_process_exit(self) -> None:
        # Cancel and error out any pending requests
        pending = list(self._pending_requests.items())
        self._pending_requests.clear()
        for msg_id, future in pending:
            if not future.done():
                future.set_exception(LSPCommunicationError("LSP server process exited unexpectedly."))

        if self._is_running:
            logger.warning("LSP server process exited unexpectedly. Recycling connection...")
            self.recycle_count += 1
            try:
                await self.start()
            except Exception as e:
                logger.error(f"Failed to recycle LSP connection: {e}")
                self._is_running = False

    @trace_async("lsp.client.send_request")
    async def send_request(self, method: str, params: dict) -> dict:
        """Sends a JSON-RPC request and returns the response payload."""
        if not self._is_running or not self.process or not self.process.stdin:
            raise LSPCommunicationError("LSP server process is not running.")

        self._req_id += 1
        msg_id = self._req_id
        future = asyncio.get_running_loop().create_future()
        self._pending_requests[msg_id] = future

        payload = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params
        }

        await self._write_payload(payload)
        return await future

    @trace_async("lsp.client.send_notification")
    async def send_notification(self, method: str, params: dict) -> None:
        """Sends a JSON-RPC notification."""
        if not self._is_running or not self.process or not self.process.stdin:
            raise LSPCommunicationError("LSP server process is not running.")

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        await self._write_payload(payload)

    async def _write_payload(self, payload: dict) -> None:
        try:
            body = json.dumps(payload).encode("utf-8")
            header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
            self.process.stdin.write(header + body)
            await self.process.stdin.drain()
        except Exception as e:
            raise LSPCommunicationError(f"Failed to write payload: {e}")

    async def stop(self) -> None:
        """Stops the subprocess cleanly."""
        self._is_running = False
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None

        if self.process:
            try:
                await self.send_notification("shutdown", {})
                await self.send_notification("exit", {})
            except Exception:
                pass
            try:
                self.process.terminate()
                await self.process.wait()
            except Exception:
                pass
            self.process = None


class LSPClient(LSPProcessClient):
    """Wrapper matching the legacy LSPClient interface, mapping language to ecosystem binary."""

    def __init__(self, language: str, project_path: str):
        info = SUPPORTED_ECOSYSTEMS.get(language)
        if not info:
            # Fallback/default to python
            info = SUPPORTED_ECOSYSTEMS.get("python")

        binary_path = None
        for binary in info.get("binaries", []):
            resolved = shutil.which(binary)
            if resolved:
                binary_path = resolved
                break

        if not binary_path:
            binary_path = info.get("binaries")[0] if info.get("binaries") else "pyright-langserver"

        super().__init__(
            binary_path=binary_path,
            args=info.get("args", ["--stdio"]),
            workspace_root=project_path,
            language_id=info.get("language_id", language)
        )


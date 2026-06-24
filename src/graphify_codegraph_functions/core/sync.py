import asyncio
import json
import logging
import os
import time
import hashlib
import re
import threading
from typing import Any, Dict, List, Optional, Set
import nats

from ..database.base import GraphDatabaseBridge
from ..lsp.client import LSPProcessClient
from ..lsp.resolver import LSPResolver
from .telemetry import trace_async

logger = logging.getLogger("graphify.sync")

@trace_async("sync.handle_incremental_file_sync")
async def handle_incremental_file_sync(
    file_path: str,
    db: GraphDatabaseBridge,
    lsp: LSPProcessClient
) -> None:
    """Execute the atomic incremental synchronization loop when a file changes.

    Step 1: Read file and notify LSP via textDocument/didChange.
    Step 2: Purge stale metadata using MATCH ... DETACH DELETE.
    Step 3: Query documentSymbol to extract classes, methods, and functions.
    Step 4: Resolve cross-file references and resynthesize nodes and edges.
    """
    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        logger.warning(f"File not found on disk, skipping sync: {abs_path}")
        return

    # Step 1: LSP Memory Sync
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.error(f"Failed to read file {abs_path}: {e}")
        return

    uri = f"file://{abs_path}"
    try:
        # Check if the document was already opened, send didChange notification
        did_change_params = {
            "textDocument": {
                "uri": uri,
                "version": int(time.time())
            },
            "contentChanges": [{"text": content}]
        }
        await lsp.send_notification("textDocument/didChange", did_change_params)
        logger.info(f"Notified LSP of changes in {abs_path}")
    except Exception as e:
        logger.error(f"Failed to notify LSP of change: {e}")

    # Determine workspace/root paths to write relative file paths
    root_dir = lsp.workspace_root
    rel_path = os.path.relpath(abs_path, root_dir)
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Step 2: Atomic Subtree Purge
    purge_query = (
        "MATCH (e:Entity {file_path: $file_path, file_type: 'code'}) "
        "DETACH DELETE e"
    )
    try:
        await db.execute_query(purge_query, {"file_path": rel_path})
        logger.info(f"Purged stale metadata for {rel_path} in the database.")
    except Exception as e:
        logger.error(f"Failed to purge stale database metadata: {e}")

    # Step 3: LSP Extraction
    symbols = []
    try:
        symbols_params = {"textDocument": {"uri": uri}}
        symbols_res = await lsp.send_request("textDocument/documentSymbol", symbols_params)
        
        def flatten_symbols(syms: Any, parent_scope: str = "") -> List[Dict[str, Any]]:
            if not isinstance(syms, list):
                syms = [syms]
            flat = []
            for sym in syms:
                if not isinstance(sym, dict):
                    continue
                name = sym.get("name", "")
                kind = sym.get("kind")
                symbol_range = sym.get("range", sym.get("location", {}).get("range", {}))
                fqn = f"{parent_scope}.{name}" if parent_scope else name
                
                flat.append({
                    "name": name,
                    "fully_qualified_name": fqn,
                    "kind": kind,
                    "range": symbol_range
                })
                
                children = sym.get("children", [])
                if children:
                    flat.extend(flatten_symbols(children, fqn))
            return flat

        if symbols_res:
            symbols = flatten_symbols(symbols_res)
            logger.info(f"Extracted {len(symbols)} symbols from {rel_path}")
    except Exception as e:
        logger.error(f"Failed to retrieve document symbols: {e}")

    # Step 4: Graph Resynthesis
    nodes_batch = []
    relationships: Dict[str, List[Dict[str, Any]]] = {
        "CALLS": [],
        "IMPORTS": [],
        "REFERENCES": []
    }

    keywords = {
        "def", "class", "return", "import", "from", "as", "if", "else", "elif", "for", "while",
        "try", "except", "finally", "with", "pass", "in", "is", "and", "or", "not", "true", "false",
        "none", "lambda", "global", "nonlocal", "assert", "break", "continue", "del", "yield",
        "let", "const", "var", "function", "fn", "struct", "enum", "trait", "impl", "pub", "use"
    }

    file_lines = content.splitlines()

    for sym in symbols:
        name = sym["name"]
        fqn = sym["fully_qualified_name"]
        symbol_range = sym["range"]
        
        start_line = symbol_range.get("start", {}).get("line", 0) + 1
        end_line = symbol_range.get("end", {}).get("line", 0) + 1
        start_char = symbol_range.get("start", {}).get("character", 0)

        node_id = f"{rel_path}:{fqn}"
        nodes_batch.append({
            "id": node_id,
            "name": name,
            "file_path": rel_path,
            "start_line": start_line,
            "end_line": end_line,
            "sha256": sha256
        })

        # Trace references/calls from the symbol's scope
        symbol_lines = file_lines[start_line - 1 : end_line]
        
        # Limit definitions queries to prevent performance bottlenecks
        queries_sent = 0
        for line_offset, line_text in enumerate(symbol_lines):
            if queries_sent >= 25:
                break
            actual_line = start_line - 1 + line_offset
            for match in re.finditer(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", line_text):
                if queries_sent >= 25:
                    break
                word = match.group(0)
                if word.lower() in keywords:
                    continue
                
                char_offset = match.start()
                try:
                    def_res = await lsp.send_request("textDocument/definition", {
                        "textDocument": {"uri": uri},
                        "position": {"line": actual_line, "character": char_offset}
                    })
                    queries_sent += 1
                    
                    targets = def_res if isinstance(def_res, list) else [def_res] if def_res else []
                    for target in targets:
                        if not isinstance(target, dict):
                            continue
                        target_uri = target.get("uri")
                        if not target_uri:
                            continue
                        
                        target_abs_path = target_uri.replace("file://", "")
                        target_rel_path = os.path.relpath(target_abs_path, root_dir)
                        target_start_line = target.get("range", {}).get("start", {}).get("line", 0) + 1

                        # Skip self-referencing links
                        if target_rel_path == rel_path and target_start_line == start_line:
                            continue

                        # Determine relationship type
                        rel_type = "REFERENCES"
                        if "import" in line_text or "from" in line_text or "require" in line_text:
                            rel_type = "IMPORTS"
                        elif f"{word}(" in line_text.replace(" ", ""):
                            rel_type = "CALLS"

                        target_id = f"{target_rel_path}:{word}:{target_start_line}"
                        relationships[rel_type].append({
                            "source_id": node_id,
                            "target_id": target_id,
                            "target_path": target_rel_path,
                            "target_line": target_start_line,
                            "target_name": word
                        })
                except Exception:
                    pass

    # Commit nodes in batch
    if nodes_batch:
        nodes_query = (
            "UNWIND $batch AS node "
            "MERGE (e:Entity {id: node.id}) "
            "ON CREATE SET e.name = node.name, e.file_type = 'code', e.file_path = node.file_path, "
            "              e.start_line = node.start_line, e.end_line = node.end_line, e.sha256 = node.sha256 "
            "ON MATCH SET e.name = node.name, e.file_path = node.file_path, "
            "             e.start_line = node.start_line, e.end_line = node.end_line, e.sha256 = node.sha256"
        )
        try:
            await db.execute_query(nodes_query, {"batch": nodes_batch})
            logger.info(f"Merged {len(nodes_batch)} nodes for {rel_path}.")
        except Exception as e:
            logger.error(f"Failed to merge nodes: {e}")

    # Commit relationships by type
    for rel_type, batch in relationships.items():
        if not batch:
            continue
        edges_query = (
            "UNWIND $batch AS item "
            "MATCH (src:Entity {id: item.source_id}) "
            "MERGE (dest:Entity {file_path: item.target_path, start_line: item.target_line}) "
            "ON CREATE SET dest.id = item.target_id, dest.name = item.target_name, dest.file_type = 'code', dest.gitnexus_risk_factor = 'LOCALIZED' "
            f"MERGE (src)-[r:{rel_type}]->(dest) "
            "ON CREATE SET r.confidence_tag = 'EXTRACTED' "
            "ON MATCH SET r.confidence_tag = 'EXTRACTED'"
        )
        try:
            await db.execute_query(edges_query, {"batch": batch})
            logger.info(f"Merged {len(batch)} relationships of type {rel_type} for {rel_path}.")
        except Exception as e:
            logger.error(f"Failed to merge relationships of type {rel_type}: {e}")


class IncrementalSyncEngine:
    """Orchestration manager running incremental sync inside NATS or threaded filesystem watchdog."""

    def __init__(
        self,
        db: Optional[GraphDatabaseBridge],
        lsp: Optional[LSPProcessClient],
        nats_servers: Optional[List[str]] = None,
        root_dir: str = "."
    ):
        self.db = db
        self.lsp = lsp
        self.nats_servers = nats_servers
        self.root_dir = os.path.abspath(root_dir)
        self.semaphore = asyncio.Semaphore(10)
        self.nc: Optional[nats.NATS] = None
        self._running = False
        self._watchdog_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> None:
        """Start the synchronization loop, using NATS if available or falling back to local watchdog."""
        self._running = True
        self._loop = asyncio.get_running_loop()
        
        nats_success = False
        if self.nats_servers:
            try:
                self.nc = await nats.connect(
                    servers=self.nats_servers,
                    connect_timeout=2,
                    max_reconnect_attempts=2
                )
                logger.info(f"IncrementalSyncEngine connected to NATS at {self.nats_servers}")
                await self.nc.subscribe(
                    "gitnexus.events.file_changed",
                    cb=self._on_nats_file_changed
                )
                logger.info("Subscribed to gitnexus.events.file_changed on NATS")
                nats_success = True
            except Exception as e:
                logger.warning(f"Failed NATS connection in engine: {e}. Activating watchdog fallback.")
                self.nc = None
        
        if not nats_success:
            self._start_watchdog()

    def _start_watchdog(self) -> None:
        logger.info("Starting threaded file-system watchdog fallback...")
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        mtimes: Dict[str, float] = {}
        watch_extensions = {".py", ".ts", ".rs", ".js", ".go", ".java", ".kt", ".swift", ".cpp", ".c", ".h"}
        
        # Initial scan
        for root, dirs, files in os.walk(self.root_dir):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "venv", ".venv", "__pycache__", "build", "dist")]
            for file in files:
                ext = os.path.splitext(file)[1]
                if ext in watch_extensions:
                    path = os.path.join(root, file)
                    try:
                        mtimes[path] = os.path.getmtime(path)
                    except Exception:
                        pass
        
        while self._running:
            time.sleep(2.0)
            for root, dirs, files in os.walk(self.root_dir):
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "venv", ".venv", "__pycache__", "build", "dist")]
                for file in files:
                    ext = os.path.splitext(file)[1]
                    if ext in watch_extensions:
                        path = os.path.join(root, file)
                        try:
                            mtime = os.path.getmtime(path)
                            old_mtime = mtimes.get(path)
                            if old_mtime is not None and mtime > old_mtime:
                                logger.info(f"Watchdog detected modification: {path}")
                                if self._loop and self._loop.is_running():
                                    asyncio.run_coroutine_threadsafe(
                                        self.trigger_sync(path),
                                        self._loop
                                    )
                            mtimes[path] = mtime
                        except Exception:
                            pass

    async def trigger_sync(self, file_path: str) -> None:
        async with self.semaphore:
            if self.db and self.lsp:
                try:
                    await handle_incremental_file_sync(file_path, self.db, self.lsp)
                except Exception as e:
                    logger.error(f"Error executing handle_incremental_file_sync for {file_path}: {e}")

    async def _on_nats_file_changed(self, msg) -> None:
        try:
            payload = json.loads(msg.data.decode("utf-8"))
            file_path = payload.get("file_path")
            if file_path:
                logger.info(f"IncrementalSyncEngine received NATS notification for: {file_path}")
                asyncio.create_task(self.trigger_sync(file_path))
        except Exception as e:
            logger.error(f"Failed to handle NATS sync message: {e}")

    async def stop(self) -> None:
        """Gracefully shutdown NATS connection."""
        self._running = False
        if self.nc:
            await self.nc.close()
            logger.info("IncrementalSyncEngine NATS connection closed.")


class IncrementalSyncManager:
    """Wrapper manager maintaining backward compatibility with the existing handlers."""

    def __init__(self, db: Optional[GraphDatabaseBridge], lsp: Optional[LSPResolver], root_dir: str = "."):
        self.db = db
        self.lsp = lsp
        self.root_dir = root_dir
        self.watch_extensions = {".py", ".ts", ".rs"}
        self._mtimes: Dict[str, float] = {}
        self.lock = asyncio.Lock()

    async def sync_file(self, file_path: str) -> None:
        abs_path = os.path.abspath(file_path)
        if not self.db or not self.lsp:
            logger.warning("Database or LSP resolver not configured, skipping sync.")
            return

        # Extract underlying client from resolver wrapper if necessary
        lsp_client = self.lsp.client if hasattr(self.lsp, "client") else self.lsp
        await handle_incremental_file_sync(abs_path, self.db, lsp_client)

    async def run_local_watchdog(self) -> None:
        """Original watchdog functionality wrapped."""
        logger.info(f"Starting fallback local watchdog in {self.root_dir}")
        self._scan_mtimes()

        while True:
            await asyncio.sleep(2.0)
            async with self.lock:
                modified_files = self._scan_mtimes()
                for path in modified_files:
                    await self.sync_file(path)

    def _scan_mtimes(self) -> List[str]:
        modified = []
        for root, _, files in os.walk(self.root_dir):
            if "/." in root or root.startswith("./."):
                continue
            for file in files:
                ext = os.path.splitext(file)[1]
                if ext in self.watch_extensions:
                    full_path = os.path.join(root, file)
                    try:
                        mtime = os.path.getmtime(full_path)
                        old_mtime = self._mtimes.get(full_path)
                        if old_mtime is not None and mtime > old_mtime:
                            modified.append(full_path)
                        self._mtimes[full_path] = mtime
                    except Exception:
                        pass
        return modified

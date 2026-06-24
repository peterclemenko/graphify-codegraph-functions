import asyncio
import logging
import os
from typing import Any, Dict, List, Optional
from ..database.base import GraphDatabaseBridge
from ..lsp.resolver import LSPResolver, resolve_symbol_at_position
from ..lsp.client import LSPProcessClient
from .telemetry import trace_async


logger = logging.getLogger(__name__)

class CodeGraphHandlers:
    """Decoupled core processing engines executing GitNexus over Graphify's schema."""

    def __init__(self, db: Optional[GraphDatabaseBridge], lsp: Optional[LSPResolver]):
        self.db = db
        self.lsp = lsp

    @trace_async("handlers.class.handle_impact")
    async def handle_impact(self, symbol_name: str, max_depth: int = 3) -> Dict[str, Any]:
        """Compute the blast radius of a symbol using variable-length traversal.
        
        Applies a decaying confidence score based on topological path length.
        """
        logger.info(f"Computing impact for symbol '{symbol_name}' with depth limit {max_depth}")
        if not self.db:
            return {"symbol": symbol_name, "impacted_nodes": [], "fallback": True}

        # openCypher query traversing CALLS, IMPORTS, and REFERENCES relationships
        query = (
            "MATCH (start:Entity {name: $name}) "
            "MATCH path = (start)-[:CALLS|IMPORTS|REFERENCES*1..$max_depth]->(dep:Entity) "
            "RETURN dep.id AS id, dep.name AS name, dep.file_type AS file_type, "
            "length(path) AS path_length, dep.gitnexus_risk_factor AS risk_factor"
        )
        
        try:
            records = await self.db.execute_query(query, {"name": symbol_name, "max_depth": max_depth})
            
            impacted_nodes = {}
            for r in records:
                name = r.get("name")
                depth = r.get("path_length", 1)
                # Decaying confidence score: e.g., 0.9^depth
                score = round(0.9 ** depth, 3)
                
                # If we find the same node at multiple depths, keep the higher confidence (shorter path)
                if name not in impacted_nodes or score > impacted_nodes[name]["confidence"]:
                    impacted_nodes[name] = {
                        "name": name,
                        "file_type": r.get("file_type"),
                        "depth": depth,
                        "confidence": score,
                        "risk_factor": r.get("risk_factor", "LOCALIZED")
                    }

            return {
                "symbol": symbol_name,
                "impacted_nodes": list(impacted_nodes.values()),
                "total_count": len(impacted_nodes),
                "fallback": False
            }
        except Exception as e:
            logger.error(f"Failed to execute handle_impact: {e}")
            return {"symbol": symbol_name, "error": str(e), "fallback": True}

    @trace_async("handlers.class.handle_detect_changes")
    async def handle_detect_changes(self, diff_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Uses LSP to resolve exact runtime symbols under edit, then runs impact analysis."""
        logger.info(f"Detecting changes on diff list of size {len(diff_list)}")
        resolved_symbols = []

        if not self.lsp:
            return {"changed_symbols": [], "blast_radius": [], "fallback": True}

        for diff in diff_list:
            file_uri = diff.get("uri")
            content = diff.get("content", "")
            line = diff.get("line")
            character = diff.get("character")
            
            if not file_uri:
                continue

            try:
                # Open current runtime file
                await self.lsp.open_document(file_uri, content)
                
                # If line/char coordinates are provided, resolve the exact symbol using hover
                if line is not None and character is not None:
                    hover_data = await self.lsp.hover(file_uri, line, character)
                    if hover_data and "contents" in hover_data:
                        # Extract fuzzy name from signature
                        contents = hover_data["contents"]
                        symbol_name = None
                        if isinstance(contents, dict) and "value" in contents:
                            symbol_name = contents["value"].split("\n")[0].split()[-1]
                        elif isinstance(contents, str):
                            symbol_name = contents.split("\n")[0].split()[-1]
                        
                        if symbol_name:
                            resolved_symbols.append(symbol_name)
                else:
                    # Fallback to document symbols
                    symbols = await self.lsp.document_symbols(file_uri)
                    for s in symbols:
                        if s.get("name"):
                            resolved_symbols.append(s["name"])
            except Exception as e:
                logger.error(f"LSP failure resolving changes for {file_uri}: {e}")

        # Compute blast radius for resolved symbols
        blast_radius = []
        if self.db:
            for sym in set(resolved_symbols):
                impact = await self.handle_impact(sym)
                blast_radius.append(impact)

        return {
            "changed_symbols": list(set(resolved_symbols)),
            "blast_radius": blast_radius,
            "fallback": self.db is None
        }

    @trace_async("handlers.class.handle_context")
    async def handle_context(self, symbol_name: str) -> Dict[str, Any]:
        """Returns attributes, direct connections, and process root ancestors of the target Entity."""
        logger.info(f"Fetching context for: {symbol_name}")
        if not self.db:
            return {"symbol": symbol_name, "context": {}, "fallback": True}

        # Query to fetch target properties
        target_query = "MATCH (e:Entity {name: $name}) RETURN e"
        # Query to fetch incoming/outgoing relations
        relations_query = (
            "MATCH (e:Entity {name: $name})-[r]-(neighbor:Entity) "
            "RETURN type(r) AS relation, startNode(r) = e AS is_outgoing, neighbor.name AS neighbor"
        )
        # Query to fetch ProcessRoot ancestors via NEXT_STEP tracking edges
        roots_query = (
            "MATCH (e:Entity {name: $name})<-[:NEXT_STEP*]-(root:ProcessRoot) "
            "RETURN root.name AS root_name"
        )

        try:
            target_res = await self.db.execute_query(target_query, {"name": symbol_name})
            attributes = target_res[0]["e"] if target_res else {}

            relations_res = await self.db.execute_query(relations_query, {"name": symbol_name})
            roots_res = await self.db.execute_query(roots_query, {"name": symbol_name})

            return {
                "symbol": symbol_name,
                "attributes": attributes,
                "connections": relations_res,
                "process_roots": [r["root_name"] for r in roots_res],
                "fallback": False
            }
        except Exception as e:
            logger.error(f"Failed to fetch context for {symbol_name}: {e}")
            return {"symbol": symbol_name, "error": str(e), "fallback": True}

    @trace_async("handlers.class.handle_rename")
    async def handle_rename(self, file_uri: str, line: int, character: int, new_name: str) -> Dict[str, Any]:
        """Performs renaming across files using LSP and syncs the schema state in the database."""
        logger.info(f"Requesting rename to '{new_name}' at {file_uri}:{line}:{character}")
        
        workspace_edit = None
        old_name = None

        if self.lsp:
            try:
                # 1. Resolve current symbol name via hover
                hover_data = await self.lsp.hover(file_uri, line, character)
                if hover_data and "contents" in hover_data:
                    contents = hover_data["contents"]
                    if isinstance(contents, dict) and "value" in contents:
                        old_name = contents["value"].split("\n")[0].split()[-1]
                    elif isinstance(contents, str):
                        old_name = contents.split("\n")[0].split()[-1]
                
                # 2. Query workspace edits
                workspace_edit = await self.lsp.rename(file_uri, line, character, new_name)
            except Exception as e:
                logger.error(f"LSP rename invocation failed: {e}")

        db_synced = False
        if self.db and old_name:
            # Sync renamed entity properties in the database
            sync_query = (
                "MATCH (e:Entity {name: $old_name}) "
                "SET e.name = $new_name "
                "RETURN e"
            )
            try:
                res = await self.db.execute_query(sync_query, {"old_name": old_name, "new_name": new_name})
                db_synced = len(res) > 0
                logger.info(f"Database symbol sync: '{old_name}' -> '{new_name}' (status: {db_synced})")
            except Exception as e:
                logger.error(f"Failed to sync database during rename: {e}")

        return {
            "workspace_edit": workspace_edit,
            "db_synced": db_synced,
            "fallback": self.db is None or self.lsp is None
        }

    @trace_async("handlers.class.execute_raw_query")
    async def execute_raw_query(self, query: str, parameters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Run standard Cypher query against the active database."""
        if not self.db:
            return []
        return await self.db.execute_query(query, parameters)


@trace_async("handlers.handle_impact_module")
async def handle_impact(db: GraphDatabaseBridge, symbol_name: str, max_depth: int = 3) -> Dict[str, Any]:
    """Evaluate the upstream dependency impact (blast radius) of a symbol.

    Performs a variable-length openCypher query to trace dependent code items and calculates
    decaying impact confidence scores.

    Args:
        db: Active bridge adapter interface to the graph database.
        symbol_name: Exact name of the symbol to evaluate upstream dependencies for.
        max_depth: Maximum recursion depth of path traversal. Defaults to 3.

    Returns:
        A dictionary containing the target symbol, a list of structured dependents,
        total dependent counts, and a fallback indicator.

    Raises:
        GraphDatabaseError: If the database execution fails.
    """
    logger.info(f"Computing impact (module-level) for symbol '{symbol_name}' up to depth {max_depth}")
    query = (
        "MATCH (start:Entity {name: $name}) "
        "MATCH path = (dep:Entity)-[:CALLS|IMPORTS|REFERENCES*1..$max_depth]->(start) "
        "RETURN dep.id AS id, dep.name AS name, dep.file_type AS file_type, "
        "dep.start_line AS start_line, dep.end_line AS end_line, "
        "length(path) AS depth, [n in nodes(path) | n.name] AS path_names"
    )
    try:
        records = await db.execute_query(query, {"name": symbol_name, "max_depth": max_depth})
        dependents = []
        for r in records:
            depth = r.get("depth", 1)
            impact_confidence = max(0.1, 1.0 - (depth * 0.25))
            
            type_mapping = r.get("file_type") or "unknown"
            start_line = r.get("start_line")
            end_line = r.get("end_line")
            
            dependents.append({
                "id": r.get("id"),
                "name": r.get("name"),
                "type": type_mapping,
                "line_scope": {
                    "start_line": int(start_line) if start_line is not None else None,
                    "end_line": int(end_line) if end_line is not None else None,
                },
                "depth": depth,
                "impact_confidence": float(impact_confidence),
                "path": r.get("path_names", [])
            })
        return {
            "symbol": symbol_name,
            "dependents": dependents,
            "total_count": len(dependents),
            "fallback": False
        }
    except Exception as e:
        logger.error(f"Failed to execute module-level handle_impact: {e}")
        return {"symbol": symbol_name, "error": str(e), "fallback": True}


@trace_async("handlers.handle_context_module")
async def handle_context(db: GraphDatabaseBridge, symbol_name: str) -> Dict[str, Any]:
    """Retrieve a 360-degree neighborhood context for the target symbol.

    Gathers docstrings, line coordinates, incoming callers, outgoing callee targets,
    and attached architecture rationales.

    Args:
        db: Active bridge adapter interface to the graph database.
        symbol_name: Name of the symbol to fetch context for.

    Returns:
        A dictionary containing resolved attributes, incoming/outgoing connections,
        and architecture rationales.

    Raises:
        GraphDatabaseError: If database queries fail.
    """
    logger.info(f"Fetching context (module-level) for: {symbol_name}")
    
    target_query = "MATCH (e:Entity {name: $name}) RETURN e"
    
    incoming_query = (
        "MATCH (caller:Entity)-[r:CALLS|IMPORTS|REFERENCES]->(e:Entity {name: $name}) "
        "RETURN DISTINCT caller.name AS name, type(r) AS relation_type, caller.file_type AS file_type"
    )
    
    outgoing_query = (
        "MATCH (e:Entity {name: $name})-[r:CALLS|IMPORTS|REFERENCES]->(callee:Entity) "
        "RETURN DISTINCT callee.name AS name, type(r) AS relation_type, callee.file_type AS file_type"
    )
    
    rationales_query = (
        "MATCH (e:Entity {name: $name})-[r:HAS_RATIONALE|RATIONALE|ARCHITECTURE_RATIONALE|ATTACHED_TO]-(rat) "
        "RETURN rat.text AS text, rat.rationale AS rationale, rat.name AS name, labels(rat) AS labels"
    )
    
    try:
        target_res = await db.execute_query(target_query, {"name": symbol_name})
        if not target_res:
            return {"symbol": symbol_name, "error": "Symbol not found", "fallback": False}
        
        target_entity = target_res[0].get("e", {})
        
        docstring = target_entity.get("docstring") or target_entity.get("comment") or target_entity.get("doc")
        start_line = target_entity.get("start_line")
        end_line = target_entity.get("end_line")
        file_path = target_entity.get("file_path")
        
        incoming_res = await db.execute_query(incoming_query, {"name": symbol_name})
        outgoing_res = await db.execute_query(outgoing_query, {"name": symbol_name})
        rationales_res = await db.execute_query(rationales_query, {"name": symbol_name})
        
        rationales = []
        if "architecture_rationale" in target_entity and target_entity["architecture_rationale"]:
            rationales.append(target_entity["architecture_rationale"])
        if "rationale" in target_entity and target_entity["rationale"]:
            rationales.append(target_entity["rationale"])
            
        for r in rationales_res:
            txt = r.get("text") or r.get("rationale") or r.get("name")
            if txt:
                rationales.append(txt)
                
        incoming_callers = [
            {
                "name": r.get("name"),
                "relation_type": r.get("relation_type"),
                "type": r.get("file_type")
            } for r in incoming_res
        ]
        
        outgoing_targets = [
            {
                "name": r.get("name"),
                "relation_type": r.get("relation_type"),
                "type": r.get("file_type")
            } for r in outgoing_res
        ]
        
        payload = {
            "symbol": symbol_name,
            "attributes": {
                "docstring": docstring,
                "start_line": int(start_line) if start_line is not None else None,
                "end_line": int(end_line) if end_line is not None else None,
                "file_path": file_path,
                "type": target_entity.get("file_type"),
                "risk_factor": target_entity.get("gitnexus_risk_factor")
            },
            "incoming_callers": incoming_callers,
            "outgoing_targets": outgoing_targets,
            "architecture_rationales": list(set(rationales)),
            "fallback": False
        }
        return payload
    except Exception as e:
        logger.error(f"Failed to execute module-level handle_context: {e}")
        return {"symbol": symbol_name, "error": str(e), "fallback": True}


@trace_async("handlers.handle_detect_changes_module")
async def handle_detect_changes(
    db: GraphDatabaseBridge, lsp: LSPProcessClient, diff_summary: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Identify modified symbols via LSP hover and calculate their system-wide impact.

    Args:
        db: Active bridge adapter interface to the graph database.
        lsp: Connected LSP process client instance.
        diff_summary: List of summaries detailing file mutations and coordinates.

    Returns:
        A dictionary aggregation containing unique symbols, blast radius results,
        total impacted nodes, and fallback flags.
    """
    logger.info(f"Detecting changes (module-level) on diff summary of size {len(diff_summary)}")
    
    async def resolve_one(diff: Dict[str, Any]) -> Optional[str]:
        file_path = diff.get("file_path")
        line = diff.get("line")
        character = diff.get("character")
        if not file_path or line is None or character is None:
            return None
        try:
            symbol_info = await resolve_symbol_at_position(lsp, file_path, line, character)
            if symbol_info:
                return symbol_info.get("fully_qualified_name") or symbol_info.get("signature")
        except Exception as e:
            logger.error(f"Failed to resolve symbol at {file_path}:{line}:{character}: {e}")
        return None

    try:
        tasks = [resolve_one(diff) for diff in diff_summary]
        resolved_names = await asyncio.gather(*tasks)
        unique_symbols = {name for name in resolved_names if name}
        
        impact_results = []
        for symbol in unique_symbols:
            impact = await handle_impact(db, symbol)
            impact_results.append(impact)
            
        total_impacted = sum(len(res.get("dependents", [])) for res in impact_results if "dependents" in res)
        
        return {
            "resolved_symbols": list(unique_symbols),
            "blast_radius": impact_results,
            "total_impacted_nodes": total_impacted,
            "fallback": False
        }
    except Exception as e:
        logger.error(f"Error during module-level change detection: {e}")
        return {"error": str(e), "fallback": True}


@trace_async("handlers.handle_query_module")
async def handle_query(db: GraphDatabaseBridge, query_string: str) -> Dict[str, Any]:
    """Execute hybrid regex/substring and structural path evaluation mapped to parent execution flows.

    Args:
        db: Active bridge adapter interface to the graph database.
        query_string: The query search query string matching node names or comments.

    Returns:
        A dictionary containing matching symbols grouped by ProcessRoot flows.
    """
    logger.info(f"Executing hybrid structural query (module-level) for: '{query_string}'")
    
    query = (
        "MATCH (n:Entity) "
        "WHERE n.name CONTAINS $query_string OR n.docstring CONTAINS $query_string OR n.comment CONTAINS $query_string "
        "OPTIONAL MATCH path = (root:ProcessRoot)-[:NEXT_STEP|CALLS|IMPORTS|REFERENCES*0..]->(n) "
        "RETURN n.id AS id, n.name AS name, n.file_type AS file_type, n.docstring AS docstring, "
        "n.comment AS comment, root.name AS root_name, root.id AS root_id"
    )
    
    try:
        records = await db.execute_query(query, {"query_string": query_string})
        
        flows: Dict[str, List[Dict[str, Any]]] = {}
        total_matches = 0
        
        for r in records:
            match_id = r.get("id")
            if not match_id:
                continue
                
            node_info = {
                "id": match_id,
                "name": r.get("name"),
                "file_type": r.get("file_type"),
                "docstring": r.get("docstring") or r.get("comment")
            }
            
            root_name = r.get("root_name") or "standalone"
            if root_name not in flows:
                flows[root_name] = []
                
            if not any(item["id"] == match_id for item in flows[root_name]):
                flows[root_name].append(node_info)
                total_matches += 1
                
        return {
            "query": query_string,
            "flows": flows,
            "total_matches": total_matches,
            "fallback": False
        }
    except Exception as e:
        logger.error(f"Failed to execute hybrid query: {e}")
        return {"query": query_string, "error": str(e), "fallback": True}


@trace_async("handlers.handle_rename_module")
async def handle_rename(
    db: GraphDatabaseBridge, lsp: LSPProcessClient, file_path: str, line: int, character: int, new_name: str
) -> Dict[str, Any]:
    """Execute a rename request via LSP, apply workspace edits asynchronously, and synchronize database state.

    Args:
        db: Active bridge adapter interface to the graph database.
        lsp: Connected LSP process client instance.
        file_path: Relative or absolute path of the file containing the symbol definition.
        line: 0-indexed line coordinate.
        character: 0-indexed character coordinate.
        new_name: Target replacement name string.

    Returns:
        A dictionary with the workspace edits, db sync success flag, and fallback status.
    """
    logger.info(f"Executing rename (module-level) to '{new_name}' at {file_path}:{line}:{character}")
    
    file_uri = f"file://{os.path.abspath(file_path)}"
    old_name = None
    
    try:
        symbol_info = await resolve_symbol_at_position(lsp, file_path, line, character)
        if symbol_info:
            old_name = symbol_info.get("fully_qualified_name") or symbol_info.get("signature")
    except Exception as e:
        logger.warning(f"Could not hover to resolve old name before rename: {e}")

    params = {
        "textDocument": {"uri": file_uri},
        "position": {"line": line, "character": character},
        "newName": new_name
    }
    
    try:
        workspace_edit = await lsp.send_request("textDocument/rename", params)
        if not workspace_edit:
            return {"workspace_edit": None, "db_synced": False, "fallback": False}

        async def _apply_workspace_edit(ws_edit: Dict[str, Any]) -> None:
            edits_by_path: Dict[str, List[Dict[str, Any]]] = {}
            if "changes" in ws_edit and ws_edit["changes"]:
                for uri, edits in ws_edit["changes"].items():
                    path = uri.replace("file://", "")
                    edits_by_path[path] = edits
            elif "documentChanges" in ws_edit and ws_edit["documentChanges"]:
                for doc_change in ws_edit["documentChanges"]:
                    if "textDocument" in doc_change and "edits" in doc_change:
                        uri = doc_change["textDocument"]["uri"]
                        path = uri.replace("file://", "")
                        edits_by_path[path] = doc_change["edits"]
            
            loop = asyncio.get_running_loop()

            def apply_edits_to_file(path: str, edits: List[Dict[str, Any]]) -> None:
                if not os.path.exists(path):
                    return
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                lines = content.splitlines(keepends=True)
                offsets = [0]
                for line_str in lines:
                    offsets.append(offsets[-1] + len(line_str))
                    
                sorted_edits = sorted(edits, key=lambda x: (x.get("range", {}).get("start", {}).get("line", 0), x.get("range", {}).get("start", {}).get("character", 0)), reverse=True)
                
                for edit in sorted_edits:
                    range_val = edit.get("range", {})
                    start = range_val.get("start", {})
                    end = range_val.get("end", {})
                    new_text = edit.get("newText", "")
                    
                    s_idx = offsets[start.get("line", 0)] + start.get("character", 0)
                    e_idx = offsets[end.get("line", 0)] + end.get("character", 0)
                    
                    content = content[:s_idx] + new_text + content[e_idx:]
                    lines = content.splitlines(keepends=True)
                    offsets = [0]
                    for line_str in lines:
                        offsets.append(offsets[-1] + len(line_str))
                        
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)

            for path, edits in edits_by_path.items():
                await loop.run_in_executor(None, apply_edits_to_file, path, edits)

        db_task = asyncio.create_task(db.execute_query(
            "MATCH (n:Entity) WHERE n.name = $old_name SET n.name = $new_name RETURN n",
            {"old_name": old_name, "new_name": new_name}
        )) if old_name else asyncio.sleep(0)
        
        file_task = asyncio.create_task(_apply_workspace_edit(workspace_edit))
        
        await asyncio.gather(db_task, file_task)
        
        db_synced = False
        if old_name:
            db_res = await db_task
            db_synced = len(db_res) > 0
            
        return {
            "workspace_edit": workspace_edit,
            "db_synced": db_synced,
            "fallback": False
        }
    except Exception as e:
        logger.error(f"Rename pipeline failed: {e}")
        return {"error": str(e), "fallback": True}


@trace_async("handlers.handle_list_repos_module")
async def handle_list_repos(db: GraphDatabaseBridge) -> Dict[str, Any]:
    """Query global configurations, reporting active indexed directories, baseline commit hashes, and branch designations.

    Args:
        db: Active bridge adapter interface to the graph database.

    Returns:
        A dictionary containing repository summaries (path, hash, branch, language) and fallbacks.
    """
    logger.info("Listing registered code repositories")
    from .config import load_config
    config = load_config()
    
    root_path = os.path.abspath(config.get("lsp_root_path", "."))
    commit_hash = "unknown"
    branch_name = "unknown"
    
    if os.path.exists(os.path.join(root_path, ".git")):
        try:
            proc_hash = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                cwd=root_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout_hash, _ = await proc_hash.communicate()
            if proc_hash.returncode == 0:
                commit_hash = stdout_hash.decode().strip()
                
            proc_branch = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--abbrev-ref", "HEAD",
                cwd=root_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout_branch, _ = await proc_branch.communicate()
            if proc_branch.returncode == 0:
                branch_name = stdout_branch.decode().strip()
        except Exception as e:
            logger.warning(f"Failed to query git details for {root_path}: {e}")
            
    repos = [
        {
            "directory": root_path,
            "commit_hash": commit_hash,
            "branch": branch_name,
            "lsp_language": config.get("lsp_language")
        }
    ]
    
    return {
        "repositories": repos,
        "total_count": len(repos),
        "fallback": False
    }


@trace_async("handlers.handle_cypher_escape_module")
async def handle_cypher_escape(db: GraphDatabaseBridge, query: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Execute raw custom openCypher queries directly on the database bridge.

    Provides a query execution bypass for custom metric tools.

    Args:
        db: Active bridge adapter interface to the graph database.
        query: Arbitrary Cypher command string to execute.
        params: Binding parameters dictionary.

    Returns:
        List of database records returned from the query.

    Raises:
        GraphDatabaseError: If raw query fails in db bridge.
    """
    logger.info(f"Escaping custom Cypher query execution: '{query}'")
    try:
        return await db.execute_query(query, params)
    except Exception as e:
        logger.error(f"Custom Cypher escape execution failed: {e}")
        raise

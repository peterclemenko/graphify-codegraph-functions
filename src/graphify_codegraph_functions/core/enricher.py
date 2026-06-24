import asyncio
import argparse
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set
from ..database.base import GraphDatabaseBridge
from ..database.neo4j_impl import Neo4jDatabase
from ..database.falkor_impl import FalkorDatabase
from ..lsp.client import LSPClient
from ..lsp.resolver import LSPResolver
from .telemetry import trace_async
from .config import load_config

logger = logging.getLogger("graphify.enricher")

class GraphEnricher:
    """Async pipeline resolving syntax elements via LSP to write semantic edges back to the DB."""

    def __init__(self, db: GraphDatabaseBridge, lsp: LSPResolver):
        self.db = db
        self.lsp = lsp
        self.semaphore = asyncio.Semaphore(10)

    @trace_async("enricher.enrich")
    async def enrich(self) -> Dict[str, Any]:
        logger.info("Starting Graph Enrichment phase...")
        
        # 1. Fetch all code nodes with position coordinates
        fetch_query = (
            "MATCH (e:Entity {file_type: 'code'}) "
            "WHERE e.file_path IS NOT NULL AND e.start_line IS NOT NULL AND e.end_line IS NOT NULL "
            "RETURN e.id AS id, e.name AS name, e.file_path AS file_path, "
            "e.start_line AS start_line, e.end_line AS end_line"
        )
        
        try:
            nodes = await self.db.execute_query(fetch_query)
        except Exception as e:
            logger.error(f"Failed to fetch initial entity list: {e}")
            return {"status": "error", "message": str(e)}

        logger.info(f"Retrieved {len(nodes)} code entities for enrichment.")

        # Synchronize LSP with file contents on disk
        unique_paths = list(set(n["file_path"] for n in nodes))
        for path in unique_paths:
            try:
                uri = f"file://{path}"
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                    await self.lsp.open_document(uri, content)
            except Exception as e:
                logger.warning(f"Failed to synchronize {path} with LSP: {e}")

        # 2. Process nodes through multi-request extraction loops
        tasks = [self._process_single_node(node) for node in nodes]
        results = await asyncio.gather(*tasks)

        # Flatten relations
        implements_batch = []
        references_batch = []
        overrides_batch = []

        for r in results:
            if not r:
                continue
            implements_batch.extend(r.get("implements", []))
            references_batch.extend(r.get("references_type", []))
            overrides_batch.extend(r.get("overrides", []))

        # 3. Commit semantic edges using unwind batches
        await self._commit_batch("IMPLEMENTS", implements_batch)
        await self._commit_batch("REFERENCES_TYPE", references_batch)
        await self._commit_batch("OVERRIDES", overrides_batch)

        logger.info("Graph Enrichment phase completed successfully.")
        return {
            "status": "success",
            "implements_edges_count": len(implements_batch),
            "references_type_edges_count": len(references_batch),
            "overrides_edges_count": len(overrides_batch)
        }

    async def _process_single_node(self, node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with self.semaphore:
            node_id = node["id"]
            file_path = node["file_path"]
            uri = f"file://{file_path}"
            # LSP is 0-indexed, while database lines are typically 1-indexed (or custom). 
            # We assume start_line property is 1-indexed; convert to 0-indexed.
            line = max(0, int(node["start_line"]) - 1)
            # Fetch position. Assume first character for definition/references
            char = 0 

            relations = {
                "implements": [],
                "references_type": [],
                "overrides": []
            }

            try:
                # A. textDocument/definition
                defs = await self.lsp.definition(uri, line, char)
                if defs:
                    for d in self._parse_lsp_locations(defs):
                        relations["references_type"].append({
                            "source_id": node_id,
                            "target_uri": d["uri"],
                            "target_line": d["line"]
                        })

                # B. textDocument/implementation
                impls = await self.lsp.implementation(uri, line, char)
                if impls:
                    for impl in self._parse_lsp_locations(impls):
                        relations["implements"].append({
                            "source_id": node_id,
                            "target_uri": impl["uri"],
                            "target_line": impl["line"]
                        })

                # C. Overrides check: subclass overrides parent implementation
                # Usually resolved via definition or references. We can fuzzy map them.
                # If there's an implementation matching this node, we record it.
                if relations["implements"]:
                    for item in relations["implements"]:
                        relations["overrides"].append({
                            "source_id": node_id,
                            "target_uri": item["target_uri"],
                            "target_line": item["target_line"]
                        })

            except Exception as e:
                logger.error(f"LSP enrichment query failed for node '{node['name']}' at {file_path}:{line}: {e}")
                return None

            return relations

    def _parse_lsp_locations(self, lsp_response: Any) -> List[Dict[str, Any]]:
        # LSP location can be a dict, or list of dicts
        locations = []
        if isinstance(lsp_response, list):
            items = lsp_response
        else:
            items = [lsp_response]

        for item in items:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri")
            range_val = item.get("range", {})
            start_pos = range_val.get("start", {})
            line = start_pos.get("line")
            if uri and line is not None:
                locations.append({"uri": uri, "line": line + 1}) # convert back to 1-indexed
        return locations

    async def _commit_batch(self, rel_type: str, batch: List[Dict[str, Any]]) -> None:
        if not batch:
            return

        # Query matches exact code entities using source_id and resolves targets by target_uri and line
        # If target node doesn't exist, create a virtual External node to preserve graph structure
        query = (
            f"UNWIND $batch AS item "
            f"MATCH (src:Entity {{id: item.source_id}}) "
            f"MERGE (dest:Entity {{file_path: replace(item.target_uri, 'file://', ''), start_line: item.target_line}}) "
            f"ON CREATE SET dest.id = apoc.create.uuid(), dest.name = 'ExternalSymbol', dest.file_type = 'code', dest.gitnexus_risk_factor = 'LOCALIZED' "
            f"MERGE (src)-[r:{rel_type}]->(dest) "
            f"RETURN count(r) AS rel_count"
        )
        
        # fallback query in case APOC is not installed / active
        fallback_query = (
            f"UNWIND $batch AS item "
            f"MATCH (src:Entity {{id: item.source_id}}) "
            f"MERGE (dest:Entity {{file_path: replace(item.target_uri, 'file://', ''), start_line: item.target_line}}) "
            f"ON CREATE SET dest.id = src.id + '_ext', dest.name = 'ExternalSymbol', dest.file_type = 'code', dest.gitnexus_risk_factor = 'LOCALIZED' "
            f"MERGE (src)-[r:{rel_type}]->(dest) "
            f"RETURN count(r) AS rel_count"
        )

        try:
            await self.db.execute_query(query, {"batch": batch})
            logger.info(f"Committed {len(batch)} edges of type {rel_type} successfully.")
        except Exception as e:
            logger.warning(f"Primary batch commit failed, attempting fallback query: {e}")
            try:
                await self.db.execute_query(fallback_query, {"batch": batch})
                logger.info(f"Committed {len(batch)} edges of type {rel_type} via fallback query.")
            except Exception as ex:
                logger.error(f"Failed to commit batch for relationship {rel_type}: {ex}")

async def run_enrichment_from_env() -> Dict[str, Any]:
    config = load_config()
    graph_engine = config["graph_engine"]
    neo4j_uri = config["neo4j_uri"]
    neo4j_user = config["neo4j_user"]
    neo4j_password = config["neo4j_password"]
    
    falkordb_host = config["falkordb_host"]
    falkordb_port = config["falkordb_port"]
    falkordb_graph = config["falkordb_graph"]
    falkordb_password = config["falkordb_password"]

    lsp_language = config["lsp_language"]
    lsp_root_path = config["lsp_root_path"]

    db = None
    if graph_engine == "neo4j":
        db = Neo4jDatabase(uri=neo4j_uri, user=neo4j_user, secret=neo4j_password)
    elif graph_engine == "falkordb":
        db = FalkorDatabase(host=falkordb_host, port=falkordb_port, graph_name=falkordb_graph, password=falkordb_password)
    
    if not db:
        raise RuntimeError("No valid database configured.")

    await db.connect()
    
    lsp_client = LSPClient(language=lsp_language, project_path=lsp_root_path)
    await lsp_client.start()
    lsp = LSPResolver(client=lsp_client)

    enricher = GraphEnricher(db, lsp)
    try:
        result = await enricher.enrich()
        return result
    finally:
        await lsp_client.stop()
        await db.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Graphify CodeGraph Semantic Enricher CLI")
    parser.add_argument("--enrich", action="store_true", help="Trigger the semantic enrichment pipeline")
    args = parser.parse_args()

    if args.enrich:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        try:
            res = asyncio.run(run_enrichment_from_env())
            print(json.dumps(res, indent=2))
        except Exception as err:
            logger.error(f"Enrichment pipeline CLI execution failed: {err}")
            sys.exit(1)

import logging
from typing import Any
from ..database.base import GraphDatabaseBridge
from .telemetry import trace_async

logger = logging.getLogger("graphify.pipeline")

@trace_async("pipeline.run_framework_root_tagging")
async def run_framework_root_tagging(db: GraphDatabaseBridge) -> None:
    """Identify and tag framework controller/route process roots and API entry points.

    Finds all code Entity nodes that match specific architectural naming or
    directory conventions, then adds :ProcessRoot and :ApiEntryPoint labels to them.

    Args:
        db: Active bridge adapter interface to the graph database.
    """
    logger.info("Executing framework root tagging query...")
    query = (
        "MATCH (e:Entity {file_type: 'code'}) "
        "WHERE (e.name CONTAINS 'Controller' OR e.name CONTAINS 'Route' "
        "   OR e.name CONTAINS 'Handler' OR e.name CONTAINS 'Resolver' "
        "   OR e.file_path CONTAINS 'Controller' OR e.file_path CONTAINS 'Route' "
        "   OR e.file_path CONTAINS 'Handler' OR e.file_path CONTAINS 'Resolver' "
        "   OR e.file_path CONTAINS 'src/controllers/' OR e.file_path CONTAINS 'src/routes/' "
        "   OR e.file_path CONTAINS 'src/api/') "
        "SET e:ProcessRoot:ApiEntryPoint "
        "RETURN count(e) AS tagged_count"
    )
    try:
        results = await db.execute_query(query)
        tagged_count = results[0]["tagged_count"] if results else 0
        logger.info(f"Framework root tagging finished. Tagged {tagged_count} nodes as :ProcessRoot and :ApiEntryPoint.")
    except Exception as e:
        logger.error(f"Failed to execute framework root tagging query: {e}")
        raise


@trace_async("pipeline.precompute_execution_flows")
async def precompute_execution_flows(db: GraphDatabaseBridge) -> None:
    """Precompute and compile execution flow chains from process roots to leaf elements.

    Traces call hierarchies from tagged ProcessRoots downward and records explicit
    NEXT_STEP sequence tracking edges directly between adjacent node items.

    Args:
        db: Active bridge adapter interface to the graph database.
    """
    logger.info("Executing execution flow precomputation query...")
    query = (
        "MATCH path = (root:ProcessRoot)-[:CALLS*1..6]->(leaf:Entity {file_type: 'code'}) "
        "WHERE NOT (leaf)-[:CALLS]->() "
        "WITH root, nodes(path) AS ns "
        "UNWIND range(0, size(ns) - 2) AS idx "
        "WITH root, ns[idx] AS curr, ns[idx+1] AS next, idx "
        "MERGE (curr)-[r:NEXT_STEP {process: root.name, sequence_order: idx}]->(next) "
        "RETURN count(r) AS path_edges_count"
    )
    try:
        results = await db.execute_query(query)
        edges_count = results[0]["path_edges_count"] if results else 0
        logger.info(f"Execution flow precomputation finished. Created/merged {edges_count} :NEXT_STEP edges.")
    except Exception as e:
        logger.error(f"Failed to execute execution flow precomputation query: {e}")
        raise

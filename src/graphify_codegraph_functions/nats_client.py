import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
import nats
from .core.handlers import CodeGraphHandlers

logger = logging.getLogger(__name__)

class NatsClient:
    """NATS client to process events asynchronously using wildcard routing."""

    def __init__(self, servers: List[str], handlers: CodeGraphHandlers):
        self.servers = servers
        self.handlers = handlers
        self.nc: Optional[nats.NATS] = None
        self._running = False
        self._subscription = None

    async def connect(self) -> None:
        """Establish connection to NATS broker with graceful fallback to standalone mode."""
        try:
            self.nc = await nats.connect(
                servers=self.servers,
                connect_timeout=2,
                max_reconnect_attempts=2
            )
            logger.info(f"Connected to NATS at {self.servers}")
            self._running = True
            await self._subscribe()
        except Exception as e:
            logger.warning(
                f"NATS connection failed (standalone mode fallback active): {e}"
            )
            self.nc = None

    async def _subscribe(self) -> None:
        if not self.nc:
            return
        # Subscribe using the wildcard pattern gitnexus.commands.>
        self._subscription = await self.nc.subscribe(
            "gitnexus.commands.>",
            cb=self._on_message
        )
        logger.info("Configured wildcard NATS subscription for gitnexus.commands.>")
        
        # Subscribe to file change events
        await self.nc.subscribe(
            "gitnexus.events.file_changed",
            cb=self._on_file_changed
        )
        logger.info("Configured NATS subscription for gitnexus.events.file_changed")

    async def _on_file_changed(self, msg) -> None:
        try:
            data = json.loads(msg.data.decode("utf-8"))
            file_path = data.get("file_path")
            if file_path:
                logger.info(f"Received file changed event via NATS: {file_path}")
                from .core.sync import IncrementalSyncManager
                sync_manager = IncrementalSyncManager(self.handlers.db, self.handlers.lsp)
                await sync_manager.sync_file(file_path)
        except Exception as e:
            logger.error(f"Failed to handle NATS file_changed event: {e}")

    async def _on_message(self, msg) -> None:
        if not self.nc:
            return
        subject = msg.subject
        logger.info(f"Received NATS message on subject: {subject}")
        
        from ..core.telemetry import tracer
        from opentelemetry.trace import StatusCode

        action = subject.split(".")[-1]

        with tracer.start_as_current_span(f"nats.message.{action}") as span:
            span.set_attribute("messaging.system", "nats")
            span.set_attribute("messaging.destination", subject)
            if msg.reply:
                span.set_attribute("messaging.reply_to", msg.reply)

            try:
                data = json.loads(msg.data.decode("utf-8"))
            except Exception as e:
                logger.error(f"Failed to parse NATS JSON payload: {e}")
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))
                return

            result = {}

            try:
                if action == "detect_changes":
                    diffs = data.get("diffs", data) if isinstance(data, dict) else data
                    if not isinstance(diffs, list):
                        diffs = [diffs]
                    result = await self.handlers.handle_detect_changes(diffs)

                elif action == "graph_sync":
                    logger.info(f"Executing graph re-index/sync via NATS: {data}")
                    if self.handlers.db:
                        await self.handlers.execute_raw_query(
                            "MERGE (s:SyncState {id: 'global'}) ON MATCH SET s.last_sync = timestamp() ON CREATE SET s.last_sync = timestamp()"
                        )
                    result = {"status": "success", "message": "Graph sync successfully executed."}

                elif action == "impact":
                    symbol_name = data.get("symbol_name")
                    max_depth = data.get("max_depth", 3)
                    if symbol_name:
                        result = await self.handlers.handle_impact(symbol_name, max_depth)
                    else:
                        result = {"error": "Missing symbol_name parameter"}

                elif action == "context":
                    symbol_name = data.get("symbol_name")
                    if symbol_name:
                        result = await self.handlers.handle_context(symbol_name)
                    else:
                        result = {"error": "Missing symbol_name parameter"}

                elif action == "rename":
                    uri = data.get("uri")
                    line = data.get("line")
                    character = data.get("character")
                    new_name = data.get("new_name")
                    if uri and line is not None and character is not None and new_name:
                        result = await self.handlers.handle_rename(uri, line, character, new_name)
                    else:
                        result = {"error": "Missing required rename parameters"}
                elif action == "enrich_graph":
                    logger.info("Executing Graph Enrichment via NATS...")
                    if self.handlers.db and self.handlers.lsp:
                        from .core.enricher import GraphEnricher
                        enricher = GraphEnricher(self.handlers.db, self.handlers.lsp)
                        result = await enricher.enrich()
                    else:
                        result = {"status": "error", "message": "Database or LSP not initialized for enrichment"}
                else:
                    logger.warning(f"Unrecognized NATS command action: {action}")
                    result = {"error": f"Unknown action: {action}"}
                span.set_status(StatusCode.OK)
            except Exception as e:
                logger.exception(f"Error handling NATS action {action}")
                result = {"error": str(e)}
                span.record_exception(e)
                span.set_status(StatusCode.ERROR, str(e))

            # Publish the response payload back to the reply subject
            reply_subject = msg.reply or f"gitnexus.events.{action}_completed"
            try:
                response_payload = json.dumps(result).encode("utf-8")
                await self.nc.publish(reply_subject, response_payload)
                logger.info(f"Published results back to {reply_subject}")
            except Exception as e:
                logger.error(f"Failed to publish response back to NATS: {e}")

    async def close(self) -> None:
        """Gracefully disconnect from NATS."""
        self._running = False
        if self.nc:
            await self.nc.close()
            logger.info("NATS client connection closed.")

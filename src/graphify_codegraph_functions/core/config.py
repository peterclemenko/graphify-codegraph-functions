import json
import os
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

def load_config(config_path: str = "config.json") -> Dict[str, Any]:
    """Load configuration from config.json and override with environment variables.

    Environment variables take precedence over config.json values.
    """
    config: Dict[str, Any] = {}
    
    # 1. Load from file if it exists
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            logger.info(f"Loaded configuration from {config_path}")
        except Exception as e:
            logger.warning(f"Failed to read configuration file {config_path}: {e}")

    # Helper function to get config value with environment variable precedence
    def get_val(env_key: str, default: Any, config_key: str | None = None) -> Any:
        # Check env first
        if env_key in os.environ:
            val = os.environ[env_key]
            # Convert types if default is int or bool
            if isinstance(default, bool):
                return val.lower() in ("true", "1", "yes")
            if isinstance(default, int):
                try:
                    return int(val)
                except ValueError:
                    return default
            return val
        
        # Check config file next
        if config_key and config_key in config:
            return config[config_key]
        
        return default

    # 2. Build the unified config dictionary
    unified: Dict[str, Any] = {
        "graph_engine": get_val("GRAPH_ENGINE", "neo4j", "graph_engine").lower(),
        
        # Neo4j settings
        "neo4j_uri": get_val("NEO4J_URI", "bolt://127.0.0.1:7687", "neo4j_uri"),
        "neo4j_user": get_val("NEO4J_USER", "neo4j", "neo4j_user"),
        "neo4j_password": get_val("NEO4J_PASSWORD", "password", "neo4j_password"),
        
        # FalkorDB settings
        "falkordb_host": get_val("FALKORDB_HOST", "127.0.0.1", "falkordb_host"),
        "falkordb_port": get_val("FALKORDB_PORT", 6379, "falkordb_port"),
        "falkordb_graph": get_val("FALKORDB_GRAPH", "codegraph", "falkordb_graph"),
        "falkordb_password": get_val("FALKORDB_PASSWORD", None, "falkordb_password"),
        
        # Other settings
        "nats_url": get_val("NATS_URL", "nats://127.0.0.1:4222", "nats_url"),
        "lsp_language": get_val("LSP_LANGUAGE", "python", "lsp_language"),
        "lsp_root_path": get_val("LSP_ROOT_PATH", os.getcwd(), "lsp_root_path"),
        "api_host": get_val("API_HOST", "127.0.0.1", "api_host"),
        "api_port": get_val("API_PORT", 8000, "api_port"),
    }
    
    return unified

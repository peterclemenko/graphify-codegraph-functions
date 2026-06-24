import logging
import os
import re
from typing import Any, Dict, List, Optional
from .config import SUPPORTED_ECOSYSTEMS
from .client import LSPProcessClient
from ..core.telemetry import trace_async

logger = logging.getLogger(__name__)


def parse_hover_content(contents: Any, ext: str) -> Dict[str, Any]:
    """Intelligent markdown, documentation, and plaintext signature parser.
    
    Extracts the fully-qualified symbol structural layout and details specific to the matched file type.
    """
    raw_text = ""
    if isinstance(contents, str):
        raw_text = contents
    elif isinstance(contents, dict):
        raw_text = contents.get("value", "")
    elif isinstance(contents, list):
        text_parts = []
        for item in contents:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                text_parts.append(item.get("value", ""))
        raw_text = "\n".join(text_parts)

    result = {
        "raw": raw_text,
        "signature": "",
        "documentation": "",
        "kind": "unknown",
        "fully_qualified_name": ""
    }

    # Clean up standard markdown block markers
    clean_text = re.sub(r"```[a-zA-Z0-9_-]*", "", raw_text).replace("```", "").strip()
    lines = [line.strip() for line in clean_text.split("\n") if line.strip()]

    if lines:
        result["signature"] = lines[0]
        if len(lines) > 1:
            result["documentation"] = "\n".join(lines[1:])

    # Extension-specific semantics
    ext_lower = ext.lower()
    if ext_lower in (".ts", ".tsx", ".js", ".jsx"):
        # TypeScript / JavaScript
        # Check for namespaces, interface/class members, DOM components
        result["kind"] = "TypeScript/JavaScript symbol"
        match = re.search(r"(class|interface|namespace|function|const|let)\s+([a-zA-Z0-9_$.]+)", raw_text)
        if match:
            result["fully_qualified_name"] = match.group(2)
            result["kind"] = match.group(1)

    elif ext_lower in (".swift", ".m", ".h"):
        # Swift & Objective-C
        if ext_lower == ".swift":
            result["kind"] = "Swift symbol"
            match = re.search(r"(protocol|extension|class|struct|enum|actor|func)\s+([a-zA-Z0-9_$.]+)", raw_text)
            if match:
                result["fully_qualified_name"] = match.group(2)
                result["kind"] = match.group(1)
        else:
            result["kind"] = "Objective-C symbol"
            match = re.search(r"@interface\s+([a-zA-Z0-9_]+)\s*\(\s*([a-zA-Z0-9_]+)\s*\)", raw_text)
            if match:
                result["fully_qualified_name"] = f"{match.group(1)} (Category: {match.group(2)})"
                result["kind"] = "category"

    elif ext_lower in (".kt", ".kts", ".java"):
        # Kotlin & Java
        result["kind"] = "JVM symbol"
        # package / class resolver
        pkg_match = re.search(r"package\s+([a-zA-Z0-9_.]+)", raw_text)
        class_match = re.search(r"(class|interface|object|data class)\s+([a-zA-Z0-9_]+)", raw_text)
        fqn_parts = []
        if pkg_match:
            fqn_parts.append(pkg_match.group(1))
        if class_match:
            fqn_parts.append(class_match.group(2))
            result["kind"] = class_match.group(1)
        if fqn_parts:
            result["fully_qualified_name"] = ".".join(fqn_parts)

    elif ext_lower == ".rs":
        # Rust
        result["kind"] = "Rust symbol"
        match = re.search(r"(pub\s+)?(fn|struct|enum|trait|impl|mod)\s+([a-zA-Z0-9_<>:, ]+)", raw_text)
        if match:
            result["fully_qualified_name"] = match.group(3).strip()
            result["kind"] = match.group(2)

    elif ext_lower == ".go":
        # Go
        result["kind"] = "Go symbol"
        match = re.search(r"(type|func)\s+([a-zA-Z0-9_]+)\s+(struct|interface)?", raw_text)
        if match:
            result["fully_qualified_name"] = match.group(2)
            result["kind"] = match.group(3) if match.group(3) else match.group(1)

    elif ext_lower == ".php":
        # PHP
        result["kind"] = "PHP symbol"
        namespace_match = re.search(r"namespace\s+([a-zA-Z0-9_\\]+)", raw_text)
        class_match = re.search(r"(class|trait|interface|function)\s+([a-zA-Z0-9_]+)", raw_text)
        fqn_parts = []
        if namespace_match:
            fqn_parts.append(namespace_match.group(1))
        if class_match:
            fqn_parts.append(class_match.group(2))
            result["kind"] = class_match.group(1)
        if fqn_parts:
            result["fully_qualified_name"] = "\\".join(fqn_parts)

    elif ext_lower in (".yaml", ".yml", ".json"):
        # Configuration
        result["kind"] = "Configuration key"
        match = re.search(r"\"?([a-zA-Z0-9_.-]+)\"?\s*:", raw_text)
        if match:
            result["fully_qualified_name"] = match.group(1)

    elif ext_lower == ".xml":
        # XML
        result["kind"] = "XML anchor / element"
        match = re.search(r"<([a-zA-Z0-9_:-]+)", raw_text)
        if match:
            result["fully_qualified_name"] = match.group(1)

    elif ext_lower == "makefile":
        # Makefiles
        result["kind"] = "Makefile target"
        match = re.search(r"^([a-zA-Z0-9_-]+):", raw_text, re.MULTILINE)
        if match:
            result["fully_qualified_name"] = match.group(1)

    return result


@trace_async("lsp.resolver.resolve_symbol")
async def resolve_symbol_at_position(
    lsp_client: LSPProcessClient, file_path: str, line: int, character: int
) -> Optional[Dict[str, Any]]:
    """Opens the document to sync memory buffers and hovers the specified coordinates."""
    _, ext = os.path.splitext(file_path)
    if not ext:
        ext = ".py"  # Default fallback

    # Determine language_id from registry
    language_id = "python"
    for eco, info in SUPPORTED_ECOSYSTEMS.items():
        if ext in info.get("extensions", []):
            language_id = info.get("language_id", "python")
            break

    file_uri = f"file://{os.path.abspath(file_path)}"

    # 1. read doc content and textDocument/didOpen
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        logger.error(f"Failed to read file for LSP resolution: {e}")
        text = ""

    open_params = {
        "textDocument": {
            "uri": file_uri,
            "languageId": language_id,
            "version": 1,
            "text": text
        }
    }
    try:
        await lsp_client.send_notification("textDocument/didOpen", open_params)
    except Exception as e:
        logger.warning(f"Failed to send didOpen notification: {e}")

    # 2. textDocument/hover
    hover_params = {
        "textDocument": {
            "uri": file_uri
        },
        "position": {
            "line": line,
            "character": character
        }
    }
    try:
        hover_data = await lsp_client.send_request("textDocument/hover", hover_params)
        if not hover_data or "contents" not in hover_data:
            return None
        return parse_hover_content(hover_data["contents"], ext)
    except Exception as e:
        logger.error(f"Hover request failed: {e}")
        return None


@trace_async("lsp.resolver.get_document_symbols")
async def get_document_symbols(lsp_client: LSPProcessClient, file_path: str) -> List[Dict[str, Any]]:
    """Retrieves document symbols mapping scope, ranges, and bounding lines."""
    file_uri = f"file://{os.path.abspath(file_path)}"
    params = {
        "textDocument": {
            "uri": file_uri
        }
    }

    try:
        symbols = await lsp_client.send_request("textDocument/documentSymbol", params)
        if not symbols:
            return []

        parsed: List[Dict[str, Any]] = []

        def walk_symbol(sym: Dict[str, Any], parent_scope: str = "") -> None:
            name = sym.get("name", "")
            kind = sym.get("kind")
            symbol_range = sym.get("range", sym.get("location", {}).get("range", {}))
            detail = sym.get("detail", "")
            fqn = f"{parent_scope}.{name}" if parent_scope else name

            parsed.append({
                "name": name,
                "fully_qualified_name": fqn,
                "kind": kind,
                "range": symbol_range,
                "detail": detail
            })

            for child in sym.get("children", []):
                walk_symbol(child, fqn)

        for sym in symbols:
            if isinstance(sym, dict):
                walk_symbol(sym)
        return parsed
    except Exception as e:
        logger.error(f"Failed to get document symbols: {e}")
        return []


class LSPResolver:
    """Wrapper class preserving legacy interface and mapping calls to client."""

    def __init__(self, client: LSPProcessClient):
        self.client = client

    def _get_relative_path(self, path_or_uri: str) -> str:
        path = path_or_uri.replace("file://", "")
        if os.path.isabs(path):
            try:
                return os.path.relpath(path, self.client.workspace_root)
            except ValueError:
                pass
        return path

    @trace_async("lsp.resolver.open_document")
    async def open_document(self, uri: str, text: str, language_id: Optional[str] = None) -> None:
        path = uri.replace("file://", "")
        if not language_id:
            _, ext = os.path.splitext(path)
            language_id = "python"
            for eco, info in SUPPORTED_ECOSYSTEMS.items():
                if ext in info.get("extensions", []):
                    language_id = info.get("language_id", "python")
                    break

        params = {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": 1,
                "text": text
            }
        }
        await self.client.send_notification("textDocument/didOpen", params)

    @trace_async("lsp.resolver.change_document")
    async def change_document(self, uri: str, text: str, version: int = 2) -> None:
        params = {
            "textDocument": {
                "uri": uri,
                "version": version
            },
            "contentChanges": [{"text": text}]
        }
        await self.client.send_notification("textDocument/didChange", params)

    @trace_async("lsp.resolver.hover")
    async def hover(self, uri: str, line: int, character: int) -> Optional[Dict[str, Any]]:
        path = uri.replace("file://", "")
        return await resolve_symbol_at_position(self.client, path, line, character)

    @trace_async("lsp.resolver.document_symbols")
    async def document_symbols(self, uri: str) -> List[Dict[str, Any]]:
        path = uri.replace("file://", "")
        return await get_document_symbols(self.client, path)

    @trace_async("lsp.resolver.rename")
    async def rename(self, uri: str, line: int, character: int, new_name: str) -> Optional[Dict[str, Any]]:
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "newName": new_name
        }
        try:
            return await self.client.send_request("textDocument/rename", params)
        except Exception as e:
            logger.error(f"Rename request failed: {e}")
            return None

    @trace_async("lsp.resolver.definition")
    async def definition(self, uri: str, line: int, character: int) -> Optional[Any]:
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character}
        }
        try:
            return await self.client.send_request("textDocument/definition", params)
        except Exception as e:
            logger.error(f"Definition request failed: {e}")
            return None

    @trace_async("lsp.resolver.references")
    async def references(self, uri: str, line: int, character: int, include_declaration: bool = True) -> List[Any]:
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": include_declaration}
        }
        try:
            res = await self.client.send_request("textDocument/references", params)
            return res if isinstance(res, list) else []
        except Exception as e:
            logger.error(f"References request failed: {e}")
            return []

    @trace_async("lsp.resolver.implementation")
    async def implementation(self, uri: str, line: int, character: int) -> Optional[Any]:
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character}
        }
        try:
            return await self.client.send_request("textDocument/implementation", params)
        except Exception as e:
            logger.error(f"Implementation request failed: {e}")
            return None

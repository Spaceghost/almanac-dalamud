"""Responses API: flatten tool *namespaces* for backends that do not know them.

Codex sends each MCP server's tools as one ``{"type": "namespace", "name":
"mcp__srv", "tools": [...]}`` entry and expects calls back as
``{"type": "function_call", "namespace": "mcp__srv", "name": "tool"}``.
Ollama only understands plain function tools, so the gateway:

* request: replaces each namespace by its functions named ``<ns>__<tool>``
  (and rewrites namespaced ``function_call`` items in the input history);
* response (JSON or SSE stream): turns ``<ns>__<tool>`` calls back into
  ``namespace`` + ``name``.

Nothing else in the request or response is touched.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

Mapping = dict[str, tuple[str, str]]


def flatten_request(body: dict[str, Any]) -> Mapping:
    mapping: Mapping = {}
    tools = body.get("tools")
    if not isinstance(tools, list) or not any(isinstance(t, dict) and t.get("type") == "namespace" for t in tools):
        return mapping
    flat: list[Any] = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            namespace = str(tool.get("name", ""))
            for sub in tool.get("tools", []):
                if isinstance(sub, dict) and sub.get("type", "function") == "function" and sub.get("name"):
                    name = f"{namespace}__{sub['name']}"
                    mapping[name] = (namespace, str(sub["name"]))
                    flat.append({**sub, "type": "function", "name": name})
        else:
            flat.append(tool)
    body["tools"] = flat
    items = body.get("input")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("type") == "function_call" and item.get("namespace"):
                item["name"] = f"{item.pop('namespace')}__{item.get('name', '')}"
    return mapping


def restore(obj: Any, mapping: Mapping) -> Any:
    if isinstance(obj, dict):
        if obj.get("type") == "function_call" and obj.get("name") in mapping:
            obj["namespace"], obj["name"] = mapping[obj["name"]]
        for value in obj.values():
            restore(value, mapping)
    elif isinstance(obj, list):
        for value in obj:
            restore(value, mapping)
    return obj


def restore_line(line: bytes, mapping: Mapping) -> bytes:
    if not line.startswith(b"data:"):
        return line
    payload = line[5:].strip()
    if not payload or payload == b"[DONE]":
        return line
    try:
        data = json.loads(payload)
    except ValueError:
        return line
    return b"data: " + json.dumps(restore(data, mapping), separators=(",", ":")).encode()


async def restore_stream(chunks: AsyncIterator[bytes], mapping: Mapping) -> AsyncIterator[bytes]:
    """Rewrite an SSE byte stream line by line (lines may span chunks)."""
    buffer = b""
    async for chunk in chunks:
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        if lines:
            yield b"\n".join(restore_line(line, mapping) for line in lines) + b"\n"
    if buffer:
        yield restore_line(buffer, mapping)

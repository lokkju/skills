"""A tools-only MCP server over stdio, in the standard library.

The transport is the subset of MCP a tools-only server needs: newline-delimited JSON-RPC 2.0 on
stdin and stdout, `initialize`, `ping`, `tools/list` and `tools/call`, with notifications read
and ignored, plus the stateless 2026-07-28 revision (`server/discover` and `_meta`-carrying
requests). The caller supplies the tools; logs and stray prints belong on stderr, so stdout
carries only protocol.

This file is shared byte for byte by forge-tracker and term-recorder, which install separately.
forge-tracker's copy is the source: `scripts/sync-mcp-stdio.py` copies it to term-recorder, and
`--check` (CI and pre-commit) fails when the two differ. Edit forge-tracker's, then sync.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TextIO

# Newest first. A client asking for one of these gets it back; any other request gets the
# newest, and the client decides whether it can go on.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# The stateless revision: no initialize, the version and client capabilities in each request's
# `_meta`, and `server/discover` in place of the handshake.
STATELESS_VERSION = "2026-07-28"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
# The tool list never changes while the server runs.
CACHE_TTL_MS = 3_600_000

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
UNSUPPORTED_VERSION = -32022


class ToolError(Exception):
    """A call that couldn't run: a bad argument or a setup problem."""


@dataclass
class Outcome:
    """A tool's answer: its text, its structured result (None for none), whether it failed, and
    whatever the emitter acts on once the response has been written (`pending`).

    Claude Code shows the model a result's structuredContent in place of its text when it has
    one, so a tool whose text is the answer should leave `data` unset."""

    text: str
    data: Any = None
    error: bool = False
    pending: list[Any] = field(default_factory=list)


Handler = Callable[[Any, Mapping[str, Any]], Outcome]
Emit = Callable[[str, list[Any], TextIO], None]


@dataclass
class Spec:
    """What a server offers: its name and version, the instructions for the model, the tool
    definitions and a handler per tool. `handlers` is read at call time, so a test can swap
    one in."""

    name: str
    version: str
    title: str
    instructions: str
    tools: list[dict[str, Any]]
    handlers: Mapping[str, Handler]

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "version": self.version}


def schema(props: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """An inputSchema: an object of PROPS, nothing else allowed."""
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = list(required)
    return out


def boolean(args: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise ToolError(f"{key} must be true or false")
    return value


class Server:
    def __init__(self, spec: Spec, context: Any = None):
        self.spec = spec
        self.context = context
        self.protocol: Optional[str] = None

    def handle(self, message: Any) -> tuple[Optional[dict[str, Any]], list[Any]]:
        """One incoming message: the response to send (None for a notification) and what the
        emitter acts on once it is sent."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message"), []
        method = message.get("method")
        if "id" not in message:
            return None, []  # notifications/initialized, notifications/cancelled, ...
        mid = message["id"]
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                return None, []  # a response to nothing we sent
            return _error(mid, INVALID_REQUEST, "no method"), []
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(mid, INVALID_PARAMS, "params must be an object"), []
        meta = params.get("_meta")
        if method == "server/discover" or (isinstance(meta, dict) and META_VERSION in meta):
            return self.stateless(mid, method, params)
        if method == "initialize":
            return _ok(mid, self.initialize(params)), []
        if method == "ping":
            return _ok(mid, {}), []
        if method == "tools/list":
            return _ok(mid, {"tools": self.spec.tools}), []
        if method == "tools/call":
            return self.call(mid, params)
        return _error(mid, METHOD_NOT_FOUND, f"method not found: {method}"), []

    def stateless(
        self, mid: Any, method: str, params: Mapping[str, Any]
    ) -> tuple[Optional[dict[str, Any]], list[Any]]:
        """A 2026-07-28 request: no handshake; each request names its protocol version in
        `_meta`, and each result says it's complete and who answered."""
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        info = {"io.modelcontextprotocol/serverInfo": self.spec.info()}
        if method == "server/discover":
            return _ok(
                mid,
                {
                    "resultType": "complete",
                    "supportedVersions": [STATELESS_VERSION, *PROTOCOL_VERSIONS],
                    "capabilities": {"tools": {}},
                    "instructions": self.spec.instructions,
                    "ttlMs": CACHE_TTL_MS,
                    "cacheScope": "public",
                    "_meta": info,
                },
            ), []
        version = meta.get(META_VERSION)
        if version != STATELESS_VERSION:
            return _error(
                mid,
                UNSUPPORTED_VERSION,
                f"unsupported protocol version: {version}",
                {"supported": [STATELESS_VERSION], "requested": version},
            ), []
        if META_CAPABILITIES not in meta:
            return _error(mid, INVALID_PARAMS, f"_meta lacks {META_CAPABILITIES}"), []
        if method == "tools/list":
            result: dict[str, Any] = {
                "tools": self.spec.tools,
                "ttlMs": CACHE_TTL_MS,
                "cacheScope": "public",
            }
            response, pending = _ok(mid, result), []
        elif method == "tools/call":
            response, pending = self.call(mid, params)
        else:
            return _error(mid, METHOD_NOT_FOUND, f"method not found: {method}"), []
        if "result" in response:
            response["result"]["resultType"] = "complete"
            response["result"]["_meta"] = info
        return response, pending

    def initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        self.protocol = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": self.protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": self.spec.info(),
            "instructions": self.spec.instructions,
        }

    def call(self, mid: Any, params: Mapping[str, Any]) -> tuple[dict[str, Any], list[Any]]:
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = self.spec.handlers.get(name) if isinstance(name, str) else None
        if handler is None:
            return _error(mid, INVALID_PARAMS, f"unknown tool: {name}"), []
        if not isinstance(args, dict):
            return _error(mid, INVALID_PARAMS, "arguments must be an object"), []
        props = next(t for t in self.spec.tools if t["name"] == name)["inputSchema"]
        unknown = sorted(set(args) - set(props["properties"]))
        missing = [k for k in props.get("required", []) if k not in args]
        if unknown or missing:
            problems = [f"unknown argument {k}" for k in unknown]
            problems += [f"missing argument {k}" for k in missing]
            return _ok(mid, tool_result(Outcome("; ".join(problems), error=True))), []
        try:
            outcome = handler(self.context, args)
        except ToolError as error:
            outcome = Outcome(f"{self.spec.name}: {error}\n", error=True)
        except Exception as error:  # noqa: BLE001 - a tool's bug is the call's error, not ours
            outcome = Outcome(f"{self.spec.name}: {type(error).__name__}: {error}\n", error=True)
        return _ok(mid, tool_result(outcome)), outcome.pending


def tool_result(outcome: Outcome) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": outcome.text}]}
    if isinstance(outcome.data, dict):
        result["structuredContent"] = outcome.data
    if outcome.error:
        result["isError"] = True
    return result


def _ok(mid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": error}


def write_line(text: str, pending: list[Any], out: TextIO) -> None:
    out.write(text)
    out.flush()


def serve(
    spec: Spec,
    stdin: TextIO,
    stdout: TextIO,
    context: Any = None,
    emit: Emit = write_line,
    default: Optional[Callable[[Any], Any]] = None,
) -> int:
    """Read messages until stdin closes. Each response is one line on stdout, written by EMIT
    with the outcome's `pending` list; DEFAULT encodes what json can't."""
    server = Server(spec, context)
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError as error:
            response: Optional[dict[str, Any]] = _error(None, PARSE_ERROR, f"parse error: {error}")
            pending: list[Any] = []
        else:
            if isinstance(message, list):
                response, pending = _error(None, INVALID_REQUEST, "batches aren't supported"), []
            else:
                response, pending = server.handle(message)
        if response is None:
            continue
        text = json.dumps(response, default=default, ensure_ascii=False) + "\n"
        emit(text, pending, stdout)
    return 0

"""Official remote MCP connectors, with explicit configuration and revocable snapshots.

OAuth credentials are owned by ConnectorOAuth, never a desktop plugin or phone.
This module does not grant tool approval. Model calls enter through NativeTools after the engine's
normal approval gate. Remote descriptions and results remain untrusted data.
"""

import asyncio
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from contextvars import ContextVar
from .connector_permissions import ConnectorPermissions, PermissionDecision
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import aiohttp
from aiohttp import web


PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_BODY = 1024 * 1024
MAX_RESULT = 256 * 1024
MAX_SCHEMA = 64 * 1024
MAX_TOOLS = 128
MAX_PAGES = 20
MAX_CONNECTORS = 8
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,23}\Z")
CONNECTORS = web.AppKey("duet_connectors", object)


OFFICIAL_CATALOG = {
    "github": {
        "name": "GitHub", "url": "https://api.githubcopilot.com/mcp/",
        "auth_modes": ["bearer"], "default_auth": "bearer", "readonly_supported": True,
        "documentation": "https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md",
        "authentication_documentation": "https://github.com/github/github-mcp-server/blob/main/docs/host-integration.md",
        "note": "Official hosted GitHub MCP. Use your own token, or configure your own GitHub OAuth application on the server.",
    },
    "huggingface": {
        "name": "Hugging Face", "url": "https://huggingface.co/mcp",
        "auth_modes": ["bearer"], "default_auth": "bearer", "readonly_supported": False,
        "documentation": "https://huggingface.co/docs/hub/agents-mcp",
        "authentication_documentation": "https://github.com/huggingface/hf-mcp-server/blob/main/README.md",
        "note": "Official hosted Hugging Face MCP. Supply your own HF token. Tools depend on your HF MCP settings and token permissions.",
    },
    "exa": {
        "name": "Exa", "url": "https://mcp.exa.ai/mcp",
        "auth_modes": ["none", "api_key"], "default_auth": "none", "readonly_supported": False,
        "documentation": "https://exa.ai/docs/get-started/exa-mcp",
        "authentication_documentation": "https://exa.ai/docs/get-started/exa-mcp#authentication",
        "note": "Official hosted Exa MCP. Explicitly enable keyless search/fetch, supply your own API key, or configure server-managed OAuth.",
    },
    "gmail": {
        "name": "Gmail", "url": "https://gmailmcp.googleapis.com/mcp/v1",
        "auth_modes": ["oauth"], "default_auth": "oauth", "readonly_supported": False,
        "documentation": "https://developers.google.com/workspace/gmail/api/reference/mcp",
        "authentication_documentation": "https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server",
        "note": "Official Gmail MCP (Developer Preview). Requires your own Google AI agent OAuth web client. Email actions save drafts only; sending is unavailable.",
        # A provider adding a send tool must not silently widen this host's authority.
        # Schemas and descriptions still come exclusively from tools/list.
        "allowed_tools": ["create_draft", "get_message", "get_thread", "label_message", "label_thread",
                          "list_drafts", "list_labels", "search_threads", "unlabel_message", "unlabel_thread"],
    },
}


class MCPError(RuntimeError):
    """Safe error code only: never expose upstream bodies, URLs or credentials."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ConnectionConfig:
    id: str
    catalog_id: str
    enabled: bool = False
    auth_type: str = "bearer"
    token: str = field(default="", repr=False)
    readonly: bool = True
    timeout_seconds: float = 25
    additional_install: bool = False
    label: str = ""

    def private(self):
        return {key: getattr(self, key) for key in self.__dataclass_fields__}

    def public(self, catalog):
        result = self.private()
        result.pop("token")
        result["token_set"] = bool(self.token)
        result["name"] = catalog[self.catalog_id]["name"]
        result["url"] = catalog[self.catalog_id]["url"]
        return result


class ConnectorStore:
    """Private atomic configuration. Omitted token preserves it; empty clears it."""
    FIELDS = {"catalog_id", "enabled", "auth_type", "token", "readonly", "timeout_seconds", "additional_install", "label"}

    def __init__(self, data_dir, catalog=None):
        self.catalog = copy.deepcopy(OFFICIAL_CATALOG if catalog is None else catalog)
        self.root = Path(data_dir).expanduser().absolute()
        if self.root.is_symlink():
            raise ValueError("Connector data directory must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "connectors.json"
        self.primary = {}
        self.configs = self._load()

    def _load(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or self.path.stat().st_size > 64 * 1024:
            raise ValueError("Invalid connector configuration file")
        os.chmod(self.path, 0o600)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("version") not in (1, 2) or not isinstance(document.get("connectors"), list):
            raise ValueError("Invalid connector configuration file")
        if len(document["connectors"]) > MAX_CONNECTORS:
            raise ValueError("Too many connectors")
        result = {}
        for item in document["connectors"]:
            if not isinstance(item, dict) or "id" not in item:
                raise ValueError("Invalid connector configuration")
            ident = item["id"]
            if ident in result:
                raise ValueError("Duplicate connector ID")
            result[ident] = self.validate(ident, {k: v for k, v in item.items() if k != "id"})
        primary = document.get("primary", {})
        if (not isinstance(primary, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                or v not in result or result[v].catalog_id != k for k, v in primary.items())):
            raise ValueError("Invalid primary connector installation")
        self.primary = self._primaries(result, primary)
        return result

    @staticmethod
    def _primaries(configs, previous):
        # Legacy IDs remain identities: keep UUIDs, tool names, grants and epochs.
        result = {}
        for catalog in sorted({item.catalog_id for item in configs.values()}):
            candidates = sorted(k for k, v in configs.items() if v.catalog_id == catalog)
            old = previous.get(catalog)
            result[catalog] = old if old in candidates else catalog if catalog in candidates else candidates[0]
        return result

    def validate(self, ident, updates, previous=None):
        if not isinstance(ident, str) or not _IDENTIFIER.fullmatch(ident):
            raise ValueError("Connector ID must be 1..24 lowercase letters, digits or underscores, starting with a letter")
        if not isinstance(updates, dict) or set(updates) - self.FIELDS:
            raise ValueError("Unknown connector configuration field")
        catalog_id = updates.get("catalog_id", previous.catalog_id if previous else ident)
        if catalog_id not in self.catalog:
            raise ValueError("Only reviewed official MCP servers in the catalog are supported")
        if previous and catalog_id != previous.catalog_id:
            raise ValueError("Delete the connector before changing its service")
        item = previous.private() if previous else ConnectionConfig(
            ident, catalog_id, auth_type=self.catalog[catalog_id]["default_auth"]).private()
        item.update(updates)
        for key in ("enabled", "readonly", "additional_install"):
            if type(item[key]) is not bool:
                raise ValueError(key + " must be boolean")
        if not isinstance(item["label"], str) or len(item["label"]) > 120 or any(ord(c) < 32 for c in item["label"]):
            raise ValueError("Invalid connector label")
        modes = self.catalog[catalog_id]["auth_modes"]
        if item["auth_type"] not in [*modes, "oauth"]:
            raise ValueError("Authentication mode is not supported by this official server")
        token = item["token"]
        if not isinstance(token, str) or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("Invalid connector token")
        if item["auth_type"] == "none" and token:
            raise ValueError("Keyless authentication must not contain a token")
        if item["auth_type"] == "oauth" and token:
            raise ValueError("OAuth credentials are managed by the server's authorization flow")
        timeout = item["timeout_seconds"]
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
            raise ValueError("timeout_seconds must be between 1 and 60")
        return ConnectionConfig(**item)

    def _save(self, configs):
        if self.path.is_symlink():
            raise ValueError("Connector configuration must not be a symlink")
        primary = self._primaries(configs, self.primary)
        raw = json.dumps({"version": 2, "primary": primary, "connectors": [configs[k].private() for k in sorted(configs)]},
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > 64 * 1024:
            raise ValueError("Connector configuration exceeds limit")
        temp = self.root / (".connectors-" + secrets.token_hex(12) + ".tmp")
        try:
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temp.unlink(missing_ok=True)
        self.configs = configs
        self.primary = primary

    def put(self, ident, updates):
        if ident not in self.configs and len(self.configs) >= MAX_CONNECTORS:
            raise ValueError("Too many connectors")
        value = self.validate(ident, updates, self.configs.get(ident))
        self._save({**self.configs, ident: value})
        return value

    def delete(self, ident):
        if ident not in self.configs:
            raise KeyError(ident)
        self._save({k: v for k, v in self.configs.items() if k != ident})


def _json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise MCPError("invalid_json") from error


def _redact(value, private_values):
    if isinstance(value, str):
        for secret in private_values:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [_redact(item, private_values) for item in value]
    if isinstance(value, dict):
        return {_redact(key, private_values): _redact(item, private_values) for key, item in value.items()}
    return value


def tool_name(connector_id, remote_name):
    """Stable provider-safe name; preserve the original MCP name in the binding."""
    prefix = "mcp__" + connector_id + "__"
    limit = 64 - len(prefix)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", remote_name)
    if safe != remote_name or len(safe) > limit:
        suffix = "__" + hashlib.sha256(remote_name.encode("utf-8")).hexdigest()[:10]
        safe = safe[:limit - len(suffix)] + suffix
    return prefix + safe


class MCPClient:
    """Bounded Streamable HTTP JSON-RPC client. It never replays tools/call."""
    def __init__(self, url, *, token="", auth_type="none", readonly=False,
                 timeout_seconds=25, allow_loopback_http=False):
        parsed = urlsplit(url)
        local = parsed.hostname in ("127.0.0.1", "::1", "localhost")
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.query or "\\" in url or any(c.isspace() for c in url)
                or (parsed.scheme != "https" and not (allow_loopback_http and local and parsed.scheme == "http"))):
            raise ValueError("MCP endpoint must be HTTPS without credentials, query or fragment")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("Invalid MCP endpoint") from error
        if auth_type not in ("none", "bearer", "api_key"):
            raise ValueError("Unsupported MCP authentication")
        if not isinstance(token, str) or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("Invalid MCP token")
        self._url, self._token, self._auth_type = url, token, auth_type
        self._readonly, self._timeout = readonly, timeout_seconds
        self._http = None
        self._session_id = None
        self._protocol = None
        self._counter = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self.tools_changed = False

    def __repr__(self):
        return "<MCPClient>"

    async def rotate_bearer(self, token):
        """An OAuth refresh keeps the same account grant and MCP session."""
        async with self._lock:
            if self._auth_type != "bearer":
                raise MCPError("unsupported_authentication")
            if self._closed:
                raise MCPError("disconnected")
            self._token = token

    def redact(self, value):
        try:
            return _redact(value, (self._token, self._session_id))
        except RecursionError as error:
            raise MCPError("message_too_deep") from error

    def _headers(self):
        result = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self._auth_type == "bearer" and self._token:
            result["Authorization"] = "Bearer " + self._token
        elif self._auth_type == "api_key" and self._token:
            result["x-api-key"] = self._token
        if self._readonly:
            result["X-MCP-Readonly"] = "true"
        if self._session_id:
            result["Mcp-Session-Id"] = self._session_id
        if self._protocol:
            result["MCP-Protocol-Version"] = self._protocol
        return result

    def _ensure_http(self):
        if self._closed:
            raise MCPError("disconnected")
        if self._http is None:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout, connect=min(10, self._timeout)),
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False)

    async def _body(self, content, limit=MAX_BODY):
        result = bytearray()
        async for chunk in content.iter_chunked(16384):
            result.extend(chunk)
            if len(result) > limit:
                raise MCPError("response_too_large")
        return bytes(result)

    @staticmethod
    def _parse(raw):
        try:
            value = json.loads(raw)
        except (UnicodeError, ValueError, RecursionError) as error:
            raise MCPError("invalid_response") from error
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise MCPError("invalid_response")
        return value

    async def _message(self, message, request_id):
        if "method" in message:
            if not isinstance(message["method"], str):
                raise MCPError("invalid_response")
            if "id" in message:
                # No sampling, elicitation or roots capability is advertised.
                reply = {"jsonrpc": "2.0", "id": message["id"]}
                if message["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "Client capability not supported"}
                await self._post(reply, notification=True)
            elif message["method"] == "notifications/tools/list_changed":
                self.tools_changed = True
            return None
        if type(message.get("id")) is not type(request_id) or message.get("id") != request_id:
            raise MCPError("response_id_mismatch")
        if "error" in message:
            raise MCPError("rpc_error")
        if "result" not in message or not isinstance(message["result"], dict):
            raise MCPError("invalid_response")
        return message["result"]

    async def _sse(self, content, request_id):
        buffer = bytearray()
        data = []
        total, messages = 0, 0
        async for chunk in content.iter_chunked(8192):
            total += len(chunk)
            if total > MAX_BODY:
                raise MCPError("response_too_large")
            buffer.extend(chunk)
            while b"\n" in buffer:
                raw, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                raw = raw.rstrip(b"\r")
                if not raw:
                    if not data:
                        continue
                    messages += 1
                    if messages > 128:
                        raise MCPError("too_many_messages")
                    value = self._parse(b"\n".join(data))
                    data = []
                    result = await self._message(value, request_id)
                    if result is not None:
                        return result
                elif raw.startswith(b"data:"):
                    value = raw[5:]
                    data.append(value[1:] if value.startswith(b" ") else value)
                # Comments, event/id/retry fields never become model context.
        raise MCPError("stream_interrupted")

    async def _post(self, message, *, notification=False):
        self._ensure_http()
        raw = _json_bytes(message)
        if len(raw) > MAX_BODY:
            raise MCPError("request_too_large")
        try:
            async with self._http.post(self._url, data=raw, headers=self._headers(), allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise MCPError("auth_required")
                if response.status == 404 and self._session_id:
                    self._session_id = None
                    self._protocol = None
                    raise MCPError("session_expired")
                if response.status == 429:
                    raise MCPError("rate_limited")
                if 300 <= response.status < 400:
                    raise MCPError("redirect_refused")
                if not 200 <= response.status < 300:
                    raise MCPError("http_error")
                header = response.headers.get("Mcp-Session-Id")
                if header:
                    if len(header) > 256 or any(not 33 <= ord(c) <= 126 for c in header):
                        raise MCPError("invalid_session_header")
                    if message.get("method") != "initialize" and header != self._session_id:
                        raise MCPError("session_changed")
                    self._session_id = header
                if notification:
                    if await self._body(response.content, 1024):
                        raise MCPError("invalid_notification_response")
                    return None
                media_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if media_type == "application/json":
                    return await self._message(self._parse(await self._body(response.content)), message["id"])
                if media_type == "text/event-stream":
                    return await self._sse(response.content, message["id"])
                raise MCPError("unsupported_content_type")
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise MCPError("transport_failed") from error

    async def _rpc(self, method, params):
        self._counter += 1
        return await self._post({"jsonrpc": "2.0", "id": self._counter, "method": method, "params": params})

    async def _initialize(self):
        if self._protocol:
            return
        result = await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSIONS[0], "capabilities": {},
            "clientInfo": {"name": "duet", "version": "1.0"},
        })
        if result.get("protocolVersion") not in PROTOCOL_VERSIONS:
            raise MCPError("unsupported_protocol")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
            raise MCPError("tools_not_supported")
        self._protocol = result["protocolVersion"]
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notification=True)
        # The optional server instructions are intentionally not used.

    async def list_tools(self):
        async with self._lock:
            await self._initialize()
            result, seen, names, cursor = [], set(), set(), None
            total_size = 0
            for _ in range(MAX_PAGES):
                page = await self._rpc("tools/list", {"cursor": cursor} if cursor is not None else {})
                tools = page.get("tools")
                if not isinstance(tools, list):
                    raise MCPError("invalid_tools")
                for tool in tools:
                    if not isinstance(tool, dict):
                        raise MCPError("invalid_tools")
                    name, schema, description = tool.get("name"), tool.get("inputSchema"), tool.get("description", "")
                    if (not isinstance(name, str) or not 1 <= len(name) <= 128
                            or any(ord(c) < 33 for c in name) or name in names
                            or not isinstance(schema, dict) or not isinstance(description, str)
                            or len(description) > 16384 or len(_json_bytes(schema)) > MAX_SCHEMA):
                        raise MCPError("invalid_tools")
                    if len(result) >= MAX_TOOLS:
                        raise MCPError("too_many_tools")
                    total_size += len(_json_bytes(tool))
                    if total_size > MAX_BODY:
                        raise MCPError("tools_too_large")
                    execution = tool.get("execution", {})
                    if not isinstance(execution, dict):
                        raise MCPError("invalid_tools")
                    names.add(name)
                    result.append(self.redact(tool))
                cursor = page.get("nextCursor")
                if cursor is None:
                    return result
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096 or cursor in seen:
                    raise MCPError("invalid_cursor")
                seen.add(cursor)
            raise MCPError("too_many_pages")

    async def call_tool(self, name, arguments, *, authorize=None):
        if not isinstance(arguments, dict):
            raise MCPError("invalid_arguments")
        async with self._lock:
            await self._initialize()
            if authorize is not None:
                authorize()
            result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
            if ("content" not in result and "structuredContent" not in result
                    or "content" in result and not isinstance(result["content"], list)
                    or "isError" in result and type(result["isError"]) is not bool):
                raise MCPError("invalid_tool_result")
            if len(_json_bytes(result)) > MAX_RESULT:
                raise MCPError("tool_result_too_large")
            return self.redact(result)

    async def close(self):
        self._closed = True
        if self._http:
            await self._http.close()


@dataclass
class _Connection:
    config: ConnectionConfig
    client: MCPClient = field(repr=False)
    tools: list = field(default_factory=list, repr=False)
    active: bool = True
    versions: dict = field(default_factory=dict)


class ConnectorManager:
    def __init__(self, store, *, client_factory=None):
        self.store = store
        self.catalog = store.catalog
        self._factory = client_factory or MCPClient
        self._connections = {}
        self._revisions = {ident: 0 for ident in store.configs}
        self._states = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._discovery = None
        self.oauth = None
        self.permissions = ConnectorPermissions(store.root)
        self.permission_grant = ContextVar("mcp_permission_grant", default=None)
        self.dispatch_guard = ContextVar("mcp_dispatch_guard", default=None)

    def _initial_status(self, config):
        if not config.enabled:
            return "disabled"
        if config.auth_type == "oauth":
            if self.oauth is None:
                return "oauth_unsupported"
            return "configured" if self.oauth.has_grant(config.id) else "auth_required"
        if config.auth_type != "none" and not config.token:
            return "auth_required"
        return "configured"

    async def _revoke(self, ident):
        connection = self._connections.pop(ident, None)
        if connection:
            connection.active = False
            await connection.client.close()

    async def configure(self, ident, updates, *, authorize=None):
        async with self._lock:
            if authorize is not None:
                authorize()
            # Never publish a new identity before revocation is durable. Closing
            # a client may suspend/cancel; at that point the old config must
            # still be on disk and its old approvals must already be invalid.
            self.store.validate(ident, updates, self.store.configs.get(ident))
            self.permissions.invalidate(ident)
            if self.oauth is not None:
                self.oauth.invalidate(ident)
            self._revisions[ident] = self._revisions.get(ident, 0) + 1
            await self._revoke(ident)
            if authorize is not None:
                authorize()
            config = self.store.put(ident, updates)
            self._states[ident] = {"status": self._initial_status(config), "tool_count": 0}
        if self.oauth is not None:
            await self.oauth.revoke_pending(ident)
        return self.public()

    async def delete(self, ident):
        async with self._lock:
            if ident not in self.store.configs:
                raise KeyError(ident)
            self.permissions.invalidate(ident)
            if self.oauth is not None:
                self.oauth.invalidate(ident)
            self._revisions[ident] = self._revisions.get(ident, 0) + 1
            await self._revoke(ident)
            self.store.delete(ident)
            self._states.pop(ident, None)
        if self.oauth is not None:
            await self.oauth.revoke_pending(ident)

    async def reset_oauth_permissions(self, ident):
        """Persist a fresh identity before suspension or publishing a new grant."""
        async with self._lock:
            self._revisions[ident] = self._revisions.get(ident, 0) + 1
            try:
                self.permissions.invalidate(ident)
            except (OSError, sqlite3.Error) as error:
                await self._revoke(ident)
                self._states[ident] = {"status": "auth_required", "tool_count": 0}
                raise MCPError("connector_permissions_unavailable") from error
            await self._revoke(ident)
            self._states[ident] = {"status": "auth_required", "tool_count": 0}

    async def invalidate_oauth(self, ident, *, generation=None):
        """Revoke an OAuth snapshot without changing its approved transport configuration."""
        async with self._lock:
            if self.oauth is None or (generation is not None and self.oauth.generation(ident) != generation):
                return False
            self.permissions.invalidate(ident)
            self.oauth.invalidate(ident)
            self._revisions[ident] = self._revisions.get(ident, 0) + 1
            await self._revoke(ident)
            if ident in self.store.configs:
                self._states[ident] = {"status": self._initial_status(self.store.configs[ident]), "tool_count": 0}
        await self.oauth.revoke_pending(ident)
        return True

    async def disconnect(self, ident):
        if ident not in self.store.configs:
            raise KeyError(ident)
        return await self.configure(ident, {"enabled": False, "token": ""})

    async def refresh(self, ident):
        async with self._lock:
            config = self.store.configs[ident]
            await self._revoke(ident)
            revision = self._revisions.get(ident, 0) + 1
            self._revisions[ident] = revision
            status = self._initial_status(config)
            self._states[ident] = {"status": status, "tool_count": 0}
            if status != "configured" or self._closed:
                return self.public()
            self._states[ident]["status"] = "connecting"
        entry = self.catalog[config.catalog_id]
        client = None
        registered = False
        try:
            token, auth_type = config.token, config.auth_type
            if auth_type == "oauth":
                token, auth_type = await self.oauth.access_token(ident), "bearer"
            # Retrying discovery is safe; tools/call is never replayed.
            for attempt in range(2):
                client = self._factory(entry["url"], token=token, auth_type=auth_type,
                                       readonly=config.readonly and entry["readonly_supported"],
                                       timeout_seconds=config.timeout_seconds)
                try:
                    tools = await client.list_tools()
                    if "allowed_tools" in entry:
                        tools = [tool for tool in tools if tool["name"] in entry["allowed_tools"]]
                    break
                except MCPError as error:
                    await client.close()
                    client = None
                    if config.auth_type != "oauth" or attempt or error.code != "auth_required":
                        raise
                    token = await self.oauth.access_token(ident, force=True)
            async with self._lock:
                if self._closed or self._revisions.get(ident) != revision:
                    return self.public()
                versions = {tool["name"]: self.permissions.version(ident, tool) for tool in tools}
                self.permissions.record_verified(ident, versions.values())
                self._connections[ident] = _Connection(config, client, tools, versions=versions)
                self._states[ident] = {"status": "connected", "tool_count": len(tools)}
                registered = True
            return self.public()
        except MCPError as error:
            async with self._lock:
                if self._revisions.get(ident) == revision:
                    self._states[ident] = {"status": error.code, "tool_count": 0}
            return self.public()
        finally:
            # Also covers cancellation while waiting to commit under _lock;
            # this client is not yet reachable through _connections.
            if not registered and client is not None:
                await client.close()

    async def refresh_enabled(self):
        tasks = [asyncio.create_task(self.refresh(ident)) for ident, config in list(self.store.configs.items()) if config.enabled]
        try:
            await asyncio.gather(*tasks)
        finally:
            # Cancellation (or an unexpected child failure) must not strand a
            # sibling's not-yet-registered discovery client.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def start_discovery(self):
        """Start discovery without delaying the HTTP listener or login routes."""
        if self._closed or self._discovery is not None:
            return
        for ident, config in self.store.configs.items():
            status = self._initial_status(config)
            self._states[ident] = {"status": "connecting" if status == "configured" else status, "tool_count": 0}
        self._discovery = asyncio.create_task(self.refresh_enabled(), name="duet-mcp-discovery")

    def display_servers(self):
        """Original Android bootstrap metadata, with no credentials or schemas."""
        result = []
        for ident, connection in sorted(self._connections.items()):
            if not connection.active or self._states.get(ident, {}).get("status") != "connected":
                continue
            config = connection.config
            entry = self.catalog[config.catalog_id]
            result.append({"uuid": str(uuid5(NAMESPACE_URL, "duet-official-mcp:" + config.id + ":" + entry["url"])),
                           "name": entry["name"], "url": entry["url"],
                           "usedAuthentication": config.auth_type != "none",
                           "tools": [{"name": tool["name"], "description": tool.get("description", "")}
                                     for tool in connection.tools]})
        return result

    def server_uuid(self, ident):
        config = self.store.configs[ident]
        return str(uuid5(NAMESPACE_URL, "duet-official-mcp:" + ident + ":" + self.catalog[config.catalog_id]["url"]))

    def directory_uuid(self, catalog):
        return str(uuid5(NAMESPACE_URL, "duet-official-directory:" + catalog + ":" + self.catalog[catalog]["url"]))

    def bootstrap_frames(self):
        servers = []
        for ident, config in sorted(self.store.configs.items()):
            entry = self.catalog[config.catalog_id]
            status = self._states.get(ident, {}).get("status", self._initial_status(config))
            connection = self._connections.get(ident)
            connected = bool(connection and connection.active and status == "connected")
            auth_status = "not_required" if config.auth_type == "none" else "authenticated" if connected else {
                "auth_required": "auth_required", "disabled": "auth_required", "token_invalid": "token_invalid",
                "refresh_failed": "refresh_failed", "oauth_unsupported": "auth_required",
            }.get(status, "unknown")
            servers.append({"uuid": self.server_uuid(ident), "name": entry["name"], "url": entry["url"],
                            "label": config.label or None, "labelIsAutomatic": True, "needsLabel": False,
                            "additionalInstall": config.additional_install,
                            "connectorOrigin": "directory", "directoryEntryUuid": self.directory_uuid(config.catalog_id),
                            "directoryEntryName": entry["name"], "connected": connected,
                            "usedAuthentication": config.auth_type != "none", "authStatus": auth_status,
                            "memberTurnedOff": not config.enabled,
                            "tools": [self.native_tool(connection, tool) for tool in connection.tools] if connected else []})
        frames = [("server_list", {"servers": [{k: s[k] for k in (
            "uuid", "name", "url", "connectorOrigin", "directoryEntryUuid", "directoryEntryName", "memberTurnedOff",
            "label", "labelIsAutomatic", "needsLabel", "additionalInstall")}
            for s in servers], "servers_may_be_partial": False})]
        for server in servers:
            frames.append(("server_base", {k: v for k, v in server.items() if k != "tools"}))
            frames.append(("tools", {"server_uuid": server["uuid"], "tools": server["tools"]}))
        return frames + [("completed", {})]

    def permission_versions(self):
        return {version for connection in self._connections.values() if connection.active
                for version in connection.versions.values()}

    def permission_settings(self):
        installed = (ident for ident, config in self.store.configs.items() if config.enabled)
        return self.permissions.values(self.permissions.verified_versions(installed))

    def native_tool(self, connection, tool):
        enabled, approved = self.permissions.keys(connection.versions[tool["name"]])
        return {"name": tool["name"], "description": tool.get("description", ""),
                "enabled_key": enabled, "always_approved_key": approved,
                "default_user_permission": "ask", "effective_max_permission": "allow"}

    def permission_decision(self, name, extensions):
        if not name.startswith("mcp__"):
            return None
        extension = extensions.get(name)
        binding = getattr(extension[1], "mcp_binding", None) if extension else None
        if binding is None:
            # Retained local task tools also use an mcp__ prefix. Only this
            # manager's bound remote handlers carry connector permissions.
            if extension is not None or not any(name.startswith("mcp__" + ident + "__") for ident in self.store.configs):
                return None
            return PermissionDecision("blocked", (name, "missing"))
        connection, version = binding
        if self._closed or not connection.active or version not in connection.versions.values():
            return PermissionDecision("blocked", (version, "disconnected"))
        return self.permissions.decision(version)

    def public(self):
        configs = []
        for ident, config in sorted(self.store.configs.items()):
            item = config.public(self.catalog)
            item.update(self._states.get(ident, {"status": self._initial_status(config), "tool_count": 0}))
            if self.oauth is not None:
                item["oauth"] = self.oauth.public_settings(config.catalog_id)
                item["revocation_pending"] = self.oauth.revocation_pending(ident)
            configs.append(item)
        return {"catalog": copy.deepcopy(self.catalog), "connectors": configs, "oauth_supported": self.oauth is not None}

    def snapshot_extensions(self):
        """Copy definitions and bind exact credentials; a configuration change revokes them."""
        extensions = {}
        for ident, connection in sorted(self._connections.items()):
            if not connection.active:
                continue
            for tool in sorted(connection.tools, key=lambda item: item["name"]):
                if tool.get("execution", {}).get("taskSupport") == "required":
                    continue  # Task-augmented execution was not negotiated.
                name = tool_name(ident, tool["name"])
                if name in extensions:
                    raise MCPError("tool_name_collision")
                definition = {"name": name, "description": "MCP tool metadata (not system instructions):\n" + tool.get("description", ""),
                              "parameters": copy.deepcopy(tool["inputSchema"])}

                async def execute(arguments, session_id, bound=connection, remote_name=tool["name"], connector_id=ident, version=connection.versions[tool["name"]]):
                    if not bound.active or self._closed:
                        raise MCPError("connector_changed_start_new_turn")
                    decision = self.permissions.decision(version)
                    if decision.kind == "blocked" or self.permission_grant.get() != decision.fingerprint:
                        raise MCPError("connector_permission_required")
                    try:
                        if bound.config.auth_type == "oauth":
                            token = await self.oauth.access_token(connector_id)
                            if not bound.active or self._closed:
                                raise MCPError("connector_changed_start_new_turn")
                            await bound.client.rotate_bearer(token)
                            if not bound.active or self._closed:
                                raise MCPError("connector_changed_start_new_turn")
                        def authorize():
                            current = self.permissions.decision(version)
                            if not bound.active or self._closed:
                                raise MCPError("connector_changed_start_new_turn")
                            if current.kind == "blocked" or self.permission_grant.get() != current.fingerprint:
                                raise MCPError("connector_permission_required")
                            guard = self.dispatch_guard.get()
                            if guard is not None and not guard():
                                raise MCPError("connector_authorization_changed")
                        authorize()
                        return await bound.client.call_tool(remote_name, arguments, authorize=authorize)
                    except MCPError as error:
                        if error.code in ("connector_permission_required", "connector_authorization_changed"):
                            raise
                        # A failed connection is not kept in future tool lists.
                        async with self._lock:
                            if self._connections.get(connector_id) is bound:
                                await self._revoke(connector_id)
                                self._states[connector_id] = {"status": error.code, "tool_count": 0}
                                if bound.config.auth_type == "oauth" and error.code == "auth_required":
                                    self.oauth.mark_invalid(connector_id)
                        raise

                execute.mcp_binding = (connection, connection.versions[tool["name"]])
                extensions[name] = (definition, execute)
        return extensions

    async def close(self):
        self._closed = True
        if self._discovery is not None:
            self._discovery.cancel()
            await asyncio.gather(self._discovery, return_exceptions=True)
            self._discovery = None
        async with self._lock:
            for ident in list(self._connections):
                await self._revoke(ident)
        if self.oauth is not None:
            await self.oauth.close()


def register_connectors(app, data_dir, engine=None, *, store=None, client_factory=None):
    """Register authenticated admin APIs; the owning app's auth middleware applies."""
    manager = ConnectorManager(store or ConnectorStore(data_dir), client_factory=client_factory)
    app[CONNECTORS] = manager
    if engine is not None:
        engine.connector_manager = manager

    async def public(request):
        return web.json_response(manager.public(), headers={"Cache-Control": "no-store"})

    async def put(request):
        try:
            result = await manager.configure(request.match_info["connector_id"], await request.json())
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid official connector configuration"}, status=400)
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def refresh(request):
        try:
            return web.json_response(await manager.refresh(request.match_info["connector_id"]))
        except KeyError:
            return web.json_response({"error": "Connector not found"}, status=404)

    async def disconnect(request):
        try:
            return web.json_response(await manager.disconnect(request.match_info["connector_id"]))
        except KeyError:
            return web.json_response({"error": "Connector not found"}, status=404)

    async def delete(request):
        try:
            await manager.delete(request.match_info["connector_id"])
        except KeyError:
            return web.json_response({"error": "Connector not found"}, status=404)
        return web.json_response({"ok": True})

    app.router.add_get("/duet/connectors", public)
    app.router.add_put("/duet/connectors/{connector_id}", put)
    app.router.add_post("/duet/connectors/{connector_id}/refresh", refresh)
    app.router.add_post("/duet/connectors/{connector_id}/disconnect", disconnect)
    app.router.add_delete("/duet/connectors/{connector_id}", delete)

    async def startup(application):
        manager.start_discovery()

    async def cleanup(application):
        await manager.close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return manager

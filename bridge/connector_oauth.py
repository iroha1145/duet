"""Account-owned OAuth for the reviewed MCP catalog; no credentials leave the server."""
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from urllib.parse import quote, urlencode, urlsplit

import aiohttp

from .connectors import MCPError


CALLBACK_PREFIX = "/duet/oauth/callback/"
CLIENT_PREFIX = "/duet/oauth/clients/"
TRANSACTION_TTL = 10 * 60
TICKET_TTL = 2 * 60
MAX_RESPONSE = 64 * 1024
PROVIDERS = {
    "github": {
        "issuer": "https://github.com",
        "authorization_endpoint": "https://github.com/login/oauth/authorize",
        "token_endpoint": "https://github.com/login/oauth/access_token",
        "revocation_endpoint": "https://api.github.com/applications/{client_id}/token",
        "scopes": ["read:user"], "registration": False,
    },
    "huggingface": {
        "issuer": "https://huggingface.co",
        "resource_metadata": "https://huggingface.co/.well-known/oauth-protected-resource/mcp?login",
        "metadata": "https://huggingface.co/.well-known/oauth-authorization-server",
        "authorization_endpoint": "https://huggingface.co/oauth/authorize",
        "token_endpoint": "https://huggingface.co/oauth/token",
        "registration_endpoint": "https://huggingface.co/oauth/register",
        "scopes": ["read-mcp"], "registration": True,
    },
    "exa": {
        "issuer": "https://auth.exa.ai",
        "resource_metadata": "https://mcp.exa.ai/.well-known/oauth-protected-resource/mcp",
        "metadata": "https://auth.exa.ai/.well-known/oauth-authorization-server",
        "authorization_endpoint": "https://auth.exa.ai/oauth/authorize",
        "token_endpoint": "https://auth.exa.ai/api/oauth/token",
        "registration_endpoint": "https://auth.exa.ai/api/oauth/register",
        "revocation_endpoint": "https://auth.exa.ai/api/oauth/revoke",
        "scopes": ["mcp:tools"], "registration": True,
    },
    "gmail": {
        "issuer": "https://accounts.google.com",
        # Google's protected-resource metadata uses a trailing slash; its OIDC
        # issuer and authorization-response issuer omit it. This exact alias is
        # reviewed here, not generalized to other providers or arbitrary URLs.
        "resource_issuer": "https://accounts.google.com/",
        "resource_metadata": "https://gmailmcp.googleapis.com/.well-known/oauth-protected-resource/mcp/v1",
        "metadata": "https://accounts.google.com/.well-known/openid-configuration",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "revocation_endpoint": "https://oauth2.googleapis.com/revoke",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose"],
        "allowed_scopes": ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose",
                           "https://www.googleapis.com/auth/gmail.labels"],
        "registration": False,
        "authorization_parameters": {"access_type": "offline", "prompt": "consent"},
    },
}


class OAuthError(MCPError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.status = status


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _secret(value, maximum=4096):
    return isinstance(value, str) and len(value) <= maximum and all(33 <= ord(c) <= 126 for c in value)


def _owner(principal):
    return [principal.account_id, principal.organization_id, principal.device_id]


def _basic(client):
    pair = quote(client["client_id"], safe="") + ":" + quote(client["client_secret"], safe="")
    return "Basic " + base64.b64encode(pair.encode("ascii")).decode("ascii")


class ConnectorOAuth:
    def __init__(self, manager, public_origin, *, owner_valid, clock=time.time,
                 profiles=None, allow_loopback_http=False):
        self.manager, self.owner_valid, self.clock = manager, owner_valid, clock
        self.profiles = PROVIDERS if profiles is None else profiles
        self.allow_loopback_http = allow_loopback_http
        self.origin = self._origin(public_origin)
        self._locks = {}
        self._provider_locks = {}
        self._http = None
        self._closed = False
        self.path = manager.store.root / "connector-oauth.sqlite3"
        if self.path.is_symlink():
            raise ValueError("OAuth database must not be a symlink")
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings(catalog TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS generations(id TEXT PRIMARY KEY, generation INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY, generation INTEGER NOT NULL, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS clients(cache_key TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS transactions(state_hash TEXT PRIMARY KEY, id TEXT NOT NULL,
            generation INTEGER NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS revocations(id INTEGER PRIMARY KEY, connector TEXT NOT NULL, body TEXT NOT NULL);
        """)
        self.db.commit()
        os.chmod(self.path, 0o600)

    def _origin(self, value):
        parsed = urlsplit(value)
        local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or "\\" in value or any(c.isspace() for c in value)
                or (parsed.scheme != "https" and not (self.allow_loopback_http and local and parsed.scheme == "http"))):
            return None
        try:
            parsed.port
        except ValueError:
            return None
        # Production callbacks must be reachable by the user's browser and the AS.
        if local and not self.allow_loopback_http:
            return None
        return value.rstrip("/")

    def _settings(self, catalog):
        row = self.db.execute("SELECT revision,body FROM settings WHERE catalog=?", (catalog,)).fetchone()
        return (row[0], json.loads(row[1])) if row else (0, {
            "enabled": False, "registration_mode": "pre_registered", "client_id": "", "client_secret": "",
            "scopes": list(self.profiles[catalog]["scopes"]),
        })

    def flow_enabled(self, catalog):
        return bool(self.origin and self._settings(catalog)[1]["enabled"])

    def public_settings(self, catalog):
        revision, config = self._settings(catalog)
        result = {k: v for k, v in config.items() if k != "client_secret"}
        result.update(client_secret_set=bool(config["client_secret"]), revision=revision,
                      callback_url=self.origin + CALLBACK_PREFIX + catalog if self.origin else None,
                      available=bool(self.origin and config["enabled"]),
                      unavailable_reason=None if self.origin else "public_https_origin_required")
        result["remote_revocation_supported"] = "revocation_endpoint" in self.profiles[catalog]
        return result

    async def configure(self, catalog, updates):
        fields = {"enabled", "registration_mode", "client_id", "client_secret", "scopes"}
        if catalog not in self.profiles or not isinstance(updates, dict) or set(updates) - fields:
            raise OAuthError("invalid_oauth_configuration")
        revision, config = self._settings(catalog)
        config.update(updates)
        if type(config["enabled"]) is not bool or config["registration_mode"] not in ("pre_registered", "dcr", "cimd"):
            raise OAuthError("invalid_oauth_configuration")
        if not _secret(config["client_id"]) or not _secret(config["client_secret"]):
            raise OAuthError("invalid_oauth_configuration")
        scopes = config["scopes"]
        if (not isinstance(scopes, list) or not 1 <= len(scopes) <= 20
                or any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9:_.\-/]{1,100}", s) for s in scopes)
                or len(set(scopes)) != len(scopes)):
            raise OAuthError("invalid_oauth_configuration")
        if catalog == "github" and config["registration_mode"] != "pre_registered":
            raise OAuthError("github_requires_owner_oauth_app")
        if catalog == "gmail":
            if config["registration_mode"] != "pre_registered":
                raise OAuthError("gmail_requires_owner_agent_oauth_app")
            if not set(scopes).issubset(self.profiles[catalog]["allowed_scopes"]):
                raise OAuthError("gmail_scope_not_allowed")
        if config["enabled"] and config["registration_mode"] == "pre_registered":
            if not config["client_id"] or (catalog in ("github", "gmail") and not config["client_secret"]):
                raise OAuthError("owner_oauth_client_required")
        if catalog == "exa" and config["client_secret"]:
            raise OAuthError("exa_requires_public_oauth_client")
        if config["registration_mode"] != "pre_registered" and (config["client_id"] or config["client_secret"]):
            raise OAuthError("registration_mode_has_managed_client")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?)", (catalog, revision + 1, json.dumps(config)))
            for client in self.db.execute("SELECT cache_key,body FROM clients").fetchall():
                if json.loads(client[1]).get("catalog") == catalog:
                    self.db.execute("DELETE FROM clients WHERE cache_key=?", (client[0],))
        for ident, item in list(self.manager.store.configs.items()):
            if item.catalog_id == catalog:
                await self.manager.invalidate_oauth(ident)
        return self.public_settings(catalog)

    def generation(self, ident):
        row = self.db.execute("SELECT generation FROM generations WHERE id=?", (ident,)).fetchone()
        return row[0] if row else 0

    def invalidate(self, ident):
        """Called under the manager's lock before a changed configuration is published."""
        grant = self.db.execute("SELECT body FROM grants WHERE id=?", (ident,)).fetchone()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO generations VALUES(?,?)", (ident, self.generation(ident) + 1))
            self.db.execute("DELETE FROM grants WHERE id=?", (ident,))
            self.db.execute("DELETE FROM transactions WHERE id=?", (ident,))
            if grant:
                self.db.execute("INSERT INTO revocations(connector,body) VALUES(?,?)", (ident, grant[0]))

    def has_grant(self, ident):
        row = self.db.execute("SELECT generation FROM grants WHERE id=?", (ident,)).fetchone()
        return bool(row and row[0] == self.generation(ident))

    def mark_invalid(self, ident, generation=None):
        row = self.db.execute("SELECT generation,body FROM grants WHERE id=?", (ident,)).fetchone()
        if row and (generation is None or row[0] == generation):
            value = json.loads(row[1])
            with self.db:
                for member, member_generation, grant in self._members(self._group(ident, value)):
                    grant["invalid"] = True
                    self.db.execute("UPDATE grants SET body=? WHERE id=? AND generation=?", (json.dumps(grant), member, member_generation))

    def revocation_pending(self, ident):
        return self.db.execute("SELECT count(*) FROM revocations WHERE connector=?", (ident,)).fetchone()[0]

    def _session(self):
        if self._closed:
            raise OAuthError("oauth_closed", 503)
        if self._http is None:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25, connect=10),
                                             cookie_jar=aiohttp.DummyCookieJar(), trust_env=False)
        return self._http

    def _endpoint(self, url):
        p = urlsplit(url)
        local = p.hostname in ("127.0.0.1", "localhost", "::1")
        if (not p.hostname or p.username is not None or p.password is not None or p.fragment
                or "\\" in url or any(c.isspace() for c in url)
                or (p.scheme != "https" and not (self.allow_loopback_http and local and p.scheme == "http"))):
            raise OAuthError("oauth_endpoint_rejected")
        return url

    async def _json(self, method, url, *, form=None, body=None, authorization=None):
        try:
            headers = {"Accept": "application/json"}
            if authorization is not None:
                headers["Authorization"] = authorization
            async with self._session().request(method, self._endpoint(url), data=form, json=body,
                                               headers=headers, allow_redirects=False) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise OAuthError("oauth_response_too_large", 502)
                if not 200 <= response.status < 300:
                    raise OAuthError("oauth_upstream_rejected", 502)
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeDecodeError, RecursionError) as error:
                    raise OAuthError("oauth_invalid_response", 502) from error
                if not isinstance(result, dict) or "error" in result:
                    raise OAuthError("oauth_upstream_rejected", 502)
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise OAuthError("oauth_upstream_unavailable", 502) from error

    async def _discovery(self, catalog):
        profile = self.profiles[catalog]
        if catalog == "github":
            return {**profile, "resource": None, "issuer_required": False}
        resource = await self._json("GET", profile["resource_metadata"])
        allowed_resources = {self.manager.catalog[catalog]["url"]}
        if catalog == "huggingface":
            allowed_resources.add(self.manager.catalog[catalog]["url"] + "?login")
        if (resource.get("resource") not in allowed_resources
                or resource.get("authorization_servers") != [profile.get("resource_issuer", profile["issuer"])]):
            raise OAuthError("oauth_resource_mismatch", 502)
        metadata = await self._json("GET", profile["metadata"])
        if metadata.get("issuer") != profile["issuer"] or "S256" not in metadata.get("code_challenge_methods_supported", []):
            raise OAuthError("oauth_issuer_mismatch", 502)
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
            if key in metadata and metadata[key] != profile.get(key):
                raise OAuthError("oauth_endpoint_rejected", 502)
        if any(metadata.get(k) != profile[k] for k in ("authorization_endpoint", "token_endpoint")):
            raise OAuthError("oauth_endpoint_rejected", 502)
        return {**profile, "resource": resource["resource"],
                "cimd": metadata.get("client_id_metadata_document_supported") is True,
                "dcr": metadata.get("registration_endpoint") == profile.get("registration_endpoint"),
                "issuer_required": metadata.get("authorization_response_iss_parameter_supported") is True}

    async def _client(self, catalog, revision, config, metadata, redirect):
        mode = config["registration_mode"]
        if mode == "pre_registered":
            return {"client_id": config["client_id"], "client_secret": config["client_secret"]}
        if mode == "cimd":
            if not metadata.get("cimd"):
                raise OAuthError("oauth_cimd_unavailable", 503)
            return {"client_id": self.origin + CLIENT_PREFIX + catalog, "client_secret": ""}
        if not metadata.get("dcr"):
            raise OAuthError("oauth_registration_unavailable", 503)
        key = _digest(json.dumps([catalog, revision, redirect]))
        cached = self.db.execute("SELECT body FROM clients WHERE cache_key=?", (key,)).fetchone()
        if cached:
            client = json.loads(cached[0])
            if not client.get("client_secret_expires_at") or client["client_secret_expires_at"] > self.clock():
                return client
        result = await self._json("POST", metadata["registration_endpoint"], body={
            "client_name": "duet", "redirect_uris": [redirect], "response_types": ["code"],
            "grant_types": ["authorization_code", "refresh_token"], "token_endpoint_auth_method": "none",
        })
        client = {"client_id": result.get("client_id"), "client_secret": result.get("client_secret", "")}
        if not _secret(client["client_id"]) or not client["client_id"] or not _secret(client["client_secret"]):
            raise OAuthError("oauth_invalid_client_response", 502)
        if result.get("token_endpoint_auth_method", "none") not in ("none", "client_secret_post", "client_secret_basic"):
            raise OAuthError("oauth_invalid_client_response", 502)
        client["token_endpoint_auth_method"] = result.get(
            "token_endpoint_auth_method", "client_secret_basic" if client["client_secret"] else "none")
        expiry = result.get("client_secret_expires_at", 0)
        if type(expiry) not in (int, float) or not 0 <= expiry < 10 ** 12:
            raise OAuthError("oauth_invalid_client_response", 502)
        if client["token_endpoint_auth_method"] != "none" and not client["client_secret"]:
            raise OAuthError("oauth_invalid_client_response", 502)
        client["client_secret_expires_at"] = expiry
        client["catalog"] = catalog
        if "redirect_uris" in result and result["redirect_uris"] != [redirect]:
            raise OAuthError("oauth_invalid_client_response", 502)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO clients VALUES(?,?)", (key, json.dumps(client)))
        return client

    def _bound(self, ident, generation, owner, redirect):
        config = self.manager.store.configs.get(ident)
        return bool(config and config.enabled and config.auth_type == "oauth"
                    and generation == self.generation(ident) and self.origin
                    and redirect == self.origin + CALLBACK_PREFIX + config.catalog_id and self.owner_valid(owner))

    def _grant(self, ident):
        row = self.db.execute("SELECT generation,body FROM grants WHERE id=?", (ident,)).fetchone()
        if not row or row[0] != self.generation(ident):
            raise OAuthError("auth_required", 401)
        grant = json.loads(row[1])
        config = self.manager.store.configs.get(ident)
        if (not config or config.catalog_id != grant["catalog"] or not config.enabled or config.auth_type != "oauth"
                or not self.origin or grant["redirect"] != self.origin + CALLBACK_PREFIX + grant["catalog"]
                or grant["revision"] != self._settings(grant["catalog"])[0]):
            raise OAuthError("auth_required", 401)
        return row[0], grant

    @staticmethod
    def _group(ident, grant):
        return grant.get("group", "legacy:" + ident)

    def _members(self, group):
        result = []
        for row in self.db.execute("SELECT id FROM grants").fetchall():
            try:
                generation, grant = self._grant(row[0])
            except OAuthError:
                continue
            if self._group(row[0], grant) == group:
                result.append((row[0], generation, grant))
        return result

    def _share_targets(self, ident, targets, owner):
        config = self.manager.store.configs[ident]
        if len(targets) > 7 or len(set(targets)) != len(targets) or ident in targets:
            raise OAuthError("mcp_invalid_grant_selection", 409)
        selected = []
        for target in targets:
            other = self.manager.store.configs.get(target)
            generation = self.generation(target)
            redirect = self.origin + CALLBACK_PREFIX + config.catalog_id
            if (not other or other.catalog_id != config.catalog_id
                    or not self._bound(target, generation, owner, redirect) or self.has_grant(target)):
                raise OAuthError("mcp_incompatible_grant_selection", 409)
            selected.append({"id": target, "generation": generation, "consent": self._consent_state(target)})
        return selected

    def _consent_state(self, ident):
        return [list(row) for row in self.db.execute("SELECT state_hash,status FROM transactions WHERE id=? ORDER BY state_hash", (ident,))]

    def _check_shares(self, body):
        for target in body.get("share", []):
            if (not self._bound(target["id"], target["generation"], body["owner"], body["redirect"])
                    or self.has_grant(target["id"]) or self._consent_state(target["id"]) != target["consent"]):
                raise OAuthError("mcp_invalid_state", 409)

    async def adopt(self, ident, source, principal, *, authorize=None):
        """Explicit same-account selection, not a fallback for a failed login."""
        owner = _owner(principal)
        selected = self._share_targets(source, [ident], owner)
        source_generation, original = self._grant(source)
        if original["owner"][:2] != owner[:2]:
            raise OAuthError("mcp_incompatible_grant_selection", 409)
        group = self._group(source, original)
        guard = {"owner": owner, "redirect": original["redirect"], "share": selected}
        committed = False
        try:
            async with self._locks.setdefault(("grant", group), asyncio.Lock()):
                generation, grant = self._grant(source)
                if generation != source_generation or self._group(source, grant) != group:
                    raise OAuthError("mcp_invalid_state", 409)
                revision, settings = self._settings(grant["catalog"])
                metadata = await self._discovery(grant["catalog"])
                self._check_shares(guard)
                client = await self._client(grant["catalog"], revision, settings, metadata, grant["redirect"])
                self._check_shares(guard)
                current_generation, current = self._grant(source)
                if (current_generation != generation or current != grant or grant.get("invalid")
                        or grant.get("refresh_uncertain") or revision != grant["revision"]
                        or metadata != grant["metadata"] or client != grant["client"]
                        or set(grant.get("requested_scopes", settings["scopes"])) != set(settings["scopes"])
                        or grant.get("scope") is not None and not set(settings["scopes"]).issubset(grant["scope"].split())):
                    raise OAuthError("mcp_incompatible_grant_selection", 409)
                await self.manager.reset_oauth_permissions(ident)
                if authorize is not None:
                    authorize()
                self._check_shares(guard)
                if self._grant(source) != (generation, grant):
                    raise OAuthError("mcp_invalid_state", 409)
                # Adding the group marker does not change the old install's identity.
                shared = dict(grant, group=group)
                with self.db:
                    self.db.execute("DELETE FROM transactions WHERE id=?", (ident,))
                    self.db.execute("UPDATE grants SET body=? WHERE id=?", (json.dumps(shared), source))
                    self.db.execute("INSERT INTO grants VALUES(?,?,?)", (ident, selected[0]["generation"], json.dumps(shared)))
                committed = True
            await self.manager.refresh(ident)
            if authorize is not None:
                authorize()
            if (not self._bound(ident, selected[0]["generation"], owner, grant["redirect"])
                    or self._group(ident, self._grant(ident)[1]) != group):
                raise OAuthError("mcp_invalid_state", 409)
            return {"connected": self.manager._states.get(ident, {}).get("status") == "connected"}
        except BaseException:
            if committed:
                row = self.db.execute("SELECT body FROM grants WHERE id=?", (ident,)).fetchone()
                if row and self._group(ident, json.loads(row[0])) == group:
                    await self.manager.invalidate_oauth(ident, generation=selected[0]["generation"])
            raise

    async def begin(self, ident, principal, *, share=(), authorize=None):
        config = self.manager.store.configs[ident]
        catalog = config.catalog_id
        revision, settings = self._settings(catalog)
        if not self.flow_enabled(catalog):
            raise OAuthError("mcp_oauth_unavailable", 503)
        owner, generation = _owner(principal), self.generation(ident)
        selected = self._share_targets(ident, share, owner)
        redirect = self.origin + CALLBACK_PREFIX + catalog
        if not self._bound(ident, generation, owner, redirect):
            raise OAuthError("mcp_invalid_state", 409)
        async with self._locks.setdefault(ident, asyncio.Lock()):
            metadata = await self._discovery(catalog)
            if not self._bound(ident, generation, owner, redirect) or revision != self._settings(catalog)[0]:
                raise OAuthError("mcp_invalid_state", 409)
            client = await self._client(catalog, revision, settings, metadata, redirect)
            if not self._bound(ident, generation, owner, redirect) or revision != self._settings(catalog)[0]:
                raise OAuthError("mcp_invalid_state", 409)
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            body = {"catalog": catalog, "owner": owner, "redirect": redirect, "verifier": verifier,
                    "metadata": metadata, "client": client, "scopes": settings["scopes"], "revision": revision,
                    "share": selected}
            self._check_shares(body)
            if authorize is not None:
                authorize()
            with self.db:
                self.db.execute("DELETE FROM transactions WHERE id=? OR expires<?", (ident, self.clock()))
                self.db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?)", (
                    _digest(state), ident, generation, self.clock() + TRANSACTION_TTL, "pending", json.dumps(body)))
            parameters = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
                          "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                          "scope": " ".join(settings["scopes"])}
            if metadata["resource"]:
                parameters["resource"] = metadata["resource"]
            parameters.update(metadata.get("authorization_parameters", {}))
            return {"auth_url": metadata["authorization_endpoint"] + "?" + urlencode(parameters), "state": state,
                    "connected": False}

    def _transaction(self, state):
        if not _secret(state, 128) or len(state) < 32:
            raise OAuthError("mcp_invalid_state", 409)
        row = self.db.execute("SELECT * FROM transactions WHERE state_hash=?", (_digest(state),)).fetchone()
        if not row or row["status"] == "consumed":
            raise OAuthError("mcp_invalid_state", 409)
        body = json.loads(row["body"])
        if row["expires"] <= self.clock() or not self._bound(row["id"], row["generation"], body["owner"], body["redirect"]):
            raise OAuthError("mcp_invalid_state", 409)
        if body["revision"] != self._settings(body["catalog"])[0]:
            raise OAuthError("mcp_invalid_state", 409)
        return row, body

    def _save_transaction(self, row, body, status, expires=None):
        with self.db:
            self.db.execute("UPDATE transactions SET status=?,body=?,expires=? WHERE state_hash=?", (
                status, json.dumps(body), row["expires"] if expires is None else expires, row["state_hash"]))

    async def _token(self, metadata, client, form):
        form = dict(form, client_id=client["client_id"])
        method = client.get("token_endpoint_auth_method", "client_secret_post" if client["client_secret"] else "none")
        authorization = None
        if method == "client_secret_basic":
            authorization = _basic(client)
        elif method == "client_secret_post":
            form["client_secret"] = client["client_secret"]
        result = await self._json("POST", metadata["token_endpoint"], form=form, authorization=authorization)
        if not _secret(result.get("access_token")) or not result["access_token"] or str(result.get("token_type", "")).lower() != "bearer":
            raise OAuthError("oauth_invalid_token_response", 502)
        refresh = result.get("refresh_token", "")
        lifetime = result.get("expires_in")
        if not _secret(refresh) or (lifetime is not None and (type(lifetime) not in (int, float) or not 0 < lifetime <= 366 * 86400)):
            raise OAuthError("oauth_invalid_token_response", 502)
        scope = result.get("scope")
        if scope is not None and (not isinstance(scope, str) or len(scope) > 2048):
            raise OAuthError("oauth_invalid_token_response", 502)
        return {"access_token": result["access_token"], "refresh_token": refresh,
                "expires_at": self.clock() + lifetime if lifetime is not None else None,
                "scope": scope, "invalid": False}

    @asynccontextmanager
    async def _provider_guard(self, catalog, client):
        # Google revocation covers the whole project/user grant. Serialize its
        # entire remote request with token issuance and durable grant commits.
        # Installation/group locks precede this guard; revocation never acquires
        # those locks or the manager lock while holding it.
        if catalog == "gmail":
            key = (catalog, client["client_id"])
            async with self._provider_locks.setdefault(key, asyncio.Lock()):
                yield
        else:
            yield

    async def provider_callback(self, catalog, state, *, code=None, issuer=None, error=None):
        row, body = self._transaction(state)
        if catalog != body["catalog"] or row["status"] != "pending":
            raise OAuthError("mcp_invalid_state", 409)
        metadata = body["metadata"]
        if (issuer is not None and issuer != metadata["issuer"]) or (metadata["issuer_required"] and issuer is None):
            raise OAuthError("oauth_issuer_mismatch", 409)
        if error is not None:
            body["error"] = "oauth_access_denied" if error == "access_denied" else "oauth_authorization_failed"
            self._save_transaction(row, body, "failed")
            return False
        if not _secret(code) or not code:
            raise OAuthError("oauth_code_required")
        self._save_transaction(row, body, "exchanging")
        ident, generation = row["id"], row["generation"]
        grant, grant_stored = None, False
        installations = [{"id": ident, "generation": generation}, *body.get("share", [])]
        try:
            async with self._locks.setdefault(ident, asyncio.Lock()), self._provider_guard(catalog, body["client"]):
                form = {"grant_type": "authorization_code", "code": code, "code_verifier": body["verifier"],
                        "redirect_uri": body["redirect"]}
                if metadata["resource"]:
                    form["resource"] = metadata["resource"]
                token = await self._token(metadata, body["client"], form)
                grant = {**token, "metadata": metadata, "client": body["client"], "owner": body["owner"],
                         "catalog": catalog, "redirect": body["redirect"], "revision": body["revision"],
                         "requested_scopes": body["scopes"], "group": secrets.token_hex(24)}
                self._transaction(state)
                self._check_shares(body)
                if body.get("share") and token["scope"] is not None and not set(body["scopes"]).issubset(token["scope"].split()):
                    raise OAuthError("mcp_incompatible_grant_selection", 409)
                for installation in installations:
                    await self.manager.reset_oauth_permissions(installation["id"])
                    self._transaction(state)
                    self._check_shares(body)
                with self.db:
                    old = self.db.execute("SELECT body FROM grants WHERE id=?", (ident,)).fetchone()
                    if old and json.loads(old[0])["access_token"] != grant["access_token"]:
                        self.db.execute("INSERT INTO revocations(connector,body) VALUES(?,?)", (ident, old[0]))
                    self.db.execute("INSERT OR REPLACE INTO grants VALUES(?,?,?)", (ident, generation, json.dumps(grant)))
                    for target in body.get("share", []):
                        self.db.execute("DELETE FROM transactions WHERE id=?", (target["id"],))
                        self.db.execute("INSERT INTO grants VALUES(?,?,?)", (target["id"], target["generation"], json.dumps(grant)))
                grant_stored = True
            for installation in installations:
                await self.manager.refresh(installation["id"])
                self._transaction(state)
            row, body = self._transaction(state)
            if self.manager._states.get(ident, {}).get("status") != "connected":
                raise OAuthError("mcp_discovery_failed", 502)
            body.pop("verifier", None)
            body.pop("client", None)
            body["ticket"] = "duet_mcp_" + secrets.token_urlsafe(32)
            self._save_transaction(row, body, "ready", min(row["expires"], self.clock() + TICKET_TTL))
            await self.revoke_pending(ident)
            return True
        except (MCPError, asyncio.CancelledError) as failure:
            # A completed exchange whose owner changed must never become usable.
            if grant is not None and (isinstance(failure, asyncio.CancelledError) or not grant_stored or row["expires"] <= self.clock()
                                      or not self._bound(ident, generation, body["owner"], body["redirect"])):
                invalidated = False
                if grant_stored:
                    for installation in installations:
                        try:
                            _, active = self._grant(installation["id"])
                        except OAuthError:
                            continue
                        if active.get("group") == grant["group"]:
                            invalidated = await self.manager.invalidate_oauth(installation["id"], generation=installation["generation"]) or invalidated
                if not invalidated:
                    with self.db:
                        self.db.execute("INSERT INTO revocations(connector,body) VALUES(?,?)", (ident, json.dumps(grant)))
                    await self.revoke_pending(ident)
            current = self.db.execute("SELECT * FROM transactions WHERE state_hash=?", (_digest(state),)).fetchone()
            if current:
                body.pop("verifier", None)
                body.pop("client", None)
                body["error"] = failure.code if isinstance(failure, MCPError) else "oauth_authorization_cancelled"
                self._save_transaction(current, body, "failed")
            raise

    def status(self, ident, state, principal):
        row, body = self._transaction(state)
        if ident is None:
            ident = row["id"]
        if row["id"] != ident or body["owner"] != _owner(principal):
            raise OAuthError("mcp_invalid_state", 409)
        result = {"status": row["status"], "state": state, "connector_id": ident,
                  "server_uuid": self.manager.server_uuid(ident)}
        if row["status"] == "ready":
            result["code"] = body["ticket"]
        if row["status"] == "failed":
            result["error_code"] = body.get("error", "oauth_authorization_failed")
        return result

    def complete(self, code, state, principal):
        row, body = self._transaction(state)
        if body["owner"] != _owner(principal) or row["status"] != "ready" or not isinstance(code, str):
            raise OAuthError("mcp_invalid_state", 409)
        if not secrets.compare_digest(body["ticket"], code):
            raise OAuthError("mcp_invalid_state", 409)
        self._save_transaction(row, {}, "consumed")
        return {"success": True, "needs_label": False}

    async def access_token(self, ident, *, force=False):
        captured_generation, captured = self._grant(ident)
        group = self._group(ident, captured)
        async with self._locks.setdefault(("grant", group), asyncio.Lock()):
            generation, grant = self._grant(ident)
            if generation != captured_generation or self._group(ident, grant) != group:
                raise OAuthError("connector_changed_start_new_turn", 409)
            if grant.get("refresh_uncertain"):
                raise OAuthError("token_invalid", 401)
            expired = grant["expires_at"] is not None and grant["expires_at"] <= self.clock() + 30
            # Another member may already have rotated while this caller waited.
            if force and grant["access_token"] != captured["access_token"]:
                force = False
            if not force and not expired and not grant.get("invalid"):
                return grant["access_token"]
            if not grant["refresh_token"]:
                self.mark_invalid(ident, generation)
                raise OAuthError("token_invalid", 401)
            form = {"grant_type": "refresh_token", "refresh_token": grant["refresh_token"]}
            if grant["metadata"]["resource"]:
                form["resource"] = grant["metadata"]["resource"]
            # Durable before sending: cancellation, a lost response, or process
            # death may have spent this one-time refresh token. Never replay it.
            with self.db:
                for member, member_generation, current in self._members(group):
                    current.update(invalid=True, refresh_uncertain=True)
                    self.db.execute("UPDATE grants SET body=? WHERE id=? AND generation=?",
                                    (json.dumps(current), member, member_generation))
            async with self._provider_guard(grant["catalog"], grant["client"]):
                try:
                    token = await self._token(grant["metadata"], grant["client"], form)
                except MCPError:
                    raise OAuthError("refresh_failed", 401)
                if not token["refresh_token"]:
                    token["refresh_token"] = grant["refresh_token"]
                if token["scope"] is None:
                    token["scope"] = grant.get("scope")
                grant.update(token)
                grant["refresh_uncertain"] = False
                members = self._members(group)
                with self.db:
                    for member, member_generation, _ in members:
                        self.db.execute("UPDATE grants SET body=? WHERE id=? AND generation=?", (json.dumps(grant), member, member_generation))
                    if not members:
                        self.db.execute("INSERT INTO revocations(connector,body) VALUES(?,?)", (ident, json.dumps(grant)))
            if not any(member == ident and member_generation == generation for member, member_generation, _ in members):
                if not members:
                    await self.revoke_pending(ident)
                raise OAuthError("connector_changed_start_new_turn", 409)
            return grant["access_token"]

    async def revoke_pending(self, ident):
        rows = self.db.execute("SELECT id,body FROM revocations WHERE connector=? ORDER BY id", (ident,)).fetchall()
        for row in rows:
            grant = json.loads(row[1])
            async with self._provider_guard(grant["catalog"], grant["client"]):
                # A competing cleanup can consume a queued row while we wait.
                if self.db.execute("SELECT 1 FROM revocations WHERE id=?", (row[0],)).fetchone():
                    await self._revoke_pending_row(ident, row, grant)

    async def _revoke_pending_row(self, ident, row, grant):
        active_grants = [json.loads(item[0]) for item in self.db.execute("SELECT body FROM grants")]
        if grant["catalog"] == "gmail":
            # Google revocation is project-wide for a Google user, and we
            # deliberately do not discover external account identity. Protect
            # even independent sibling consents under this OAuth client.
            client_id = grant["client"]["client_id"]
            same_client_grant = any(active["catalog"] == "gmail"
                and active["client"]["client_id"] == client_id for active in active_grants)
            current_client = self._settings("gmail")[1]["client_id"]
            pending_sibling = current_client == client_id and any(
                item.id != ident and item.catalog_id == "gmail" and item.enabled
                for item in self.manager.store.configs.values())
            if same_client_grant or pending_sibling:
                # Erase removed credentials, not a deferred credential cache.
                # Last active install can revoke its own token/project later.
                with self.db:
                    self.db.execute("DELETE FROM revocations WHERE id=?", (row[0],))
                return
        if any(grant["catalog"] == active["catalog"] and grant["owner"][:2] == active["owner"][:2]
                and grant["client"]["client_id"] == active["client"]["client_id"]
                and grant["metadata"]["issuer"] == active["metadata"]["issuer"]
                and (grant.get("group") is not None and grant.get("group") == active.get("group")
                     or grant["access_token"] == active["access_token"]
                     or grant["refresh_token"] and grant["refresh_token"] == active["refresh_token"])
                for active in active_grants):
            # Some providers return the same token after a new, explicit consent.
            # A queued revocation from the old installation must not revoke that new grant.
            with self.db:
                self.db.execute("DELETE FROM revocations WHERE id=?", (row[0],))
            return
        endpoint = grant["metadata"].get("revocation_endpoint")
        if endpoint:
            try:
                if grant["catalog"] == "github":
                    url = endpoint.replace("{client_id}", quote(grant["client"]["client_id"], safe=""))
                    await self._revoke_request("DELETE", url, body={"access_token": grant["access_token"]},
                                               authorization=_basic(grant["client"]))
                elif grant["catalog"] == "gmail":
                    # Google revokes the grant/project, including associated
                    # tokens. A second access-token request can therefore fail
                    # after successful refresh-token revocation.
                    await self._revoke_request("POST", endpoint,
                        form={"token": grant["refresh_token"] or grant["access_token"]})
                else:
                    for token, hint in ((grant["refresh_token"], "refresh_token"), (grant["access_token"], "access_token")):
                        if token:
                            form = {"token": token, "token_type_hint": hint, "client_id": grant["client"]["client_id"]}
                            if grant["client"]["client_secret"]:
                                form["client_secret"] = grant["client"]["client_secret"]
                            await self._revoke_request("POST", endpoint, form=form)
            except MCPError:
                return  # Local access is already revoked; retain a private retry record.
        with self.db:
            self.db.execute("DELETE FROM revocations WHERE id=?", (row[0],))

    async def _revoke_request(self, method, url, *, body=None, form=None, authorization=None):
        try:
            headers = {"Authorization": authorization} if authorization is not None else {}
            async with self._session().request(method, self._endpoint(url), json=body, data=form,
                                               headers=headers, allow_redirects=False) as response:
                if not 200 <= response.status < 300:
                    raise OAuthError("oauth_revocation_failed", 502)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise OAuthError("oauth_revocation_failed", 502) from error

    def client_metadata(self, catalog):
        _, config = self._settings(catalog)
        if not self.flow_enabled(catalog) or config["registration_mode"] != "cimd":
            raise OAuthError("oauth_client_not_found", 404)
        return {"client_id": self.origin + CLIENT_PREFIX + catalog, "client_name": "duet",
                "redirect_uris": [self.origin + CALLBACK_PREFIX + catalog],
                "token_endpoint_auth_method": "none", "response_types": ["code"],
                "grant_types": ["authorization_code", "refresh_token"]}

    async def close(self):
        self._closed = True
        if self._http is not None:
            await self._http.close()
        self.db.close()

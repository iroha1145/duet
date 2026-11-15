"""The retained Android Connections protocol over the reviewed server-side catalog."""
import functools
import asyncio
import secrets

from aiohttp import web

from .auth import AUTH_MANAGER, AUTH_PRINCIPAL
from .connector_oauth import CALLBACK_PREFIX, CLIENT_PREFIX, ConnectorOAuth, OAuthError, PROVIDERS
from .connectors import MCPError, MAX_CONNECTORS
from .rest import ACCOUNT_ID, ORGANIZATION_ID, ACCOUNT_SETTINGS
from .connector_permissions import PermissionConflict
from .voice_preferences import VOICE_PREFERENCES, FIELDS as VOICE_FIELDS
from .speech import limited_body
import json


NATIVE_CONNECTORS = web.AppKey("duet_native_connectors", object)
# Add these exact GET routes to the host's public_routes before constructing it.
OAUTH_PUBLIC_ROUTES = frozenset(("GET", prefix + catalog)
                                for prefix in (CALLBACK_PREFIX, CLIENT_PREFIX) for catalog in PROVIDERS)
CONNECTOR_DIRECTORY_FEATURE = {"defaultValue": {"visibility": ["public"]}}
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff"}


def _json(value, status=200):
    return web.json_response(value, status=status, headers=_HEADERS)


def _endpoint(handler):
    @functools.wraps(handler)
    async def wrapped(self, request):
        try:
            return await handler(self, request)
        except OAuthError as error:
            return _json({"type": "error", "error": {"type": error.code, "message": error.code}}, error.status)
        except PermissionConflict as error:
            return _json({"type": "error", "error": {"type": "permission_conflict", "message": str(error)}}, 409)
        except (ValueError, TypeError) as error:
            return _json({"type": "error", "error": {"type": "invalid_argument", "message": "Invalid connector request"}}, 400)
        except MCPError as error:
            return _json({"type": "error", "error": {"type": error.code, "message": error.code}}, 502)
    return wrapped


class NativeConnectors:
    def __init__(self, manager, public_origin, *, auth_manager=None, owner_valid=None,
                 profiles=None, allow_loopback_http=False, clock=None):
        self.manager, self.auth_manager = manager, auth_manager
        self._create_lock = asyncio.Lock()

        def active(owner):
            return bool(auth_manager and owner[0] == ACCOUNT_ID and owner[1] == ORGANIZATION_ID
                        and auth_manager.session_valid(owner[2]))

        kwargs = {"owner_valid": owner_valid or active, "profiles": profiles,
                  "allow_loopback_http": allow_loopback_http}
        if clock is not None:
            kwargs["clock"] = clock
        self.oauth = ConnectorOAuth(manager, public_origin, **kwargs)
        manager.oauth = self.oauth

    def principal(self, request):
        principal = request.get(AUTH_PRINCIPAL)
        if principal is None:
            raise OAuthError("authentication_required", 401)
        if principal.account_id != ACCOUNT_ID or principal.organization_id != ORGANIZATION_ID:
            raise OAuthError("connector_account_not_supported", 403)
        if not self.oauth.owner_valid([principal.account_id, principal.organization_id, principal.device_id]):
            raise OAuthError("authentication_required", 401)
        organization = request.match_info.get("organization")
        if organization is not None and organization != principal.organization_id:
            raise OAuthError("not_found", 404)
        return principal

    def ident(self, uuid):
        for ident in self.manager.store.configs:
            if self.manager.server_uuid(ident) == uuid:
                return ident
        raise OAuthError("mcp_server_not_found", 404)

    def catalog(self, ident):
        for catalog in self.manager.catalog:
            if ident == self.manager.directory_uuid(catalog):
                return catalog
        raise OAuthError("mcp_directory_entry_not_found", 404)

    def settings_commit_principal(self, request):
        # Body upload yields to logout/refresh/revocation. Recheck both the
        # account binding and the exact authenticated access-token epoch at
        # the final synchronous commit boundary, for MCP and voice alike.
        principal = self.principal(request)
        authorized = request.get("duet_authorized")
        if authorized is not None and not authorized():
            raise OAuthError("authentication_required", 401)
        if self.auth_manager is not None and not self.auth_manager.session_valid(
                principal.device_id, principal.access_token_hash):
            raise OAuthError("authentication_required", 401)
        return principal

    @_endpoint
    async def account_settings(self, request):
        self.principal(request)
        # Preserve the host's original 1 MiB account JSON boundary: a valid
        # 2049-leaf native MCP map is substantially larger than a voice delta.
        raw = await limited_body(request.content, 1024 * 1024)
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("Unsupported account settings")
        if set(body) != {"enabled_mcp_tools"}:
            # Native sends pure deltas. Reject mixed MCP/voice updates before
            # either store can write, preserving permission epoch/ETag behavior.
            preferences = request.app.get(VOICE_PREFERENCES)
            if not preferences or not body or set(body) - VOICE_FIELDS or len(raw) > 8192:
                raise ValueError("Unsupported account settings")
            self.settings_commit_principal(request)
            return _json(preferences.update(body))
        self.settings_commit_principal(request)
        flags = self.manager.permissions.update(body["enabled_mcp_tools"], self.manager.permission_versions(),
                                                if_match=request.headers.get("If-Match"))
        response = _json({"enabled_mcp_tools": flags})
        response.headers["ETag"] = self.manager.permissions.etag()
        return response

    @_endpoint
    async def directory(self, request):
        self.principal(request)
        if request.query.get("type", "remote") != "remote":
            return _json({"servers": [], "total": 0, "next_cursor": None})
        if request.query.get("visibility", "public") != "public":
            return _json({"servers": [], "total": 0, "next_cursor": None})
        limit = int(request.query.get("limit", 50))
        if not 1 <= limit <= 100:
            raise OAuthError("invalid_argument")
        query = request.query.get("q", "").casefold()
        if len(query) > 200:
            raise OAuthError("invalid_argument")
        ids = request.query.get("ids")
        ids = set(ids.split(",")) if ids is not None else None
        servers = []
        for catalog, entry in sorted(self.manager.catalog.items()):
            uuid = self.manager.directory_uuid(catalog)
            if ids is not None and uuid not in ids:
                continue
            if query and query not in (entry["name"] + " " + catalog).casefold():
                continue
            servers.append({"id": uuid, "type": "remote", "name": entry["name"],
                            "display_name": entry["name"], "one_liner": entry["note"],
                            "documentation": entry["documentation"], "has_mcp_app": False,
                            "remote": {"url": entry["url"], "is_authless": entry["default_auth"] == "none",
                                       "required_fields": [], "use_web_form": False}})
        cursor = request.query.get("cursor", "")
        offset = int(cursor) if cursor else 0
        if offset < 0 or offset > len(servers):
            raise OAuthError("invalid_cursor")
        total = len(servers)
        return _json({"servers": servers[offset:offset + limit], "total": total,
                      "next_cursor": str(offset + limit) if offset + limit < total else None})

    @_endpoint
    async def create(self, request):
        self.principal(request)
        body = await request.json()
        if (not isinstance(body, dict) or set(body) - {"name", "url", "attestations", "mcp_directory_server_uuid", "additional_install"}
                or not isinstance(body.get("name"), str) or not 1 <= len(body["name"]) <= 120):
            raise OAuthError("invalid_argument")
        additional = body.get("additional_install")
        if additional is not None and type(additional) is not bool:
            raise OAuthError("invalid_argument")
        if body.get("attestations") not in (None, []):
            raise OAuthError("mcp_attestations_not_supported")
        matches = [catalog for catalog, entry in self.manager.catalog.items() if body.get("url") == entry["url"]]
        if len(matches) != 1:
            raise OAuthError("mcp_server_url_not_allowed")
        catalog = matches[0]
        if body.get("mcp_directory_server_uuid") not in (None, self.manager.directory_uuid(catalog)):
            raise OAuthError("mcp_directory_entry_mismatch")
        async with self._create_lock:
            self.settings_commit_principal(request)
            ident = None if additional else self.manager.store.primary.get(catalog)
            existing = ident is not None
            if not existing:
                if len(self.manager.store.configs) >= MAX_CONNECTORS:
                    raise OAuthError("mcp_additional_install_limit" if additional else "mcp_install_limit", 409)
                ident = "i" + secrets.token_hex(11)
                while ident in self.manager.store.configs:
                    ident = "i" + secrets.token_hex(11)
                auth_type = "oauth" if self.oauth.flow_enabled(catalog) else self.manager.catalog[catalog]["default_auth"]
                label = body["name"] if body["name"] != self.manager.catalog[catalog]["name"] else "连接 " + ident[-6:]
                await self.manager.configure(ident, {"catalog_id": catalog, "enabled": True, "auth_type": auth_type,
                                             "additional_install": bool(additional), "label": label},
                                             authorize=lambda: self.settings_commit_principal(request))
            elif not self.manager.store.configs[ident].enabled:
                await self.manager.configure(ident, {"enabled": True}, authorize=lambda: self.settings_commit_principal(request))
            generation = self.oauth.generation(ident)
        try:
            await self.manager.refresh(ident)
        except KeyError:
            raise OAuthError("mcp_server_not_found", 404) from None
        self.settings_commit_principal(request)
        if ident not in self.manager.store.configs:
            raise OAuthError("mcp_server_not_found", 404)
        if generation != self.oauth.generation(ident):
            raise OAuthError("mcp_invalid_state", 409)
        entry = self.manager.catalog[catalog]
        connected = self.manager._states.get(ident, {}).get("status") == "connected"
        return _json({"uuid": self.manager.server_uuid(ident), "name": entry["name"], "url": entry["url"],
                      "signInPending": not connected and self.manager.store.configs[ident].auth_type != "none",
                      "existing": existing, "additionalInstall": self.manager.store.configs[ident].additional_install})

    @_endpoint
    async def start(self, request):
        principal = self.principal(request)
        ident = self.ident(request.match_info["server"])
        adopted = request.query.getall("adopt_from", [])
        shared = request.query.getall("share_grant_with", [])
        if len(adopted) > 1 or adopted and shared or any(not value for value in [*adopted, *shared]):
            raise OAuthError("invalid_argument")
        source = self.ident(adopted[0]) if adopted else None
        targets = [self.ident(value) for value in shared]
        if request.query.get("initiation") not in (None, "auto"):
            raise OAuthError("invalid_argument")
        config = self.manager.store.configs[ident]
        if not config.enabled:
            await self.manager.configure(ident, {"enabled": True}, authorize=lambda: self.settings_commit_principal(request))
            config = self.manager.store.configs[ident]
        generation = self.oauth.generation(ident)
        try:
            await self.manager.refresh(ident)
        except KeyError:
            raise OAuthError("mcp_server_not_found", 404) from None
        principal = self.settings_commit_principal(request)
        if generation != self.oauth.generation(ident) or ident not in self.manager.store.configs:
            raise OAuthError("mcp_invalid_state", 409)
        if source is not None:
            return _json(await self.oauth.adopt(ident, source, principal, authorize=lambda: self.settings_commit_principal(request)))
        if not targets and self.manager._states.get(ident, {}).get("status") == "connected":
            return _json({"connected": True})
        if config.auth_type == "none" or (config.auth_type != "oauth" and config.token):
            raise OAuthError("mcp_discovery_failed", 502)
        if not self.oauth.flow_enabled(config.catalog_id):
            raise OAuthError("mcp_oauth_unavailable", 503)
        if config.auth_type != "oauth":
            await self.manager.configure(ident, {"auth_type": "oauth", "token": ""}, authorize=lambda: self.settings_commit_principal(request))
        principal = self.settings_commit_principal(request)
        return _json(await self.oauth.begin(ident, principal, share=targets, authorize=lambda: self.settings_commit_principal(request)))

    @_endpoint
    async def status(self, request):
        principal = self.principal(request)
        if len(request.query.getall("state", [])) != 1:
            raise OAuthError("invalid_argument")
        ident = request.match_info.get("connector_id")
        if ident is not None and ident not in self.manager.store.configs:
            raise OAuthError("mcp_server_not_found", 404)
        return _json(self.oauth.status(ident, request.query.get("state", ""), principal))

    @_endpoint
    async def complete(self, request):
        principal = self.principal(request)
        if any(len(request.query.getall(field, [])) != 1 for field in ("code", "state")):
            raise OAuthError("invalid_argument")
        # The native endpoint accepts a server-issued completion ticket, never a provider code.
        return _json(self.oauth.complete(request.query.get("code", ""), request.query.get("state", ""), principal))

    @_endpoint
    async def logout(self, request):
        self.principal(request)
        await self.manager.disconnect(self.ident(request.match_info["server"]))
        return _json({})

    @_endpoint
    async def delete(self, request):
        self.principal(request)
        await self.manager.delete(self.ident(request.match_info["server"]))
        return _json({})

    @_endpoint
    async def oauth_get(self, request):
        self.principal(request)
        catalog = request.match_info["catalog"]
        if catalog not in self.manager.catalog:
            raise OAuthError("mcp_directory_entry_not_found", 404)
        return _json(self.oauth.public_settings(catalog))

    @_endpoint
    async def oauth_put(self, request):
        self.principal(request)
        return _json(await self.oauth.configure(request.match_info["catalog"], await request.json()))

    async def provider_callback(self, request):
        # No bearer/cookie is expected from the provider's browser redirect.
        # This handler is public only at the exact reviewed catalog callback paths.
        try:
            for field in ("state", "code", "iss", "error"):
                if len(request.query.getall(field, [])) > 1:
                    raise OAuthError("invalid_argument")
            ok = await self.oauth.provider_callback(request.match_info["catalog"], request.query.get("state", ""),
                code=request.query.get("code"), issuer=request.query.get("iss"), error=request.query.get("error"))
            message = "Authorization completed. Return to duet." if ok else "Authorization was not completed. Return to duet."
            status = 200 if ok else 400
        except MCPError:
            message, status = "Authorization could not be completed. Return to duet and try again.", 400
        return web.Response(text=message, status=status, content_type="text/plain", headers=_HEADERS)

    @_endpoint
    async def client_metadata(self, request):
        return _json(self.oauth.client_metadata(request.match_info["catalog"]))


def register_native_connectors(app, manager, public_origin, *, auth_manager=None, **kwargs):
    """Call after register_connectors, before the host's catch-all route."""
    service = NativeConnectors(manager, public_origin, auth_manager=auth_manager or app.get(AUTH_MANAGER), **kwargs)
    app[NATIVE_CONNECTORS] = service
    def account_settings():
        preferences = app.get(VOICE_PREFERENCES)
        return {"enabled_mcp_tools": manager.permission_settings(),
                **(preferences.projection() if preferences else {})}
    app[ACCOUNT_SETTINGS] = account_settings
    for prefix in ("", "/api"):
        app.router.add_patch(prefix + "/account/settings", service.account_settings)
        app.router.add_get(prefix + "/directory/servers", service.directory)
        app.router.add_post(prefix + "/organizations/{organization}/mcp/remote_servers", service.create)
        app.router.add_delete(prefix + "/organizations/{organization}/mcp/remote_servers/{server}", service.delete)
        app.router.add_post(prefix + "/organizations/{organization}/mcp/logout/{server}", service.logout)
        app.router.add_get(prefix + "/organizations/{organization}/mcp/start-auth/{server}", service.start)
        app.router.add_get(prefix + "/mcp/auth_callback", service.complete)
    app.router.add_get("/duet/connectors/{connector_id}/oauth/status", service.status)
    app.router.add_get("/duet/connector-oauth/status", service.status)
    app.router.add_get("/duet/connector-oauth/{catalog}", service.oauth_get)
    app.router.add_put("/duet/connector-oauth/{catalog}", service.oauth_put)
    # Concrete paths keep the owning middleware's public exception finite.
    catalog_path = "{catalog:" + "|".join(PROVIDERS) + "}"
    app.router.add_get(CALLBACK_PREFIX + catalog_path, service.provider_callback, allow_head=False)
    app.router.add_get(CLIENT_PREFIX + catalog_path, service.client_metadata, allow_head=False)
    return service

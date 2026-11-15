"""Native preferences and opt-in owner-project FCM delivery.

Delivery remains disabled without explicit private owner configuration and a
verified device channel. Provider acceptance is never reported as OS delivery.
"""
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from aiohttp import web

from .auth import AUTH_MANAGER
from .rest import ACCOUNT_ID, ORGANIZATION_ID
from .fcm import CONFIG_LIMIT, FCMError, PACKAGE
from .notification_outbox import NotificationOutbox, installation_id


NOTIFICATIONS = web.AppKey("duet_notifications", object)
LEGACY_FEATURES = ("compass", "completion", "tool_notification", "bogosort", "code_requires_action",
                   "dispatch", "hearth_project", "marketing", "routines")
FEATURES = (*LEGACY_FEATURES, "sleepsort")
CHANNEL_PREFERENCES = ("enable_email", "enable_push")
MAX_BODY = 16 * 1024
_HEADERS = {"Cache-Control": "no-store", "Vary": "Authorization", "X-Duet-Push-State": "not_configured"}


class NotificationError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def _response(value, status=200, *, revision=None):
    headers = dict(_HEADERS)
    if revision is not None:
        headers["ETag"] = '"notifications-' + str(revision) + '"'
    return web.json_response(value, status=status, headers=headers)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-finite JSON value")


async def _body(request, limit=MAX_BODY):
    raw = bytearray()
    async for chunk in request.content.iter_chunked(4096):
        raw.extend(chunk)
        if len(raw) > limit:
            raise NotificationError("notification_request_too_large", 413)
    try:
        body = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise NotificationError("invalid_notification_request") from error
    if not isinstance(body, dict):
        raise NotificationError("invalid_notification_request")
    return body


def _validate_patch(body):
    if set(body) != {"preferences"} or not isinstance(body["preferences"], dict):
        raise NotificationError("invalid_notification_preferences")
    preferences = body["preferences"]
    if set(preferences) - {"feature_preference"}:
        raise NotificationError("invalid_notification_preferences")
    features = preferences.get("feature_preference", {})
    if features is None:
        return None
    if not isinstance(features, dict) or set(features) - set(FEATURES):
        raise NotificationError("unknown_notification_feature")
    for value in features.values():
        if value is None:
            continue
        if not isinstance(value, dict) or set(value) - set(CHANNEL_PREFERENCES):
            raise NotificationError("invalid_notification_channel_preference")
        if any(v is not None and type(v) is not bool for v in value.values()):
            raise NotificationError("invalid_notification_channel_preference")
    return features


def native_preferences(stored):
    # h1w is the older nine-field read DTO. Modern Aibo's u640 write DTO adds
    # sleepsort, and wn30 reads it exclusively through effective_preferences.
    legacy = {key: value for key, value in stored.items() if key in LEGACY_FEATURES}
    return {"preferences": {"feature_preference": legacy},
            "effective_preferences": {feature: {"enable_push": stored.get(feature, {}).get("enable_push", False)}
                                      for feature in FEATURES}}


class NotificationPreferences:
    def __init__(self, data_dir):
        root = Path(data_dir).expanduser().absolute()
        if root.is_symlink():
            raise ValueError("Notification data directory must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.path = root / "notifications.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Notification database must not be a symlink")
        self.db = sqlite3.connect(self.path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS notification_preferences(
          account TEXT NOT NULL, organization TEXT NOT NULL, revision INTEGER NOT NULL,
          body TEXT NOT NULL, PRIMARY KEY(account,organization))""")
        self.db.commit()
        os.chmod(self.path, 0o600)

    def read(self, account, organization):
        row = self.db.execute("SELECT revision,body FROM notification_preferences WHERE account=? AND organization=?",
                              (account, organization)).fetchone()
        return (row[0], json.loads(row[1])) if row else (0, {})

    def patch(self, account, organization, changes, if_match=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            revision, previous = self.read(account, organization)
            etag = '"notifications-' + str(revision) + '"'
            if if_match is not None and if_match not in (etag, "*"):
                raise NotificationError("notification_preferences_changed", 412)
            updated = {} if changes is None else {feature: dict(value) for feature, value in previous.items()}
            if changes is not None:
                for feature, fields in changes.items():
                    if fields is None:
                        updated.pop(feature, None)
                        continue
                    current = updated.setdefault(feature, {})
                    for channel, value in fields.items():
                        if value is None:
                            current.pop(channel, None)
                        else:
                            current[channel] = value
                    if not current:
                        updated.pop(feature, None)
            if updated != previous:
                revision += 1
                self.db.execute("INSERT OR REPLACE INTO notification_preferences VALUES(?,?,?,?)",
                                (account, organization, revision, json.dumps(updated, sort_keys=True)))
        return revision, updated

    def close(self):
        self.db.close()


class Notifications:
    def __init__(self, data_dir, auth_manager, *, transport=None, clock=None):
        self.auth = auth_manager
        self.preferences = NotificationPreferences(data_dir)
        self.outbox = NotificationOutbox(data_dir, self.auth, self.preferences, transport=transport,
                                         clock=clock or (auth_manager.clock if auth_manager else time.time))

    async def principal(self, request):
        principal = await self.auth.authenticate(request) if self.auth is not None else None
        if principal is None:
            raise NotificationError("authentication_required", 401)
        if principal.account_id != ACCOUNT_ID or principal.organization_id != ORGANIZATION_ID:
            raise NotificationError("notification_account_not_supported", 403)
        if request.match_info.get("organization_uuid", principal.organization_id) != principal.organization_id:
            raise NotificationError("organization_not_found", 404)
        return principal

    def current(self, principal):
        # Parsing a streamed body can outlast an access token or a logout.
        if not self.auth.session_valid(principal.device_id, principal.access_token_hash):
            raise NotificationError("authentication_required", 401)

    async def get_preferences(self, request, principal):
        revision, stored = self.preferences.read(principal.account_id, principal.organization_id)
        return _response(native_preferences(stored), revision=revision)

    async def patch_preferences(self, request, principal):
        changes = _validate_patch(await _body(request))
        self.current(principal)
        if len(request.headers.getall("If-Match", [])) > 1:
            raise NotificationError("invalid_notification_revision")
        if_match = request.headers.get("If-Match")
        if if_match is not None and len(if_match) > 128:
            raise NotificationError("invalid_notification_revision")
        revision, stored = self.preferences.patch(principal.account_id, principal.organization_id, changes, if_match)
        if not self.outbox.opted_in():
            self.outbox.store.revoke_all()
        return _response(native_preferences(stored), revision=revision)

    async def channels(self, request, principal):
        return _response(self.outbox.channels(principal))

    async def register_channel(self, request, principal):
        body = await _body(request)
        self.current(principal)
        required = {"channel_type", "client_app_name", "registration_token", "os_push_permission_granted", "form_factor", "timezone"}
        if set(body) != required:
            raise NotificationError("invalid_notification_channel")
        if body["channel_type"] not in ("FCM", "APNS"):
            raise NotificationError("invalid_notification_channel")
        if body["channel_type"] == "APNS":
            raise NotificationError("notification_channel_unsupported", 501)
        if body["client_app_name"] != PACKAGE:
            raise NotificationError("notification_client_app_not_supported")
        token = body["registration_token"]
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(not 33 <= ord(c) <= 126 for c in token):
            raise NotificationError("invalid_notification_channel")
        if body["os_push_permission_granted"] is not None and type(body["os_push_permission_granted"]) is not bool:
            raise NotificationError("invalid_notification_channel")
        if body["form_factor"] not in ("phone", "tablet", "computer"):
            raise NotificationError("invalid_notification_channel")
        timezone = body["timezone"]
        if not isinstance(timezone, str) or not re.fullmatch(r"[A-Za-z0-9_+./:\-]{1,80}", timezone) or ".." in timezone:
            raise NotificationError("invalid_notification_channel")
        config = self.outbox.config.read()
        if not config.enabled or not config.configured:
            raise NotificationError("push_transport_not_configured", 503)
        if len(request.headers.getall("X-Duet-Installation-Id", [])) != 1:
            raise NotificationError("push_installation_id_required")
        installation = installation_id(request.headers["X-Duet-Installation-Id"])
        proof_headers = request.headers.getall("X-Duet-FCM-Prove", [])
        if proof_headers not in ([], ["1"]):
            raise NotificationError("invalid_push_proof_request")
        return _response(self.outbox.register(principal, body, installation, prove=bool(proof_headers)))

    async def test_push(self, request, principal):
        body = await _body(request)
        self.current(principal)
        if set(body) != {"category"} or body["category"] not in ("completion", "bogosort"):
            raise NotificationError("invalid_test_push_category")
        if not self.outbox.config.read().enabled:
            raise NotificationError("push_transport_not_configured", 503)
        return _response(self.outbox.test_push(principal, body["category"]), status=202)

    async def status(self, request, principal):
        return _response(self.outbox.status(principal))

    async def fcm_config(self, request, principal):
        if request.method == "PUT":
            body = await _body(request, CONFIG_LIMIT)
            self.current(principal)
            value = self.outbox.config.write(body)
            self.outbox.config_changed()
        else:
            value = self.outbox.config.read()
        return _response(value.public())

    async def fcm_client_config(self, request, principal):
        value = self.outbox.config.read().public()
        value.update(device_id=principal.device_id, opted_in=self.outbox.opted_in())
        return _response(value)

    async def fcm_ack(self, request, principal):
        body = await _body(request)
        self.current(principal)
        required = {"channel_uuid", "generation", "event_id"}
        if (not required <= set(body) or set(body) - required - {"nonce"}
                or any(not isinstance(body[key], str) or not 1 <= len(body[key]) <= 160 for key in required)):
            raise NotificationError("invalid_push_acknowledgement")
        return _response(self.outbox.acknowledge(principal, body))

    async def fcm_revoke(self, request, principal):
        self.outbox.store.revoke(principal.device_id)
        return web.Response(status=204, headers=_HEADERS)

    def bind_sources(self, source, *, tasks=None, routines=None):
        self.outbox.bind_sources(source, tasks=tasks, routines=routines)

    def handler(self, action):
        async def handle(request):
            try:
                principal = await self.principal(request)
                response = await action(request, principal)
                self.current(principal)
                configured = self.outbox.config.read()
                response.headers["X-Duet-Push-State"] = "configured" if configured.enabled and configured.configured else "not_configured"
                return response
            except NotificationError as error:
                return _response({"type": "error", "error": {"type": error.code, "message": error.code}}, error.status)
            except FCMError as error:
                unavailable = {"push_transport_not_configured", "push_verified_channel_required", "push_outbox_full",
                               "fcm_dependency_unavailable", "fcm_configuration_unavailable", "push_device_limit"}
                conflict = {"push_subscription_changed", "push_event_not_current", "push_token_already_bound", "push_not_enabled"}
                status = 503 if error.code in unavailable else 409 if error.code in conflict else 400
                return _response({"type": "error", "error": {"type": error.code, "message": error.code}}, status)
            except (sqlite3.Error, OSError):
                return _response({"type": "error", "error": {"type": "notification_storage_unavailable",
                                                               "message": "notification_storage_unavailable"}}, 503)
        return handle


def register_notifications(app, data_dir, *, auth_manager=None, transport=None, clock=None, start_worker=True):
    """Authenticated routes only; no new public-route or Firebase exception is required."""
    service = Notifications(data_dir, auth_manager or app.get(AUTH_MANAGER), transport=transport, clock=clock)
    app[NOTIFICATIONS] = service
    for prefix in ("", "/api"):
        base = prefix + "/organizations/{organization_uuid}/notification"
        app.router.add_get(base + "/preferences", service.handler(service.get_preferences))
        app.router.add_patch(base + "/preferences", service.handler(service.patch_preferences))
        app.router.add_get(base + "/channels", service.handler(service.channels))
        app.router.add_post(base + "/channels", service.handler(service.register_channel))
        app.router.add_post(base + "/debug/test_push", service.handler(service.test_push))
    app.router.add_get("/duet/notifications/status", service.handler(service.status))
    app.router.add_get("/duet/notifications/fcm/config", service.handler(service.fcm_config))
    app.router.add_put("/duet/notifications/fcm/config", service.handler(service.fcm_config))
    app.router.add_get("/duet/notifications/fcm/client-config", service.handler(service.fcm_client_config))
    app.router.add_post("/duet/notifications/fcm/ack", service.handler(service.fcm_ack))
    app.router.add_delete("/duet/notifications/fcm/channel", service.handler(service.fcm_revoke))

    async def startup(application):
        if start_worker:
            service.outbox.start()

    async def shutdown(application):
        await service.outbox.stop()

    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)

    async def cleanup(application):
        service.outbox.close()
        service.preferences.close()

    app.on_cleanup.append(cleanup)
    return service

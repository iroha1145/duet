"""Small, local-only startup REST surface for the original Android UI.

This module deliberately has no HTTP client and never forwards a request. Its
identity is a synthetic account belonging to this bridge, not a vendor session.
"""
import logging
import re
from collections import deque
from copy import deepcopy

from aiohttp import web

ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
ORGANIZATION_ID = "22222222-2222-4222-8222-222222222222"
LOCAL_EMAIL = "local@duet.invalid"
CREATED_AT = "2026-11-14T00:00:00Z"
LOG = logging.getLogger(__name__)
UNKNOWN_REQUESTS = web.AppKey("duet_unknown_rest", deque)
BRIDGE = web.AppKey("duet_rest_bridge", object)
PROFILE = web.AppKey("duet_local_profile", object)
FEATURES = web.AppKey("duet_runtime_features", object)
ACCOUNT_SETTINGS = web.AppKey("duet_account_settings", object)


def local_account():
    """Required g7/op00/je20/rf20 fields, without vendor plan entitlements."""
    return {
        "uuid": ACCOUNT_ID,
        "email_address": LOCAL_EMAIL,
        "full_name": "duet local",
        "display_name": "duet",
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
        "is_verified": True,
        "onboarding_complete": True,
        "memberships": [{
            "organization": {
                "uuid": ORGANIZATION_ID,
                "name": "duet local",
                "settings": {
                    "claude_console_privacy": "private",
                    "claude_code_trusted_devices_required": False,
                },
                # hmu.q / ag20 / rrl filter memberships by this exact string.
                # No pro/max/raven, connector, voice, or hosted-computer claim.
                "capabilities": ["chat"],
                "has_hosted_code_environments": False,
            },
            "role": "owner",
            "created_at": CREATED_AT,
            "updated_at": CREATED_AT,
            "notification_preferences": None,
        }],
        "settings": {
            "has_finished_claudeai_onboarding": True,
            "has_started_claudeai_onboarding": True,
        },
    }


def bootstrap_payload():
    # GrowthBookSchema requires features; a missing schema logs an exception.
    return {"account": local_account(), "growthbook": {"features": ui_features()}}


def ui_features():
    # Preserve the supplied app's Chat/channel layout rather than its newer Home experiment.
    return {"claudeai_marl_beck_whinchat": {"defaultValue": False},
            "cai_abundant_kazoo": {"defaultValue": True},
            "claudeai_tansy_mere_brambling": {"defaultValue": True},
            "mobile_deedee_config": {"defaultValue": {"speech_input": {}}},
            "cai_tingly_clock": {"defaultValue": False},
            "claudeai_aibo_conversation": {"defaultValue": False},
            "claudeai_aibo_voicebox": {"defaultValue": False}}


def app_start_payload():
    # AppStartResponse's only mandatory root field is account. The optional
    # access object has its own schema, distinct from available_elsewhere below.
    return {
        "account": local_account(),
        "org_growthbook": {"features": ui_features()},
        "current_user_access": {
            "features": [{"feature": "chat", "status": "available"}], "account_features": [], "organization_permissions": [],
        },
    }


def _response(payload, status=200):
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})


def _has_local_org(request):
    return request.match_info.get("organization") == ORGANIZATION_ID


async def _account(request):
    payload = {"account": local_account()}
    _profile(request, payload)
    return _response(payload["account"])


async def _bootstrap(request):
    payload = bootstrap_payload()
    _profile(request, payload)
    return _response(payload)


async def _app_start(request):
    if not _has_local_org(request):
        return _response({"error": {"code": "not_found", "message": "Unknown local organization"}}, 404)
    payload = app_start_payload()
    _profile(request, payload)
    return _response(payload)


def _profile(request, payload):
    settings = request.app.get(ACCOUNT_SETTINGS)
    if settings:
        payload["account"]["settings"].update(settings())
    features = request.app.get(FEATURES)
    if features:
        overrides = features()
        for name in ("growthbook", "org_growthbook"):
            if name in payload:
                payload[name]["features"].update(overrides)
    getter = request.app.get(PROFILE)
    if getter:
        profile = getter()
        name = profile.get("display_name", "duet")
        payload["account"].update(display_name=name, full_name=name)
        username = profile.get("username")
        if username:
            payload["account"]["email_address"] = username if "@" in username else username + "@duet.invalid"


async def _available_elsewhere(request):
    if not _has_local_org(request):
        return _response({"error": {"code": "not_found", "message": "Unknown local organization"}}, 404)
    # dm1 byte 8 decodes pu1/ou1, not AppStartResponse.current_user_access.
    # null means that this bridge offers no alternate vendor execution location.
    return _response({"available_elsewhere": None})


def register_rest(app, bridge, profile=None, features=None):
    """Register explicit REST routes; call register_unknown last if desired.

    Both prefixes are intentional: y7n paths are relative and k7n's environment
    determines whether /api has already been appended to the selected base URL.
    The original RPC paths remain untouched.
    """
    app[BRIDGE] = bridge
    if profile is not None:
        app[PROFILE] = profile
    if features is not None:
        app[FEATURES] = features
    app[UNKNOWN_REQUESTS] = deque(maxlen=200)
    for prefix in ("", "/api"):
        app.router.add_get(prefix + "/bootstrap", _bootstrap)
        app.router.add_get(prefix + "/account", _account)
        app.router.add_get(prefix + "/bootstrap/{organization}/app_start", _app_start)
        app.router.add_get(prefix + "/bootstrap/{organization}/current_user_access", _available_elsewhere)
    return bridge


def safe_path(path):
    """Record route evidence without query strings or credential-like segments."""
    path = path.split("?", 1)[0]
    segments = path.split("/")
    previous_sensitive = False
    safe = []
    for segment in segments:
        sensitive = previous_sensitive or len(segment) > 160 or bool(re.search(
            r"(?i)(?:^|[=:])(?:sk-|sk_ant_|sk-ant-|cshk_|ainvk_|bearer\b)", segment))
        safe.append("[redacted]" if sensitive else segment)
        previous_sensitive = segment.lower() in {"token", "api_key", "apikey", "password", "secret", "authorization"}
    return "/".join(safe)


async def unsupported_rest(request):
    method, path = request.method, safe_path(request.path)
    request.app[UNKNOWN_REQUESTS].append({"method": method, "path": path})
    LOG.warning("Unsupported local endpoint: %s %s", method, path)
    return _response({"error": {"code": "not_implemented", "message": "This local bridge endpoint is not implemented", "method": method, "path": path}}, 501)


def register_unknown(app):
    """Install once, after every RPC/REST/root route. No success-shaped fallback."""
    if UNKNOWN_REQUESTS not in app:
        app[UNKNOWN_REQUESTS] = deque(maxlen=200)
    app.router.add_route("*", "/{tail:.*}", unsupported_rest)

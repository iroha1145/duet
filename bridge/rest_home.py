"""Persistent local Aibo home/project REST state for the supplied Android build.

Only real local entities are returned. This module has no network client and
does not advertise hosted agents, remote computers, or model capabilities.
"""
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

from .rest import ACCOUNT_ID, ORGANIZATION_ID
from .attachment_policy import attachment_rules


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class HomeStore:
    """One local organization. Stable IDs are shared with later message backends.

    ``channel(id)`` returns the live record; callers which mutate it must call
    ``save()``. No route treats an unknown channel as a new channel.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.capabilities = []
        self.routines = None
        self.state = {"version": 1, "organization_id": ORGANIZATION_ID,
                      "project": None, "channels": {}, "app_build": None}
        if self.path.exists():
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if state.get("version") != 1 or state.get("organization_id") != ORGANIZATION_ID:
                raise ValueError("Unsupported or foreign local home store")
            self.state = state
            if self._coordinator_references():
                self.save()

    def _coordinator_references(self):
        """Migrate pre-coordinator home files without changing project/channel IDs.

        Session bodies belong to AiboBridge's aibo.json, not this metadata file.
        AiboBridge materializes these stable references as real local sessions.
        """
        changed = False
        for channel in self.state["channels"].values():
            if not channel.get("channel_session_id"):
                existing = channel.get("overview_session_id")
                if isinstance(existing, str) and existing.strip():
                    session_id = "session_" + existing[4:] if existing.startswith("cse_") else existing
                else:
                    session_id = "session_" + str(uuid4())
                channel["channel_session_id"] = session_id
                changed = True
        return changed

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(self.state, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)

    def channel(self, channel_id):
        return self.state["channels"].get(channel_id)

    def ensure_project(self, body):
        project = self.state["project"]
        created = project is None
        if created:
            now = utc_now()
            channel_id = "chan_" + str(uuid4())
            member = {"account_id": ACCOUNT_ID, "role": "owner", "display_name": "duet"}
            self.state["channels"][channel_id] = {
                "id": channel_id, "name": "duet", "topic": "本地工作区",
                "template": "aibo", "visibility": "private",
                "created_by_account_id": ACCOUNT_ID, "caller_relations": ["owner"],
                "members": [member], "members_truncated": False, "membership": member,
                "created_at": now, "updated_at": now, "has_unread": False,
                "has_working_session": False, "disabled_by_plan": False,
                "conversation_service_available": False, "trusted_devices_required": False,
                "caller_alone": True, "caller_own_project": True,
                "caller_own_project_in_use": True, "guest_access_enabled": False,
                "guests": [], "owner_instructions_available": False,
                "channel_session_id": "session_" + str(uuid4()),
            }
            project = {"id": str(uuid4()), "channel_id": channel_id,
                       "created_at": now, "settings": {}, "preferences": {}}
            self.state["project"] = project
        for key in ("time_zone", "language", "agent_name", "quiet_hours"):
            if body.get(key) is not None:
                project["settings"][key] = deepcopy(body[key])
        for key in ("check_ins_on", "morning_brief_on", "routines_paused"):
            if body.get(key) is not None:
                project["preferences"][key] = body[key]
        if body.get("agent_name"):
            self.channel(project["channel_id"])["name"] = body["agent_name"]
        self.save()
        return project, created

    def project_response(self, created=False):
        project = self.state["project"]
        result = {"channel": deepcopy({key: value for key, value in self.channel(project["channel_id"]).items()
                                     if not key.startswith("_")}),
                "created": created, "settings": deepcopy(project["settings"]),
                "capabilities": list(self.capabilities), "awaits_first_run": False, "attachment_rules": attachment_rules(),
                "routines_paused": project["preferences"].get("routines_paused", False),
                "plugin_sets": [], "connector_highlights": [], "connector_account_kinds": []}
        if self.routines is not None:
            result["routine_switches"] = self.routines.switches()
        return result


HOME_STORE = web.AppKey("duet_home_store", HomeStore)


def response(payload, status=200):
    return web.json_response(payload, status=status, headers={"Cache-Control": "no-store"})


def error(code, message, status):
    return response({"error": {"code": code, "message": message}}, status)


async def object_body(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="JSON object required")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="JSON object required")
    return body


async def app_build(request):
    body = await object_body(request)
    keys = ("build_number", "platform", "distribution")
    if not all(isinstance(body.get(key), str) and 0 < len(body[key]) <= 256 for key in keys):
        return error("invalid_argument", "build_number, platform and distribution strings required", 400)
    store = request.app[HOME_STORE]
    store.state["app_build"] = {key: body[key] for key in keys}
    store.save()
    # Local bridge never offers an APK download or requires an official upgrade.
    return response({"update_available": False, "update_required": False})


async def ensure_project(request):
    body = await object_body(request)
    authorized = request.get("duet_authorized", lambda: True)
    if not authorized():
        return error("unauthorized", "Login expired", 401)
    known = {"time_zone", "language", "agent_name", "quiet_hours", "helper",
             "check_ins_on", "morning_brief_on", "routines_paused", "find_only"}
    if body.keys() - known:
        return error("not_implemented", "Unknown project setting", 501)
    for key in ("time_zone", "language", "agent_name"):
        if body.get(key) is not None and (not isinstance(body[key], str) or len(body[key]) > 512):
            return error("invalid_argument", "Invalid project text setting", 400)
    if body.get("time_zone") is not None:
        try:
            ZoneInfo(body["time_zone"])
        except (ZoneInfoNotFoundError, ValueError):
            return error("invalid_argument", "time_zone must be an IANA time zone", 400)
    for key in ("check_ins_on", "morning_brief_on", "routines_paused", "find_only"):
        if body.get(key) is not None and not isinstance(body[key], bool):
            return error("invalid_argument", "Invalid project boolean setting", 400)
    store = request.app[HOME_STORE]
    if (body.get("check_ins_on") or body.get("morning_brief_on")) and store.routines is None:
        return error("not_implemented", "Automatic check-ins and morning briefs are not configured; create an explicit scheduled task instead", 501)
    quiet = body.get("quiet_hours")
    if quiet is not None:
        if (not isinstance(quiet, dict) or set(quiet) != {"start", "end"}
                or any(type(value) is not int or not 0 <= value <= 23 for value in quiet.values())
                or not 1 <= (quiet["end"] - quiet["start"]) % 24 <= 21):
            return error("invalid_argument", "quiet_hours need whole hours 0..23 and duration 1..21 hours", 400)
    if body.get("helper") is not None:
        from .channels import CHANNEL_SERVICE
        if CHANNEL_SERVICE not in request.app:
            return error("unavailable", "The local helper engine is unavailable", 503)
        # Helper requests share this native endpoint but never update the main
        # project's settings, routines, or identity.
        service = request.app[CHANNEL_SERVICE]
        if not authorized():
            return error("unauthorized", "Login expired", 401)
        return await service.wrap(service.ensure_helper)(request)
    find_only = body.get("find_only") is True
    if find_only:
        project = store.state["project"]
        if project is None:
            return error("not_found", "No local project exists", 404)
        existing_channel = store.channel(project["channel_id"])
        existing_identity = (project["id"], project["channel_id"],
                             existing_channel.get("channel_session_id") if existing_channel else None)
        # Original d7v includes find_only:true on explicit settings writes.
        # It forbids creating a project, not updating the existing one.
        if not any(body.get(key) is not None for key in known - {"find_only", "helper"}):
            return response(store.project_response())
    if store.routines is not None:
        def update_authorized():
            if not authorized():
                return False
            # apply checks this inside the scheduler lock. A concurrent reset
            # must not turn a find-only update into creation or a different target.
            if find_only:
                current = store.state["project"]
                if current is None:
                    raise web.HTTPNotFound()
                current_channel = store.channel(current["channel_id"])
                current_identity = (current["id"], current["channel_id"],
                                    current_channel.get("channel_session_id") if current_channel else None)
                if current_identity != existing_identity:
                    raise web.HTTPConflict()
            return True
        try:
            created = await store.routines.apply(body, authorized=update_authorized)
        except web.HTTPUnauthorized:
            return error("unauthorized", "Login expired", 401)
        except web.HTTPNotFound:
            return error("not_found", "No local project exists", 404)
        except web.HTTPConflict:
            return error("conflict", "Local project changed while updating", 409)
        except (ValueError, RuntimeError):
            return error("routine_unavailable", "Routine configuration is unavailable", 409)
    else:
        _, created = store.ensure_project(body)
    return response(store.project_response(created))


async def read_project(request):
    store = request.app[HOME_STORE]
    project = store.state["project"]
    channel = None if project is None else {"id": project["channel_id"], "trusted_devices_required": False}
    return response({"channel": channel, "capabilities": list(store.capabilities)})


async def channels(request):
    from .channels import CHANNEL_SERVICE
    if CHANNEL_SERVICE in request.app:
        service = request.app[CHANNEL_SERVICE]
        return await service.wrap(service.listing)(request)
    store = request.app[HOME_STORE]
    include_archived = request.query.get("include_archived", "false").lower() == "true"
    if request.query.get("cursor"):
        return error("invalid_argument", "No further local channel page exists", 400)
    data = [deepcopy(channel) for channel in store.state["channels"].values()
            if include_archived or not channel.get("archived_at")]
    return response({"data": data, "next_cursor": None})


def get_channel(request):
    return request.app[HOME_STORE].channel(request.match_info["channel_id"])


async def channel_detail(request):
    from .channels import CHANNEL_SERVICE
    if CHANNEL_SERVICE in request.app:
        service = request.app[CHANNEL_SERVICE]
        return await service.wrap(service.detail)(request)
    channel = get_channel(request)
    if channel is None:
        return error("not_found", "Unknown local channel", 404)
    return response({"channel": deepcopy(channel)})


async def channel_config(request):
    if get_channel(request) is None:
        return error("not_found", "Unknown local channel", 404)
    # All routine runs follow the server-selected real model profile. The
    # retained native chooser must not imply an independent mobile selection.
    return response({"config": {"can_pick_routine_models": False,
                                "check_in_model": None, "morning_brief_model": None}})


async def update_channel(request):
    from .channels import CHANNEL_SERVICE
    if CHANNEL_SERVICE in request.app:
        service = request.app[CHANNEL_SERVICE]
        return await service.wrap(service.update)(request)
    channel = get_channel(request)
    if channel is None:
        return error("not_found", "Unknown local channel", 404)
    body = await object_body(request)
    supported = {"name", "topic", "icon", "color"}
    if body.keys() - supported:
        return error("not_implemented", "This channel update is not implemented locally", 501)
    for key, value in body.items():
        if value is not None and (not isinstance(value, str) or len(value) > 4096):
            return error("invalid_argument", "Invalid channel text", 400)
        if key == "name" and (value is None or not value.strip()):
            return error("invalid_argument", "Channel name cannot be blank", 400)
    channel.update(body)
    channel["updated_at"] = utc_now()
    request.app[HOME_STORE].save()
    return response({"channel": deepcopy(channel)})


async def memory_settings(request):
    if request.match_info["organization"] != ORGANIZATION_ID:
        return error("not_found", "Unknown local organization", 404)
    return response({"enabled_melange": False, "enabled_saffron": False,
                     "enabled_saffron_search": False, "classic_mode_available": False,
                     "sensitive_consent_recorded": False})


async def experiences(request):
    if request.match_info["organization"] != ORGANIZATION_ID:
        return error("not_found", "Unknown local organization", 404)
    return response({"experiences": [], "rules": None})


def register_home(app, bridge, data_dir=None):
    """Register before the unknown-route fallback; return the shared HomeStore.

    Persistence defaults to the existing bridge's store directory. Callers with
    an ephemeral bridge must specify data_dir; no implicit per-user path is used.
    """
    if data_dir is None:
        store_path = getattr(bridge, "store_path", None)
        if store_path is None:
            raise ValueError("data_dir is required when bridge has no persistent store")
        data_dir = Path(store_path).parent
    store = HomeStore(Path(data_dir) / "home.json")
    app[HOME_STORE] = store
    for prefix in ("", "/api"):
        app.router.add_post(prefix + "/v1/code/aibo/app-build", app_build)
        app.router.add_post(prefix + "/v1/code/aibo/project", ensure_project)
        app.router.add_get(prefix + "/v1/code/aibo/project", read_project)
        app.router.add_get(prefix + "/v1/code/channels", channels)
        app.router.add_get(prefix + "/v1/code/channels/{channel_id}", channel_detail)
        app.router.add_patch(prefix + "/v1/code/channels/{channel_id}", update_channel)
        app.router.add_get(prefix + "/v1/code/channels/{channel_id}/config", channel_config)
        app.router.add_get(prefix + "/organizations/{organization}/memory/settings", memory_settings)
        app.router.add_get(prefix + "/organizations/{organization}/experiences/claude_corbel_android", experiences)
    return store

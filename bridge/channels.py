"""Original Aibo room protocol backed by real, isolated local channels.

The main ``aibo`` channel is Everything; ``aibo_room`` channels retain their
own coordinators and conversations. No subscription or hosted service is used.

Native helper protocol: z2s/iz70.P0, r060's aibo_helper template and zm8's
private-channel/coordinator/request-key checks. This local implementation does
not establish whether the original account's server still enabled helpers;
the original client also handles aibo_agents_retired. No obsolete UI is forced.
"""
import json
import unicodedata
from copy import deepcopy
from uuid import uuid4

from aiohttp import web

from .aibo import AiboError
from .rest import ACCOUNT_ID, ORGANIZATION_ID
from .rest_home import object_body, response, error, utc_now
from .prompts import helper_instructions


CHANNEL_SERVICE = web.AppKey("duet_channel_service", object)


class ChannelService:
    def __init__(self, home, aibo, engine=None):
        self.home, self.aibo, self.engine = home, aibo, engine
        self.schedule_cleanup = None
        self.home.state.setdefault("room_requests", {})
        self.home.state.setdefault("helper_requests", {})

    def _private_owner(self, channel):
        members = channel.get("members")
        return (channel.get("visibility") == "private"
                and channel.get("created_by_account_id") == ACCOUNT_ID
                and channel.get("caller_relations") == ["owner"]
                and channel.get("caller_alone") is True
                and channel.get("members_truncated") is False
                and channel.get("guest_access_enabled") is False
                and channel.get("guests") == []
                and isinstance(members, list) and len(members) == 1 and isinstance(members[0], dict)
                and members[0].get("account_id") == ACCOUNT_ID and members[0].get("role") == "owner")

    async def ensure_helper(self, request):
        body = await object_body(request)
        if not request.get("duet_authorized", lambda: True)():
            raise AiboError("unauthorized", "Login expired", 401)
        from .auth import AUTH_PRINCIPAL
        principal = request.get(AUTH_PRINCIPAL)
        if principal is not None and (principal.account_id != ACCOUNT_ID
                                      or principal.organization_id != ORGANIZATION_ID):
            raise AiboError("forbidden", "This account does not own the helper workspace", 403)
        return response(self.create_helper(body))

    def create_helper(self, body):
        """Original EnsureCodeProjectRequest.helper; no await in keyed commit.

        r060 recognizes aibo_helper. zm8 requires a private owned channel,
        coordinator and echoed request key before binding the local record.
        A failed coordinator write is repaired by the same durable-key retry.
        """
        if self.engine is None:
            raise AiboError("unavailable", "The local helper engine is unavailable", 503)
        if any(body.get(key) is not None for key in body.keys() - {"helper", "time_zone"}):
            raise AiboError("aibo_agent_top_level_fields", "Helper settings belong inside helper", 400)
        helper = body.get("helper")
        if not isinstance(helper, dict) or helper.keys() - {"client_request_id", "name", "purpose", "icon"}:
            raise AiboError("invalid_argument", "Invalid native helper request", 400)
        rid = helper.get("client_request_id")
        if not isinstance(rid, str) or not rid.strip() or len(rid) > 256:
            raise AiboError("aibo_agent_key_required", "A helper request key is required", 400)
        name, purpose, icon = helper.get("name"), helper.get("purpose"), helper.get("icon")
        if not isinstance(name, str) or not name.strip() or len(name) > 40:
            raise AiboError("aibo_agent_name_invalid", "Helper name must contain 1..40 characters", 400)
        if purpose is not None and (not isinstance(purpose, str) or len(purpose) > 500):
            raise AiboError("aibo_agent_purpose_invalid", "Helper purpose must contain at most 500 characters", 400)
        if icon is not None and (not isinstance(icon, str) or len(icon) > 256):
            raise AiboError("invalid_argument", "Invalid helper icon", 400)
        if any(any(0xD800 <= ord(char) <= 0xDFFF for char in value) for value in (rid, name, purpose or "", icon or "")):
            raise AiboError("invalid_argument", "Helper text must contain valid Unicode", 400)
        fingerprint = json.dumps({key: helper.get(key) for key in ("client_request_id", "name", "purpose", "icon")},
                                 sort_keys=True, ensure_ascii=False)
        requests = self.home.state["helper_requests"]
        existing = requests.get(rid)
        if existing:
            if existing["body"] != fingerprint:
                raise AiboError("client_request_id_conflict", "Helper key was used with different settings", 409)
            channel = self.get(existing["id"])
            if channel.get("template") != "aibo_helper" or not self._private_owner(channel):
                raise AiboError("agent_identity", "Helper channel is no longer privately owned", 409)
            self.aibo.coordinator(channel["id"])
            return {"channel": self.public(channel), "created": False, "client_request_id": rid,
                    "capabilities": list(self.home.capabilities)}
        project = self.home.state.get("project")
        if not project:
            raise AiboError("not_found", "Create the main project first", 404)
        main = self.writable(project["channel_id"])
        if not self._private_owner(main):
            raise AiboError("project_read_only", "The main project is not privately owned", 409)
        now = utc_now()
        channel = {key: deepcopy(main[key]) for key in (
            "visibility", "created_by_account_id", "caller_relations", "members", "members_truncated", "membership",
            "guest_access_enabled", "guests")}
        channel.update(id="chan_" + str(uuid4()), name=name, topic=purpose, icon=icon, template="aibo_helper",
                       channel_session_id="session_" + str(uuid4()), created_at=now, updated_at=now,
                       caller_alone=True, caller_own_project=False, caller_own_project_in_use=False,
                       has_unread=False, has_working_session=False, disabled_by_plan=False,
                       conversation_service_available=False, trusted_devices_required=False,
                       owner_instructions_available=True, _owner_instructions="", _instructions_revision=str(uuid4()),
                       _helper_purpose=purpose or "")
        self.home.state["channels"][channel["id"]] = channel
        requests[rid] = {"body": fingerprint, "id": channel["id"]}
        try:
            self.home.save()
        except BaseException:
            del self.home.state["channels"][channel["id"]]
            del requests[rid]
            raise
        self.aibo.coordinator(channel["id"])
        return {"channel": self.public(channel), "created": True, "client_request_id": rid,
                "capabilities": list(self.home.capabilities)}

    @staticmethod
    def public(channel):
        return deepcopy({key: value for key, value in channel.items() if not key.startswith("_")})

    def get(self, cid):
        channel = self.home.channel(cid)
        if channel is None:
            raise AiboError("not_found", "Unknown local channel", 404)
        return channel

    def is_main(self, cid):
        return cid == (self.home.state.get("project") or {}).get("channel_id")

    def writable(self, cid):
        channel = self.get(cid)
        if channel.get("archived_at") or channel.get("disabled_at"):
            raise AiboError("conflict", "Channel is archived or disabled", 409)
        return channel

    def _idle(self, cid):
        if any(s["channel_id"] == cid and s["phase"] == "running" for s in self.aibo.state["sessions"].values()):
            raise AiboError("conflict", "Stop running channel tasks first", 409)
        if self.engine is not None:
            for sid, session in self.aibo.state["sessions"].items():
                if session["channel_id"] == cid and sid in getattr(self.engine, "tasks", {}):
                    raise AiboError("conflict", "Stop running channel tasks first", 409)

    def _text(self, value, maximum=4096, required=False):
        if value is None and not required:
            return None
        if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
            raise AiboError("invalid_argument", "Invalid channel text")
        return value.strip() if required else value

    def _name(self, name, except_id=None):
        name = self._text(name, 80, True)
        normalized = unicodedata.normalize("NFKC", name).casefold()
        if normalized == "everything":
            raise AiboError("aibo_room_name_invalid", "Everything is the main channel")
        if any(unicodedata.normalize("NFKC", c["name"]).casefold() == normalized and c["id"] != except_id
               for c in self.home.state["channels"].values()):
            raise AiboError("aibo_room_name_invalid", "Channel name already exists", 409)
        return name

    def create(self, body):
        if body.keys() - {"client_request_id", "name", "topic", "icon", "owner_instructions"}:
            raise AiboError("invalid_argument", "Unknown room setting")
        rid = self._text(body.get("client_request_id"), 256, True)
        fingerprint = json.dumps(body, sort_keys=True, ensure_ascii=False)
        existing = self.home.state["room_requests"].get(rid)
        if existing:
            if existing["body"] != fingerprint:
                raise AiboError("client_request_id_conflict", "Request ID was used with different room settings", 409)
            channel = self.get(existing["id"])
            return {"channel": self.public(channel), "created": False, "client_request_id": rid, "claude_woken": False}
        project = self.home.state.get("project")
        if not project:
            raise AiboError("aibo_room_no_main_chat", "Create the main project first", 409)
        name = self._name(body.get("name"))
        topic = self._text(body.get("topic"))
        icon = self._text(body.get("icon"), 256)
        instructions = self._text(body.get("owner_instructions"), 20000)
        now = utc_now()
        channel = deepcopy(self.get(project["channel_id"]))
        for key in ("archived_at", "disabled_at", "overview_session_id", "last_message_at", "last_activity_at", "context_sources"):
            channel.pop(key, None)
        channel = {key: value for key, value in channel.items() if not key.startswith("_")}
        channel.update(id="chan_" + str(uuid4()), name=name, topic=topic, icon=icon,
                       template="aibo_room", created_at=now, updated_at=now,
                       channel_session_id="session_" + str(uuid4()), has_unread=False,
                       has_working_session=False, owner_instructions_available=True,
                       _owner_instructions=instructions or "", _instructions_revision=str(uuid4()))
        self.home.state["channels"][channel["id"]] = channel
        self.home.state["room_requests"][rid] = {"body": fingerprint, "id": channel["id"]}
        self.home.save()
        self.aibo.coordinator(channel["id"])
        return {"channel": self.public(channel), "created": True, "client_request_id": rid, "claude_woken": False}

    async def create_route(self, request):
        return response(self.create(await object_body(request)))

    async def listing(self, request):
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            raise AiboError("invalid_argument", "Invalid channel limit")
        if not 1 <= limit <= 1000:
            raise AiboError("invalid_argument", "Channel limit must be 1..1000")
        archived = request.query.get("include_archived", "false").lower() == "true"
        channels = [self.public(c) for c in self.home.state["channels"].values() if archived or not c.get("archived_at")]
        cursor = request.query.get("cursor")
        if cursor:
            index = next((i for i, c in enumerate(channels) if c["id"] == cursor), None)
            if index is None:
                raise AiboError("invalid_argument", "Unknown channel cursor")
            channels = channels[index + 1:]
        selected = channels[:limit]
        return response({"data": selected, "next_cursor": selected[-1]["id"] if len(channels) > limit else None})

    async def detail(self, request):
        return response({"channel": self.public(self.get(request.match_info["channel_id"]))})

    async def update(self, request):
        cid = request.match_info["channel_id"]
        channel = self.get(cid)
        body = await object_body(request)
        if body.keys() - {"name", "topic", "icon", "color", "disabled", "context_sources", "clear_context_sources"}:
            raise AiboError("not_implemented", "Unknown channel setting", 501)
        changes = {}
        for key in ("name", "topic", "icon", "color"):
            if key in body:
                changes[key] = self._name(body[key], cid) if key == "name" else self._text(body[key])
        for key in ("disabled", "clear_context_sources"):
            if key in body and type(body[key]) is not bool:
                raise AiboError("invalid_argument", "Channel flag must be boolean")
        if body.get("disabled"):
            if self.is_main(cid):
                raise AiboError("conflict", "The main channel cannot be disabled", 409)
            self._idle(cid)
        if "disabled" in body:
            changes["disabled_at"] = utc_now() if body["disabled"] else None
        # Source metadata has the original shape, but is not a fetched context.
        # Reject external sources until the backend actually supports reading them.
        if body.get("context_sources"):
            raise AiboError("not_implemented", "External context sources are not connected", 501)
        if "context_sources" in body and body["context_sources"] not in (None, []):
            raise AiboError("invalid_argument", "context_sources must be a list")
        if body.get("clear_context_sources") or body.get("context_sources") == []:
            changes["context_sources"] = []
        channel.update(changes, updated_at=utc_now())
        self.home.save()
        return response({"channel": self.public(channel)})

    async def archive(self, request):
        cid = request.match_info["channel_id"]
        channel = self.get(cid)
        if self.is_main(cid):
            raise AiboError("conflict", "The main channel cannot be archived", 409)
        self._idle(cid)
        channel["archived_at"] = channel.get("archived_at") or utc_now()
        channel["updated_at"] = utc_now()
        self.home.save()
        return response({"channel": self.public(channel)})

    async def unarchive(self, request):
        channel = self.get(request.match_info["channel_id"])
        channel["archived_at"] = None
        channel["updated_at"] = utc_now()
        self.home.save()
        return response({"channel": self.public(channel)})

    async def delete(self, request):
        cid = request.match_info["channel_id"]
        previous = self.get(cid)
        reset_main = self.is_main(cid)
        self._idle(cid)
        if self.schedule_cleanup is not None:
            # The scheduler rechecks idleness while holding its dispatch lock.
            # No await follows this hook, so no new channel task can race purge.
            await self.schedule_cleanup(cid, lambda: self._idle(cid))
        session_ids = [sid for sid, s in self.aibo.state["sessions"].items() if s["channel_id"] == cid]
        engine_store = getattr(self.engine, "store", None)
        if engine_store is not None:
            for sid in session_ids:
                engine_store.delete_session(sid)
                self.engine.native_listeners.pop(sid, None)
                self.engine.native_tools.pop(sid, None)
                self.engine.authorized_sessions.discard(sid)
                self.engine.takeover_sessions.discard(sid)
        # Keep deduplication IDs durable, so replay cannot silently recreate a
        # deleted channel. Its original request receives a 404 after deletion.
        del self.home.state["channels"][cid]
        replacement = None
        if reset_main:
            replacement = deepcopy(previous)
            for key in ("overview_session_id", "last_message_at", "last_activity_at", "archived_at", "disabled_at"):
                replacement.pop(key, None)
            now = utc_now()
            replacement.update(id="chan_" + str(uuid4()), channel_session_id="session_" + str(uuid4()),
                               created_at=now, updated_at=now, has_unread=False, has_working_session=False)
            self.home.state["channels"][replacement["id"]] = replacement
            self.home.state["project"]["channel_id"] = replacement["id"]
        self.home.save()
        for sid in session_ids:
            for queue in tuple(self.aibo.listeners.get(("session", sid), ())):
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            del self.aibo.state["sessions"][sid]
        for queue in tuple(self.aibo.listeners.get(("channel", cid), ())):
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
        self.aibo.state["channels"].pop(cid, None)
        if replacement is not None:
            self.aibo.coordinator(replacement["id"])
        self.aibo.save()
        return response({})

    async def instructions(self, request):
        channel = self.get(request.match_info["channel_id"])
        text = channel.get("_owner_instructions", "")
        revision = channel.get("_instructions_revision", "0")
        if request.method == "GET":
            return response({"text": text, "revision": revision, "editable": True,
                             "max_length": 20000, "length_unit": "characters"})
        body = await object_body(request)
        if body.keys() - {"text", "source", "if_revision"}:
            raise AiboError("invalid_argument", "Unknown instruction setting")
        updated = self._text(body.get("text"), 20000, False)
        if updated is None or not isinstance(body.get("source"), str) or not isinstance(body.get("if_revision"), str):
            raise AiboError("invalid_argument", "text, source and if_revision are required")
        if body["if_revision"] != revision:
            return response({"outcome": "conflict", "text": text, "revision": revision})
        if text == updated:
            return response({"outcome": "unchanged", "text": text, "revision": revision})
        revision = str(uuid4())
        channel.update(_owner_instructions=updated, _instructions_revision=revision,
                       owner_instructions_available=True, updated_at=utc_now())
        self.home.save()
        return response({"outcome": "saved", "text": updated, "revision": revision})

    def context_for_session(self, sid, maximum=60000):
        """Bounded labeled snapshot; callers add this only to the current turn.

        Main/Everything sees completed messages from every active channel.
        A room sees its instructions and previous threads in that room only.
        Quoted conversation data is never promoted to system instructions.
        """
        session = self.aibo.session(sid)
        cid = session["channel_id"]
        own = self.get(cid)
        everything = self.is_main(cid)
        sources = [c for c in self.home.state["channels"].values() if
                   not c.get("archived_at") and not c.get("disabled_at") and (everything or c["id"] == cid)]
        records = []
        for source in sources:
            messages = [dict(channel_id=source["id"], channel_name=source["name"], session_id=s["id"],
                             role="user" if m.get("author_account_id") else "assistant",
                             created_at=m["created_at"], body=m["body"]) for s in self.aibo.state["sessions"].values()
                        if s["channel_id"] == source["id"] and s["id"] != session["id"]
                        for m in s["messages"] if m.get("_complete")]
            records.extend(messages)
        records.sort(key=lambda m: (m["created_at"], m["session_id"]))
        selected, size = [], 0
        for record in reversed(records):
            encoded = json.dumps(record, ensure_ascii=False)
            if size + len(encoded) > maximum:
                break
            selected.append(record)
            size += len(encoded)
        instructions = own.get("_owner_instructions", "")
        if own.get("template") == "aibo_helper":
            instructions = helper_instructions(own["name"], own["_helper_purpose"], instructions)
        return {"scope": "everything" if everything else "channel", "channel_id": cid,
                "owner_instructions": instructions,
                "channels": [{"id": c["id"], "name": c["name"], "topic": c.get("topic")} for c in sources],
                "messages": list(reversed(selected)), "truncated": len(selected) < len(records)}

    def wrap(self, method):
        async def handler(request):
            try:
                return await method(request)
            except AiboError as exc:
                return error(exc.code, exc.message, exc.status)
        return handler


def register_channels(app, home, aibo, engine=None):
    service = ChannelService(home, aibo, engine)
    app[CHANNEL_SERVICE] = service
    if engine is not None:
        home.capabilities = sorted(set(getattr(home, "capabilities", [])) | {"rooms", "helper_keys"})
    for prefix in ("", "/api"):
        app.router.add_post(prefix + "/v1/code/aibo/rooms", service.wrap(service.create_route))
        path = prefix + "/v1/code/channels/{channel_id}"
        app.router.add_post(path + "/archive", service.wrap(service.archive))
        app.router.add_post(path + "/unarchive", service.wrap(service.unarchive))
        app.router.add_delete(path, service.wrap(service.delete))
        instructions = prefix + "/v1/code/aibo/channels/{channel_id}/instructions"
        app.router.add_get(instructions, service.wrap(service.instructions))
        app.router.add_post(instructions, service.wrap(service.instructions))
    if engine is not None and hasattr(engine, "extensions"):
        async def create_project(arguments, session_id):
            aibo.session(session_id)
            if arguments.get("template", "aibo_room") != "aibo_room":
                raise ValueError("Only aibo_room channels are supported")
            return service.create({"client_request_id": arguments.get("client_request_id") or str(uuid4()),
                                   "name": arguments.get("name"), "topic": arguments.get("purpose"),
                                   "owner_instructions": arguments.get("handover")})
        definition = {"name": "mcp__hearthbot__create_project", "description": "Create a channel requested by the user, with its own conversations and purpose.",
                      "parameters": {"type": "object", "properties": {key: {"type": "string"} for key in
                          ("name", "purpose", "template", "handover", "client_request_id")},
                          "required": ["name"], "additionalProperties": False}}
        engine.extensions[definition["name"]] = (definition, create_project)
    return service

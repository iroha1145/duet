import asyncio
import base64
import contextlib
import hmac
import hashlib
import json
import logging
import mimetypes
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from aiohttp import web
from croniter import croniter

from . import __version__
from .desktop import desktop_view
from .desktop_access import DesktopAccess, validate_origin
from .providers import merge_usage, responses_identity, stream, validate_config
from .storage import Store, close_pending_tools, message, uid
from .tools import Tools, workspace_path

MAX_ATTACHMENT = 20 * 1024 * 1024


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if not k.startswith("_") and k != "api_key"}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


def limited_text(value, name, limit=20000, default=None):
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text up to {limit} characters")
    return value


class Service:
    def __init__(self, root, token, display=":99", vnc_port=5900, novnc_dir="/usr/share/novnc", desktop_dir="~/.local/share/duet/desktop"):
        self.store = Store(root)
        self.token = token
        self.tools = Tools(self.store.workspace, display, desktop_dir)
        self.tasks = {}
        self.live_sessions = {}
        self.approvals = {}
        self.authorized_sessions = set()
        self.changed = asyncio.Event()
        self.scheduler = None
        self.legacy_scheduler_enabled = True
        self.http = None
        self.maintenance = False
        self.vnc_port = vnc_port
        self.novnc_dir = Path(novnc_dir)
        self.desktop_lock = asyncio.Lock()
        self.desktop_access = None
        self.takeover_sessions = {s["id"] for s in self.store.sessions() if s.get("manual_takeover")}
        settings = self.store.root / "config.json"
        settings.write_text(json.dumps({"version": __version__, "display": display, "vnc_port": vnc_port}), encoding="utf-8")
        os.chmod(settings, 0o600)
        digest = hashlib.sha256()
        for source in sorted(Path(__file__).parent.glob("*.py")):
            digest.update(source.name.encode())
            digest.update(source.read_bytes())
        self.release_id = os.environ.get("DUET_RELEASE_ID") or "source-" + digest.hexdigest()[:20]

    def emit(self, session_id, kind, data):
        self.store.event(session_id, kind, data)
        old = self.changed
        self.changed = asyncio.Event()
        old.set()

    def save(self, session, event="session_updated"):
        self.store.save_session(session)
        self.emit(session["id"], event, {"session": public(session)})

    def session(self, ident):
        result = self.live_sessions.get(ident) or self.store.get("sessions", ident)
        if result is None:
            raise web.HTTPNotFound(text='{"error":"session not found"}', content_type="application/json")
        return result

    def launch(self, session, config):
        ident = session["id"]
        self.live_sessions[ident] = session
        task = asyncio.create_task(self.run(session, config))
        self.tasks[ident] = task
        def finished(done):
            if self.tasks.get(ident) is done:
                self.tasks.pop(ident, None)
                self.live_sessions.pop(ident, None)
        task.add_done_callback(finished)
        return task

    async def start(self, app):
        self.http = aiohttp.ClientSession()
        if self.legacy_scheduler_enabled:
            self.scheduler = asyncio.create_task(self.schedule_loop())

    async def desktop_capabilities(self):
        result = self.tools.desktop_status()
        result["novnc_ready"] = (self.novnc_dir / "vnc.html").is_file()
        result["vnc_port"] = self.vnc_port
        result["vnc_ready"] = False
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.vnc_port), 0.3)
            writer.close()
            await writer.wait_closed()
            result["vnc_ready"] = True
        except (OSError, asyncio.TimeoutError):
            pass
        result["computer_ready"] = result["ready"]
        result["ready"] = result["ready"] and result["novnc_ready"] and result["vnc_ready"]
        if not result["ready"] and result["reason"] is None:
            result["reason"] = "VNC listener or noVNC static resources unavailable"
        return result

    async def close(self, app):
        tasks = [*self.tasks.values()]
        if self.scheduler:
            tasks.append(self.scheduler)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.http:
            await self.http.close()
        self.store.close()

    async def approve_tool(self, session, tool, config):
        if config["auto_approve"] or session["id"] in self.authorized_sessions:
            return True
        return await self.request_approval(session, tool)

    def can_approve_execution(self, session):
        """Conversation-input adapters may require an answer instead of a grant."""
        return True

    async def request_approval(self, session, tool):
        """One explicit approval, without provider/session auto-approval shortcuts."""
        approval = {"id": uid(), "tool": tool["name"], "arguments": tool["arguments"]}
        future = asyncio.get_running_loop().create_future()
        self.approvals[approval["id"]] = (session["id"], future)
        session["pending_approval"] = approval
        session["status"] = "waiting"
        self.save(session, "approval_required")
        try:
            return await future
        finally:
            self.approvals.pop(approval["id"], None)
            session["pending_approval"] = None

    async def authorize_dispatch(self, session, tool, config):
        """Optional policy recheck after approval and before the actual call."""

    def dispatch_authorization_current(self, session, tool, config):
        """A lock wait must not silently reuse an outdated permission decision."""
        return True

    async def execute_tool(self, session, tool, config):
        if not await self.approve_tool(session, tool, config):
            return {"error": "user denied tool execution"}
        session["status"] = "running"
        self.save(session)
        if tool["name"] == "computer":
            while True:
                # Policy-driven approval happens outside the desktop lock, so
                # another device can still take over while an answer is pending.
                await self.authorize_dispatch(session, tool, config)
                async with self.desktop_lock:
                    current = self.session(session["id"])
                    session["manual_takeover"] = current.get("manual_takeover", False)
                    held = self.desktop_access.active() if self.desktop_access else bool(self.takeover_sessions)
                    if not held:
                        if not self.dispatch_authorization_current(session, tool, config):
                            continue
                        session["status"] = "running"
                        self.store.save_session(session)
                        return await self.tools.execute(tool["name"], tool["arguments"])
                session["status"] = "waiting"
                self.save(session)
                wake = self.changed
                await wake.wait()
        await self.authorize_dispatch(session, tool, config)
        return await self.tools.execute(tool["name"], tool["arguments"])

    async def run(self, session, config):
        ident = session["id"]
        self.live_sessions[ident] = session
        try:
            # Explicit auto_approve is authorization for this session only.
            if config["auto_approve"]:
                self.authorized_sessions.add(ident)
            for turn in range(16):
                session["status"] = "running"
                assistant = message("assistant", status="streaming")
                provider_request = {"started_at": time.time(), "protocol": config["protocol"],
                                    "model": config["model"], "profile_id": config.get("_model_profile_id")}
                tools = self.tool_definitions(config)
                identity = responses_identity(config, tools) if config.get("api_mode") == "responses" else None
                for previous in session["messages"]:
                    if previous.get("_responses", {}).get("identity") != identity or identity is None:
                        previous.pop("_responses", None)
                context = self.context_messages(session, config)
                session["messages"].append(assistant)
                self.save(session)
                tool_calls = []
                provider_state = None
                last_save = time.monotonic()
                async for event in stream(self.http, config, context, tools, session_id=ident):
                    if event["type"] == "request":
                        # Count a provider attempt only once its wire request is
                        # prepared, not an assistant placeholder that failed earlier.
                        assistant["provider_request"] = dict(provider_request, **event["request"])
                        self.store.save_session(session)
                    elif event["type"] == "text":
                        assistant["text"] += event["text"]
                        if len(assistant["text"]) > 1024 * 1024:
                            raise RuntimeError("assistant output exceeds limit")
                        self.emit(ident, "text_delta", {"message_id": assistant["id"], "text": event["text"]})
                        if time.monotonic() - last_save >= 0.25:
                            self.store.save_session(session)
                            last_save = time.monotonic()
                    elif event["type"] == "tools":
                        tool_calls = event["tools"]
                    elif event["type"] == "provider_state":
                        provider_state = event["state"]
                    elif event["type"] == "usage":
                        assistant["usage"] = merge_usage(config["protocol"], assistant.get("usage"), event["usage"])
                        self.store.save_session(session)
                        self.emit(ident, "usage", {"message_id": assistant["id"], "usage": assistant["usage"]})
                assistant["status"] = "complete"
                if tool_calls:
                    if not config["allow_tools"]:
                        raise RuntimeError("provider requested tools when tools are disabled")
                    if len(tool_calls) > 16:
                        raise RuntimeError("too many tools in one turn")
                    # All provider adapters converge here. Reject non-finite
                    # numbers and invalid Unicode before persisting tool calls
                    # or publishing their arguments to the native interface.
                    json.dumps(tool_calls, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    assistant["tool_calls"] = tool_calls
                if provider_state is not None:
                    assistant["_responses"] = provider_state
                self.save(session)
                if not tool_calls:
                    session["status"] = "idle"
                    self.save(session, "completed")
                    return
                for tool in tool_calls:
                    try:
                        result = await self.execute_tool(session, tool, config)
                    except (ValueError, KeyError, OSError, RuntimeError, asyncio.TimeoutError):
                        result = {"error": "tool failed; check arguments, capabilities, permissions or timeout"}
                    images = []
                    if "image_base64" in result:
                        images = [result.pop("image_base64")]
                    text = json.dumps(result, ensure_ascii=False)
                    item = message("tool", text, tool_name=tool["name"], tool_call_id=tool["id"])
                    if images:
                        item["_images"] = images
                    session["messages"].append(item)
                    self.save(session, "tool_result")
            raise RuntimeError("agent tool loop limit reached")
        except asyncio.CancelledError:
            session["status"] = "stopped"
            session["pending_approval"] = None
            for item in session["messages"]:
                if item["status"] == "streaming":
                    item["status"] = "stopped"
            close_pending_tools(session, "cancelled")
            self.save(session, "stopped")
            raise
        except Exception as failure:
            # User-visible errors must not include exception repr: request URLs,
            # gateway response bodies and arbitrary model output may contain secrets.
            # Keep only local code locations and the exception class for diagnosis,
            # never its message, request contents, variables, or provider payload.
            frames = [(Path(frame.filename).name, frame.name, frame.lineno)
                      for frame in traceback.extract_tb(failure.__traceback__)[-8:]]
            logging.error("duet run failure type=%s frames=%s", type(failure).__name__, frames)
            session["status"] = "error"
            session["pending_approval"] = None
            for item in session["messages"]:
                if item["status"] == "streaming":
                    item["status"] = "error"
            close_pending_tools(session, "interrupted")
            self.save(session)
            self.emit(ident, "error", {"message": "Model request failed, stream interrupted, or agent limit reached. Check provider configuration and retry."})
        finally:
            self.tasks.pop(ident, None)
            self.live_sessions.pop(ident, None)

    def tool_definitions(self, config):
        return tool_definitions_if(config)

    def context_messages(self, session, config):
        return list(session["messages"])

    async def schedule_loop(self):
        while True:
            now = time.time()
            for schedule in self.store.all("schedules"):
                if self.maintenance:
                    break
                if not schedule["enabled"] or schedule["next_run_at"] > now:
                    continue
                # Commit next fire before dispatch; a restart never duplicates a fire.
                # Missed runs coalesce into one rather than replaying every side effect.
                schedule["last_run_at"] = now
                schedule["next_run_at"] = next_fire(schedule["cron"], schedule["timezone"], now)
                session = self.store.create_session(schedule["title"], "task")
                session["status"] = "running"
                session["messages"].append(message("user", schedule["prompt"]))
                schedule["last_session_id"] = session["id"]
                self.store.put("schedules", schedule)
                self.save(session, "scheduled")
                self.launch(session, schedule["_config"])
                self.emit(None, "schedule_updated", {"schedule": public(schedule)})
            await asyncio.sleep(1)


def tool_definitions_if(config):
    from .tools import tool_definitions
    return tool_definitions() if config["allow_tools"] else []


def next_fire(cron, timezone, now=None):
    try:
        zone = ZoneInfo(timezone)
        if not isinstance(cron, str) or len(cron.split()) != 5 or not croniter.is_valid(cron):
            raise ValueError("cron must have five valid fields")
        return croniter(cron, datetime.fromtimestamp(now or time.time(), zone)).get_next(datetime).timestamp()
    except ZoneInfoNotFoundError as error:
        raise ValueError("unknown timezone; install system tzdata") from error


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return web.json_response({"error": "Invalid request, path, cron or timezone"}, status=400)
    except asyncio.TimeoutError:
        return web.json_response({"error": "Operation timed out"}, status=504)
    except (OSError, RuntimeError):
        return web.json_response({"error": "Operation unavailable; check server capabilities and configuration"}, status=503)


def create_app(root, token, *, service_factory=Service, account_auth=None, public_routes=(), public_origin=None,
               expose_legacy_sessions=True, **kwargs):
    if public_origin is not None:
        public_origin = validate_origin(public_origin)
    if account_auth is not None and public_origin is None:
        raise ValueError("Account desktop requires an explicit public_origin")
    service = service_factory(root, token, **kwargs)
    service.legacy_scheduler_enabled = bool(expose_legacy_sessions)
    desktop_access = DesktopAccess(service, account_auth) if account_auth is not None else None
    service.desktop_access = desktop_access

    @web.middleware
    async def authentication(request, handler):
        if account_auth is not None:
            if request.method == "GET" and request.path == "/desktop/vnc":
                identity = desktop_access.consume(request.query.get("ticket", "")) if "token" not in request.query else None
                if identity is None:
                    return web.json_response({"error": "Unauthorized"}, status=401, headers={"Cache-Control": "no-store"})
                origin = request.headers.get("Origin")
                if origin is not None and origin != public_origin:
                    return web.json_response({"error": "Invalid desktop origin"}, status=403)
                request["duet_desktop_identity"] = identity
                return await handler(request)
            if account_auth.is_public_request(request) or (request.method, request.path) in public_routes:
                return await handler(request)
            principal = await account_auth.authenticate(request)
            if principal is None:
                return web.json_response({"error": "Unauthorized"}, status=401, headers={"Cache-Control": "no-store"})
            request["duet_authorized"] = lambda: account_auth.session_valid(principal.device_id, principal.access_token_hash)
            request["duet_desktop_identity"] = (principal.device_id, principal.access_token_hash)
            return await handler(request)
        # This nonce proof reveals no credential and is the sole unauthenticated
        # bootstrap route. SSH clients verify it before sending any bearer/key.
        if request.method == "GET" and request.path == "/duet/attestation":
            return await handler(request)
        supplied = request.headers.get("Authorization", "")
        supplied = supplied[7:] if supplied.startswith("Bearer ") else ""
        if request.path == "/desktop/vnc" and not supplied:
            supplied = request.query.get("token", "")
        if not supplied or not hmac.compare_digest(supplied, service.token):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return await handler(request)

    app = web.Application(middlewares=[authentication, errors], client_max_size=MAX_ATTACHMENT + 1024 * 1024)
    app["service"] = service
    app.on_startup.append(service.start)
    app.on_cleanup.append(service.close)
    if desktop_access:
        app.on_startup.append(desktop_access.start)
        app.on_shutdown.append(desktop_access.close)

    async def attestation(request):
        nonce = request.query.get("nonce", "")
        if len(nonce) != 64 or any(char not in "0123456789abcdef" for char in nonce):
            return web.json_response({"error": "Invalid attestation nonce"}, status=400,
                                     headers={"Cache-Control": "no-store"})
        proof = hmac.new(service.token.encode("utf-8"),
                         ("duet-service-attestation-v1\n" + nonce).encode("ascii"),
                         hashlib.sha256).hexdigest()
        return web.json_response({"protocol": "duet-service-attestation-v1", "nonce": nonce, "proof": proof},
                                 headers={"Cache-Control": "no-store"})

    async def health(request):
        desktop = await service.desktop_capabilities()
        return web.json_response({"ok": True, "version": __version__, "desktop_ready": desktop["ready"],
                                  "release_id": service.release_id, "active_runs": sum(not task.done() for task in service.tasks.values()),
                                  "pid": os.getpid(), "maintenance": service.maintenance,
                                  "event_cursor": service.store.db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]})

    async def maintenance(request):
        data = await request.json()
        if type(data.get("enabled")) is not bool:
            raise ValueError("enabled must be boolean")
        # No await between the active-run check and gate mutation. Dispatch and
        # message acceptance run on this same event loop, so this is atomic.
        if data["enabled"] and any(not task.done() for task in service.tasks.values()):
            return web.json_response({"error": "Active runs prevent service maintenance"}, status=409)
        service.maintenance = data["enabled"]
        return web.json_response({"maintenance": service.maintenance})

    async def sessions(request):
        if request.method == "GET":
            return web.json_response({"sessions": public(service.store.sessions())})
        data = await request.json()
        title = limited_text(data.get("title"), "title", 200, "New chat")
        mode = data.get("mode", "chat")
        if mode not in ("chat", "task"):
            raise ValueError("invalid mode")
        session = service.store.create_session(title, mode)
        service.emit(session["id"], "session_created", {"session": public(session)})
        return web.json_response(public(session), status=201)

    async def session_route(request):
        session = service.session(request.match_info["id"])
        if request.method == "GET":
            return web.json_response(public(session))
        if request.method == "DELETE":
            task = service.tasks.get(session["id"])
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            service.store.delete_session(session["id"])
            service.authorized_sessions.discard(session["id"])
            service.takeover_sessions.discard(session["id"])
            service.emit(session["id"], "session_deleted", {})
            return web.json_response({"ok": True})
        data = await request.json()
        for key in ("favorite", "archived"):
            if key in data:
                if type(data[key]) is not bool:
                    raise ValueError("boolean expected")
                session[key] = data[key]
        if "title" in data:
            session["title"] = limited_text(data["title"], "title", 200)
        service.save(session)
        return web.json_response(public(session))

    async def send_message(request):
        session = service.session(request.match_info["id"])
        data = await request.json()
        if service.maintenance:
            return web.json_response({"error": "Service is temporarily in maintenance"}, status=409)
        request_id = limited_text(data.get("request_id"), "request_id", 200)
        # Duplicate retries remain successful even while the original is running.
        existing = service.store.db.execute("SELECT 1 FROM requests WHERE session_id=? AND request_id=?", (session["id"], request_id)).fetchone()
        if existing:
            return web.json_response({"accepted": True, "duplicate": True}, status=202)
        if session["id"] in service.tasks:
            return web.json_response({"error": "Session already running"}, status=409)
        config = validate_config(data.get("config"))
        text = data.get("text", "")
        if not isinstance(text, str) or len(text) > 200000:
            raise ValueError("text limit")
        attachments = []
        images = []
        attachment_ids = data.get("attachments", [])
        if not isinstance(attachment_ids, list) or len(attachment_ids) > 10:
            raise ValueError("attachment limit")
        for ident in attachment_ids:
            attachment = service.store.get("attachments", ident)
            if not attachment:
                raise ValueError("unknown attachment")
            attachments.append(attachment)
            path = service.store.root / "attachments" / attachment["id"]
            if attachment["mime"] in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                if attachment["size"] > 6 * 1024 * 1024:
                    raise ValueError("image limit")
                images.append({"mime": attachment["mime"], "data": base64.b64encode(path.read_bytes()).decode()})
            else:
                if attachment["size"] > 512 * 1024:
                    raise ValueError("text attachment exceeds 512 KiB")
                try:
                    text += "\n\n[Attachment: " + attachment["name"] + "]\n" + path.read_text(encoding="utf-8")
                except UnicodeDecodeError as error:
                    raise ValueError("only images and UTF-8 text attachments are supported") from error
        if not text.strip() and not attachments:
            raise ValueError("message is empty")
        item = message("user", text, attachments=attachments)
        if images:
            item["_images"] = images
        service.store.claim_request(session["id"], request_id)
        close_pending_tools(session, "interrupted")
        session["messages"].append(item)
        session["status"] = "running"
        service.save(session)
        service.launch(session, config)
        return web.json_response({"accepted": True, "duplicate": False}, status=202)

    async def stop(request):
        session = service.session(request.match_info["id"])
        task = service.tasks.get(session["id"])
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A task can be cancelled before its coroutine's first instruction.
            # Persist stopped state even when run() never entered its finally.
            session = service.session(session["id"])
        if session["status"] != "stopped":
            session["status"] = "stopped"
            session["pending_approval"] = None
            close_pending_tools(session, "cancelled")
            service.save(session, "stopped")
        return web.json_response({"ok": True})

    async def approve(request):
        session = service.session(request.match_info["id"])
        data = await request.json()
        entry = service.approvals.get(data.get("approval_id"))
        if not entry or entry[0] != session["id"] or entry[1].done():
            return web.json_response({"error": "Approval expired"}, status=409)
        if type(data.get("allow")) is not bool or type(data.get("always", False)) is not bool:
            raise ValueError("boolean expected")
        if data["allow"] and not service.can_approve_execution(session):
            return web.json_response({"error": "Use the native request interface for this approval"}, status=409)
        if data["allow"] and data.get("always", False):
            service.authorized_sessions.add(session["id"])
        entry[1].set_result(data["allow"])
        return web.json_response({"ok": True})

    async def events(request):
        after = int(request.query.get("after", "0"))
        wait = min(30, max(0, float(request.query.get("wait", "20"))))
        if after < 0:
            raise ValueError("negative cursor")
        wake = service.changed
        result = service.store.events(after)
        if not result and wait:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), wait)
            result = service.store.events(after)
        authorized = request.get("duet_authorized")
        if authorized is not None and not authorized():
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response({"events": result, "cursor": result[-1]["id"] if result else after})

    async def attachments(request):
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "file":
            raise ValueError("multipart file field required")
        ident = uid()
        path = service.store.root / "attachments" / ident
        size = 0
        try:
            with path.open("xb") as output:
                os.chmod(path, 0o600)
                while True:
                    chunk = await part.read_chunk(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ATTACHMENT:
                        raise web.HTTPRequestEntityTooLarge(max_size=MAX_ATTACHMENT, actual_size=size)
                    output.write(chunk)
            name = Path((part.filename or "attachment").replace("\\", "/")).name[:255]
            record = dict(id=ident, name=name, mime=part.headers.get("Content-Type", "application/octet-stream"), size=size)
            service.store.put("attachments", record)
            return web.json_response(record, status=201)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    async def attachment_content(request):
        attachment = service.store.get("attachments", request.match_info["id"])
        if not attachment:
            raise web.HTTPNotFound()
        return web.FileResponse(service.store.root / "attachments" / attachment["id"], headers={"Content-Type": attachment["mime"], "Content-Disposition": "attachment"})

    async def files(request):
        return web.json_response(service.tools.files(request.query.get("path", ".")))

    async def file_content(request):
        path = workspace_path(service.store.workspace, request.query.get("path", ""))
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Disposition": "attachment"})

    async def terminal(request):
        data = await request.json()
        result = await service.tools.execute("exec", data)
        return web.json_response(result)

    async def schedules(request):
        if request.method == "GET":
            result = service.store.all("schedules")
            for item in result:
                session = service.store.get("sessions", item.get("last_session_id"))
                item["last_status"] = session["status"] if session else None
            return web.json_response({"schedules": public(result)})
        data = await request.json()
        config = validate_config(data.get("config"))
        title = limited_text(data.get("title"), "title", 200)
        prompt = limited_text(data.get("prompt"), "prompt", 200000)
        next_run = next_fire(data["cron"], data["timezone"])
        item = dict(id=uid(), title=title, prompt=prompt, cron=data["cron"], timezone=data["timezone"],
                    enabled=True, next_run_at=next_run, last_run_at=None, last_session_id=None, _config=config)
        service.store.put("schedules", item)
        service.emit(None, "schedule_updated", {"schedule": public(item)})
        return web.json_response(public(item), status=201)

    async def schedule_item(request):
        item = service.store.get("schedules", request.match_info["id"])
        if not item:
            raise web.HTTPNotFound()
        if request.method == "DELETE":
            service.store.delete("schedules", item["id"])
            service.emit(None, "schedule_deleted", {"id": item["id"]})
            return web.json_response({"ok": True})
        data = await request.json()
        if type(data.get("enabled")) is not bool:
            raise ValueError("enabled must be boolean")
        item["enabled"] = data["enabled"]
        if item["enabled"]:
            item["next_run_at"] = next_fire(item["cron"], item["timezone"])
        service.store.put("schedules", item)
        service.emit(None, "schedule_updated", {"schedule": public(item)})
        return web.json_response(public(item))

    async def desktop_status(request):
        return web.json_response(await service.desktop_capabilities())

    async def takeover(request):
        if desktop_access:
            raise web.HTTPGone(text="Use /desktop/takeover for device-scoped control")
        data = await request.json()
        if type(data.get("active")) is not bool:
            raise ValueError("active must be boolean")
        # Wait for an in-flight action before acknowledging human control.
        async with service.desktop_lock:
            session = service.session(request.match_info["id"])
            session["manual_takeover"] = data["active"]
            if data["active"]:
                service.takeover_sessions.add(session["id"])
            else:
                service.takeover_sessions.discard(session["id"])
            service.save(session, "takeover_changed")
        return web.json_response({"active": data["active"]})

    async def device_takeover(request):
        if desktop_access is None:
            raise web.HTTPNotFound()
        data = await request.json()
        if type(data.get("active")) is not bool:
            raise ValueError("active must be boolean")
        return web.json_response(await desktop_access.takeover(request["duet_desktop_identity"], data["active"]))

    async def novnc(request):
        root = service.novnc_dir.resolve()
        path = (root / request.match_info.get("path", "vnc.html")).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def desktop_wrapper(request):
        if not service.tools.desktop_auth_ready() or not (service.novnc_dir / "core" / "rfb.js").is_file():
            return web.json_response({"error": "Authenticated desktop configuration unavailable"}, status=503)
        password = (service.tools.desktop_dir / "vnc.secret").read_text(encoding="ascii").strip()
        if len(password) != 8:
            return web.json_response({"error": "Desktop credential configuration invalid"}, status=503)
        if desktop_access:
            ticket = desktop_access.issue(request["duet_desktop_identity"])
            return desktop_view(request, ticket, password, public_origin=public_origin, ticket=True)
        return desktop_view(request, service.token, password)

    async def vnc(request):
        if not service.tools.desktop_auth_ready():
            return web.json_response({"error": "Authenticated VNC configuration unavailable"}, status=503)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", service.vnc_port), 5)
        except (OSError, asyncio.TimeoutError):
            return web.json_response({"error": "VNC desktop unavailable"}, status=503)
        socket = web.WebSocketResponse(protocols=("binary",), max_msg_size=4 * 1024 * 1024, timeout=2)
        try:
            if desktop_access and not desktop_access.valid(request["duet_desktop_identity"]):
                raise web.HTTPUnauthorized()
            await socket.prepare(request)
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise
        if desktop_access:
            desktop_access.sockets[socket] = request["duet_desktop_identity"]
        async def from_vnc():
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                await socket.send_bytes(chunk)
        async def from_client():
            async for item in socket:
                if item.type == aiohttp.WSMsgType.BINARY:
                    writer.write(item.data)
                    await writer.drain()
                elif item.type == aiohttp.WSMsgType.TEXT:
                    break
        tasks = [asyncio.create_task(from_vnc()), asyncio.create_task(from_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if desktop_access:
                desktop_access.disconnected(socket)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            await socket.close()
        return socket

    app.router.add_get("/duet/attestation", attestation)
    app.router.add_get("/health", health)
    app.router.add_post("/maintenance", maintenance)
    if expose_legacy_sessions:
        app.router.add_get("/sessions", sessions)
        app.router.add_post("/sessions", sessions)
        app.router.add_route("GET", "/sessions/{id}", session_route)
        app.router.add_route("PATCH", "/sessions/{id}", session_route)
        app.router.add_route("DELETE", "/sessions/{id}", session_route)
        app.router.add_post("/sessions/{id}/messages", send_message)
        app.router.add_post("/sessions/{id}/stop", stop)
        app.router.add_post("/sessions/{id}/approve", approve)
        app.router.add_post("/sessions/{id}/takeover", takeover)
        app.router.add_get("/events", events)
    app.router.add_post("/attachments", attachments)
    app.router.add_get("/attachments/{id}/content", attachment_content)
    app.router.add_get("/files", files)
    app.router.add_get("/files/content", file_content)
    app.router.add_post("/terminal", terminal)
    if expose_legacy_sessions:
        app.router.add_get("/schedules", schedules)
        app.router.add_post("/schedules", schedules)
        app.router.add_patch("/schedules/{id}", schedule_item)
        app.router.add_delete("/schedules/{id}", schedule_item)
    app.router.add_get("/desktop/status", desktop_status)
    app.router.add_post("/desktop/takeover", device_takeover)
    app.router.add_get("/desktop/view", desktop_wrapper)
    app.router.add_get("/desktop/vnc", vnc)
    app.router.add_get("/desktop/novnc/{path:.*}", novnc)
    return app

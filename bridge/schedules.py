"""Local trigger scheduler using Aibo's existing session/backend execution path."""
import asyncio
import copy
import json
import os
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiohttp import web
from duet_server.app import next_fire

from .aibo import AiboError, canonical_session
from .rest_home import object_body, utc_now


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if value is not None else None


def instant(value):
    if not isinstance(value, str):
        raise ValueError("run_once_at must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("run_once_at needs an explicit offset")
    return parsed.timestamp()


class LocalSchedules:
    def __init__(self, home, aibo, config=None, data_dir=None, engine=None):
        self.home, self.aibo, self.config, self.engine = home, aibo, config, engine
        self.path = Path(data_dir) / "native-triggers.json" if data_dir is not None else None
        self.state = {"version": 1, "triggers": {}}
        self.lock = asyncio.Lock()
        self.task = None
        self.routines = None
        if self.path and self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("version") != 1:
                raise ValueError("Unsupported trigger store")
            # A claimed dispatch is never replayed; reconcile its owned session only.
            for trigger in self.state["triggers"].values():
                # Older task moves changed channel_id but left the native
                # listing owner behind. Historical run sessions stay in place.
                if self.home.channel(trigger["channel_id"]):
                    trigger["owner_session_id"] = self.aibo.coordinator(trigger["channel_id"])["id"]
                for run in trigger["runs"]:
                    if run["status"] == "dispatching":
                        run.update(status="interrupted", failure_reason="Dispatch interrupted; not replayed")
            self.save()

    def save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(self.state, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)

    async def delete_channel(self, channel_id, ensure_idle):
        """Remove only this channel's schedules under the dispatch lock.

        The caller's synchronous guard runs after acquiring the lock, so a
        dispatch which started while waiting cannot be purged underneath a run.
        Existing sessions and schedules belonging to other channels stay intact.
        """
        async with self.lock:
            ensure_idle()
            identifiers = [ident for ident, trigger in self.state["triggers"].items()
                           if trigger["channel_id"] == channel_id]
            for ident in identifiers:
                del self.state["triggers"][ident]
            if self.routines is not None:
                self.routines.invalidate_channel(channel_id)
            if identifiers or self.routines is not None:
                self.save()

    def maintenance(self):
        return self.engine is not None and bool(getattr(self.engine, "maintenance", False))

    def _get(self, tid):
        result = self.state["triggers"].get(tid)
        if result is None:
            raise AiboError("not_found", "Unknown local trigger", 404)
        return result

    def _prompt(self, body, previous):
        prompt = body.get("prompt", previous.get("prompt"))
        if "session_request" in body:
            request = body["session_request"]
            if not isinstance(request, dict) or set(request) - {"events", "environment_id"}:
                raise AiboError("not_implemented", "Unsupported trigger session request", 501)
            events = request.get("events")
            if not isinstance(events, list) or len(events) != 1 or not isinstance(events[0], dict):
                raise AiboError("not_implemented", "Only a single SDK text user event is supported", 501)
            event = events[0].get("payload", events[0])
            if not isinstance(event, dict) or event.get("type") != "user" or not isinstance(event.get("message"), dict):
                raise AiboError("not_implemented", "Only an SDK text user event is supported", 501)
            content = event["message"].get("content")
            if isinstance(content, list):
                if not all(isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str) for p in content):
                    raise AiboError("not_implemented", "Only text trigger content is supported", 501)
                content = "".join(p["text"] for p in content)
            if "prompt" in body and prompt != content:
                raise AiboError("invalid_argument", "Conflicting prompt and session_request")
            prompt = content
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 200000:
            raise AiboError("invalid_argument", "A nonempty trigger prompt is required")
        return prompt

    def validate(self, body, previous=None, now=None):
        now = time.time() if now is None else now
        previous = previous or {}
        if previous.get("builtin_routine"):
            raise AiboError("invalid_argument", "Use the account routine switches for built-in routines", 409)
        known = {"name", "prompt", "cron_expression", "run_once_at", "enabled", "session_request", "timezone", "hearth_channel_id"}
        if set(body) - known:
            raise AiboError("not_implemented", "Unsupported trigger fields; models/plugins/persistent sessions are not overridden", 501)
        result = copy.deepcopy(previous)
        result["prompt"] = self._prompt(body, previous)
        name = body.get("name", previous.get("name", result["prompt"][:80]))
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise AiboError("invalid_argument", "Trigger name required")
        result["name"] = name
        enabled = body.get("enabled", previous.get("enabled", True))
        if type(enabled) is not bool:
            raise AiboError("invalid_argument", "enabled must be boolean")
        result["enabled"] = enabled
        project = self.home.state.get("project") or {}
        cid = body.get("hearth_channel_id", previous.get("channel_id", project.get("channel_id")))
        if not isinstance(cid, str) or not self.home.channel(cid):
            raise AiboError("not_found", "A real local project/channel is required", 404)
        if previous and cid != previous["channel_id"]:
            raise AiboError("not_implemented", "Moving triggers between channels is not implemented", 501)
        result["channel_id"] = cid
        result["owner_session_id"] = self.aibo.coordinator(cid)["id"]
        cron = body.get("cron_expression", previous.get("cron_expression"))
        cron_zone = None
        if isinstance(cron, str) and cron.startswith(("CRON_TZ=", "TZ=")):
            prefix, separator, cron = cron.partition(" ")
            cron_zone = prefix.split("=", 1)[1]
            if not separator or not cron_zone:
                raise AiboError("invalid_argument", "A cron time zone must precede a five-field expression")
            if "timezone" in body and body["timezone"] != cron_zone:
                raise AiboError("invalid_argument", "Conflicting cron and explicit time zones")
            cron = cron.strip()
        zone = body.get("timezone", cron_zone or previous.get("timezone", project.get("settings", {}).get("time_zone") or "UTC"))
        if not isinstance(zone, str):
            raise AiboError("invalid_argument", "timezone must be an IANA zone")
        ZoneInfo(zone)
        result["timezone"] = zone
        once = body.get("run_once_at", previous.get("run_once_at"))
        if bool(cron) == bool(once):
            raise AiboError("invalid_argument", "Choose exactly one cron_expression or run_once_at")
        result["cron_expression"], result["run_once_at"] = cron, once
        planned = next_fire(cron, zone, now) if cron else instant(once)
        changed = not previous or any(k in body for k in ("cron_expression", "run_once_at", "timezone")) or (enabled and not previous["enabled"])
        if once and planned <= now and changed:
            raise AiboError("invalid_argument", "run_once_at must be in the future")
        result["next_epoch"] = planned if changed else previous["next_epoch"]
        if not enabled:
            result["next_epoch"] = None
        result["updated_at"] = utc_now()
        return result

    def reconcile(self):
        changed = False
        for trigger in self.state["triggers"].values():
            for run in trigger["runs"]:
                if run["status"] != "running":
                    continue
                session = self.aibo.state["sessions"].get(run["session_id"])
                phase = session.get("phase") if session else "interrupted"
                if phase != "running":
                    run["status"] = "completed" if phase == "idle" else phase
                    if phase in ("error", "interrupted") and not run.get("failure_reason"):
                        run["failure_reason"] = "Execution failed" if phase == "error" else "Execution interrupted; not replayed"
                    changed = True
        if changed:
            self.save()

    def public(self, trigger):
        last = trigger["runs"][-1] if trigger["runs"] else None
        last_wire = {k: last[k] for k in ("status", "failure_reason", "fired_at", "session_id")} if last else None
        if last_wire:
            last_wire["status"] = {"error": "failed", "interrupted": "abandoned", "stopped": "abandoned"}.get(last_wire["status"], last_wire["status"])
            if last_wire["failure_reason"] == "Model configuration missing or invalid":
                last_wire["failure_reason"] = "CONFIG_REJECTED"
        # The native client treats a bare cron as UTC. Keep the execution cron
        # separate, and carry its IANA zone in the native parser's wire syntax.
        cron = trigger["cron_expression"]
        wire_cron = "CRON_TZ=" + trigger["timezone"] + " " + cron if cron else None
        # Explicit whitelist: no config object or upstream API keys enter this model.
        return {"id": trigger["id"], "name": trigger["name"], "cron_expression": wire_cron,
                "run_once_at": trigger["run_once_at"], "enabled": trigger["enabled"],
                "next_run_at": iso(trigger["next_epoch"]), "last_fired_at": last["fired_at"] if last else None,
                "created_at": trigger["created_at"], "updated_at": trigger["updated_at"],
                "hearth_channel_id": trigger["channel_id"], "persistent_session_id": trigger["owner_session_id"],
                "persist_session": False, "derived_state": {"prompt": trigger["prompt"]},
                "display_cadence": trigger["cron_expression"] or "一次",
                "display_schedule": trigger["timezone"], "last_run": last_wire,
                "ended_reason": "run_once_fired" if trigger["run_once_at"] and any(not r.get("manual") for r in trigger["runs"]) else None}

    async def dispatch(self, trigger, now=None, manual=False, token=None):
        """Caller owns lock. Commit fire and intended session ID before any execution."""
        if self.maintenance():
            raise AiboError("unavailable", "Service maintenance pauses new trigger dispatch", 503)
        if token:
            previous = next((r for r in trigger["runs"] if r.get("manual_token") == token), None)
            if previous:
                return previous
        channel = self.home.channel(trigger["channel_id"])
        if channel is None or channel.get("archived_at") or channel.get("disabled_at"):
            raise AiboError("conflict", "Channel is archived, disabled or deleted", 409)
        if trigger.get("builtin_routine"):
            if self.routines is None or not self.routines.can_run(trigger):
                raise AiboError("conflict", "Built-in routine is not currently enabled", 409)
            if any(r["status"] in ("dispatching", "running") for r in trigger["runs"]):
                raise AiboError("conflict", "This routine already has an active run", 409)
        now = time.time() if now is None else now
        sid = "session_" + str(uuid4())
        run = {"id": str(uuid4()), "session_id": sid, "fired_at": iso(now), "status": "dispatching", "failure_reason": None, "manual": manual}
        if token:
            run["manual_token"] = token
        trigger["runs"].append(run)
        if trigger.get("builtin_routine"):
            self.routines.claimed(trigger, now)
        if not manual:
            if trigger["cron_expression"]:
                trigger["next_epoch"] = (self.routines.next_epoch(trigger, now) if trigger.get("builtin_routine")
                                         else next_fire(trigger["cron_expression"], trigger["timezone"], now))
            else:
                trigger["next_epoch"] = None
                trigger["enabled"] = False
        self.save()
        try:
            session = self.aibo._new_session(trigger["channel_id"], trigger["name"], sid)
            session["trigger_id"] = trigger["id"]
            if trigger.get("builtin_routine"):
                self.routines.begin(trigger, run, session)
                run["status"] = "running"
                self.save()
                return run
            missing_config = False
            if self.config is not None:
                try:
                    self.config.load()  # Validate current config only; never persist credentials in a plan.
                except (OSError, ValueError, RuntimeError):
                    missing_config = True
            self.aibo._begin(session, trigger["prompt"], "trigger:" + run["id"])
            run["status"] = "running"
            if missing_config:
                task = self.aibo.tasks.pop(sid, None)
                if task:
                    task.cancel()  # _run has not entered; do not contact an unconfigured provider.
                session["messages"][-1]["body"] = "计划执行失败：模型配置缺失或无效，请先配置 API 和模型。"
                self.aibo._finish(session, "error")
                if task:
                    await asyncio.gather(task, return_exceptions=True)
                run.update(status="error", failure_reason="Model configuration missing or invalid")
        except Exception:
            run.update(status="error", failure_reason="Local session dispatch failed")
        self.save()
        return run

    async def tick(self, now=None):
        async with self.lock:
            self.reconcile()
            if self.routines is not None:
                self.routines.reconcile()
            project = self.home.state.get("project") or {}
            if self.maintenance():
                return
            now = time.time() if now is None else now
            for trigger in list(self.state["triggers"].values()):
                channel = self.home.channel(trigger["channel_id"])
                if channel is None or channel.get("archived_at") or channel.get("disabled_at"):
                    continue
                if project.get("preferences", {}).get("routines_paused", False) and not trigger["run_once_at"]:
                    continue
                if trigger.get("builtin_routine"):
                    if not self.routines or not self.routines.can_run(trigger):
                        continue
                    if any(r["status"] in ("dispatching", "running") for r in trigger["runs"]):
                        if trigger["next_epoch"] is not None and trigger["next_epoch"] <= now:
                            trigger["next_epoch"] = self.routines.next_epoch(trigger, now)
                            self.save()  # Skip overlapping slots, never queue a catch-up fire.
                        continue
                if trigger["enabled"] and trigger["next_epoch"] is not None and trigger["next_epoch"] <= now:
                    if trigger.get("builtin_routine") and not self.routines.due_current(trigger, now):
                        continue
                    await self.dispatch(trigger, now)

    async def loop(self):
        while True:
            try:
                await self.tick()
            except (OSError, ValueError, RuntimeError) as error:
                # Keep a recoverable scheduler alive; never replay an already claimed fire.
                logging.getLogger(__name__).error("Native scheduler step failed: %s", type(error).__name__)
            await asyncio.sleep(1)

    async def start(self, app):
        self.task = asyncio.create_task(self.loop())

    async def close(self, app):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.reconcile()
        self.save()

    async def collection(self, request):
        async with self.lock:
            self.reconcile()
            if request.method == "POST":
                value = self.validate(await object_body(request))
                value.update(id="trig_" + str(uuid4()), created_at=utc_now(), runs=[])
                self.state["triggers"][value["id"]] = value
                self.save()
                return web.json_response({"trigger": self.public(value)}, status=201)
            items = list(self.state["triggers"].values())
            owner = request.query.get("persistent_session_id")
            if owner:
                items = [t for t in items if t["owner_session_id"] == canonical_session(owner)]
            if request.query.get("exclude_completed_run_once", "false").lower() == "true":
                items = [t for t in items if not (t["run_once_at"] and any(not r.get("manual") for r in t["runs"]))]
            limit, offset = int(request.query.get("limit", 100)), int(request.query.get("offset", 0))
            if not 1 <= limit <= 1000 or offset < 0:
                raise ValueError("Invalid pagination")
            return web.json_response({"data": [self.public(t) for t in items[offset:offset + limit]], "has_more": len(items) > offset + limit})

    async def item(self, request):
        async with self.lock:
            trigger = self._get(request.match_info["tid"])
            self.reconcile()
            if request.method == "DELETE":
                del self.state["triggers"][trigger["id"]]
                self.save()  # Existing owned sessions continue; no unrelated task is cancelled.
                return web.json_response({})
            if request.method == "POST":
                updated = self.validate(await object_body(request), trigger)
                self.state["triggers"][trigger["id"]] = updated
                self.save()
                trigger = updated
            return web.json_response({"trigger": self.public(trigger)})

    async def run_now(self, request):
        if request.can_read_body and await request.read():
            raise AiboError("not_implemented", "Native run endpoint has no request body", 501)
        token = request.headers.get("Idempotency-Key")
        if token is not None and (not token or len(token) > 200):
            raise ValueError("Invalid idempotency key")
        async with self.lock:
            trigger = self._get(request.match_info["tid"])
            run = await self.dispatch(trigger, manual=True, token=token)
            return web.json_response({"trigger": self.public(trigger), "session_id": run["session_id"]})

    def wrap(self, method):
        async def handler(request):
            try:
                return await method(request)
            except AiboError as error:
                return web.json_response({"error": {"code": error.code, "message": error.message}}, status=error.status)
            except (ValueError, TypeError, KeyError):
                return web.json_response({"error": {"code": "invalid_argument", "message": "Invalid trigger schedule or parameters"}}, status=400)
        return handler

    def register(self, app):
        for method in ("GET", "POST"):
            app.router.add_route(method, "/v1/code/triggers", self.wrap(self.collection))
        for method in ("GET", "POST", "DELETE"):
            app.router.add_route(method, "/v1/code/triggers/{tid}", self.wrap(self.item))
        app.router.add_post("/v1/code/triggers/{tid}/run", self.wrap(self.run_now))
        app.on_startup.append(self.start)
        app.on_shutdown.append(self.close)
        return self


def register_schedules(app, home, aibo, config=None, data_dir=None, engine=None):
    scheduler = LocalSchedules(home, aibo, config, data_dir, engine).register(app)
    if engine is not None and hasattr(engine, "extensions"):
        async def create(arguments, session_id):
            session = aibo.session(session_id)
            async with scheduler.lock:
                engine.recheck_tool_dispatch(session_id)
                value = scheduler.validate(dict(arguments, hearth_channel_id=session["channel_id"]))
                value.update(id="trig_" + str(uuid4()), created_at=utc_now(), runs=[])
                scheduler.state["triggers"][value["id"]] = value
                scheduler.save()
                return {"trigger": scheduler.public(value)}

        async def list_plans(arguments, session_id):
            session = aibo.session(session_id)
            scheduler.reconcile()
            return {"triggers": [scheduler.public(t) for t in scheduler.state["triggers"].values() if t["channel_id"] == session["channel_id"]]}

        async def change(arguments, session_id):
            session = aibo.session(session_id)
            async with scheduler.lock:
                engine.recheck_tool_dispatch(session_id)
                trigger = scheduler._get(arguments["id"])
                if trigger["channel_id"] != session["channel_id"]:
                    raise ValueError("Trigger belongs to another channel")
                updated = scheduler.validate({k: v for k, v in arguments.items() if k != "id"}, trigger)
                scheduler.state["triggers"][trigger["id"]] = updated
                scheduler.save()
                return {"trigger": scheduler.public(updated)}

        async def delete(arguments, session_id):
            session = aibo.session(session_id)
            async with scheduler.lock:
                engine.recheck_tool_dispatch(session_id)
                trigger = scheduler._get(arguments["id"])
                if trigger["channel_id"] != session["channel_id"]:
                    raise ValueError("Trigger belongs to another channel")
                del scheduler.state["triggers"][trigger["id"]]
                scheduler.save()
                return {"deleted": trigger["id"]}

        properties = {"name": {"type": "string"}, "prompt": {"type": "string"}, "cron_expression": {"type": "string"},
                      "run_once_at": {"type": "string"}, "timezone": {"type": "string"}, "enabled": {"type": "boolean"}}
        definitions = (
            ("create_trigger", "Create a requested recurring or one-time task. Supply either five-field cron_expression with IANA timezone or run_once_at with explicit offset.", properties, ["name", "prompt"], create),
            ("list_triggers", "List scheduled tasks for this channel.", {}, [], list_plans),
            ("update_trigger", "Update or pause a scheduled task in this channel.", dict(properties, id={"type": "string"}), ["id"], change),
            ("delete_trigger", "Delete a scheduled task requested by the user.", {"id": {"type": "string"}}, ["id"], delete),
        )
        for name, description, fields, required, callback in definitions:
            engine.extensions[name] = ({"name": name, "description": description, "parameters": {
                "type": "object", "properties": fields, "required": required, "additionalProperties": False}}, callback)
            if callback is list_plans and hasattr(engine, "plan_read_only"):
                # Reconciles cached run status only; cannot launch/change a schedule.
                engine.plan_read_only[name] = callback
    return scheduler

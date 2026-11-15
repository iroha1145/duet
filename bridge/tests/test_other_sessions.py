"""Real Aibo/task lifecycle -> original other-sessions JSON, synthetic only."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bridge.aibo import AiboBridge
from bridge.auth import AUTH_MANAGER, AUTH_PRINCIPAL, Principal
from bridge.channel_tasks import register_channel_tasks
from bridge.channels import register_channels
from bridge.other_sessions import CAPABILITY, OtherSessions, register_other_sessions
from bridge.request_cards import register_request_cards
from bridge.rest import ACCOUNT_ID, ORGANIZATION_ID
from bridge.rest_home import HomeStore
from bridge.schedules import LocalSchedules
from bridge.task_watch import register_task_watch


class Backend:
    def __init__(self):
        self.entered, self.release = {}, {}
        self.answers = []

    async def handle_message(self, sid, uid, aid, text, emit):
        if text == "approval":
            await emit({"type": "approval_required", "id": "approval-local", "tool_call_id": "tool-local",
                        "tool": "exec", "arguments": {"command": "PRIVATE_SYNTHETIC_ARGUMENT"}})
        self.entered.setdefault(sid, asyncio.Event()).set()
        await self.release.setdefault(sid, asyncio.Event()).wait()
        if text == "failure":
            raise RuntimeError("PRIVATE_SYNTHETIC_BACKEND_FAILURE")
        await emit("synthetic completed")

    async def approve(self, sid, rid, allow, always):
        self.answers.append((sid, rid, allow, always))
        self.release[sid].set()


class OtherSessionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.home = HomeStore(self.path / "home.json")
        project, _ = self.home.ensure_project({})
        self.cid = project["channel_id"]
        self.backend = Backend()
        self.aibo = AiboBridge(self.home, self.backend, self.path / "aibo.json")
        self.valid = True
        self.revoke_after_settings = False

        @web.middleware
        async def auth(request, handler):
            request["duet_authorized"] = lambda: self.valid
            if request.headers.get("Unauthenticated") != "yes":
                request[AUTH_PRINCIPAL] = Principal(request.headers.get("Device", "one"),
                    request.headers.get("Username", "owner"), "Synthetic", request.headers.get("Account", ACCOUNT_ID),
                    request.headers.get("Organization", ORGANIZATION_ID))
            return await handler(request)

        self.app = web.Application(middlewares=[auth])
        self.app[AUTH_MANAGER] = SimpleNamespace(public_user=lambda: {"username": "owner"})
        self.aibo.register(self.app)
        self.channels = register_channels(self.app, self.home, self.aibo)
        self.schedules = LocalSchedules(self.home, self.aibo, data_dir=self.path)
        self.app.router.add_post("/v1/code/triggers", self.schedules.wrap(self.schedules.collection))
        self.app.router.add_delete("/v1/code/triggers/{tid}", self.schedules.wrap(self.schedules.item))
        self.tasks = register_channel_tasks(self.app, self.home, self.aibo, self.schedules,
                                          SimpleNamespace(live_sessions={}), data_dir=self.path)
        self.watch = register_task_watch(self.app, self.aibo)
        self.cards = register_request_cards(self.app, self.home, self.aibo, self.path, watch=self.watch)
        self.other = register_other_sessions(self.app, self.home, self.aibo, watch=self.watch, cards=self.cards,
                                             tasks=self.tasks, page_size=2)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        for event in self.backend.release.values():
            event.set()
        if self.aibo.tasks:
            await asyncio.gather(*list(self.aibo.tasks.values()), return_exceptions=True)
        await self.client.close()
        self.temp.cleanup()

    async def listing(self, body=None, *, headers=None, alias=False):
        result = await self.client.post(("/api" if alias else "") + "/v1/code/aibo/other-sessions:list",
                                        json={} if body is None else body, headers=headers)
        data = await result.json()
        self.assertNotIn("PRIVATE_SYNTHETIC", json.dumps(data))
        return result.status, data

    async def task(self, tid, mode="working", cid=None):
        cid = cid or self.cid
        result = await self.client.post(f"/v1/code/aibo/channels/{cid}/tasks",
            json={"task_id": tid, "title": "Synthetic " + tid, "description": mode, "assignee": "agent"})
        self.assertEqual(result.status, 200, await result.text())
        sid = self.tasks.state["channels"][cid][tid]["session_id"]
        await asyncio.wait_for(self.backend.entered.setdefault(sid, asyncio.Event()).wait(), 2)
        return self.aibo.state["sessions"][sid]

    async def finish(self, worker):
        self.backend.release[worker["id"]].set()
        await asyncio.gather(self.aibo.tasks[worker["id"]])

    async def schedule_discussion(self, title):
        created = await self.client.post("/v1/code/triggers", json={"name": title, "prompt": "scheduled",
            "run_once_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        self.assertEqual(created.status, 201, await created.text())
        tid = (await created.json())["trigger"]["id"]
        opened = await self.client.post(f"/v1/code/aibo/channels/{self.cid}/tasks/{tid}/thread", json={})
        self.assertEqual(opened.status, 200, await opened.text())
        sid = (await opened.json())["task"]["thread_own_session_id"]
        worker = self.aibo.state["sessions"][sid]
        await asyncio.wait_for(self.backend.entered.setdefault(sid, asyncio.Event()).wait(), 2)
        await self.finish(worker)
        return tid, worker

    async def test_real_worker_lifecycle_approval_and_native_contract(self):
        self.assertNotIn(CAPABILITY, self.home.capabilities)  # Native opening requires separate integration.
        self.assertEqual((await self.listing())[1]["entries"], [])
        ordinary = await self.client.post(f"/v1/code/channels/{self.cid}/messages",
            json={"body": "ordinary chat", "client_message_id": "ordinary"})
        self.assertEqual(ordinary.status, 200)
        coordinator = self.aibo.coordinator(self.cid)
        await asyncio.wait_for(self.backend.entered.setdefault(coordinator["id"], asyncio.Event()).wait(), 2)
        self.assertEqual((await self.listing())[1]["entries"], [])
        worker = await self.task("running")
        status, data = await self.listing(alias=True)
        self.assertEqual(status, 200)
        row = data["entries"][0]
        self.assertEqual((row["session_id"], row["state"]), (worker["id"], "working"))
        self.assertEqual(row["where"], {"product": "duet", "runs_on": "own_runner"})
        self.assertEqual(row["task"], {"channel_id": self.cid, "task_id": "running", "thread_id": worker["root_id"]})
        self.assertEqual(row["duet_context"]["thread_root_id"], worker["root_id"])
        self.assertFalse(row["page"]["can_open"])
        self.assertFalse(row["page"]["needs_device_proof"])
        self.assertEqual(row["page"]["can"], [])
        self.assertEqual(row["page"]["duet_context"], row["duet_context"])
        self.assertEqual((row["page"]["product"], row["page"]["why_not"]), ("duet", "open_in_channel_tasks"))
        self.assertFalse({"claude_path", "computer_key", "computer", "url"} & row.keys())
        await self.finish(worker)
        finished = (await self.listing({"session_id": worker["id"]}))[1]["entries"][0]
        self.assertEqual((finished["state"], finished["files_under"], finished["stage"]), ("idle", "ready_for_review", "at_rest"))
        approval = await self.task("approval", "approval")
        row = (await self.listing({"session_id": approval["id"]}))[1]["entries"][0]
        self.assertEqual((row["state"], row["request"]), ("needs_you", {"wait_key": "approval-local", "can_open_card": True}))
        self.assertEqual(self.backend.answers, [])
        result = await self.client.post("/v1/code/aibo/request-cards:get", json={"session_id": approval["id"]})
        card = (await result.json())["card"]
        approval_run = self.aibo.tasks[approval["id"]]
        result = await self.client.post("/v1/code/aibo/request-cards:answer", json={"session_id": approval["id"],
            "request_id": card["request_id"], "token": card["token"], "answer": "allow_once"})
        self.assertEqual((await result.json())["outcome"], "accepted")
        await asyncio.gather(approval_run)
        self.assertEqual(len(self.backend.answers), 1)
        self.assertNotIn("request", (await self.listing({"session_id": approval["id"]}))[1]["entries"][0])
        failure = await self.task("failure", "failure")
        await self.finish(failure)
        row = (await self.listing({"session_id": failure["id"]}))[1]["entries"][0]
        self.assertEqual((row["state"], row["files_under"], row["stage"]), ("failed", "couldnt_finish", "ended"))

    async def test_keyset_paging_alias_filters_and_tamper_are_scoped(self):
        workers = [await self.task("task-" + str(i)) for i in range(3)]
        for worker in workers:
            await self.finish(worker)
        first = (await self.listing({"include_project_threads": True}))[1]
        self.assertEqual(len(first["entries"]), 2)
        self.assertTrue(first["next_cursor"])
        # An activity update must not move this row into an already-read page.
        workers[0]["updated_at"] = "2099-01-01T00:00:00Z"
        second = (await self.listing({"include_project_threads": True, "cursor": first["next_cursor"]}, alias=True))[1]
        seen = [row["session_id"] for page in (first, second) for row in page["entries"]]
        self.assertEqual(set(seen), {worker["id"] for worker in workers})
        self.assertEqual(len(seen), len(set(seen)))
        self.assertIsNone(second["next_cursor"])
        for changed in ({"include_project_threads": False}, {"can_sign": True}):
            self.assertEqual((await self.listing(dict(changed, cursor=first["next_cursor"])))[0], 410)
        corrupt = first["next_cursor"][:-1] + ("a" if first["next_cursor"][-1] != "a" else "b")
        self.assertEqual((await self.listing({"include_project_threads": True, "cursor": corrupt}))[0], 410)
        self.assertEqual((await self.listing({"include_project_threads": False}))[1]["entries"], [])
        self.assertEqual((await self.listing({"session_id": workers[0]["id"].replace("session_", "cse_", 1)}))[1]["entries"][0]["session_id"], workers[0]["id"])
        self.assertEqual((await self.listing({"session_id": "session_404"}))[1]["state"], "none")

    async def test_settings_shared_without_new_ledger_and_expired_auth_fails_closed(self):
        worker = await self.task("visible")
        before = deepcopy(self.aibo.state)
        result = await self.client.post("/v1/code/aibo/other-work-settings:set", json={"show": False})
        self.assertEqual(result.status, 200)
        self.assertEqual((await self.listing({"honour_show_setting": True}))[1], {"state": "off", "entries": [], "next_cursor": None})
        self.assertEqual((await self.listing())[1]["entries"][0]["session_id"], worker["id"])
        self.assertEqual(self.aibo.state, before)
        get_settings = self.cards.get_settings
        async def revoke(request, body):
            result = await get_settings(request, body)
            self.valid = False
            return result
        self.cards.get_settings = revoke
        self.assertEqual((await self.listing())[0], 401)
        self.assertEqual(worker["phase"], "running")
        self.assertFalse(self.backend.release[worker["id"]].is_set())

    async def test_identity_ownership_channel_and_root_boundaries(self):
        worker = await self.task("owned")
        for headers, code in [({"Unauthenticated": "yes"}, 401), ({"Account": "foreign"}, 403),
                              ({"Organization": "foreign"}, 403), ({"Username": "foreign"}, 403)]:
            self.assertEqual((await self.listing(headers=headers))[0], code)
        # Genuine membership is necessary even with a valid account principal.
        channel = self.home.channel(self.cid)
        owner = channel["created_by_account_id"]
        channel["created_by_account_id"] = "foreign"
        self.assertEqual((await self.listing())[1]["entries"], [])
        channel["created_by_account_id"] = owner
        worker["task_channel_id"] = "chan_foreign"
        self.assertEqual((await self.listing())[1]["entries"], [])
        worker["task_channel_id"] = self.cid
        worker["root_id"] = "cmsg_absent"
        self.assertEqual((await self.listing())[1]["entries"], [])

    async def test_real_discussion_schedule_and_explicit_ended_filter(self):
        created = await self.client.post(f"/v1/code/aibo/channels/{self.cid}/tasks",
            json={"task_id": "discussion", "title": "Discuss this task"})
        self.assertEqual(created.status, 200)
        opened = await self.client.post(f"/v1/code/aibo/channels/{self.cid}/tasks/discussion/thread", json={})
        self.assertEqual(opened.status, 200)
        discussion_id = self.tasks.state["channels"][self.cid]["discussion"]["discussion_session_id"]
        await asyncio.wait_for(self.backend.entered.setdefault(discussion_id, asyncio.Event()).wait(), 2)
        row = (await self.listing({"session_id": discussion_id}))[1]["entries"][0]
        self.assertEqual(row["duet_context"]["kind"], "task_discussion")
        self.assertEqual(row["task"]["task_id"], "discussion")
        await self.finish(self.aibo.state["sessions"][discussion_id])
        trigger = self.schedules.validate({"name": "Actual synthetic schedule", "prompt": "scheduled",
            "run_once_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        trigger.update(id="trigger-synthetic", runs=[], created_at=trigger["updated_at"])
        self.schedules.state["triggers"][trigger["id"]] = trigger
        async with self.schedules.lock:
            run = await self.schedules.dispatch(trigger, manual=True)
        sid = run["session_id"]
        await asyncio.wait_for(self.backend.entered.setdefault(sid, asyncio.Event()).wait(), 2)
        rows = (await self.listing({"include_project_threads": False}))[1]["entries"]
        self.assertEqual([row["session_id"] for row in rows], [sid])
        self.assertEqual(rows[0]["duet_context"]["kind"], "scheduled_task")
        self.assertEqual(rows[0]["task"], {"channel_id": self.cid, "task_id": trigger["id"],
                                         "thread_id": self.aibo.state["sessions"][sid]["root_id"]})
        scheduled = self.aibo.state["sessions"][sid]
        await self.finish(scheduled)
        scheduled["ended_at"] = scheduled["updated_at"]
        self.assertEqual((await self.listing({"session_id": sid}))[1]["state"], "none")
        self.assertEqual((await self.listing({"session_id": sid, "include_ended": True}))[1]["entries"][0]["session_id"], sid)
    async def test_strict_twelve_field_wire_and_unsupported_semantics(self):
        for body in ({"include_ended": 1}, {"can_plan": "true"}, {"session_id": 4}, {"session_id": "foreign"},
                     {"cursor": ""}, {"session_id": "session_a", "cursor": "x"}, []):
            self.assertEqual((await self.listing(body))[0], 400, body)
        self.assertEqual((await self.listing({"limit": 2}))[0], 501)
        self.assertEqual((await self.listing({"include_own_code_sessions": True}))[0], 501)
        flags = {"include_project_threads": True, "include_ended": True, "can_plan": True,
                 "can_next_request": True, "can_sign": True, "can_sign_pick": True,
                 "can_sign_pick_several": True, "can_connector_question": True,
                 "honour_show_setting": False, "include_own_code_sessions": None}
        self.assertEqual((await self.listing(flags))[0], 200)
        self.assertEqual((await self.listing({name: None for name in flags}))[0], 200)

    async def test_legacy_schedule_without_task_override_opens_only_its_current_run(self):
        trigger = self.schedules.validate({"name": "Legacy schedule", "prompt": "scheduled",
            "run_once_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        trigger.update(id="trigger-legacy", runs=[], created_at=trigger["updated_at"])
        self.schedules.state["triggers"][trigger["id"]] = trigger
        async with self.schedules.lock:
            first = await self.schedules.dispatch(trigger, manual=True)
        worker = self.aibo.state["sessions"][first["session_id"]]
        await asyncio.wait_for(self.backend.entered.setdefault(worker["id"], asyncio.Event()).wait(), 2)
        await self.finish(worker)
        # Old persisted schedules have a canonical Tasks row, without the
        # optional metadata record introduced by later lifecycle support.
        self.tasks.state["channels"].get(self.cid, {}).pop(trigger["id"], None)
        self.tasks.save()
        response = await self.client.get(f"/v1/code/aibo/channels/{self.cid}/tasks")
        actual = next(row for row in (await response.json())["tasks"] if row["id"] == trigger["id"])
        self.assertEqual(actual["thread_own_session_id"], worker["id"])
        row = (await self.listing({"session_id": worker["id"]}))[1]["entries"][0]
        self.assertEqual(row["task"], {"channel_id": self.cid, "task_id": trigger["id"], "thread_id": worker["root_id"]})
        async with self.schedules.lock:
            current = await self.schedules.dispatch(trigger, manual=True)
        await asyncio.wait_for(self.backend.entered.setdefault(current["session_id"], asyncio.Event()).wait(), 2)
        await self.finish(self.aibo.state["sessions"][current["session_id"]])
        renamed = await self.client.post(f"/v1/code/aibo/channels/{self.cid}/tasks/{trigger['id']}",
                                        json={"title": "Current renamed schedule"})
        self.assertEqual(renamed.status, 200, await renamed.text())
        old_row = (await self.listing({"session_id": worker["id"]}))[1]["entries"][0]
        self.assertNotIn("task", old_row)
        self.assertNotIn("task_id", old_row["page"]["duet_context"])
        self.assertEqual(old_row["title"], "Legacy schedule")
        current_row = (await self.listing({"session_id": current["session_id"]}))[1]["entries"][0]
        self.assertEqual(current_row["title"], "Current renamed schedule")
        self.assertEqual(current_row["task"]["thread_id"], self.aibo.state["sessions"][current["session_id"]]["root_id"])

    async def test_missing_task_is_omitted_without_erasing_worker_history(self):
        worker = await self.task("history")
        await self.finish(worker)
        del self.tasks.state["channels"][self.cid]["history"]
        before = deepcopy(worker)
        self.assertEqual((await self.listing({"session_id": worker["id"]}))[1]["entries"], [])
        self.assertEqual(self.aibo.state["sessions"][worker["id"]], before)

    async def test_deleted_schedules_leave_no_orphan_discussions_or_paging_slots(self):
        discussions = [await self.schedule_discussion(title) for title in ("Morning brief", "Check-ins")]
        for tid, worker in discussions:
            row = (await self.listing({"session_id": worker["id"]}))[1]["entries"][0]
            self.assertEqual(row["title"], "Discuss: " + self.schedules.state["triggers"][tid]["name"])
            self.assertEqual(row["task"], {"channel_id": self.cid, "task_id": tid, "thread_id": worker["root_id"]})
            removed = await self.client.delete("/v1/code/triggers/" + tid)
            self.assertEqual(removed.status, 200, await removed.text())
        before = deepcopy(self.aibo.state)
        self.assertEqual((await self.listing())[1]["entries"], [])
        self.assertEqual(self.aibo.state, before)
        for tid, worker in discussions:
            self.assertFalse(self.tasks.state["channels"][self.cid][tid]["data"].get("retired"))
            self.assertEqual((await self.listing({"session_id": worker["id"], "include_ended": True}))[1]["entries"], [])
        workers = [await self.task("visible-" + str(i)) for i in range(3)]
        first = (await self.listing())[1]
        second = (await self.listing({"cursor": first["next_cursor"]}, alias=True))[1]
        ids = [row["session_id"] for page in (first, second) for row in page["entries"]]
        self.assertEqual(len(first["entries"]), 2)
        self.assertEqual(set(ids), {worker["id"] for worker in workers})
        self.assertEqual(len(ids), 3)
        self.assertIsNone(second["next_cursor"])

    async def test_retired_task_and_moved_schedule_preserve_history_without_old_entries(self):
        worker = await self.task("retired")
        await self.finish(worker)
        removed = await self.client.delete(f"/v1/code/aibo/channels/{self.cid}/tasks/retired")
        self.assertEqual(removed.status, 200, await removed.text())
        self.assertEqual((await self.listing({"session_id": worker["id"]}))[1]["entries"], [])
        self.assertIn(worker["id"], self.aibo.state["sessions"])
        tid, discussion = await self.schedule_discussion("Move this schedule")
        target = self.channels.create({"client_request_id": "destination", "name": "Destination"})["channel"]["id"]
        moved = await self.client.post(f"/v1/code/aibo/channels/{self.cid}/tasks/{tid}/move", json={"to_channel_id": target})
        self.assertEqual(moved.status, 200, await moved.text())
        current = await self.client.get(f"/v1/code/aibo/channels/{target}/tasks")
        self.assertIn(tid, {row["id"] for row in (await current.json())["tasks"]})
        before = deepcopy(discussion)
        self.assertEqual((await self.listing({"session_id": discussion["id"]}))[1]["entries"], [])
        self.assertEqual(self.aibo.state["sessions"][discussion["id"]], before)

    async def test_current_task_titles_follow_renames_with_bound_navigation(self):
        worker = await self.task("rename")
        await self.finish(worker)
        tid, discussion = await self.schedule_discussion("Original schedule")
        for task_id, session, title, expected in (("rename", worker, "Renamed task", "Renamed task"),
                (tid, discussion, "Renamed schedule", "Discuss: Renamed schedule")):
            changed = await self.client.post(f"/v1/code/aibo/channels/{self.cid}/tasks/{task_id}", json={"title": title})
            self.assertEqual(changed.status, 200, await changed.text())
            row = (await self.listing({"session_id": session["id"]}))[1]["entries"][0]
            self.assertEqual(row["title"], expected)
            self.assertEqual(row["task"], {"channel_id": self.cid, "task_id": task_id, "thread_id": session["root_id"]})
            self.assertEqual(row["page"]["duet_context"], row["duet_context"])
            self.assertEqual(row["duet_context"]["task_id"], task_id)

    async def test_registration_rejects_unrelated_state_and_only_adds_list_cap(self):
        with self.assertRaises(ValueError):
            OtherSessions(self.home, self.aibo, SimpleNamespace(aibo=object()), self.cards)
        with self.assertRaises(ValueError):
            OtherSessions(self.home, self.aibo, self.watch, self.cards, page_size=True)
        home = HomeStore(self.path / "other-home.json")
        aibo = AiboBridge(home)
        app = web.Application()
        prior = set(home.capabilities)
        register_other_sessions(app, home, aibo, watch=SimpleNamespace(aibo=aibo),
            cards=SimpleNamespace(aibo=aibo), advertise=True)
        self.assertEqual(set(home.capabilities) - prior, {CAPABILITY})


if __name__ == "__main__":
    unittest.main()

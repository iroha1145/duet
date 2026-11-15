"""Native task note/noted projections from committed local execution evidence.

The original client reads these on AiboTaskRecordData; it has no note CRUD
endpoint. brj/nrj/krj/frj define the wire, and k6k gates native Refresh on
tasks_notes + tasks_refresh. This module never interprets a refresh click as
new progress and never executes model work or tools.
"""
from hashlib import sha256
import json


def execution_sessions(tasks, cid, tid, record, row):
    """Only the worker(s) of this task and its verified chain of prior owners."""
    sessions = {}
    seen = set()

    def add(owner, source):
        candidates = {source.get("session_id"), source.get("prior_session_id")}
        if source.get("origin") == "schedule" and tasks.schedules:
            trigger = tasks.schedules.state["triggers"].get(tid, {})
            candidates.update(run["session_id"] for run in trigger.get("runs", []))
        for sid in candidates - {None}:
            session = tasks.aibo.state["sessions"].get(sid)
            if (session and session["channel_id"] == owner
                    and not session.get("discussion_task_id")):
                sessions[sid] = session

    def visit(owner, source):
        if owner in seen:
            return
        seen.add(owner)
        add(owner, source)
        prior = source.get("data", {}).get("priorChannelId")
        old = tasks.state["channels"].get(prior, {}).get(tid)
        if (old and old["data"].get("retired")
                and old["data"].get("movedToRoomId") == owner
                and old["data"].get("movedToTaskId") == tid):
            visit(prior, old)

    visit(cid, record)
    # Session/trigger-backed rows can precede their first overlay record.
    sid = row.get("thread_own_session_id")
    session = tasks.aibo.state["sessions"].get(sid)
    if session and session["channel_id"] == cid and not session.get("discussion_task_id"):
        sessions[sid] = session
    return sessions.values()


def synchronize(tasks, cid, tid, record, row):
    """Update persisted notes only if actual committed evidence changed."""
    updates, resources, evidence, times = [], {}, [], []
    for session in sorted(execution_sessions(tasks, cid, tid, record, row), key=lambda item: item["id"]):
        for message in session["messages"]:
            if (not message.get("_complete") or message.get("server_notice")
                    or message.get("author_session_id") != session["id"]):
                continue
            # The lifecycle callback runs just before final channel publication,
            # which may assign created_at/edited_at again. Publication alone is
            # not new task evidence; fingerprint content and provenance, not its
            # transport timestamp. The initial note uses the actual finish time.
            at = (message.get("edited_at") or message["created_at"]
                  if message.get("_published") else session["updated_at"])
            text = message.get("body", "").strip()
            if text:
                updates.append({"text": text[:20000], "source": "Task reply", "at": at})
                evidence.append([session["id"], message["id"], text])
                times.append(at)
            for output in message.get("attached_outputs", []):
                # These are published by the workspace tool hook, not parsed
                # from model text or guessed from files in a shared directory.
                ident, name = output.get("id"), output.get("filename")
                if (not ident or not name or output.get("delivery_state") != "delivered"
                        or output.get("provenance_reply_id") != message["id"]):
                    continue
                resource = {"name": name, "kind": "file", "source": "Task output"}
                frame = output.get("frame_slug")
                if (output.get("kind") == "frame" and isinstance(frame, str)
                        and output.get("href") == "https://claude.ai/code/artifact/" + frame):
                    resource.update(link=output["href"], ownPage=True)
                    # Native attached-output handles can migrate independently
                    # of the actual artifact. Keep HTML evidence keyed by its
                    # immutable frame UUID, so that migration is not progress.
                    ident = frame
                resources[ident] = resource
                evidence.append([session["id"], message["id"], ident, name])
                if output.get("created_at"):
                    times.append(output["created_at"])
    if not evidence:
        return False
    updates.sort(key=lambda update: (update["at"], update["text"]))
    fingerprint = sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if record.get("notes_evidence") == fingerprint:
        return False
    data = record["data"]
    at = max(times) if times else record["updated_at"]
    data["noted"] = {"at": at, "updates": updates[-50:],
                     "resources": [resources[key] for key in sorted(resources)][:100]}
    note = {}
    if updates:
        note["source"] = updates[-1]["text"]
    # A next step is shown only when a real task progress tool supplied one.
    next_step = next((step["content"] for step in data.get("steps", [])
                      if step["state"] in ("in_progress", "pending")), None)
    if next_step:
        note["next"] = next_step
    if note:
        data["note"] = note
    record["notes_evidence"] = fingerprint
    record["updated_at"] = max(record["updated_at"], at)
    return True

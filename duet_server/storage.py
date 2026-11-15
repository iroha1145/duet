import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


def uid():
    return uuid.uuid4().hex


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(exist_ok=True)
        (self.root / "attachments").mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "duet.sqlite3")
        os.chmod(self.root / "duet.sqlite3", 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, type TEXT, data TEXT);
          CREATE TABLE IF NOT EXISTS requests(session_id TEXT, request_id TEXT, PRIMARY KEY(session_id,request_id));
          CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        ''')
        # A restart never replays a possibly side-effecting operation.
        for session in self.sessions():
            if session["status"] in ("running", "waiting"):
                session["status"] = "error"
                session["pending_approval"] = None
                for message in session["messages"]:
                    if message["status"] == "streaming":
                        message["status"] = "interrupted"
                close_pending_tools(session, "interrupted")
                self.save_session(session)
                self.event(session["id"], "interrupted", {"reason": "service_restarted"})

    def close(self):
        self.db.close()

    def get(self, table, ident):
        row = self.db.execute(f"SELECT body FROM {table} WHERE id=?", (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, table):
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT body FROM {table}")]

    def put(self, table, obj):
        self.db.execute(f"INSERT OR REPLACE INTO {table}(id,body) VALUES(?,?)", (obj["id"], json.dumps(obj)))
        self.db.commit()

    def delete(self, table, ident):
        self.db.execute(f"DELETE FROM {table} WHERE id=?", (ident,))
        self.db.commit()

    def delete_session(self, session_id):
        """Remove one session and its durable event/request history atomically."""
        with self.db:
            self.db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            self.db.execute("DELETE FROM requests WHERE session_id=?", (session_id,))
            self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def sessions(self):
        return sorted(self.all("sessions"), key=lambda s: s["updated_at"], reverse=True)

    def create_session(self, title="New chat", mode="chat"):
        now = time.time()
        session = dict(id=uid(), title=title, mode=mode, status="idle", created_at=now,
                       updated_at=now, favorite=False, archived=False, messages=[], pending_approval=None,
                       manual_takeover=False)
        self.save_session(session)
        return session

    def save_session(self, session):
        session["updated_at"] = time.time()
        self.put("sessions", session)

    def claim_request(self, session_id, request_id):
        try:
            self.db.execute("INSERT INTO requests VALUES(?,?)", (session_id, request_id))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def event(self, session_id, kind, data):
        row = self.db.execute("INSERT INTO events(session_id,type,data) VALUES(?,?,?)",
                              (session_id, kind, json.dumps(data)))
        self.db.commit()
        return row.lastrowid

    def events(self, after):
        rows = self.db.execute("SELECT id,session_id,type,data FROM events WHERE id>? ORDER BY id LIMIT 500", (after,))
        return [dict(id=r[0], session_id=r[1], type=r[2], data=json.loads(r[3])) for r in rows]


def message(role, text="", **kwargs):
    return dict(id=uid(), role=role, text=text, created_at=time.time(), tool_name=kwargs.pop("tool_name", None),
                attachments=kwargs.pop("attachments", []), status=kwargs.pop("status", "complete"), **kwargs)


def close_pending_tools(session, reason):
    """Close each outstanding call without re-executing possibly completed effects."""
    messages = session["messages"]
    repaired = []
    pending = {}
    def flush():
        for call in pending.values():
            repaired.append(message("tool", json.dumps({
                "error": reason,
                "detail": "Execution may have started; result is unknown. This call will not be replayed."
            }), tool_name=call["name"], tool_call_id=call["id"], status="complete"))
        pending.clear()
    for item in messages:
        if item["role"] != "tool" and pending:
            flush()
        repaired.append(item)
        if item["role"] == "assistant":
            pending.update({call["id"]: call for call in item.get("tool_calls", [])})
        elif item["role"] == "tool":
            pending.pop(item.get("tool_call_id"), None)
    flush()
    session["messages"] = repaired

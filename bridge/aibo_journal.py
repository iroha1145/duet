"""Small append-only streaming journal; JSON checkpoints retain stable cursors.

The event-loop owns state. A worker only appends immutable delta records; the
same file lock orders those appends against synchronous important checkpoints.
"""
import json
import os
import threading
from pathlib import Path


def _sync_directory(directory):
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def apply_delta(state, record):
    """Replay only complete, consecutive records newer than the checkpoint."""
    revision = record["revision"]
    current = state.get("stream_revision", 0)
    if revision <= current:
        return
    if revision != current + 1:
        raise ValueError("Nonconsecutive Aibo journal revision")
    session = state["sessions"][record["session_id"]]
    channel = state["channels"][record["channel_id"]]
    assistant = next(item for item in session["messages"] if item["id"] == record["assistant_id"])
    channel_event, session_event = record["channel_event"], record["session_event"]
    if (channel_event["id"] != len(channel["events"]) + 1
            or session_event["sequence_num"] != len(session["events"]) + 1):
        raise ValueError("Nonconsecutive Aibo journal cursor")
    assistant["body"] += record["delta"]
    channel.setdefault("partial_texts", {})[assistant["id"]] = assistant["body"]
    if channel_event["data"]["_partial_ref"] != [assistant["id"], len(assistant["body"])]:
        raise ValueError("Invalid Aibo partial reference")
    session["updated_at"] = record["at"]
    channel["events"].append(channel_event)
    session["events"].append(session_event)
    state["stream_revision"] = revision


class DeltaJournal:
    def __init__(self, checkpoint):
        self.path = Path(checkpoint)
        self.journal = self.path.with_suffix(self.path.suffix + ".delta")
        self.lock = threading.Lock()
        self.checkpoint_revision = 0
        self.failed = False

    def recover(self, state):
        self.checkpoint_revision = state.get("stream_revision", 0)
        if not self.journal.exists():
            return
        data = self.journal.read_bytes()
        complete_end = data.rfind(b"\n") + 1
        for line in data[:complete_end].splitlines():
            # Corrupt complete records fail closed. Only an incomplete final
            # record can be a torn append; it was never acknowledged to SSE.
            apply_delta(state, json.loads(line))
        if complete_end != len(data):
            with self.journal.open("r+b") as output:
                output.truncate(complete_end)
                output.flush()
                os.fsync(output.fileno())

    def append(self, record):
        data = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self.lock:
            if record["revision"] <= self.checkpoint_revision:
                return  # A concurrent important checkpoint already owns it.
            if self.failed:
                raise OSError("Aibo journal requires a successful checkpoint")
            created = not self.journal.exists()
            descriptor = os.open(self.journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                with os.fdopen(descriptor, "ab") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                if created:
                    _sync_directory(self.journal.parent)
            except BaseException:
                # Do not append another valid line behind a potentially torn
                # record. A complete checkpoint can safely replace this log.
                self.failed = True
                raise

    def checkpoint(self, state):
        with self.lock:
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    json.dump(state, output, ensure_ascii=False, allow_nan=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
                # The checkpoint's rename must be durable before removing the
                # journal that could recover the previous checkpoint.
                _sync_directory(self.path.parent)
                if self.journal.exists():
                    with self.journal.open("wb") as output:
                        output.flush()
                        os.fsync(output.fileno())
                self.checkpoint_revision = state.get("stream_revision", 0)
                self.failed = False
            except BaseException:
                self.failed = True
                raise

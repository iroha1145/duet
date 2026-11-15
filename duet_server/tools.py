import asyncio
import base64
import io
import os
import shutil
import signal
import stat
import tempfile
from pathlib import Path

OUTPUT_LIMIT = 128 * 1024


def workspace_path(root, name):
    root = Path(root).resolve()
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("path must remain inside workspace")
    return candidate


async def run_process(*args, cwd=None, timeout=60, env=None):
    """Bound output and lifetime. Cancellation kills the entire Linux process group."""
    options = {"start_new_session": True} if os.name == "posix" else {}
    process = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options)

    async def collect(reader):
        out = bytearray()
        truncated = False
        while True:
            chunk = await reader.read(8192)
            if not chunk:
                break
            available = OUTPUT_LIMIT - len(out)
            out.extend(chunk[:max(0, available)])
            truncated |= len(chunk) > available
        return out.decode("utf-8", errors="replace") + ("\n[output truncated]" if truncated else "")

    readers = [asyncio.create_task(collect(process.stdout)), asyncio.create_task(collect(process.stderr))]
    try:
        async def complete():
            await process.wait()
            return await asyncio.gather(*readers)
        stdout, stderr = await asyncio.wait_for(complete(), timeout)
        return {"stdout": stdout, "stderr": stderr, "exit_code": process.returncode}
    finally:
        # Even after the parent exits, descendants can keep pipe descriptors open.
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            process.kill()
        if process.returncode is None:
            await process.wait()
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


def tool_definitions():
    def definition(name, description, properties, required):
        return dict(name=name, description=description, parameters={"type": "object", "properties": properties,
                    "required": required, "additionalProperties": False})
    string = {"type": "string"}
    return [
        definition("exec", "Run a shell command in the workspace. Bounded time and output.",
                   {"command": string, "timeout": {"type": "number"}}, ["command"]),
        definition("read_file", "Read a UTF-8 file within workspace.", {"path": string}, ["path"]),
        definition("write_file", "Write a UTF-8 file within workspace.", {"path": string, "text": string}, ["path", "text"]),
        definition("list_files", "List files within workspace.", {"path": string}, []),
        definition("computer", "Use Linux virtual desktop. Screenshot returns image pixels. Paused during human takeover.",
                   {"action": {"type": "string", "enum": ["screenshot", "click", "type", "key", "scroll", "open_url"]},
                    "x": {"type": "integer"}, "y": {"type": "integer"}, "text": string, "key": string,
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "amount": {"type": "integer"}, "url": string}, ["action"])
    ]


class Tools:
    def __init__(self, workspace, display=":99", desktop_dir="~/.local/share/duet/desktop"):
        self.workspace = workspace
        self.display = display
        self.desktop_dir = Path(desktop_dir).expanduser().resolve()
        self.xauthority = self.desktop_dir / "Xauthority"

    def desktop_auth_ready(self):
        paths = [self.desktop_dir, self.xauthority, self.desktop_dir / "vnc.pass", self.desktop_dir / "vnc.secret"]
        try:
            if not self.desktop_dir.is_dir() or any(not path.is_file() or path.stat().st_size == 0 for path in paths[1:]):
                return False
            if any(path.is_symlink() for path in paths):
                return False
            if os.name == "posix" and any(path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077 for path in paths):
                return False
            return self.managed_desktop_processes()
        except OSError:
            return False

    def managed_desktop_processes(self):
        if os.name != "posix":
            return False
        for filename, program, required in (
            ("xvfb.pid", "Xvfb", {"-auth": str(self.xauthority), "-nolisten": "tcp"}),
            ("vnc.pid", "x11vnc", {"-auth": str(self.xauthority), "-rfbauth": str(self.desktop_dir / "vnc.pass")})):
            try:
                pid = int((self.desktop_dir / filename).read_text().strip())
                process = Path("/proc") / str(pid)
                if pid <= 1 or process.stat().st_uid != os.getuid():
                    return False
                args = [os.fsdecode(a) for a in (process / "cmdline").read_bytes().split(b"\0") if a]
                if not args or Path(args[0]).name != program:
                    return False
                if (program == "Xvfb" and (self.display not in args or "-ac" in args)) or (program == "x11vnc" and "-localhost" not in args):
                    return False
                if any(flag not in args or args.index(flag) + 1 >= len(args) or args[args.index(flag) + 1] != value
                       for flag, value in required.items()):
                    return False
            except (OSError, ValueError):
                return False
        return True

    def desktop_status(self):
        missing = [name for name in ("xdotool", "scrot") if not shutil.which(name)]
        socket = Path("/tmp/.X11-unix") / ("X" + self.display.split(":")[-1].split(".")[0])
        auth_ready = self.desktop_auth_ready()
        ready = not missing and socket.exists() and auth_ready
        return {"ready": ready, "display": self.display, "missing": missing,
                "auth_ready": auth_ready,
                "reason": None if ready else "desktop components or X display unavailable"}

    def files(self, name="."):
        path = workspace_path(self.workspace, name)
        if not path.is_dir():
            raise ValueError("not a workspace directory")
        entries = []
        for item in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            # Do not expose symlink targets outside the workspace.
            if not item.resolve().is_relative_to(self.workspace.resolve()):
                continue
            entries.append(dict(name=item.name, path=str(item.relative_to(self.workspace)).replace("\\", "/"),
                                size=item.stat().st_size, is_dir=item.is_dir()))
            if len(entries) >= 1000:
                break
        return {"path": str(path.relative_to(self.workspace)).replace("\\", "/"), "entries": entries}

    async def execute(self, name, args):
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be an object")
        if name == "exec":
            command = args.get("command")
            if not isinstance(command, str) or not command or len(command) > 65536:
                raise ValueError("command must be nonempty text up to 65536 characters")
            timeout = float(args.get("timeout", 60))
            if not 0 < timeout <= 120:
                raise ValueError("timeout must be >0 and <=120 seconds")
            shell = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("sh")
            if not shell:
                raise RuntimeError("Linux shell is unavailable")
            return await run_process(shell, "-lc", command, cwd=self.workspace, timeout=timeout)
        if name == "list_files":
            return self.files(args.get("path", "."))
        if name in ("read_file", "write_file"):
            path = workspace_path(self.workspace, args["path"])
            if name == "read_file":
                if path.stat().st_size > OUTPUT_LIMIT:
                    raise ValueError("file exceeds read limit")
                return {"text": path.read_text(encoding="utf-8")}
            text = args.get("text")
            if not isinstance(text, str) or len(text.encode()) > 1024 * 1024:
                raise ValueError("write text must be <=1 MiB")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")
            return {"path": args["path"], "size": path.stat().st_size}
        if name == "computer":
            return await self.computer(args)
        raise ValueError("unknown tool")

    async def computer(self, args):
        if not self.desktop_status()["ready"]:
            raise RuntimeError("desktop unavailable; run bootstrap-desktop.sh and start its services")
        env = dict(os.environ, DISPLAY=self.display, XAUTHORITY=str(self.xauthority))
        action = args.get("action")
        if action == "screenshot":
            from PIL import Image
            descriptor, filename = tempfile.mkstemp(suffix=".png")
            os.close(descriptor)
            try:
                result = await run_process("scrot", "--overwrite", filename, env=env, timeout=15)
                if result["exit_code"]:
                    raise RuntimeError("desktop screenshot failed")
                with Image.open(filename) as source:
                    output = io.BytesIO()
                    source.convert("RGB").save(output, format="PNG")
                    data = output.getvalue()
                    if len(data) > 6 * 1024 * 1024:
                        raise RuntimeError("screenshot exceeds image limit")
                    return {"text": "Desktop screenshot", "width": source.width, "height": source.height,
                            "image_base64": base64.b64encode(data).decode()}
            finally:
                Path(filename).unlink(missing_ok=True)
        if action == "click":
            x, y = int(args["x"]), int(args["y"])
            if not 0 <= x <= 16384 or not 0 <= y <= 16384:
                raise ValueError("invalid desktop coordinates")
            command = ["xdotool", "mousemove", "--sync", str(x), str(y), "click", "1"]
        elif action == "type":
            text = args["text"]
            if not isinstance(text, str) or len(text) > 16384:
                raise ValueError("typed text exceeds limit")
            command = ["xdotool", "type", "--clearmodifiers", "--", text]
        elif action == "key":
            key = args["key"]
            if not isinstance(key, str) or len(key) > 100 or key.startswith("-"):
                raise ValueError("invalid key")
            command = ["xdotool", "key", "--clearmodifiers", key]
        elif action == "scroll":
            button = {"up": "4", "down": "5", "left": "6", "right": "7"}.get(args.get("direction", "down"))
            amount = int(args.get("amount", 3))
            if not button or not 1 <= amount <= 30:
                raise ValueError("invalid scroll")
            command = ["xdotool", "click", "--repeat", str(amount), button]
        elif action == "open_url":
            url = args["url"]
            if not isinstance(url, str) or not url.startswith(("https://", "http://")) or len(url) > 8192:
                raise ValueError("only HTTP(S) URLs are supported")
            command = ["xdg-open", url]
        else:
            raise ValueError("unknown computer action")
        result = await run_process(*command, timeout=20, env=env)
        if result["exit_code"]:
            raise RuntimeError("desktop action failed")
        return {"ok": True, "action": action}

"""Short-lived desktop capabilities and a device-scoped human-control lease."""
import asyncio
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web


def validate_origin(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("public_origin must be an HTTPS origin")
    url = urlsplit(value)
    if (url.scheme not in ("https", "http") or not url.hostname or url.username is not None
            or url.password is not None or url.path or url.query or url.fragment
            or any(c in value for c in ("\\", "'", '"', ";", "<", ">", "?", "#"))):
        raise ValueError("public_origin must have no credentials, path, query or fragment")
    if url.scheme == "http" and url.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("HTTP public_origin is restricted to loopback")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("invalid public_origin port")
    return url.scheme + "://" + url.netloc


class DesktopAccess:
    TICKET_SECONDS = 30
    LEASE_SECONDS = 60
    CHECK_SECONDS = 5

    def __init__(self, service, auth):
        self.service, self.auth = service, auth
        self.tickets = {}
        self.owner = None
        self.deadline = 0
        self.sockets = {}
        self.expired_owners = set()
        self.monitor = None

    def valid(self, identity):
        return self.auth.session_valid(*identity)

    def issue(self, identity):
        now = time.monotonic()
        self.tickets = {k: v for k, v in self.tickets.items() if v[1] > now}
        if len(self.tickets) >= 256:
            raise web.HTTPTooManyRequests(text="Too many pending desktop views")
        ticket = secrets.token_urlsafe(32)
        self.tickets[ticket] = (identity, now + self.TICKET_SECONDS)
        return ticket

    def consume(self, ticket):
        item = self.tickets.pop(ticket, None)
        if item is None or item[1] <= time.monotonic() or not self.valid(item[0]):
            return None
        return item[0]

    def active(self):
        if self.owner and (self.deadline <= time.monotonic() or not self.valid(self.owner)):
            self.expired_owners.add(self.owner)
            self.release()
        return self.owner is not None

    def release(self):
        if self.owner is not None:
            self.owner = None
            self.deadline = 0
            self.service.emit(None, "desktop_takeover_changed", {"active": False})

    async def takeover(self, identity, active):
        async with self.service.desktop_lock:
            self.active()
            if self.owner is not None and self.owner != identity:
                raise web.HTTPConflict(text="Desktop is controlled by another device")
            if active:
                if identity not in self.sockets.values():
                    raise web.HTTPConflict(text="Connect the desktop view before taking control")
                self.owner = identity
                self.deadline = time.monotonic() + self.LEASE_SECONDS
                self.service.emit(None, "desktop_takeover_changed", {"active": True})
            else:
                self.release()
        return {"active": active, "lease_seconds": self.LEASE_SECONDS if active else 0}

    def disconnected(self, socket):
        identity = self.sockets.pop(socket, None)
        if identity == self.owner and identity not in self.sockets.values():
            self.release()

    async def start(self, app):
        self.monitor = asyncio.create_task(self.watch())

    async def watch(self):
        while True:
            await asyncio.sleep(self.CHECK_SECONDS)
            self.active()
            expired, self.expired_owners = self.expired_owners, set()
            closing = []
            for socket, identity in list(self.sockets.items()):
                if not self.valid(identity) or identity in expired:
                    closing.append(socket.close(code=1008, message=b"Desktop authorization expired"))
            if closing:
                await asyncio.gather(*closing, return_exceptions=True)

    async def close(self, app):
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        await asyncio.gather(*(s.close(code=1001) for s in list(self.sockets)), return_exceptions=True)
        self.tickets.clear()

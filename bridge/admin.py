"""Public static login shell; all model/account data remains behind outer auth."""
from pathlib import Path

from aiohttp import web

_ASSETS = Path(__file__).with_name("admin_assets")
ADMIN_PUBLIC_PATHS = frozenset(("/duet/admin", "/duet/admin/",
                              "/duet/admin/assets/admin.css", "/duet/admin/assets/admin.js"))
_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'",
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
}


def is_admin_request(request):
    return request.method in ("GET", "HEAD") and request.path in ADMIN_PUBLIC_PATHS


def register_admin(app):
    """The host auth middleware must allow only is_admin_request() publicly."""
    async def index(request):
        return web.FileResponse(_ASSETS / "index.html", headers={**_HEADERS, "Content-Type": "text/html; charset=utf-8"})

    def asset(filename):
        async def serve(request):
            mime = "text/javascript" if filename.endswith(".js") else "text/css"
            return web.FileResponse(_ASSETS / filename, headers={**_HEADERS, "Content-Type": mime + "; charset=utf-8"})
        return serve

    app.router.add_get("/duet/admin", index)
    app.router.add_get("/duet/admin/", index)
    app.router.add_get("/duet/admin/assets/admin.css", asset("admin.css"))
    app.router.add_get("/duet/admin/assets/admin.js", asset("admin.js"))

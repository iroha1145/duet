"""Authenticated noVNC shell using its documented public RFB API."""
import json
import re
import secrets

from aiohttp import web
from .desktop_access import validate_origin


def desktop_view(request, token, password, *, public_origin=None, ticket=False):
    # The service is consumed over loopback SSH forwarding. Keep connect-src an
    # exact origin instead of allowing arbitrary ws:/wss: destinations.
    if public_origin is None and not re.fullmatch(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?", request.host):
        raise web.HTTPBadRequest(text="Loopback desktop origin required")
    origin = validate_origin(public_origin or (request.scheme + "://" + request.host))
    view_only = True if ticket else request.query.get("view_only", "true").lower() not in ("0", "false", "no")
    config = json.dumps({"credential": token, "parameter": "ticket" if ticket else "token", "password": password, "viewOnly": view_only}, ensure_ascii=True)
    config = config.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    nonce = secrets.token_urlsafe(24)
    ws_origin = ("wss" if origin.startswith("https:") else "ws") + origin[origin.index(":"):]
    csp = ("default-src 'none'; script-src 'self' 'nonce-" + nonce + "'; "
           "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
           "connect-src 'self' " + ws_origin + "; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")
    html = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>duet desktop</title><style>
html,body,#screen{margin:0;width:100%;height:100%;overflow:hidden;background:#141413}
#status{position:absolute;inset:0;display:grid;place-items:center;color:#faf9f5;font:16px system-ui;pointer-events:none}
#status[hidden]{display:none}
</style></head><body><main id="screen" aria-label="Remote desktop"></main>
<div id="status" role="status">Connecting to desktop…</div>
<script type="module" nonce="NONCE">
import RFB from '/desktop/novnc/core/rfb.js';
const config = CONFIG;
const status = document.getElementById('status');
const url = new URL('/desktop/vnc', window.location.href);
url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
url.searchParams.set(config.parameter, config.credential);
const rfb = new RFB(document.getElementById('screen'), url.href,
                    {credentials: {password: config.password}, shared: true, wsProtocols: ['binary']});
rfb.scaleViewport = true;
rfb.resizeSession = !config.viewOnly;
rfb.viewOnly = config.viewOnly;
window.duetSetViewOnly = (value) => {
    rfb.viewOnly = Boolean(value);
    rfb.resizeSession = !rfb.viewOnly;
};
window.duetSendText = (value) => {
    if (rfb.viewOnly || typeof value !== 'string' || value.length > 10000) return;
    for (const character of value) {
        const point = character.codePointAt(0);
        const symbol = point === 10 ? 0xff0d : point === 9 ? 0xff09 : point > 255 ? 0x01000000 | point : point;
        rfb.sendKey(symbol);
    }
};
window.duetDesktopConnected = false;
rfb.addEventListener('connect', () => { window.duetDesktopConnected = true; status.hidden = true; });
rfb.addEventListener('disconnect', () => { window.duetDesktopConnected = false; status.hidden = false; status.textContent = 'Desktop disconnected'; });
rfb.addEventListener('securityfailure', () => { status.hidden = false; status.textContent = 'Desktop authentication failed'; });
window.addEventListener('pagehide', () => rfb.disconnect(), {once: true});
</script></body></html>'''.replace("NONCE", nonce).replace("CONFIG", config)
    return web.Response(text=html, content_type="text/html", headers={
        "Content-Security-Policy": csp, "Cache-Control": "no-store", "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY", "Cross-Origin-Resource-Policy": "same-origin"})

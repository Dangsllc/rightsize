"""The Workbench can spend money and show held-out items, so it trusts nothing about a request.

* It binds to loopback only (`rightsize ui` refuses any other host).
* Every request must name a loopback Host, so a web page can't reach it by DNS rebinding.
* Every request must carry the session token: the URL printed at startup holds it once, after
  which it lives in an HttpOnly, SameSite=Strict cookie that other sites can't send.
* State-changing requests must also come from this origin (Origin or Referer).
"""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

COOKIE = "rightsize_session"
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _host_only(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[: host.index("]") + 1] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class SessionGuard(BaseHTTPMiddleware):
    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self.token = token

    def _ok(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value, self.token)

    async def dispatch(self, request: Request, call_next) -> Response:
        if _host_only(request.headers.get("host", "")) not in LOOPBACK:
            return PlainTextResponse("The Workbench only answers on 127.0.0.1.", status_code=421)
        if self._ok(request.query_params.get("token")) and request.method == "GET":
            # Trade the URL token for a cookie, and drop it from the address bar.
            target = request.url.remove_query_params("token")
            resp = RedirectResponse(str(target.path) + (f"?{target.query}" if target.query else ""), status_code=303)
            resp.set_cookie(COOKIE, self.token, httponly=True, samesite="strict", path="/")
            return resp
        if not self._ok(request.cookies.get(COOKIE)):
            return PlainTextResponse(
                "Open the Workbench with the tokenized URL that `rightsize ui` printed in your terminal.",
                status_code=401)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if not origin or _host_only(urlsplit(origin).netloc) not in LOOPBACK \
                    or urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
                return PlainTextResponse("Cross-origin requests are refused.", status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return resp

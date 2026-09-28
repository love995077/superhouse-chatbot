"""
HTTP-layer protections for the Superhouse Chatbot.

- SessionStore        server-side conversation context (incl. the SQL of earlier turns) keyed by an opaque,
                      server-issued session id. The browser only ever holds the id in an HttpOnly cookie.
- rate limiting       sliding windows per session and globally (behind the Caddy layer-4 proxy every request
                      arrives from 127.0.0.1, so the client IP cannot be used as a key).
- SecurityHeaders     CSP, clickjacking, MIME-sniffing, referrer and isolation headers on every response.
- BodySizeLimit       rejects oversized request bodies before they are read into memory.

State is in-process memory: run a single uvicorn worker (the Docker CMD does).
"""
import json
import re
import secrets
import threading
import time
from collections import OrderedDict, deque

_SID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")          # secrets.token_urlsafe(32)


class SessionStore:
    """Thread-safe, bounded (LRU + idle TTL) store of per-session history and request timestamps."""

    def __init__(self, max_sessions=5000, ttl_seconds=2 * 3600, max_turns=12,
                 per_session_limit=20, global_limit=120, window_seconds=60):
        self.max_sessions, self.ttl, self.max_turns = max_sessions, ttl_seconds, max_turns
        self.per_session_limit, self.global_limit, self.window = per_session_limit, global_limit, window_seconds
        self._sessions = OrderedDict()                  # sid -> {"history": deque, "hits": deque, "seen": float}
        self._global_hits = deque()
        self._lock = threading.Lock()

    def _new(self, now):
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = {"history": deque(maxlen=self.max_turns), "hits": deque(), "seen": now}
        while len(self._sessions) > self.max_sessions:   # evict least-recently-used
            self._sessions.popitem(last=False)
        return sid

    def resolve(self, sid):
        """Return a valid existing session id, or a fresh server-generated one (never trusts a client-chosen id)."""
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            if sid and _SID_RE.match(sid) and sid in self._sessions:
                self._sessions.move_to_end(sid)
                self._sessions[sid]["seen"] = now
                return sid
            return self._new(now)

    def _expire(self, now):
        while self._sessions:
            sid, s = next(iter(self._sessions.items()))
            if now - s["seen"] <= self.ttl:
                break
            self._sessions.popitem(last=False)

    def allow(self, sid):
        """Sliding-window rate limit. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            for q in (self._global_hits, self._sessions.get(sid, {}).get("hits", deque())):
                while q and now - q[0] > self.window:
                    q.popleft()
            s = self._sessions.get(sid)
            if s is None:
                return False, 1
            if len(s["hits"]) >= self.per_session_limit:
                return False, max(1, int(self.window - (now - s["hits"][0])) + 1)
            if len(self._global_hits) >= self.global_limit:
                return False, max(1, int(self.window - (now - self._global_hits[0])) + 1)
            s["hits"].append(now)
            self._global_hits.append(now)
            return True, 0

    def history(self, sid):
        with self._lock:
            s = self._sessions.get(sid)
            return [dict(t) for t in s["history"]] if s else []

    def append(self, sid, *turns):
        with self._lock:
            s = self._sessions.get(sid)
            if s is not None:
                s["history"].extend(dict(t) for t in turns)

    def drop(self, sid):
        with self._lock:
            self._sessions.pop(sid, None)

    def __len__(self):
        with self._lock:
            return len(self._sessions)


# ---------------------------------------------------------------------------
# ASGI middlewares
# ---------------------------------------------------------------------------
BASE_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"strict-transport-security", b"max-age=31536000"),
]
DEFAULT_CSP = b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class SecurityHeadersMiddleware:
    """Adds security headers to every HTTP response. A route may set its own CSP (the page does, with a nonce)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server"]
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in BASE_HEADERS if k not in present]
                if b"content-security-policy" not in present:
                    headers.append((b"content-security-policy", DEFAULT_CSP))
                if path.startswith("/api/") or path == "/health":
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """Rejects request bodies larger than max_bytes (by Content-Length, and while streaming) with a generic 413."""

    def __init__(self, app, max_bytes=16 * 1024):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            return await _reject(send)
        body, more = b"", True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.max_bytes:
                return await _reject(send)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _reject(send):
    payload = json.dumps({"type": "error", "html": "That request is too large.", "columns": [], "rows": [],
                          "row_count": 0, "truncated": False, "title": None, "chart": None}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]})
    await send({"type": "http.response.body", "body": payload})

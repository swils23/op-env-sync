"""A stand-in for the Coolify v4 API endpoints op-env-sync calls, run as a local HTTP server.

It copies the real server's quirks that matter here (checked against Coolify 4.4.6's
ApplicationsController and EnvironmentVariable model): a new app variable gets a
preview-deployment copy, values are only returned to tokens with `read:sensitive`
and never for "shown once" variables, and an update resets is_literal / is_multiline /
is_shown_once when the request leaves them out.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ALL = {"read", "write", "read:sensitive", "deploy"}


def env(key: str, value: str, **flags: bool) -> dict:
    return {
        "uuid": uuid.uuid4().hex[:24],
        "key": key,
        "value": value,
        "real_value": value,
        "is_preview": False,
        "is_literal": False,
        "is_multiline": False,
        "is_shown_once": False,
        "is_runtime": True,
        "is_buildtime": True,
        **flags,
    }


class FakeCoolify:
    def __init__(self):
        self.tokens: dict[str, set[str]] = {"good-token": set(ALL)}
        self.apps: dict[str, list[dict]] = {}
        self.deploys: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.raw: tuple[int, dict[str, str], bytes] | None = None  # answer every request with this instead
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def get(self, app: str, key: str) -> dict | None:
        return next((e for e in self.apps[app] if e["key"] == key and not e["is_preview"]), None)

    def values(self, app: str) -> dict[str, str]:
        return {e["key"]: e["value"] for e in self.apps[app] if not e["is_preview"]}

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def reply(self, code: int, body: object = None) -> None:
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def handle_any(self, method: str) -> None:
                url = urlparse(self.path)
                fake.requests.append((method, url.path))
                if fake.raw:
                    code, headers, data = fake.raw
                    self.send_response(code)
                    for name, value in {**headers, "Content-Length": str(len(data))}.items():
                        self.send_header(name, value)
                    self.end_headers()
                    return self.wfile.write(data)
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                abilities = fake.tokens.get(token)
                if abilities is None:
                    return self.reply(401, {"message": "Unauthenticated."})
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                needs = "read" if method == "GET" else ("deploy" if url.path.endswith("/deploy") else "write")
                if needs not in abilities:
                    return self.reply(403, {"message": "You are not allowed to perform this action."})

                if url.path == "/api/v1/deploy" and method == "POST":
                    uuids = parse_qs(url.query)["uuid"][0].split(",")
                    fake.deploys += uuids
                    return self.reply(200, {"deployments": [{"resource_uuid": u} for u in uuids]})

                m = re.fullmatch(r"/api/v1/applications/([^/]+)/envs(?:/([^/]+))?", url.path)
                if not m or m.group(1) not in fake.apps:
                    return self.reply(404, {"message": "Application not found"})
                envs = fake.apps[m.group(1)]

                if method == "GET":
                    out = []
                    for e in envs:
                        e = dict(e)
                        if "read:sensitive" not in abilities or e["is_shown_once"]:
                            e.pop("value")
                            e.pop("real_value")
                        out.append(e)
                    return self.reply(200, out)
                if method == "POST":
                    if any(e["key"] == body["key"] and not e["is_preview"] for e in envs):
                        return self.reply(409, {"message": "Environment variable already exists."})
                    flags = {k: v for k, v in body.items() if k.startswith("is_")}
                    new = env(body["key"], body["value"], **flags)
                    envs.append(new)
                    # Coolify's EnvironmentVariable::created hook: a new app variable gets a preview copy.
                    if not new["is_preview"] and not any(e["key"] == new["key"] and e["is_preview"] for e in envs):
                        envs.append(env(new["key"], new["value"], **{**flags, "is_preview": True}))
                    return self.reply(201, {"uuid": new["uuid"]})
                if method == "PATCH":
                    e = next((e for e in envs if e["key"] == body["key"] and not e["is_preview"]), None)
                    if e is None:
                        return self.reply(404, {"message": "Environment variable not found."})
                    e["value"] = e["real_value"] = body["value"]
                    # The real controller's behaviour: these three fall back to false/null when omitted.
                    e["is_literal"] = body.get("is_literal", False)
                    e["is_multiline"] = body.get("is_multiline")
                    e["is_shown_once"] = body.get("is_shown_once")
                    for flag in ("is_runtime", "is_buildtime"):
                        if flag in body:
                            e[flag] = body[flag]
                    return self.reply(201, e)
                if method == "DELETE":
                    before = len(envs)
                    envs[:] = [e for e in envs if e["uuid"] != m.group(2)]
                    if len(envs) == before:
                        return self.reply(404, {"message": "Environment variable not found."})
                    return self.reply(200, {"message": "Environment variable deleted."})
                return self.reply(405, {"message": "Method not allowed"})

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

            def do_PATCH(self):
                self.handle_any("PATCH")

            def do_DELETE(self):
                self.handle_any("DELETE")

        return Handler

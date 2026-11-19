#!/usr/bin/env python3
"""Agent access demo: a local web app that calls the dev agents through the AI gateway with your two tokens.

Pick an agent, choose which token goes in which header, send, and compare: the same question with the gateway token,
the Foundry token, both, or none. A throwaway demo, not part of any pipeline.

  python3 demo/agent-access-app/app.py            then open http://127.0.0.1:8765

Needs: Python 3.10+ with PyYAML (pip install pyyaml), `az login` as the person calling, and the dev coordinates
(APIM_NAME, AGENTS_APP_ID: environment variables, else read from GitHub environment agent-dev with gh).
Your tokens come from the Azure CLI and stay in this process: the browser only sees what is inside them (audience,
your object ID, roles, expiry), never the tokens themselves. The server listens on 127.0.0.1 only.
"""
import base64
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PORT = int(os.environ.get("PORT", "8765"))


def setting(name):
    value = os.environ.get(name)
    if not value:
        r = subprocess.run(["gh", "variable", "get", name, "--env", "agent-dev"], capture_output=True, text=True, cwd=ROOT)
        value = r.stdout.strip()
    if not value:
        sys.exit(f"Set {name} (environment variable) or sign in to gh so it can be read from GitHub environment agent-dev.")
    return value


APIM, APP_ID = setting("APIM_NAME"), setting("AGENTS_APP_ID")
GATEWAY = f"https://{APIM}.azure-api.net"
SCOPES = {"gateway": f"{APP_ID}/.default", "foundry": "https://ai.azure.com/.default"}
_tokens = {}


def token(kind, refresh=False):
    """(token, error) for kind gateway or foundry, from the Azure CLI as the signed-in user; cached until near expiry."""
    t = _tokens.get(kind)
    if t and not refresh and t.get("exp", 0) - time.time() > 300:
        return t.get("value"), t.get("error")
    r = subprocess.run(["az", "account", "get-access-token", "--scope", SCOPES[kind], "-o", "json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        err = r.stderr.strip().splitlines()
        msg = next((line for line in err if "AADSTS" in line), err[0] if err else "az failed")
        _tokens[kind] = {"error": msg[:400], "exp": time.time() + 60}
        return None, _tokens[kind]["error"]
    value = json.loads(r.stdout)["accessToken"]
    _tokens[kind] = {"value": value, "exp": claims(value).get("exp", time.time() + 600)}
    return value, None


def claims(tok):
    p = tok.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


def token_info(kind, refresh=False):
    value, error = token(kind, refresh)
    if not value:
        hint = ("You hold no role on the gateway's agent app yet, so Microsoft Entra issues no gateway token. Ask an "
                "agent owner for a role (postman/grant_demo_access.sh)." if "AADSTS50105" in (error or "") else "")
        return {"ok": False, "error": error, "hint": hint}
    c = claims(value)
    return {"ok": True, "audience": c.get("aud"), "oid": c.get("oid"), "name": c.get("name") or c.get("upn") or c.get("appid"),
            "roles": c.get("roles", []), "expires_in_min": int((c.get("exp", 0) - time.time()) / 60), "tail": value[-6:],
            "scope": SCOPES[kind]}


def agents():
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "projects", "*", "agents", "*", "agent.yaml"))):
        a = yaml.safe_load(open(path)) or {}
        project = path.split(os.sep)[-4]
        p = yaml.safe_load(open(os.path.join(ROOT, "projects", project, "project.yaml"))) or {}
        kind = a.get("kind", "prompt")
        tests = yaml.safe_load(open(os.path.join(os.path.dirname(path), a.get("tests_file", "tests.yaml")))) or []
        out.append({"id": f"{project}/{a['name']}", "project": project, "name": a["name"],
                    "display": a.get("display_name", a["name"]), "kind": kind, "access": a.get("access", ["gateway"]),
                    "description": a.get("description", ""),
                    "path": (p.get("gateway") or {}).get("path", f"ai/{project}") + f"/{a['name']}/" +
                            ("endpoint/protocols/openai/responses?api-version=v1" if kind == "hosted" else "openai/v1/responses"),
                    "samples": [t["input"] for t in tests[:3] if isinstance(t, dict) and t.get("input")]})
    return out


GATEWAY_CODES = {
    "AGENT_NOT_ALLOWED": "The gateway accepted your gateway token, but it holds no role for this agent.",
    "CALLER_MISMATCH": "The two tokens belong to different identities.",
    "AGENT_PATH_NOT_ALLOWED": "That path isn't part of the agent.",
    "AI_ROUTE_SUSPENDED": "The agent (or the whole gateway) is switched off by a kill switch.",
}


def explain(status, body):
    """Where the call ended and why, in plain words."""
    err = body.get("error") if isinstance(body, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    if status == 200 and not err:
        return {"where": "agent", "title": "Answered", "text": "Every check passed and the agent answered."}
    if code in GATEWAY_CODES:
        return {"where": "gateway", "title": code, "text": GATEWAY_CODES[code]}
    if isinstance(body, dict) and body.get("statusCode") == 401:   # the gateway's token check (validate-jwt)
        return {"where": "gateway", "title": "Wrong or missing token",
                "text": "The gateway didn't accept the token for this agent: " + str(body.get("message", ""))}
    if status in (401, 403):
        return {"where": "foundry", "title": "Refused by Foundry",
                "text": "The gateway passed your Foundry token on; Foundry refused it (no Foundry role on the project?). "
                        + (str(err.get("message", ""))[:200] if isinstance(err, dict) else "")}
    if status == 429:
        return {"where": "gateway", "title": "Limit reached", "text": "A rate limit or daily quota for you was reached."}
    return {"where": "agent" if status < 500 else "upstream", "title": f"HTTP {status}",
            "text": (str(err.get("message", "")) if isinstance(err, dict) else "")[:300]}


def call(agent_id, authorization, foundry_header, text):
    agent = next((a for a in agents() if a["id"] == agent_id), None)
    if not agent:
        return {"error": f"unknown agent {agent_id}"}
    headers, sent = {"Content-Type": "application/json"}, {}
    for header, kind in (("Authorization", authorization), ("x-foundry-authorization", foundry_header)):
        if kind in SCOPES:
            value, error = token(kind)
            if not value:
                return {"status": 0, "sent": sent, "explain": {"where": "you", "title": f"No {kind} token",
                        "text": error or "Microsoft Entra issued no token."}}
            headers[header] = f"Bearer {value}"
            sent[header] = kind
    req = urllib.request.Request(f"{GATEWAY}/{agent['path']}", method="POST", headers=headers,
                                 data=json.dumps({"input": text}).encode())
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            status, raw, rh = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        status, raw, rh = e.code, e.read(), e.headers
    except Exception as e:   # network
        return {"status": 0, "sent": sent, "explain": {"where": "network", "title": "No response", "text": str(e)[:300]}}
    ms = int((time.time() - start) * 1000)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {"text": raw[:500].decode(errors="replace")}
    answer = " ".join(c.get("text", "") for item in (body.get("output") or []) if item.get("type") == "message"
                      for c in item.get("content", [])) if isinstance(body, dict) else ""
    why = explain(status, body)
    if sent.get("x-foundry-authorization") and not {"gateway", "foundry"} <= set(agent["access"]):
        why["text"] += (" Your Foundry token in x-foundry-authorization was not used: this agent doesn't read that "
                        "header, and the gateway removes it, so it never reaches Foundry.")
    return {"status": status, "ms": ms, "sent": sent, "answer": answer, "explain": why,
            "correlation_id": rh.get("x-correlation-id"), "url": f"{GATEWAY}/{agent['path']}",
            "body": body if not answer else None}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, {"gateway": GATEWAY, "app_id": APP_ID, "agents": agents(),
                                    "tokens": {k: token_info(k) for k in SCOPES}})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        if self.path == "/api/tokens/refresh":
            return self._send(200, {k: token_info(k, refresh=True) for k in SCOPES})
        if self.path == "/api/call":
            return self._send(200, call(body.get("agent"), body.get("authorization"), body.get("foundry_header"),
                                        body.get("input") or "Reply with the single word OK."))
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")


if __name__ == "__main__":
    print(f"Agent access demo on http://127.0.0.1:{PORT}  (gateway {GATEWAY}, agent app {APP_ID})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

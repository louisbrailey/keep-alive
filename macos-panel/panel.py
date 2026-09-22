#!/usr/bin/env python3
"""Keep Alive control panel — a tiny local web UI for ~/bin/keep-tabs-alive.sh.

Serves on http://127.0.0.1:47391 only. Reads/writes ~/.keep-alive/config.json (which the
daemon re-reads every cycle) and shows ~/.keep-alive/state.json plus the log tail.
"""
import json, os, subprocess, sys, tempfile, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
CONF_DIR = os.path.join(HOME, ".keep-alive")
CONFIG = os.path.join(CONF_DIR, "config.json")
STATE = os.path.join(CONF_DIR, "state.json")
LOG = os.path.join(HOME, "Library/Logs/keep-tabs-alive.log")
SCRIPT = os.path.join(HOME, "bin/keep-tabs-alive.sh")
LABEL = "com.louisbrailey.keep-tabs-alive"
PORT = 47391
HTML = os.path.join(CONF_DIR, "panel.html")


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_config(cfg):
    fd, tmp = tempfile.mkstemp(dir=CONF_DIR, prefix=".config.")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)


def service_loaded():
    return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                          capture_output=True).returncode == 0


def mask(url):
    if not url:
        return ""
    return url[:40] + "…" if len(url) > 40 else url


def log_tail(n=40):
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 20000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def send_discord(webhook, text):
    data = json.dumps({"username": "Keep Alive", "content": text}).encode()
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json",
                                                              "User-Agent": "KeepAlive/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/":
            with open(HTML, "rb") as f:
                return self._send(200, f.read(), "text/html")
        if self.path == "/api/state":
            cfg = read_json(CONFIG, {})
            pub = json.loads(json.dumps(cfg))
            d = pub.setdefault("discord", {})
            d["webhook_set"] = bool(d.get("webhook"))
            d["webhook_masked"] = mask(d.get("webhook", ""))
            d.pop("webhook", None)
            return self._send(200, {
                "config": pub,
                "state": read_json(STATE, {}),
                "service_loaded": service_loaded(),
                "log": log_tail(),
                "now": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        if self.path == "/api/apps":
            names = set()
            for d in ("/Applications", os.path.join(HOME, "Applications"), "/System/Applications"):
                try:
                    names.update(n[:-4] for n in os.listdir(d) if n.endswith(".app"))
                except Exception:
                    pass
            return self._send(200, sorted(names))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self._body()
        except Exception:
            return self._send(400, {"error": "bad json"})
        cfg = read_json(CONFIG, {})

        if self.path == "/api/config":
            # Merge editable fields; the webhook is only changed when explicitly provided.
            for k in ("enabled", "browser", "check_interval", "apps", "sites"):
                if k in body:
                    cfg[k] = body[k]
            if "discord" in body:
                d = cfg.setdefault("discord", {})
                for k in ("events", "heartbeat_hours"):
                    if k in body["discord"]:
                        d[k] = body["discord"][k]
                wh = body["discord"].get("webhook")
                if isinstance(wh, str) and wh.strip():
                    d["webhook"] = wh.strip()
            cfg["check_interval"] = max(15, int(cfg.get("check_interval", 60)))
            write_config(cfg)
            return self._send(200, {"ok": True})

        if self.path == "/api/service":
            action = body.get("action")
            uid = os.getuid()
            plist = os.path.join(HOME, "Library/LaunchAgents", LABEL + ".plist")
            if action == "restart":
                subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"], capture_output=True)
            elif action == "start":
                subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist], capture_output=True)
            elif action == "stop":
                subprocess.run(["launchctl", "bootout", f"gui/{uid}", plist], capture_output=True)
            else:
                return self._send(400, {"error": "unknown action"})
            return self._send(200, {"ok": True, "service_loaded": service_loaded()})

        if self.path == "/api/test-discord":
            wh = cfg.get("discord", {}).get("webhook")
            if not wh:
                return self._send(400, {"error": "no webhook configured"})
            try:
                st = send_discord(wh, f"🧪 Test from Keep Alive control panel on {os.uname().nodename.split('.')[0]}")
                return self._send(200, {"ok": True, "status": st})
            except Exception as e:
                return self._send(502, {"error": str(e)})

        if self.path == "/api/open-log":
            subprocess.run(["open", "-a", "Console", LOG])
            return self._send(200, {"ok": True})

        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    os.makedirs(CONF_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

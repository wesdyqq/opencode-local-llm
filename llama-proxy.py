#!/usr/bin/env python3
"""llama-proxy — on-demand llama.cpp server with idle shutdown.

Listens on LOCAL_PORT (default 8080) and, on the first request, starts
`llama-server` on UPSTREAM_PORT (default 8081) with the configured model.
After `IDLE` seconds without any active task, the server is terminated and
its memory (RAM / VRAM) is released.

Usage:
    LLAMA_MODEL=/path/to/model.gguf python3 llama-proxy.py

Environment:
    LLAMA_MODEL      path to a .gguf file (required)
    LLAMA_IDLE       idle timeout in seconds (default: 30)
    LLAMA_PORT       proxy listen port (default: 8080)
    LLAMA_UP_PORT    llama-server port (default: 8081)
    LLAMA_ALIAS      model name reported by the API (default: local)
    LLAMA_NGL        layers to offload to GPU (default: 99)
    LLAMA_CTX        context size (default: 8192)
    LLAMA_THREADS    CPU threads (default: 6)
    LLAMA_EXTRA      extra llama-server args, space separated
    LLAMA_LOG        log file (default: /tmp/llama-proxy.log)
"""
from __future__ import annotations

import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

PROXY_PORT = int(os.environ.get("LLAMA_PORT", "8080"))
UP_PORT = int(os.environ.get("LLAMA_UP_PORT", "8081"))
IDLE = int(os.environ.get("LLAMA_IDLE", "30"))
MODEL = os.environ.get("LLAMA_MODEL", "")
ALIAS = os.environ.get("LLAMA_ALIAS", "local")
NGL = os.environ.get("LLAMA_NGL", "99")
CTX = os.environ.get("LLAMA_CTX", "8192")
THREADS = os.environ.get("LLAMA_THREADS", "6")
EXTRA = os.environ.get("LLAMA_EXTRA", "").split()
LOG = os.environ.get("LLAMA_LOG", "/tmp/llama-proxy.log")

START_TIMEOUT = int(os.environ.get("LLAMA_START_TIMEOUT", "180"))
REQ_TIMEOUT = int(os.environ.get("LLAMA_REQ_TIMEOUT", "1800"))

state = {"last": time.time(), "proc": None}
lock = threading.Lock()


def up_alive() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{UP_PORT}/health", timeout=0.5) as r:
            return b'"ok"' in r.read()
    except Exception:
        return False


def up_busy() -> bool:
    """True while llama-server is processing or streaming a response.

    llama.cpp does not always reset `id_task`, so only `is_processing`
    is a reliable signal.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{UP_PORT}/slots", timeout=1.0) as r:
            slots = json.loads(r.read())
    except Exception:
        # Server too busy to answer /slots — treat as busy, never kill mid-work.
        return True
    return any(s.get("is_processing") for s in slots)


def start_up() -> bool:
    with lock:
        if up_alive():
            return True

        log = open(LOG, "ab")
        cmd = [
            "llama-server", "-m", MODEL,
            "--n-gpu-layers", NGL,
            "--host", "127.0.0.1", "--port", str(UP_PORT),
            "-c", CTX, "-np", "1", "-t", THREADS,
            "--alias", ALIAS,
        ] + EXTRA
        state["proc"] = subprocess.Popen(
            cmd,
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

        for _ in range(START_TIMEOUT):
            if up_alive():
                return True
            time.sleep(1)
        return False


def _stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        return
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


def gc_loop() -> None:
    while True:
        time.sleep(5)
        with lock:
            proc = state["proc"]
            if proc is None or proc.poll() is not None:
                state["proc"] = None
                continue
            if up_busy():
                state["last"] = time.time()
                continue
            if time.time() - state["last"] >= IDLE:
                _stop(proc)
                state["proc"] = None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _relay(self, method: str) -> None:
        with lock:
            state["last"] = time.time()

        if not start_up():
            self.send_error(503, "llama-server failed to start")
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None

        req = urllib.request.Request(
            f"http://127.0.0.1:{UP_PORT}{self.path}",
            data=body, method=method,
        )
        for header in ("Content-Type", "Accept", "Authorization"):
            if self.headers.get(header):
                req.add_header(header, self.headers[header])

        try:
            with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() in ("transfer-encoding", "content-length", "connection"):
                        continue
                    self.send_header(k, v)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            try:
                self.send_error(502, str(e))
            except Exception:
                pass

    do_GET = lambda self: self._relay("GET")       # noqa: E731
    do_POST = lambda self: self._relay("POST")     # noqa: E731
    do_DELETE = lambda self: self._relay("DELETE")  # noqa: E731


def main() -> None:
    if not MODEL:
        sys.exit("LLAMA_MODEL is not set (path to a .gguf file)")
    if not os.path.exists(MODEL):
        sys.exit(f"model not found: {MODEL}")

    threading.Thread(target=gc_loop, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), Handler)
    print(f"llama-proxy listening on 127.0.0.1:{PROXY_PORT} -> :{UP_PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

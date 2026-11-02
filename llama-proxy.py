#!/usr/bin/env python3
"""llama-proxy — on-demand llama.cpp server with idle shutdown
   + tool-call shim for models that emit calls as plain text.

Listens on LOCAL_PORT (default 8080) and, on the first request, starts
`llama-server` on UPSTREAM_PORT (default 8081). After `IDLE` seconds without
an active task the server is terminated and memory is released.

The shim: some fine-tuned models (e.g. abliterated builds) lose the ability to
emit the `<tool_call>` envelope that llama.cpp parses. They still produce the
right JSON body. The proxy detects such output and rewrites it into a proper
OpenAI `tool_calls` response, so agent frameworks work again.

Environment:
    LLAMA_MODEL      path to a .gguf file (required)
    LLAMA_IDLE       idle timeout in seconds (default: 30)
    LLAMA_PORT       proxy listen port (default: 8080)
    LLAMA_UP_PORT    llama-server port (default: 8081)
    LLAMA_ALIAS      model name reported by the API (default: local)
    LLAMA_NGL        layers to offload to GPU (default: 99)
    LLAMA_CTX        context size (default: 8192)
    LLAMA_THREADS    CPU threads (default: 6)
    LLAMA_EXTRA      extra llama-server args (space separated)
    LLAMA_LOG        log file (default: /tmp/llama-proxy.log)
    LLAMA_TOOL_SHIM  set to 0 to disable tool-call rewriting (default: 1)
"""
from __future__ import annotations

import http.server
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

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
TOOL_SHIM = os.environ.get("LLAMA_TOOL_SHIM", "1") != "0"

START_TIMEOUT = int(os.environ.get("LLAMA_START_TIMEOUT", "180"))
REQ_TIMEOUT = int(os.environ.get("LLAMA_REQ_TIMEOUT", "1800"))

state = {"last": time.time(), "proc": None}
lock = threading.Lock()


# ───────────────────────────── server lifecycle ─────────────────────────────

def up_alive() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{UP_PORT}/health", timeout=0.5) as r:
            return b'"ok"' in r.read()
    except Exception:
        return False


def up_busy() -> bool:
    """llama.cpp does not reliably reset `id_task`, so only is_processing counts."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{UP_PORT}/slots", timeout=1.0) as r:
            slots = json.loads(r.read())
    except Exception:
        return True  # too busy to answer — never kill mid-work
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
            "--alias", ALIAS, "--jinja",
        ] + EXTRA
        state["proc"] = subprocess.Popen(
            cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(START_TIMEOUT):
            if up_alive():
                return True
            time.sleep(1)
        return False


def _stop(proc) -> None:
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


# ─────────────────────────────── tool shim ─────────────────────────────────

TOOL_PREFIXES = ("```", "{", "<tool", "<function", "<tools", "[tool", "tool_call")


def _looks_like_call_start(buf: str) -> bool:
    s = buf.lstrip()
    if not s:
        return True  # still whitespace, keep waiting
    low = s.lower()
    return any(low.startswith(p) for p in TOOL_PREFIXES)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _extract_call(content: str):
    """Return (name, arguments_json_string) or None."""
    if not content:
        return None
    text = _strip_fences(content)

    # also try to drop <tool_call>/<function-call>/<tool_response> wrappers
    m = re.search(r"<(?:tool_call|function-call|tool_response|tools?|function)>\s*(.*?)\s*</(?:tool_call|function-call|tool_response|tools?|function)>",
                  text, re.S | re.I)
    if m:
        text = _strip_fences(m.group(1))

    def valid(obj):
        if not isinstance(obj, dict):
            return None
        name = obj.get("name") or obj.get("tool") or obj.get("function")
        if isinstance(name, dict):
            args = name.get("arguments", obj.get("arguments"))
            name = name.get("name")
        else:
            args = obj.get("arguments", obj.get("parameters"))
        if not isinstance(name, str) or not name:
            return None
        if isinstance(args, str):
            argstr = args
        elif args is None:
            argstr = "{}"
        else:
            argstr = json.dumps(args, ensure_ascii=False)
        return name, argstr

    try:
        r = valid(json.loads(text))
        if r:
            return r
    except Exception:
        pass

    i = text.find("{")
    while i != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[i:])
            r = valid(obj)
            if r:
                return r
        except Exception:
            pass
        i = text.find("{", i + 1)
    return None


def _tool_call_obj(name: str, args: str) -> dict:
    return {
        "id": "call_" + uuid.uuid4().hex[:24],
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def _sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


# ─────────────────────────────── HTTP layer ────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # ---------- helpers ----------
    def _send_chunk(self, data: bytes):
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _end_chunks(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _json_response(self, code: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- main relay ----------
    def _relay(self, method: str):
        with lock:
            state["last"] = time.time()

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        req_json = None
        if body:
            try:
                req_json = json.loads(body)
            except Exception:
                req_json = None

        if not start_up():
            self._json_response(503, {"error": {"message": "llama-server failed to start"}})
            return

        has_tools = bool(req_json and req_json.get("tools"))
        streaming = bool(req_json and req_json.get("stream"))
        shim = TOOL_SHIM and has_tools and self.path.endswith("/chat/completions")

        req = urllib.request.Request(
            f"http://127.0.0.1:{UP_PORT}{self.path}", data=body or None, method=method,
        )
        for h in ("Content-Type", "Accept", "Authorization"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])

        try:
            resp = urllib.request.urlopen(req, timeout=REQ_TIMEOUT)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except Exception as e:
            self._json_response(502, {"error": {"message": str(e)}})
            return

        with resp:
            if not shim:
                self._pass_through(resp)
                return
            if streaming:
                self._stream_with_shim(resp, req_json)
            else:
                self._json_with_shim(resp)

    # ---------- no shim: raw passthrough ----------
    def _pass_through(self, resp):
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
            self._send_chunk(chunk)
        self._end_chunks()

    # ---------- non-stream shim ----------
    def _json_with_shim(self, resp):
        data = json.loads(resp.read())
        try:
            msg = data["choices"][0]["message"]
        except Exception:
            msg = None
        if msg and not msg.get("tool_calls"):
            call = _extract_call(msg.get("content") or "")
            if call:
                msg["content"] = None
                msg["tool_calls"] = [_tool_call_obj(*call)]
                data["choices"][0]["finish_reason"] = "tool_calls"
        self._json_response(200, data)

    # ---------- streaming shim ----------
    def _stream_with_shim(self, resp, req_json):
        self.send_response(resp.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        buf = ""
        mode = "undecided"      # undecided | text | tool
        finished = False
        tail_finish = None

        while True:
            line = resp.readline()
            if not line:
                break
            line = line.strip()
            if not line or not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except Exception:
                continue

            choices = chunk.get("choices") or []
            if not choices:
                continue
            ch = choices[0]
            delta = ch.get("delta") or {}
            piece = delta.get("content") or ""
            if ch.get("finish_reason"):
                tail_finish = ch["finish_reason"]
                finished = True

            if piece:
                if mode == "text":
                    self._send_chunk(_sse(chunk))
                else:
                    buf += piece
                    if mode == "undecided" and (len(buf) >= 24 or "\n" in buf):
                        mode = "tool" if _looks_like_call_start(buf) else "text"
                        if mode == "text":
                            # flush what we held back
                            flushed = dict(chunk)
                            flushed["choices"] = [dict(ch, delta={"content": buf}, finish_reason=None)]
                            self._send_chunk(_sse(flushed))

            if finished:
                break

        call = _extract_call(buf) if mode in ("tool", "undecided") else None

        if call:
            base = {
                "id": "chatcmpl-" + uuid.uuid4().hex[:12],
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": req_json.get("model", ALIAS) if req_json else ALIAS,
            }
            open_chunk = dict(base)
            open_chunk["choices"] = [{
                "index": 0,
                "delta": {"role": "assistant", "tool_calls": [{
                    "index": 0,
                    "id": "call_" + uuid.uuid4().hex[:24],
                    "type": "function",
                    "function": {"name": call[0], "arguments": call[1]},
                }]},
                "finish_reason": None,
            }]
            self._send_chunk(_sse(open_chunk))

            fin_chunk = dict(base)
            fin_chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]
            self._send_chunk(_sse(fin_chunk))
        else:
            # no call detected — emit buffered text, then the original finish
            if buf and mode != "text":
                base = {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:12],
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": req_json.get("model", ALIAS) if req_json else ALIAS,
                }
                txt = dict(base)
                txt["choices"] = [{"index": 0, "delta": {"content": buf}, "finish_reason": None}]
                self._send_chunk(_sse(txt))
            if tail_finish:
                base = {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:12],
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": req_json.get("model", ALIAS) if req_json else ALIAS,
                }
                fin = dict(base)
                fin["choices"] = [{"index": 0, "delta": {}, "finish_reason": tail_finish}]
                self._send_chunk(_sse(fin))

        self._send_chunk(b"data: [DONE]\n\n")
        self._end_chunks()

    do_GET = lambda self: self._relay("GET")         # noqa: E731
    do_POST = lambda self: self._relay("POST")       # noqa: E731
    do_DELETE = lambda self: self._relay("DELETE")   # noqa: E731


def main() -> None:
    if not MODEL:
        sys.exit("LLAMA_MODEL is not set (path to a .gguf file)")
    if not os.path.exists(MODEL):
        sys.exit(f"model not found: {MODEL}")

    threading.Thread(target=gc_loop, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), Handler)
    print(f"llama-proxy on 127.0.0.1:{PROXY_PORT} -> :{UP_PORT} (tool shim={'on' if TOOL_SHIM else 'off'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

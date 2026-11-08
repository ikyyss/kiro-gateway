#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kiro_gateway.py — local OpenAI-compatible gateway for the Kiro/CodeWhisperer
streaming service. Forwards /v1/chat/completions to Kiro's runtime endpoint
while impersonating the official Kiro IDE client.

Python stdlib only. No dependencies.

Protocol (verified)
-------------------
Refresh:    POST https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken
            body {"refreshToken": ...} -> {"accessToken", "refreshToken",
            "profileArn", "expiresIn"}
Chat:       POST https://runtime.us-east-1.kiro.dev/generateAssistantResponse
            (AWS eventstream-encoded request/response, see encode_frame/decode_frame)
"""

import json
import os
import re
import sys
import time
import uuid
import zlib
import socket
import threading
import http.server
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "20132"))

RUNTIME_URL = "https://runtime.us-east-1.kiro.dev/generateAssistantResponse"
REFRESH_URL = "https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken"
BUILDER_PROFILE_ARN = "arn:aws:codewhisperer:us-east-1:638616132270:profile/AAAACCCCXXXX"

STATE_PATH = os.environ.get("KIRO_STATE_PATH",
                            os.path.join(os.path.expanduser("~"), ".kiro_gateway_state.json"))

USER_AGENT = "AWS-SDK-JS/3.0.0 kiro-ide/1.0.0"
X_AMZ_UA = "aws-sdk-js/3.0.0 kiro-ide/1.0.0"

_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# AWS EventStream encode/decode
# ---------------------------------------------------------------------------

def _crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def _encode_header(name, value_type, value):
    if isinstance(value, str):
        vb = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        vb = bytes(value)
    else:
        vb = str(value).encode("utf-8")
    nb = name.encode("utf-8")
    # string(7) and byteArray(6) carry a 2-byte length prefix
    if value_type in (6, 7):
        vb = (len(vb)).to_bytes(2, "big") + vb
    return (len(nb)).to_bytes(1, "big") + nb + bytes([value_type]) + vb


def encode_frame(payload: bytes, headers=None):
    """Encode an AWS eventstream message frame. headers: {name: str_value}."""
    hd = b""
    if headers:
        for k, v in headers.items():
            hd += _encode_header(k, 7, v)  # 7 = string
    prelude = (12 + len(hd) + len(payload)).to_bytes(4, "big") + \
        len(hd).to_bytes(4, "big")
    prelude_crc = _crc32(prelude).to_bytes(4, "big")
    body = prelude + prelude_crc + hd + payload
    return body + _crc32(body).to_bytes(4, "big")


def decode_frames(buf: bytes):
    """Yield (headers: dict, payload: bytes) tuples from an eventstream buffer."""
    off = 0
    n = len(buf)
    while off + 12 <= n:
        total = int.from_bytes(buf[off:off + 4], "big")
        hlen = int.from_bytes(buf[off + 4:off + 8], "big")
        prelude_crc = int.from_bytes(buf[off + 8:off + 12], "big")
        if _crc32(buf[off:off + 8]) != prelude_crc:
            break
        end = off + total
        if end + 4 > n:
            break
        crc = int.from_bytes(buf[end:end + 4], "big")
        if _crc32(buf[off:end]) != crc:
            break
        msg = buf[off + 12:end]
        headers, payload = {}, None
        ho = 0
        while ho < hlen:
            nl = msg[ho]
            ho += 1
            name = msg[ho:ho + nl].decode("utf-8", "replace")
            ho += nl
            vt = msg[ho]
            ho += 1
            if vt == 0:
                val = True
            elif vt == 1:
                val = False
            elif vt == 2:
                val = msg[ho]
                ho += 1
            elif vt == 3:
                val = int.from_bytes(msg[ho:ho + 2], "big"); ho += 2
            elif vt == 4:
                val = int.from_bytes(msg[ho:ho + 4], "big"); ho += 4
            elif vt == 5:
                val = int.from_bytes(msg[ho:ho + 8], "big", signed=True); ho += 8
            elif vt == 6:
                l = int.from_bytes(msg[ho:ho + 2], "big"); ho += 2
                val = msg[ho:ho + l]; ho += l
            elif vt == 7:
                l = int.from_bytes(msg[ho:ho + 2], "big"); ho += 2
                val = msg[ho:ho + l].decode("utf-8", "replace"); ho += l
            elif vt == 8:
                val = int.from_bytes(msg[ho:ho + 8], "big", signed=True); ho += 8
            elif vt == 9:
                val = msg[ho:ho + 16]; ho += 16
            else:
                break
            headers[name] = val
        payload = msg[hlen:]
        yield headers, payload
        off = end + 4


# ---------------------------------------------------------------------------
# State / accounts
# ---------------------------------------------------------------------------

def load_accounts():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else [data]


def save_accounts(accounts):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def refresh_account(acc):
    """Refresh a single account token; returns updated account dict."""
    body = json.dumps({"refreshToken": acc.get("refreshToken")}).encode()
    req = Request(REFRESH_URL, data=body,
                  headers={"Content-Type": "application/json",
                           "User-Agent": USER_AGENT})
    with urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    now = time.time()
    acc["accessToken"] = data["accessToken"]
    acc["refreshToken"] = data.get("refreshToken", acc.get("refreshToken"))
    if data.get("profileArn"):
        acc["profileArn"] = data["profileArn"]
    acc["expiresIn"] = data.get("expiresIn")
    acc["expiresAt"] = str(now + float(data.get("expiresIn", 3600)))
    return acc


def pick_account():
    """Round-robin account selection with token refresh when near expiry."""
    accounts = load_accounts()
    if not accounts:
        return None
    with _state_lock:
        idx = 0
        # simplest stable rotation: rotate based on time so no global counter needed
        idx = int(time.time() / 1) % len(accounts)
        acc = accounts[idx]
    # refresh if expired within 60s
    try:
        exp = float(acc.get("expiresAt", 0))
    except (TypeError, ValueError):
        exp = 0
    if time.time() + 60 > exp:
        try:
            acc = refresh_account(acc)
            accounts[idx] = acc
            save_accounts(accounts)
        except Exception as e:
            print(f"[kiro] refresh failed for {acc.get('name')}: {e}", flush=True)
    return acc


# ---------------------------------------------------------------------------
# Kiro chat
# ---------------------------------------------------------------------------

def kiro_chat(acc, message, model, max_tokens, conversation_id):
    """Call Kiro runtime; returns (full_content, stream_of_chunks)."""
    access = acc.get("accessToken")
    profile_arn = acc.get("profileArn") or BUILDER_PROFILE_ARN
    body = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": conversation_id,
            "currentMessage": {
                "userInputMessage": {
                    "content": message,
                    "modelId": model,
                    "origin": "AI_EDITOR",
                }
            },
        },
        "inferenceConfig": {"maxTokens": max_tokens},
        "profileArn": profile_arn,
    }
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    frame = encode_frame(payload, {":message-type": "event",
                                   ":content-type": "application/json"})
    req = Request(RUNTIME_URL, data=frame, headers={
        "Content-Type": "application/json",
        "Accept": "application/vnd.amazon.eventstream",
        "User-Agent": USER_AGENT,
        "X-Amz-User-Agent": X_AMZ_UA,
        "Authorization": "Bearer " + access,
    })
    with urlopen(req, timeout=300) as r:
        raw = r.read()

    content_chunks = []
    for headers, chunk in decode_frames(raw):
        ev = headers.get(":event-type", "")
        if ev == "assistantResponseEvent":
            try:
                obj = json.loads(chunk.decode("utf-8"))
            except ValueError:
                continue
            c = obj.get("content", "")
            content_chunks.append(c)
            yield c
        elif ev == "metadataEvent":
            try:
                obj = json.loads(chunk.decode("utf-8"))
            except ValueError:
                obj = {}
            if obj.get("stopReason") == "END_TURN":
                break
    return "".join(content_chunks)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def _json(data, status=200):
    b = json.dumps(data).encode("utf-8")
    return status, b, "application/json"


class GatewayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "kiro_gateway/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[kiro] %s\n" % (fmt % args))

    def _send(self, status, body, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            accounts = load_accounts()
            self._send(200, json.dumps({
                "status": "ok",
                "port": PORT,
                "accounts": len(accounts),
            }).encode(), "application/json")
        else:
            self._send(404, b"{}", "application/json")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/v1/chat/completions":
            self._send(404, b"{}", "application/json")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            self._send(400, json.dumps({"error": "invalid json"}).encode(),
                       "application/json")
            return

        acc = pick_account()
        if acc is None:
            self._send(503, json.dumps({
                "error": "no accounts", "message": "state file missing/empty",
                "state": STATE_PATH}).encode(), "application/json")
            return

        message = (data.get("messages") or [{}])[-1].get("content", "")
        model = data.get("model", "default")
        max_tokens = int(data.get("max_tokens") or data.get("maxTokens") or 4096)
        stream = bool(data.get("stream", False))
        conversation_id = str(uuid.uuid4())

        try:
            chunks = kiro_chat(acc, message, model, max_tokens, conversation_id)
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                created = int(time.time())
                for c in chunks:
                    if not c:
                        continue
                    piece = json.dumps({"id": conversation_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "choices": [{"index": 0,
                                                     "delta": {"content": c},
                                                     "finish_reason": None}]},
                                       ensure_ascii=False)
                    self.wfile.write(f"data: {piece}\n\n".encode("utf-8"))
                    self.wfile.flush()
                done = json.dumps({"id": conversation_id,
                                   "object": "chat.completion.chunk",
                                   "created": created, "model": model,
                                   "choices": [{"index": 0, "delta": {},
                                                "finish_reason": "stop"}]})
                self.wfile.write(f"data: {done}\n\ndata: [DONE]\n\n".encode("utf-8"))
                self.wfile.flush()
                return
            content = "".join(list(chunks))
            self._send(200, json.dumps({
                "id": conversation_id, "object": "chat.completion",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                          "total_tokens": 0}},
                ensure_ascii=False).encode(), "application/json")
        except HTTPError as e:
            msg = e.read().decode("utf-8", "replace") if e.fp else str(e)
            self._send(e.code, json.dumps({"error": str(e), "detail": msg[:500]})
                       .encode(), "application/json")
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}).encode(),
                       "application/json")


def main():
    print(f"[kiro] gateway listening on http://{HOST}:{PORT}", flush=True)
    print(f"[kiro] state file: {STATE_PATH}", flush=True)
    srv = http.server.ThreadingHTTPServer((HOST, PORT), GatewayHandler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()

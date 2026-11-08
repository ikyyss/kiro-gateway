# Kiro Gateway (`kiro_gateway.py`)

Local OpenAI-compatible gateway that forwards chat to the Kiro / CodeWhisperer
streaming service while impersonating the official Kiro IDE client. Pure Python
stdlib — zero dependencies.

## What it is

Kiro ships a hosted AI chat (CodeWhisperer under the hood). The official Kiro
IDE talks to Amazon's runtime over an AWS EventStream protocol. This gateway
replays that protocol so **any** OpenAI-compatible client (curl, scripts, a
router like 9Router) can use Kiro's models through a local HTTP endpoint.

- Exposes `POST /v1/chat/completions` (OpenAI format, streaming + non-stream).
- Exposes `GET /health`.
- Reads Kiro accounts from a local snapshot file (more below).
- Rotates across accounts round-robin; refreshes tokens automatically.

## Files

| File | Purpose |
|---|---|
| `kiro_gateway.py` | The gateway. Python stdlib only. |
| `kiro_supervisor.bat` | Windows auto-restart supervisor (optional). |
| `SKILL.md` | Protocol notes / gotchas. |
| `README.md` | This install guide. |

## Requirements

- Python 3.8+ (stdlib only; no pip packages).
- A Kiro account, logged in at least once in the Kiro desktop app (see
  **Getting accounts** below).
- Windows or POSIX (tested on Windows; the server is stdlib `http.server`).

## Install & run

```bash
# ganti <you> dengan username GitHub kamu (clone repo ini: ikyyss)
git clone https://github.com/<you>/kiro-gateway.git
cd kiro-gateway
python kiro_gateway.py
```

Default port **20132** (override with env `PORT`).

### Getting accounts

The gateway needs Kiro accounts to talk to the runtime. The snapshot file is
**your** credentials — it is never committed and lives **outside** the repo:

- Default path: `~/.kiro_gateway_state.json`
- Override with env `KIRO_STATE_PATH`.

To seed it: log into each Kiro account once in the Kiro desktop app, then copy
the resulting token snapshot to the state path. The gateway watches the file
and auto-rotates accounts. If the file is missing/empty, the gateway serves
`503 no accounts` on chat requests but stays up (health still works).

> **Security:** the snapshot contains refresh tokens. Keep it private. It is
> gitignored and outside the repo by default.

## Endpoints

### Health

```bash
curl http://127.0.0.1:20132/health
# -> {"status":"ok","port":20132,"accounts":1}
```

### Chat (non-stream)

```bash
curl http://127.0.0.1:20132/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-4.5","messages":[{"role":"user","content":"hi"}]}'
```

### Chat (stream, SSE)

```bash
curl -N http://127.0.0.1:20132/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"stream":true,"model":"claude-sonnet-4.5","messages":[{"role":"user","content":"hi"}]}'
```

`model` is passed through to Kiro as `modelId`. `max_tokens`/`maxTokens`
controls the inference budget (default 4096).

## Integration with 9Router

Register the gateway as a custom OpenAI-compatible node:

1. In 9Router, add a custom node of type **openai-compatible-chat** pointing at
   `http://127.0.0.1:20132`. Set an `apiKey` field to any value.
   - **Note:** the `provider` must be the **node id** (e.g.
     `openai-compatible-chat-<uuid>`), not a friendly name.
2. Configure a prefix (e.g. `ki`) and models as `ki/<model>`
   (e.g. `ki/claude-sonnet-4.5`).
3. Keep the gateway running for account sourcing; it also refreshes tokens so
   the 9Router connections stay fresh.
4. If 9Router restarts and loses its node/connection rows, re-register the node.

## Auto-restart supervisor (Windows)

Start `kiro_supervisor.bat` (e.g. via Task Scheduler at startup, or an HKCU
`Run` value). It launches the gateway and restarts it on crash, logging to
`%TEMP%\kiro_gateway.log`.

## Configuration summary

| Env var | Default | Purpose |
|---|---|---|
| `PORT` | `20132` | Listen port |
| `KIRO_STATE_PATH` | `~/.kiro_gateway_state.json` | Account snapshot (private) |

## Troubleshooting

- **`503 no accounts`** — state file missing/empty. Log into Kiro to seed it.
- **`400 access_denied` / `403 invalid` on refresh** — token banned/expired;
  re-login that account in Kiro.
- **Stream hangs after `[DONE]`** — `curl -N` never exits on SSE close; use
  `--max-time` or grep for `[DONE]`.
- **Windows `/tmp/` paths fail** — use absolute `C:\...` paths for the state
  file/native tools.
- **Health works, chat fails** — check account tokens are fresh; gateway logs
  refresh failures to stderr.

## Protocol (verified)

1. **Refresh**: `POST https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken`
   body `{"refreshToken"}` → `{accessToken, refreshToken, profileArn, expiresIn}`.
2. **Chat**: `POST https://runtime.us-east-1.kiro.dev/generateAssistantResponse`
   headers: `Content-Type: application/json`, `Accept: application/vnd.amazon.eventstream`,
   `User-Agent: AWS-SDK-JS/3.0.0 kiro-ide/1.0.0`,
   `X-Amz-User-Agent: aws-sdk-js/3.0.0 kiro-ide/1.0.0`,
   `Authorization: Bearer <accessToken>`. Body JSON:
   `{conversationState:{chatTriggerType:"MANUAL", conversationId,
   currentMessage:{userInputMessage:{content, modelId, origin:"AI_EDITOR"}}},
   inferenceConfig:{maxTokens}, profileArn}`.
3. **Response** is AWS EventStream binary frames (`[total_len][headers_len][prelude_crc]`
   headers `[message_crc]`). Header `:event-type`: `assistantResponseEvent`
   (payload `{content, modelId}`), `metadataEvent` (`{stopReason:"END_TURN"}`),
   `contextUsageEvent`, `meteringEvent`.

## License

Private / as-is. No warranty.

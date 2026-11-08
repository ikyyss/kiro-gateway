---
name: kiro-gateway
description: Use when running the Kiro CodeWhisperer local gateway.
---

# Kiro Gateway (kiro_gateway.py)

Local OpenAI-compatible gateway that forwards chat to the Kiro/CodeWhisperer streaming service while impersonating the official Kiro IDE client. Built by reverse-engineering 9Router's kiro transport + Kiro extension `kiro.kiro-agent` (v1.4.62).

## Files
- `kiro_gateway.py` — gateway (Python stdlib, no deps)
- `kiro_supervisor.bat` — auto-start supervisor (restarts on crash)
- `README.md` — install & usage guide
- `SKILL.md` — this document

## Protocol (verified)
1. **Refresh**: `POST https://oidc.us-east-1.amazonaws.com/token` JSON `{clientId, clientSecret, refreshToken, grantType:"refresh_token"}` → `accessToken` (valid 3600s).
2. **Chat**: `POST https://runtime.us-east-1.kiro.dev/generateAssistantResponse` headers: `Content-Type: application/json`, `Accept: application/vnd.amazon.eventstream`, `User-Agent: AWS-SDK-JS/3.0.0 kiro-ide/1.0.0`, `X-Amz-User-Agent: aws-sdk-js/3.0.0 kiro-ide/1.0.0`, `Authorization: Bearer <accessToken>`. Body: `{conversationState:{chatTriggerType:"MANUAL", conversationId, currentMessage:{userInputMessage:{content, modelId, origin:"AI_EDITOR"}}}, inferenceConfig:{maxTokens}, profileArn}`.
3. **profileArn** (builder-id) = constant `arn:aws:codewhisperer:us-east-1:638616132270:profile/AAAACCCCXXXX`.
4. **Response**: AWS eventstream binary frames. Frame = `[total_len u32][headers_len u32][prelude_crc u32]` headers payload `[message_crc u32]`. Header `:event-type` values: `assistantResponseEvent` (payload JSON `{content, modelId}`), `metadataEvent` (`{stopReason:"END_TURN"}`), `contextUsageEvent`, `meteringEvent`.

## Accounts
- Source: snapshot multi-akun `scratch/kiro_gateway_state.json` (list), seeded from `~/.aws/sso/cache/kiro-auth-token.json`.
- **Multi-account:** login an akun in kiro.exe (profileArn beda) → gateway otomatis tambah ke snapshot. Round-robin across all snapshot accounts. To add accounts, just log into each in kiro.exe once; no config.
- Refreshes via `POST https://prod.us-east-1.auth.desktop.kiro.dev/refreshToken` body `{refreshToken}` → `{accessToken, refreshToken, profileArn, expiresIn}`. Uses its OWN `profileArn` (NOT the builder-id constant). Snapshot updated on each refresh.
- Backward-compat: old single-object state auto-migrates to list on load (`_load_state` normalizes name/social).
- Banned AWS/builder-id symptoms: refresh → `400 access_denied`, device flow → `authorization_pending` forever, old accessToken → `403 invalid`. Irrelevant now (single kiro-exe source).
- Per-request account chosen is logged to `scratch/kiro_account_used.log`.

## Run / Test
- Run: `python kiro_gateway.py` (port 20132, env `PORT` overrides).
- Health: `GET http://127.0.0.1:20132/health` → `{status, accounts}`.
- Direct: `POST :20132/v1/chat/completions` (stream + non-stream).
- Via 9Router: model `ki/<model>` (e.g. `ki/claude-sonnet-4.5`). Node `kiro-gateway` prefix `ki`; connection id `c2d9474b-f29c-4f8c-abe6-b5ecb1a51add`.

## Gotchas
- `curl -N` with SSE never exits after `[DONE]` → use `--max-time` or grep.
- `/tmp/` paths fail in Windows Python — use absolute `C:\...\scratch\` paths.
- 9Router custom node connection: `provider` = **node id** (`openai-compatible-chat-<uuid>`), not a friendly name; require `apiKey` field (any value).
- Re-register node if 9Router restarts and loses node/connection rows.
- Gateway lifetime: 9Router must be running for account sourcing; also refreshes tokens so the 9Router kiro connections stay fresh.

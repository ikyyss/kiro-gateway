#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seed_state.py — build ~/.kiro_gateway_state.json from local Kiro login tokens.

The Kiro desktop app stores its auth snapshot at
~/.aws/sso/cache/kiro-auth-token.json (one file per login). This script copies
that into the gateway's state file so the gateway can use the account.

Usage:
    python seed_state.py            # default source + state paths
    python seed_state.py --state PATH  # custom state file

Idempotent: won't duplicate an account whose refreshToken already exists.
"""

import json
import os
import sys

DEFAULT_SOURCE = os.path.join(os.path.expanduser("~"),
                              ".aws", "sso", "cache", "kiro-auth-token.json")
DEFAULT_STATE = os.path.join(os.path.expanduser("~"),
                             ".kiro_gateway_state.json")


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else [data]


def main():
    source = os.environ.get("KIRO_SOURCE", DEFAULT_SOURCE)
    state = os.environ.get("KIRO_STATE_PATH", DEFAULT_STATE)

    try:
        with open(source, "r", encoding="utf-8") as f:
            src = json.load(f)
    except (OSError, ValueError) as e:
        print(f"cannot read source {source}: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(src, dict):
        print(f"source not a single-account dict: {source}", file=sys.stderr)
        sys.exit(1)

    accounts = load_state(state)
    existing = {a.get("refreshToken") for a in accounts if a.get("refreshToken")}
    if src.get("refreshToken") in existing:
        print("already in state, nothing to do")
        return

    accounts.append({
        "name": src.get("provider", "kiro"),
        "accessToken": src.get("accessToken"),
        "refreshToken": src.get("refreshToken"),
        "expiresAt": src.get("expiresAt", "0"),
        "profileArn": src.get("profileArn"),
        "clientId": None,
        "clientSecret": None,
        "authMethod": src.get("authMethod", "sso"),
        "social": False,
    })
    tmp = state + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state)
    print(f"seeded {len(accounts)} account(s) -> {state}")


if __name__ == "__main__":
    main()

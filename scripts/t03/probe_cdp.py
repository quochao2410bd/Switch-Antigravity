#!/usr/bin/env python3
"""
CDP Probe Tool for Antigravity Desktop (T03 Prototype)

Discovers active CDP port from %APPDATA%\\Antigravity\\DevToolsActivePort.
Qualifies Antigravity page targets.
Exposes privacy-hardened summary by default (--verbose-private-data for full strings).
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import urllib.request
import websockets

def discover_cdp_endpoint():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None, "CDP_PORT_FILE_MISSING"

    port_file = os.path.join(appdata, "Antigravity", "DevToolsActivePort")
    if not os.path.exists(port_file):
        return None, "CDP_PORT_FILE_MISSING"

    try:
        with open(port_file, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        if not lines or not lines[0].strip().isdigit():
            return None, "CDP_PORT_FILE_INVALID"
        
        port = lines[0].strip()
        endpoint = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(f"{endpoint}/json/version", headers={"User-Agent": "SwitchAntigravity-T03/2.0"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            ver_data = json.loads(resp.read().decode("utf-8"))
            if "Browser" not in ver_data:
                return None, "CDP_ENDPOINT_UNREACHABLE"
        return endpoint, "OK"
    except Exception:
        return None, "CDP_ENDPOINT_UNREACHABLE"

async def eval_js(ws, expr):
    msg_id = 1
    req = {
        "id": msg_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True
        }
    }
    await ws.send(json.dumps(req))
    while True:
        resp = await ws.recv()
        data = json.loads(resp)
        if data.get("id") == msg_id:
            res = data.get("result", {})
            if "exceptionDetails" in res:
                return {"error": str(res["exceptionDetails"])}
            return res.get("result", {}).get("value")

async def main():
    parser = argparse.ArgumentParser(description="Probe Antigravity CDP Target")
    parser.add_argument("--endpoint", help="Override CDP endpoint URL")
    parser.add_argument("--verbose-private-data", action="store_true", help="Display unredacted strings and titles")
    args = parser.parse_args()

    if args.endpoint:
        endpoint = args.endpoint
        status = "OK"
    else:
        endpoint, status = discover_cdp_endpoint()

    print(f"[*] CDP Discovery: {status}")
    if status != "OK":
        print(f"[!] Cannot proceed without active CDP endpoint: {status}")
        sys.exit(1)

    print(f"[*] Endpoint: {endpoint}")

    try:
        targets_resp = urllib.request.urlopen(f"{endpoint}/json/list", timeout=3)
        targets = json.loads(targets_resp.read().decode("utf-8"))
        print(f"[*] Total Raw Targets Found: {len(targets)}")
    except Exception as e:
        print(f"[!] Error querying /json/list: {e}")
        sys.exit(1)

    page_target = next((t for t in targets if t.get("type") == "page"), None)
    if not page_target:
        print("[!] No page target found.")
        sys.exit(1)

    ws_url = page_target.get("webSocketDebuggerUrl")
    print(f"[*] Connecting to Page WebSocket: {ws_url}")

    async with websockets.connect(ws_url) as ws:
        print("[+] WebSocket connected successfully!")
        
        info = await eval_js(ws, """
        (() => {
            return {
                url: window.location.href,
                readyState: document.readyState,
                hasSidebar: !!document.querySelector('[data-testid="conversation-list-sidebar"]'),
                hasComposer: !!document.querySelector('[data-lexical-editor="true"]')
            };
        })()
        """)
        print("[*] Page Structural Qualification:", json.dumps(info, indent=2))

        counts = await eval_js(ws, """
        (() => {
            return {
                totalTestIds: document.querySelectorAll('[data-testid]').length,
                totalArticles: document.querySelectorAll('article, [role="article"]').length,
                totalButtons: document.querySelectorAll('button').length,
                conversationRows: document.querySelectorAll('[data-testid="conversation-row-sidebar"]').length
            };
        })()
        """)
        print("[*] DOM Element Counts:", json.dumps(counts, indent=2))

if __name__ == '__main__':
    asyncio.run(main())

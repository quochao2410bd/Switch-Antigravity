#!/usr/bin/env python3
"""
Antigravity Desktop DOM & Status Inspector (T03 Prototype)

Inspects sidebar conversation items, scoped active turn indicators,
and composer properties via CDP.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import urllib.request
import websockets

def hash_text(text):
    if not text:
        return ""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

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
        return f"http://127.0.0.1:{lines[0].strip()}", "OK"
    except Exception as e:
        return None, str(e)

async def eval_js(ws, expr):
    msg_id = 1
    payload = {"id": msg_id, "method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}
    await ws.send(json.dumps(payload))
    while True:
        raw = await ws.recv()
        data = json.loads(raw)
        if data.get("id") == msg_id:
            res = data.get("result", {})
            if "exceptionDetails" in res:
                return {"error": str(res["exceptionDetails"])}
            return res.get("result", {}).get("value")

async def main():
    parser = argparse.ArgumentParser(description="Inspect Antigravity Desktop DOM")
    parser.add_argument("--verbose-private-data", action="store_true", help="Display full unredacted conversation titles")
    args = parser.parse_args()

    endpoint, status = discover_cdp_endpoint()
    if status != "OK":
        print(f"[!] CDP Discovery failed: {status}")
        sys.exit(1)

    targets = json.loads(urllib.request.urlopen(f"{endpoint}/json/list").read().decode("utf-8"))
    page_target = next((t for t in targets if t.get("type") == "page"), None)
    if not page_target:
        print("[!] No page target found.")
        sys.exit(1)

    ws_url = page_target["webSocketDebuggerUrl"]
    async with websockets.connect(ws_url) as ws:
        convo_tree = await eval_js(ws, """
        (() => {
            const rows = Array.from(document.querySelectorAll('[data-testid="conversation-row-sidebar"]'));
            return rows.map((row, idx) => {
                const link = row.closest('a') || row.querySelector('a') || (row.parentElement && row.parentElement.querySelector('a'));
                const href = link ? link.getAttribute('href') : null;
                const titleTrigger = row.querySelector('[data-testid="lifted-context-menu-trigger"]');
                const title = titleTrigger ? titleTrigger.textContent.trim() : row.textContent.trim();
                const hasStopBtn = !!row.querySelector('button[aria-label*="Stop"], button[data-testid*="stop"]');
                
                let uuid = null;
                if (href && href.startsWith('/c/')) {
                    uuid = href.split('/c/')[1].split('?')[0];
                }
                const isActive = !!(uuid && window.location.pathname.startsWith('/c/' + uuid));

                return {
                    index: idx,
                    rawTitle: title,
                    href: href,
                    uuid: uuid,
                    isActive: isActive,
                    isExecutingInSidebar: hasStopBtn
                };
            });
        })()
        """)

        sanitized_tree = []
        for c in convo_tree:
            item = {
                "index": c["index"],
                "uuid": c["uuid"],
                "isActive": c["isActive"],
                "isExecutingInSidebar": c["isExecutingInSidebar"],
                "title_hash": hash_text(c["rawTitle"])
            }
            if args.verbose_private_data:
                item["title"] = c["rawTitle"]
            sanitized_tree.append(item)

        print("\n=== SIDEBAR CONVERSATION ITEMS ===")
        print(json.dumps(sanitized_tree, indent=2))

        turn_info = await eval_js(ws, """
        (() => {
            const mainContainer = document.querySelector('main') || document.body;
            const mainStopButtons = Array.from(mainContainer.querySelectorAll('button')).filter(b => {
                if (b.closest('[data-testid="conversation-list-sidebar"]')) return false;
                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                const text = (b.textContent || '').toLowerCase();
                return (label.includes('stop') || text.includes('stop')) && !!b.offsetParent;
            });
            const sidebarStopButtons = Array.from(document.querySelectorAll('[data-testid="conversation-list-sidebar"] button')).filter(b => {
                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                return label.includes('stop') && !!b.offsetParent;
            });

            return {
                mainExecutionActive: mainStopButtons.length > 0,
                mainStopButtonCount: mainStopButtons.length,
                sidebarExecutingConversationsCount: sidebarStopButtons.length
            };
        })()
        """)
        print("\n=== SCOPED EXECUTION STATE ===")
        print(json.dumps(turn_info, indent=2))

if __name__ == '__main__':
    asyncio.run(main())

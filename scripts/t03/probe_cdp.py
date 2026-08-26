import asyncio
import json
import os
import sys
import urllib.request
import websockets

def get_cdp_endpoint():
    appdata = os.environ.get('APPDATA')
    if appdata:
        port_file = os.path.join(appdata, 'Antigravity', 'DevToolsActivePort')
        if os.path.exists(port_file):
            with open(port_file, 'r', encoding='utf-8') as f:
                lines = f.read().splitlines()
                if lines:
                    port = lines[0].strip()
                    return f"http://127.0.0.1:{port}"
    return "http://127.0.0.1:58859"

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
                return {"error": res["exceptionDetails"]}
            return res.get("result", {}).get("value")

async def main():
    endpoint = get_cdp_endpoint()
    print(f"[*] CDP Endpoint: {endpoint}")
    
    # 1. Fetch version and targets
    try:
        ver_resp = urllib.request.urlopen(f"{endpoint}/json/version", timeout=3)
        ver_data = json.loads(ver_resp.read().decode('utf-8'))
        print(f"[*] Browser: {ver_data.get('Browser')}")
        print(f"[*] User-Agent: {ver_data.get('User-Agent')}")
    except Exception as e:
        print(f"[!] Error querying /json/version: {e}")
        return

    try:
        targets_resp = urllib.request.urlopen(f"{endpoint}/json/list", timeout=3)
        targets = json.loads(targets_resp.read().decode('utf-8'))
        print(f"[*] Found {len(targets)} targets:")
        page_target = None
        for t in targets:
            print(f"    - Type: {t.get('type')}, Title: '{t.get('title')}', URL: {t.get('url')}")
            if t.get('type') == 'page' and not page_target:
                page_target = t
    except Exception as e:
        print(f"[!] Error querying /json/list: {e}")
        return

    if not page_target:
        print("[!] No page target found.")
        return

    ws_url = page_target.get('webSocketDebuggerUrl')
    print(f"[*] Connecting to Page WebSocket: {ws_url}")
    
    async with websockets.connect(ws_url) as ws:
        print("[+] WebSocket connected successfully!")
        
        # Test basic info
        info = await eval_js(ws, """
        (() => {
            return {
                title: document.title,
                url: window.location.href,
                readyState: document.readyState,
                bodyChildrenCount: document.body ? document.body.children.length : 0
            };
        })()
        """)
        print("[*] Page Info:", json.dumps(info, indent=2))
        
        # Probe testids
        testids = await eval_js(ws, """
        (() => {
            const elements = Array.from(document.querySelectorAll('[data-testid]'));
            return elements.map(el => ({
                testid: el.getAttribute('data-testid'),
                tag: el.tagName,
                text: (el.textContent || '').trim().slice(0, 80),
                visible: !!(el.offsetParent || el.offsetWidth || el.offsetHeight)
            }));
        })()
        """)
        print(f"[*] Found {len(testids) if testids else 0} elements with data-testid:")
        if testids and isinstance(testids, list):
            for el in testids[:20]:
                print(f"    - [{el.get('testid')}] <{el.get('tag')}> '{el.get('text')}' (visible: {el.get('visible')})")
        
        # Probe composers / inputs / textareas / contenteditables
        inputs = await eval_js(ws, """
        (() => {
            const inputs = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"], [data-lexical-editor="true"], [role="textbox"]'));
            return inputs.map(el => ({
                tag: el.tagName,
                id: el.id,
                role: el.getAttribute('role'),
                placeholder: el.getAttribute('placeholder'),
                ariaLabel: el.getAttribute('aria-label'),
                contenteditable: el.getAttribute('contenteditable'),
                isLexical: el.getAttribute('data-lexical-editor'),
                className: el.className,
                visible: !!(el.offsetParent || el.offsetWidth || el.offsetHeight)
            }));
        })()
        """)
        print(f"[*] Found {len(inputs) if inputs else 0} input / composer elements:")
        if inputs and isinstance(inputs, list):
            for inp in inputs:
                print(f"    - <{inp.get('tag')}> id='{inp.get('id')}', role='{inp.get('role')}', placeholder='{inp.get('placeholder')}', ariaLabel='{inp.get('ariaLabel')}', lexical='{inp.get('isLexical')}', visible={inp.get('visible')}")
            
        # Probe conversation list / pills / sidebar
        convos = await eval_js(ws, """
        (() => {
            const pills = Array.from(document.querySelectorAll('[data-testid^="convo-pill-"]'));
            return pills.map(el => ({
                testid: el.getAttribute('data-testid'),
                text: (el.textContent || '').trim(),
                visible: !!(el.offsetParent || el.offsetWidth || el.offsetHeight)
            }));
        })()
        """)
        print(f"[*] Found {len(convos) if convos else 0} conversation pills:")
        if convos and isinstance(convos, list):
            for c in convos:
                print(f"    - {c.get('testid')}: '{c.get('text')}' (visible={c.get('visible')})")

        # Probe buttons (send, stop, model selection, etc.)
        buttons = await eval_js(ws, """
        (() => {
            const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
            return btns.map(b => ({
                tag: b.tagName,
                role: b.getAttribute('role'),
                ariaLabel: b.getAttribute('aria-label'),
                tooltipId: b.getAttribute('data-tooltip-id'),
                testid: b.getAttribute('data-testid'),
                text: (b.textContent || '').trim().slice(0, 50),
                disabled: b.disabled || b.getAttribute('aria-disabled') === 'true',
                visible: !!(b.offsetParent || b.offsetWidth || b.offsetHeight)
            })).filter(b => b.ariaLabel || b.tooltipId || b.testid || (b.text && b.text.length < 30));
        })()
        """)
        print(f"[*] Found {len(buttons) if buttons else 0} interesting button / action elements:")
        if buttons and isinstance(buttons, list):
            for b in buttons[:25]:
                print(f"    - <{b.get('tag')}> label='{b.get('ariaLabel')}', tooltip='{b.get('tooltipId')}', testid='{b.get('testid')}', text='{b.get('text')}', disabled={b.get('disabled')}, visible={b.get('visible')}")

if __name__ == '__main__':
    asyncio.run(main())

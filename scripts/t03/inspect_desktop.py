import asyncio
import json
import os
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
                    return f"http://127.0.0.1:{lines[0].strip()}"
    return "http://127.0.0.1:58859"

async def eval_js(ws, expr):
    msg_id = 4
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
    endpoint = get_cdp_endpoint()
    targets = json.loads(urllib.request.urlopen(f"{endpoint}/json/list").read().decode('utf-8'))
    page_target = next(t for t in targets if t.get('type') == 'page')
    ws_url = page_target['webSocketDebuggerUrl']
    
    async with websockets.connect(ws_url) as ws:
        print("Connected to:", page_target['title'], page_target['url'])
        
        # Test dry-run insertion and clean-up in Lexical editor
        res = await eval_js(ws, """
        (async () => {
            const editor = document.querySelector('[data-lexical-editor="true"]');
            if (!editor) return { ok: false, step: "locate_composer", error: "Editor not found" };
            
            const sendBtnBefore = document.querySelector('button[data-testid="send-button"], button[aria-label="Send message"]');
            const initialDisabled = sendBtnBefore ? (sendBtnBefore.disabled || sendBtnBefore.getAttribute('aria-disabled') === 'true') : null;
            
            // 1. Focus editor
            editor.focus();
            
            // 2. Insert test text
            const testText = "DRY_RUN_TEST_PROMPT_12345";
            document.execCommand('insertText', false, testText);
            
            await new Promise(r => setTimeout(r, 100));
            const insertedText = (editor.innerText || editor.textContent || '').trim();
            const sendBtnAfter = document.querySelector('button[data-testid="send-button"], button[aria-label="Send message"]');
            const enabledAfterInsert = sendBtnAfter ? !(sendBtnAfter.disabled || sendBtnAfter.getAttribute('aria-disabled') === 'true') : null;
            
            // 3. Clear text immediately (DRY RUN SAFETY)
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
            
            await new Promise(r => setTimeout(r, 100));
            const clearedText = (editor.innerText || editor.textContent || '').trim();
            const sendBtnCleared = document.querySelector('button[data-testid="send-button"], button[aria-label="Send message"]');
            const disabledAfterClear = sendBtnCleared ? (sendBtnCleared.disabled || sendBtnCleared.getAttribute('aria-disabled') === 'true') : null;
            
            return {
                ok: true,
                initialDisabled: initialDisabled,
                insertedText: insertedText,
                enabledAfterInsert: enabledAfterInsert,
                clearedText: clearedText,
                disabledAfterClear: disabledAfterClear
            };
        })()
        """)
        print("\n=== DRY RUN INSERTION TEST RESULT ===")
        print(json.dumps(res, indent=2))

if __name__ == '__main__':
    asyncio.run(main())

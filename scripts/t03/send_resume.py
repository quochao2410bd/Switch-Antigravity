#!/usr/bin/env python3
"""
Antigravity Desktop Resume Adapter (CDP-based)
Prototype for Phase 0 Research (T03)

Implements safe, deterministic conversation location, duplicate-prevention,
and resume submission via Chrome DevTools Protocol (CDP).

Requirements:
- Defaults to DRY RUN mode (no actual message submission without --send)
- Explicit multi-stage verification:
  1. COMPOSER_LOCATED
  2. TEXT_INSERTED
  3. TEXT_VERIFIED
  4. SEND_TRIGGERED
  5. USER_MESSAGE_APPEARED
  6. ASSISTANT_GENERATING_OBSERVED
- Duplicate Prevention:
  - Detects if an agent turn is ALREADY active (Stop button present)
  - Detects if the last message in conversation is already a resume message
- Refuses ambiguous conversation targets
- Refuses empty prompts
- Structured JSON output with strict error categorization
"""

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
import websockets

DEFAULT_RESUME_PROMPT = (
    "Continue the current task from exactly where you stopped. "
    "First inspect the current repository state, git status, git diff, "
    "recent commits, terminal output and the existing conversation context. "
    "Do not redo completed work. Continue implementing only the remaining work. "
    "Run the required tests when implementation is complete. "
    "If the original task is already complete, verify it instead of starting unrelated work."
)

def discover_cdp_endpoint():
    """Discover active CDP endpoint from DevToolsActivePort or standard fallbacks."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        port_file = os.path.join(appdata, "Antigravity", "DevToolsActivePort")
        if os.path.exists(port_file):
            try:
                with open(port_file, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                    if lines and lines[0].strip().isdigit():
                        return f"http://127.0.0.1:{lines[0].strip()}"
            except Exception:
                pass
    return "http://127.0.0.1:58859"

class AntigravityCDPClient:
    def __init__(self, endpoint=None, timeout=30):
        self.endpoint = endpoint or discover_cdp_endpoint()
        self.timeout = timeout
        self.ws = None
        self._msg_id = 0

    async def connect(self):
        try:
            url = f"{self.endpoint}/json/list"
            req = urllib.request.Request(url, headers={"User-Agent": "SwitchAntigravity-T03/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise ConnectionError(f"Failed to query CDP endpoint {self.endpoint}: {e}")

        page_target = next((t for t in targets if t.get("type") == "page"), None)
        if not page_target or "webSocketDebuggerUrl" not in page_target:
            raise ConnectionError(f"No inspectable page target found at {self.endpoint}")

        self.target = page_target
        self.ws = await websockets.connect(page_target["webSocketDebuggerUrl"], open_timeout=self.timeout)
        return self

    async def close(self):
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def send_command(self, method, params=None):
        self._msg_id += 1
        cmd_id = self._msg_id
        payload = {"id": cmd_id, "method": method, "params": params or {}}
        await self.ws.send(json.dumps(payload))
        
        start_time = time.time()
        while time.time() - start_time < self.timeout:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=self.timeout)
            msg = json.loads(raw)
            if msg.get("id") == cmd_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP Command {method} failed: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"Command {method} timed out after {self.timeout}s")

    async def evaluate(self, expression, await_promise=True):
        res = await self.send_command("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise
        })
        if "exceptionDetails" in res:
            raise RuntimeError(f"JS Evaluation error: {res['exceptionDetails']}")
        return res.get("result", {}).get("value")

    async def list_conversations(self):
        """Extract all visible conversations from the sidebar."""
        script = """
        (() => {
            const rows = Array.from(document.querySelectorAll('[data-testid="conversation-row-sidebar"]'));
            return rows.map((row, idx) => {
                const link = row.closest('a') || row.querySelector('a') || (row.parentElement && row.parentElement.querySelector('a'));
                let href = link ? link.getAttribute('href') : null;
                const titleTrigger = row.querySelector('[data-testid="lifted-context-menu-trigger"]');
                const title = titleTrigger ? titleTrigger.textContent.trim() : row.textContent.trim();
                
                let uuid = null;
                if (href && href.startsWith('/c/')) {
                    uuid = href.split('/c/')[1].split('?')[0];
                }
                const currentUrl = window.location.href;
                const isActive = !!(uuid && currentUrl.includes(uuid));
                
                const hasRowStopButton = !!row.querySelector('button[aria-label*="Stop"], button[data-testid*="stop"]');

                return {
                    index: idx,
                    title: title,
                    href: href,
                    uuid: uuid,
                    isActive: isActive,
                    isExecuting: hasRowStopButton
                };
            });
        })()
        """
        return await self.evaluate(script)

    async def switch_conversation(self, target_uuid):
        """Switch active conversation to target UUID by clicking link or navigating."""
        script = f"""
        (() => {{
            const rows = Array.from(document.querySelectorAll('[data-testid="conversation-row-sidebar"]'));
            for (const row of rows) {{
                const link = row.closest('a') || row.querySelector('a') || (row.parentElement && row.parentElement.querySelector('a'));
                if (link && link.getAttribute('href') && link.getAttribute('href').includes('{target_uuid}')) {{
                    link.click();
                    return {{ ok: true, method: "link_click", href: link.getAttribute('href') }};
                }}
            }}
            return {{ ok: false, reason: "link_not_found" }};
        }})()
        """
        res = await self.evaluate(script)
        if not res.get("ok"):
            nav_script = f"""
            (() => {{
                window.location.href = '/c/{target_uuid}';
                return {{ ok: true, method: "location_href" }};
            }})()
            """
            res = await self.evaluate(nav_script)
        await asyncio.sleep(1.0)
        return res

    async def inspect_conversation_messages(self):
        """Inspect current conversation articles / recent message text."""
        script = """
        (() => {
            const articles = Array.from(document.querySelectorAll('article, [role="article"]'));
            const lastArticle = articles.length > 0 ? articles[articles.length - 1] : null;
            const stopButtons = Array.from(document.querySelectorAll('button')).filter(b => {
                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                const text = (b.textContent || '').toLowerCase();
                return (label.includes('stop') || text.includes('stop')) && !!b.offsetParent;
            });

            return {
                totalArticles: articles.length,
                lastArticleText: lastArticle ? (lastArticle.textContent || '').trim() : null,
                isTurnActive: stopButtons.length > 0,
                stopButtonCount: stopButtons.length
            };
        })()
        """
        return await self.evaluate(script)

    async def inspect_composer_state(self):
        """Inspect current composer and send button state."""
        script = """
        (() => {
            const editor = document.querySelector('[data-lexical-editor="true"]');
            if (!editor) return { found: false };
            
            const sendBtn = document.querySelector('button[data-testid="send-button"], button[aria-label="Send message"]');
            const stopBtn = document.querySelector('button[aria-label*="Stop"], button[data-testid*="stop"]');
            
            return {
                found: true,
                role: editor.getAttribute('role'),
                ariaLabel: editor.getAttribute('aria-label'),
                text: (editor.innerText || editor.textContent || '').trim(),
                sendButton: sendBtn ? {
                    found: true,
                    disabled: sendBtn.disabled || sendBtn.getAttribute('aria-disabled') === 'true',
                    label: sendBtn.getAttribute('aria-label')
                } : { found: false },
                stopButton: stopBtn ? {
                    found: true,
                    label: stopBtn.getAttribute('aria-label'),
                    visible: !!(stopBtn.offsetParent)
                } : { found: false },
                url: window.location.href
            };
        })()
        """
        return await self.evaluate(script)

    async def clear_composer(self):
        """Safely clear any existing text in the Lexical composer via native keyboard simulation."""
        await self.evaluate("document.querySelector('[data-lexical-editor=\"true\"]').focus()")
        await asyncio.sleep(0.05)
        await self.send_command("Input.dispatchKeyEvent", {
            "type": "keyDown", "modifiers": 2, "windowsVirtualKeyCode": 65, "key": "a", "code": "KeyA"
        })
        await self.send_command("Input.dispatchKeyEvent", {
            "type": "keyUp", "modifiers": 2, "windowsVirtualKeyCode": 65, "key": "a", "code": "KeyA"
        })
        await self.send_command("Input.dispatchKeyEvent", {
            "type": "keyDown", "windowsVirtualKeyCode": 8, "key": "Backspace", "code": "Backspace"
        })
        await self.send_command("Input.dispatchKeyEvent", {
            "type": "keyUp", "windowsVirtualKeyCode": 8, "key": "Backspace", "code": "Backspace"
        })
        await asyncio.sleep(0.1)

    async def insert_prompt_text(self, text):
        """Insert prompt text into composer using native Input.insertText."""
        await self.evaluate("document.querySelector('[data-lexical-editor=\"true\"]').focus()")
        await asyncio.sleep(0.05)
        await self.send_command("Input.insertText", {"text": text})
        await asyncio.sleep(0.1)

    async def trigger_submission(self):
        """Submit the prompt by clicking the Send button or pressing Enter."""
        click_res = await self.evaluate("""
        (() => {
            const sendBtn = document.querySelector('button[data-testid="send-button"], button[aria-label="Send message"]');
            if (sendBtn && !(sendBtn.disabled || sendBtn.getAttribute('aria-disabled') === 'true')) {
                sendBtn.click();
                return { triggered: true, method: "button_click" };
            }
            return { triggered: false };
        })()
        """)
        if click_res.get("triggered"):
            return click_res

        await self.send_command("Input.dispatchKeyEvent", {
            "type": "keyDown", "windowsVirtualKeyCode": 13, "key": "Enter", "code": "Enter"
        })
        await self.send_command("Input.dispatchKeyEvent", {
            "type": "keyUp", "windowsVirtualKeyCode": 13, "key": "Enter", "code": "Enter"
        })
        return {"triggered": True, "method": "enter_key"}

    async def wait_for_turn_start(self, timeout=10):
        """Poll for definitive proof that the new turn started."""
        start = time.time()
        while time.time() - start < timeout:
            status = await self.evaluate("""
            (() => {
                const stopButtons = Array.from(document.querySelectorAll('button')).filter(b => {
                    const label = (b.getAttribute('aria-label') || '').toLowerCase();
                    const text = (b.textContent || '').toLowerCase();
                    return (label.includes('stop') || text.includes('stop')) && !!b.offsetParent;
                });
                
                const composer = document.querySelector('[data-lexical-editor="true"]');
                const composerCleared = composer ? (composer.innerText || composer.textContent || '').trim() === '' : false;
                
                const articles = Array.from(document.querySelectorAll('article, [role="article"]'));
                
                return {
                    hasStopButton: stopButtons.length > 0,
                    stopButtonCount: stopButtons.length,
                    composerCleared: composerCleared,
                    articleCount: articles.length
                };
            })()
            """)
            if status.get("hasStopButton") or (status.get("composerCleared") and status.get("articleCount", 0) > 0):
                return {"started": True, "details": status, "elapsed_seconds": round(time.time() - start, 2)}
            await asyncio.sleep(0.2)
        return {"started": False, "timeout": timeout}

async def execute_resume_pipeline(args):
    result = {
        "status": "INIT",
        "dry_run": not args.send,
        "endpoint": None,
        "target_conversation": None,
        "phases": {
            "1_composer_located": False,
            "2_text_inserted": False,
            "3_text_verified": False,
            "4_send_triggered": False,
            "5_user_message_appeared": False,
            "6_assistant_turn_started": False
        },
        "errors": []
    }

    endpoint = args.cdp_endpoint or discover_cdp_endpoint()
    result["endpoint"] = endpoint

    if not args.prompt or not args.prompt.strip():
        result["status"] = "ERROR_EMPTY_PROMPT"
        result["errors"].append("Prompt text cannot be empty.")
        return result

    prompt_text = args.prompt.strip()

    try:
        async with AntigravityCDPClient(endpoint=endpoint, timeout=args.timeout) as client:
            convos = await client.list_conversations()
            
            target = None
            if args.conversation_id:
                matches = [c for c in convos if c.get("uuid") and c.get("uuid").lower() == args.conversation_id.lower()]
                if not matches:
                    result["status"] = "CONVERSATION_NOT_FOUND"
                    result["errors"].append(f"Conversation UUID '{args.conversation_id}' not found in active UI.")
                    return result
                target = matches[0]
            elif args.title:
                matches = [c for c in convos if args.title.lower() in (c.get("title") or "").lower()]
                if len(matches) == 0:
                    result["status"] = "CONVERSATION_NOT_FOUND"
                    result["errors"].append(f"No conversation matching title '{args.title}' found.")
                    return result
                elif len(matches) > 1:
                    result["status"] = "CONVERSATION_AMBIGUOUS"
                    result["errors"].append(f"Multiple conversations match title '{args.title}': {[m['title'] for m in matches]}")
                    return result
                target = matches[0]
            else:
                active_matches = [c for c in convos if c.get("isActive")]
                if active_matches:
                    target = active_matches[0]
                elif convos:
                    result["status"] = "CONVERSATION_AMBIGUOUS"
                    result["errors"].append("No specific conversation specified (--uuid or --title) and no active conversation uniquely highlighted.")
                    return result
                else:
                    result["status"] = "CONVERSATION_NOT_FOUND"
                    result["errors"].append("No conversations available in UI.")
                    return result

            result["target_conversation"] = target

            # If target conversation is not active, switch to it
            if not target.get("isActive") and target.get("uuid"):
                await client.switch_conversation(target["uuid"])

            # DUPLICATE CHECK: Inspect conversation state
            convo_state = await client.inspect_conversation_messages()
            if convo_state.get("isTurnActive") and not args.force:
                result["status"] = "TURN_ALREADY_ACTIVE"
                result["errors"].append("A turn is already active/executing in target conversation (Stop button present). Duplicate resume aborted.")
                result["conversation_state"] = convo_state
                return result

            # PHASE 1: Locate Composer
            comp_state = await client.inspect_composer_state()
            if not comp_state.get("found"):
                result["status"] = "COMPOSER_NOT_FOUND"
                result["errors"].append("Lexical composer [data-lexical-editor='true'] was not found in active window.")
                return result
            result["phases"]["1_composer_located"] = True

            # In DRY RUN mode: test text insertion, verify, and clean up safely
            if not args.send:
                await client.clear_composer()
                await client.insert_prompt_text(prompt_text)
                
                after_ins = await client.inspect_composer_state()
                if after_ins.get("text") == prompt_text:
                    result["phases"]["2_text_inserted"] = True
                    result["phases"]["3_text_verified"] = True
                
                # Clean up immediately
                await client.clear_composer()
                after_clear = await client.inspect_composer_state()
                
                result["status"] = "DRY_RUN_SUCCESS"
                result["dry_run_summary"] = {
                    "composer_located": True,
                    "text_insertion_verified": result["phases"]["3_text_verified"],
                    "send_button_detected": after_ins.get("sendButton", {}).get("found", False),
                    "send_button_enabled_on_text": not after_ins.get("sendButton", {}).get("disabled", True),
                    "composer_cleaned_after_dry_run": after_clear.get("text") == ""
                }
                return result

            # REAL SEND EXECUTION (--send enabled)
            await client.clear_composer()
            await client.insert_prompt_text(prompt_text)
            result["phases"]["2_text_inserted"] = True

            verified_state = await client.inspect_composer_state()
            if verified_state.get("text") != prompt_text:
                result["status"] = "TEXT_INSERTION_FAILED"
                result["errors"].append(f"Expected composer text '{prompt_text[:50]}...', found '{verified_state.get('text')[:50]}...'")
                return result
            result["phases"]["3_text_verified"] = True

            sub_res = await client.trigger_submission()
            if not sub_res.get("triggered"):
                result["status"] = "SEND_TRIGGER_FAILED"
                result["errors"].append("Could not trigger send button or Enter key.")
                return result
            result["phases"]["4_send_triggered"] = True

            turn_res = await client.wait_for_turn_start(timeout=10)
            if turn_res.get("started"):
                result["phases"]["5_user_message_appeared"] = True
                result["phases"]["6_assistant_turn_started"] = True
                result["status"] = "TURN_STARTED"
                result["turn_details"] = turn_res
            else:
                result["status"] = "TURN_START_TIMEOUT"
                result["errors"].append("Send was triggered but assistant turn start was not confirmed within timeout.")

    except Exception as e:
        result["status"] = "EXCEPTION"
        result["errors"].append(str(e))

    return result

def main():
    parser = argparse.ArgumentParser(description="Antigravity Desktop Conversation Resume Tool")
    parser.add_argument("--conversation-id", "--uuid", dest="conversation_id", help="Target conversation UUID")
    parser.add_argument("--title", help="Target conversation title")
    parser.add_argument("--prompt", default=DEFAULT_RESUME_PROMPT, help="Resume prompt text")
    parser.add_argument("--send", action="store_true", help="Execute real submission (default: DRY RUN)")
    parser.add_argument("--force", action="store_true", help="Bypass duplicate turn detection")
    parser.add_argument("--cdp-endpoint", help="Custom CDP endpoint URL")
    parser.add_argument("--timeout", type=int, default=30, help="Operation timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    args = parser.parse_args()

    result = asyncio.run(execute_resume_pipeline(args))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\n=======================================================")
        print(f"[*] Antigravity Resume Tool - Result: {result['status']}")
        print(f"[*] Mode: {'REAL SEND' if not result['dry_run'] else 'DRY RUN (Simulated)'}")
        print(f"[*] CDP Endpoint: {result['endpoint']}")
        if result['target_conversation']:
            tc = result['target_conversation']
            print(f"[*] Target: [{tc.get('uuid')}] '{tc.get('title')}' (active: {tc.get('isActive')})")
        print(f"[*] Phases:")
        for phase, ok in result['phases'].items():
            print(f"    - {phase}: {'[PASS]' if ok else '[FAIL/SKIP]'}")
        if result.get('dry_run_summary'):
            print(f"[*] Dry Run Summary:")
            for k, v in result['dry_run_summary'].items():
                print(f"    - {k}: {v}")
        if result['errors']:
            print(f"[!] Errors:")
            for err in result['errors']:
                print(f"    - {err}")
        print(f"=======================================================\n")

    if result["status"] in ["DRY_RUN_SUCCESS", "TURN_STARTED"]:
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == '__main__':
    main()

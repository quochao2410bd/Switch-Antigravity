#!/usr/bin/env python3
"""
Antigravity Desktop Resume Adapter (T03 Research Prototype)

Implements safe, deterministic conversation location, duplicate prevention,
crash-safe recovery journaling, and resume submission via Chrome DevTools Protocol (CDP).

Review Round 2 Corrections:
- Structured CDP discovery: CDP_PORT_FILE_MISSING, CDP_PORT_FILE_INVALID, CDP_ENDPOINT_UNREACHABLE.
- Multi-signal page qualification: APP_PAGE_NOT_FOUND, APP_PAGE_AMBIGUOUS, APP_PAGE_QUALIFIED.
- Verified route navigation: pathname check (/c/<uuid>), CONVERSATION_SWITCH_VERIFIED, CONVERSATION_SWITCH_TIMEOUT, CONVERSATION_SWITCH_WRONG_TARGET.
- Scoped active turn detection: separates main chat container from sidebar executions.
- Pre-send baseline delta tracking: prevents false positives from pre-existing articles.
- User-only message inspection: ignores assistant messages containing prompt text.
- Distinct states: SEND_INPUT_DISPATCHED -> USER_MESSAGE_OBSERVED -> ASSISTANT_TURN_STARTED.
- Read-only default dry-run: zero DOM mutations; flags COMPOSER_DRAFT_PRESENT.
- Durable journal state ordering: writes SUBMISSION_ATTEMPTED BEFORE send action.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from recovery_journal import (
    RecoveryJournal,
    STATE_NOT_SENT,
    STATE_SUBMISSION_ATTEMPTED,
    STATE_MESSAGE_OBSERVED,
    STATE_TURN_STARTED,
    STATE_TURN_ACTIVE,
    STATE_FAILED
)

DEFAULT_RESUME_PROMPT = (
    "Continue the current task from exactly where you stopped. "
    "First inspect the current repository state, git status, git diff, "
    "recent commits, terminal output and the existing conversation context. "
    "Do not redo completed work. Continue implementing only the remaining work. "
    "Run the required tests when implementation is complete. "
    "If the original task is already complete, verify it instead of starting unrelated work."
)

def normalize_text(text):
    """Normalize whitespace for safe, idempotent string comparison."""
    if not text:
        return ""
    return " ".join(text.strip().split())

def hash_text(text):
    """Compute SHA-256 hash of normalized text."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()

def discover_cdp_endpoint():
    """
    Discover active CDP endpoint strictly from DevToolsActivePort.
    Returns (endpoint_url, status_code).
    """
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

class QualifiedAntigravityClient:
    def __init__(self, endpoint, timeout=30):
        self.endpoint = endpoint
        self.timeout = timeout
        self.ws = None
        self.target = None
        self._msg_id = 0

    async def connect_and_qualify(self):
        """Query targets and qualify the exact Antigravity main application page."""
        try:
            url = f"{self.endpoint}/json/list"
            req = urllib.request.Request(url, headers={"User-Agent": "SwitchAntigravity-T03/2.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None, "CDP_ENDPOINT_UNREACHABLE"

        candidates = [t for t in targets if t.get("type") == "page" and "webSocketDebuggerUrl" in t]
        if not candidates:
            return None, "APP_PAGE_NOT_FOUND"

        qualified = []
        for candidate in candidates:
            try:
                ws_url = candidate["webSocketDebuggerUrl"]
                async with websockets.connect(ws_url, open_timeout=3) as test_ws:
                    probe_payload = {
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": """(() => {
                                const hasSidebar = !!document.querySelector('[data-testid="conversation-list-sidebar"]');
                                const hasComposer = !!document.querySelector('[data-lexical-editor="true"]');
                                const hasAppConfig = !!(window.__APP_CONFIG__ && window.__APP_CONFIG__.productName === "antigravity");
                                const urlHasConvo = window.location.pathname.startsWith('/c/');
                                return {
                                    hasSidebar: hasSidebar,
                                    hasComposer: hasComposer,
                                    hasAppConfig: hasAppConfig,
                                    urlHasConvo: urlHasConvo,
                                    pathname: window.location.pathname
                                };
                            })()""",
                            "returnByValue": True
                        }
                    }
                    await test_ws.send(json.dumps(probe_payload))
                    raw_resp = await asyncio.wait_for(test_ws.recv(), timeout=3)
                    res = json.loads(raw_resp).get("result", {}).get("result", {}).get("value", {})
                    
                    signals = sum([
                        1 if res.get("hasSidebar") else 0,
                        1 if res.get("hasComposer") else 0,
                        1 if res.get("hasAppConfig") else 0
                    ])
                    if signals >= 2 or (signals >= 1 and res.get("urlHasConvo")):
                        qualified.append((candidate, res))
            except Exception:
                continue

        if len(qualified) == 0:
            return None, "APP_PAGE_NOT_FOUND"
        elif len(qualified) > 1:
            convo_pages = [q for q in qualified if q[1].get("urlHasConvo")]
            if len(convo_pages) == 1:
                self.target = convo_pages[0][0]
            else:
                return None, "APP_PAGE_AMBIGUOUS"
        else:
            self.target = qualified[0][0]

        self.ws = await websockets.connect(self.target["webSocketDebuggerUrl"], open_timeout=self.timeout)
        return self.target, "APP_PAGE_QUALIFIED"

    async def close(self):
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def __aenter__(self):
        _, status = await self.connect_and_qualify()
        if status != "APP_PAGE_QUALIFIED":
            raise RuntimeError(f"CDP qualification failed: {status}")
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
                const pathname = window.location.pathname;
                const isActive = !!(uuid && (pathname === '/c/' + uuid || pathname.startsWith('/c/' + uuid + '/')));
                const hasRowStopButton = !!row.querySelector('button[aria-label*="Stop"], button[data-testid*="stop"]');

                return {
                    index: idx,
                    title: title,
                    href: href,
                    uuid: uuid,
                    isActive: isActive,
                    isExecutingInSidebar: hasRowStopButton
                };
            });
        })()
        """
        return await self.evaluate(script)

    async def switch_conversation_verified(self, target_uuid, timeout=6.0):
        """Switch active conversation to target UUID and verify exact pathname and composer readiness."""
        click_script = f"""
        (() => {{
            const rows = Array.from(document.querySelectorAll('[data-testid="conversation-row-sidebar"]'));
            for (const row of rows) {{
                const link = row.closest('a') || row.querySelector('a') || (row.parentElement && row.parentElement.querySelector('a'));
                if (link && link.getAttribute('href') && link.getAttribute('href').includes('{target_uuid}')) {{
                    link.click();
                    return {{ clicked: true }};
                }}
            }}
            window.location.pathname = '/c/{target_uuid}';
            return {{ clicked: true, method: "pathname_nav" }};
        }})()
        """
        await self.evaluate(click_script)

        start = time.time()
        while time.time() - start < timeout:
            check = await self.evaluate(f"""
            (() => {{
                const pathname = window.location.pathname;
                const expected = '/c/{target_uuid}';
                const isExactMatch = (pathname === expected || pathname.startsWith(expected + '/'));
                const composerMounted = !!document.querySelector('[data-lexical-editor="true"]');
                return {{
                    isExactMatch: isExactMatch,
                    composerMounted: composerMounted,
                    currentPathname: pathname
                }};
            }})()
            """)
            if check.get("isExactMatch") and check.get("composerMounted"):
                return {"status": "CONVERSATION_SWITCH_VERIFIED", "elapsed": round(time.time() - start, 2)}
            elif check.get("composerMounted") and not check.get("isExactMatch") and check.get("currentPathname").startswith('/c/'):
                if time.time() - start > 2.0:
                    return {"status": "CONVERSATION_SWITCH_WRONG_TARGET", "currentPathname": check.get("currentPathname")}
            await asyncio.sleep(0.2)

        return {"status": "CONVERSATION_SWITCH_TIMEOUT"}

    async def inspect_scoped_conversation_state(self, target_uuid):
        """Inspect conversation history, scoped active-turn state, and user message hashes."""
        script = f"""
        (() => {{
            const pathname = window.location.pathname;
            const expected = '/c/{target_uuid}';
            if (!(pathname === expected || pathname.startsWith(expected + '/'))) {{
                return {{ error: "WRONG_CONVERSATION_ACTIVE", currentPathname: pathname }};
            }}

            const articles = Array.from(document.querySelectorAll('article, [role="article"]'));
            const mainChatContainer = document.querySelector('main') || document.body;
            const mainStopButtons = Array.from(mainChatContainer.querySelectorAll('button')).filter(b => {{
                if (b.closest('[data-testid="conversation-list-sidebar"]')) return false;
                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                const text = (b.textContent || '').toLowerCase();
                return (label.includes('stop') || text.includes('stop')) && !!b.offsetParent;
            }});

            const userMessages = [];
            const assistantMessages = [];

            articles.forEach(art => {{
                const text = (art.textContent || '').trim();
                const isUser = art.classList.contains('sticky') || text.startsWith('User:') || text.startsWith('T0') || art.getAttribute('data-author') === 'user';
                if (isUser) {{
                    userMessages.push(text);
                }} else {{
                    assistantMessages.push(text);
                }}
            }});

            const lastUserMsg = userMessages.length > 0 ? userMessages[userMessages.length - 1] : null;
            const lastAssistantMsg = assistantMessages.length > 0 ? assistantMessages[assistantMessages.length - 1] : null;

            return {{
                totalArticles: articles.length,
                userMessageCount: userMessages.length,
                assistantMessageCount: assistantMessages.length,
                lastUserMessageText: lastUserMsg,
                lastAssistantMessageText: lastAssistantMsg,
                isMainTurnActive: mainStopButtons.length > 0,
                mainStopButtonCount: mainStopButtons.length
            }};
        }})()
        """
        return await self.evaluate(script)

    async def inspect_composer_state(self):
        """Inspect current composer and send button state."""
        script = """
        (() => {
            const editor = document.querySelector('[data-lexical-editor="true"]');
            if (!editor) return { found: false };
            
            const sendBtn = document.querySelector('button[data-testid="send-button"], button[aria-label="Send message"]');
            const mainChat = document.querySelector('main') || document.body;
            const stopBtn = Array.from(mainChat.querySelectorAll('button')).find(b => {
                if (b.closest('[data-testid="conversation-list-sidebar"]')) return false;
                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                return label.includes('stop') && !!b.offsetParent;
            });

            return {
                found: true,
                role: editor.getAttribute('role'),
                ariaLabel: editor.getAttribute('aria-label'),
                text: (editor.innerText || editor.textContent || '').trim(),
                isFocused: document.activeElement === editor,
                sendButton: sendBtn ? {
                    found: true,
                    disabled: sendBtn.disabled || sendBtn.getAttribute('aria-disabled') === 'true',
                    label: sendBtn.getAttribute('aria-label')
                } : { found: false },
                stopButton: stopBtn ? {
                    found: true,
                    label: stopBtn.getAttribute('aria-label')
                } : { found: false }
            };
        })()
        """
        return await self.evaluate(script)

    async def clear_composer(self):
        """Safely clear Lexical composer via native keyboard events."""
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
        """Insert prompt text using native Input.insertText."""
        await self.evaluate("document.querySelector('[data-lexical-editor=\"true\"]').focus()")
        await asyncio.sleep(0.05)
        await self.send_command("Input.insertText", {"text": text})
        await asyncio.sleep(0.1)

    async def dispatch_submission_input(self):
        """Dispatch submission action (send button click or Enter key)."""
        click_res = await self.evaluate("""
        (() => {
            const sendBtn = document.querySelector('button[data-testid="send-button"], button[aria-label="Send message"]');
            if (sendBtn && !(sendBtn.disabled || sendBtn.getAttribute('aria-disabled') === 'true')) {
                sendBtn.click();
                return { dispatched: true, method: "button_click" };
            }
            return { dispatched: false };
        })()
        """)
        if click_res.get("dispatched"):
            return click_res

        await self.send_command("Input.dispatchKeyEvent", {
            "type": "keyDown", "windowsVirtualKeyCode": 13, "key": "Enter", "code": "Enter"
        })
        await self.send_command("Input.dispatchKeyEvent", {
            "type": "keyUp", "windowsVirtualKeyCode": 13, "key": "Enter", "code": "Enter"
        })
        return {"dispatched": True, "method": "enter_key"}

    async def wait_for_user_and_assistant_turn(self, target_uuid, prompt_hash, baseline, timeout=12):
        """Wait and separately observe user message appearance and assistant turn start."""
        start = time.time()
        user_msg_observed = False
        assistant_started = False

        while time.time() - start < timeout:
            state = await self.inspect_scoped_conversation_state(target_uuid)
            
            if not user_msg_observed:
                last_user_text = state.get("lastUserMessageText") or ""
                last_user_hash = hash_text(last_user_text)
                if last_user_hash == prompt_hash or state.get("userMessageCount", 0) > baseline.get("userMessageCount", 0):
                    user_msg_observed = True

            if state.get("isMainTurnActive") or state.get("assistantMessageCount", 0) > baseline.get("assistantMessageCount", 0):
                assistant_started = True

            if user_msg_observed and assistant_started:
                return {
                    "user_message_observed": True,
                    "assistant_turn_started": True,
                    "elapsed_seconds": round(time.time() - start, 2),
                    "state": state
                }
            await asyncio.sleep(0.3)

        return {
            "user_message_observed": user_msg_observed,
            "assistant_turn_started": assistant_started,
            "timeout": timeout,
            "elapsed_seconds": round(time.time() - start, 2)
        }

async def execute_resume_pipeline(args):
    result = {
        "status": "INIT",
        "dry_run": not args.send,
        "endpoint": None,
        "target_conversation": None,
        "recovery_attempt_id": None,
        "phases": {
            "1_composer_located": False,
            "2_text_inserted": False,
            "3_text_verified": False,
            "4_send_input_dispatched": False,
            "5_user_message_observed": False,
            "6_assistant_turn_started": False
        },
        "errors": []
    }

    journal = RecoveryJournal(args.journal_path)

    if args.cdp_endpoint:
        endpoint = args.cdp_endpoint
        status = "OK"
    else:
        endpoint, status = discover_cdp_endpoint()

    result["endpoint"] = endpoint
    if status != "OK":
        result["status"] = status
        result["errors"].append(f"CDP discovery failed: {status}")
        return result

    if not args.prompt or not args.prompt.strip():
        result["status"] = "ERROR_EMPTY_PROMPT"
        result["errors"].append("Prompt text cannot be empty.")
        return result

    prompt_text = args.prompt.strip()
    prompt_hash = hash_text(prompt_text)

    try:
        client = QualifiedAntigravityClient(endpoint=endpoint, timeout=args.timeout)
        _, qual_status = await client.connect_and_qualify()
        if qual_status != "APP_PAGE_QUALIFIED":
            result["status"] = qual_status
            result["errors"].append(f"Target page qualification failed: {qual_status}")
            return result

        async with client:
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
                matches = [c for c in convos if (c.get("title") or "").strip() == args.title.strip()]
                if len(matches) == 0:
                    result["status"] = "CONVERSATION_NOT_FOUND"
                    result["errors"].append(f"No conversation matching exact title '{args.title}' found.")
                    return result
                elif len(matches) > 1:
                    result["status"] = "CONVERSATION_AMBIGUOUS"
                    result["errors"].append(f"Multiple conversations share exact title '{args.title}'")
                    return result
                target = matches[0]
            else:
                active_matches = [c for c in convos if c.get("isActive")]
                if len(active_matches) == 1:
                    target = active_matches[0]
                elif convos:
                    result["status"] = "CONVERSATION_AMBIGUOUS"
                    result["errors"].append("Multiple conversations present; explicit --uuid required.")
                    return result
                else:
                    result["status"] = "CONVERSATION_NOT_FOUND"
                    result["errors"].append("No conversations available in UI.")
                    return result

            target_display = {
                "uuid": target.get("uuid"),
                "isActive": target.get("isActive"),
                "title_hash": hash_text(target.get("title")),
                "isExecutingInSidebar": target.get("isExecutingInSidebar")
            }
            if args.verbose_private_data:
                target_display["title"] = target.get("title")
            result["target_conversation"] = target_display

            if not target.get("isActive") and target.get("uuid"):
                switch_res = await client.switch_conversation_verified(target["uuid"])
                switch_status = switch_res.get("status")
                if switch_status != "CONVERSATION_SWITCH_VERIFIED":
                    result["status"] = switch_status
                    result["errors"].append(f"Failed to navigate to conversation {target['uuid']}: {switch_status}")
                    return result

            scoped_state = await client.inspect_scoped_conversation_state(target["uuid"])
            if scoped_state.get("error"):
                result["status"] = scoped_state["error"]
                result["errors"].append(f"Scoped state check failed: {scoped_state['error']}")
                return result

            last_user_msg = scoped_state.get("lastUserMessageText") or ""
            last_user_hash = hash_text(last_user_msg)
            
            if scoped_state.get("isMainTurnActive"):
                duplicate_status = "TURN_ALREADY_ACTIVE"
            elif last_user_hash == prompt_hash:
                duplicate_status = "RESUME_MESSAGE_PRESENT"
            elif last_user_msg:
                duplicate_status = "RESUME_NOT_PRESENT"
            else:
                duplicate_status = "DUPLICATE_STATE_UNKNOWN"

            result["duplicate_detection"] = {
                "status": duplicate_status,
                "isMainTurnActive": scoped_state.get("isMainTurnActive"),
                "lastUserMessageHash": last_user_hash
            }

            if duplicate_status in ["TURN_ALREADY_ACTIVE", "RESUME_MESSAGE_PRESENT"]:
                if not args.dangerous_manual_override:
                    result["status"] = duplicate_status
                    result["errors"].append(f"Duplicate protection triggered: {duplicate_status}. Submission aborted (DO_NOT_RESEND).")
                    return result

            comp_state = await client.inspect_composer_state()
            if not comp_state.get("found"):
                result["status"] = "COMPOSER_NOT_FOUND"
                result["errors"].append("Lexical composer [data-lexical-editor='true'] not found.")
                return result
            result["phases"]["1_composer_located"] = True

            existing_draft = comp_state.get("text", "")
            if existing_draft:
                result["existing_draft_detected"] = True
                if not args.dangerous_manual_override:
                    result["status"] = "COMPOSER_DRAFT_PRESENT"
                    result["errors"].append("Composer already contains unsubmitted user draft. Overwriting refused.")
                    return result

            if not args.send:
                if args.probe_composer_write:
                    await client.clear_composer()
                    await client.insert_prompt_text(prompt_text)
                    after_ins = await client.inspect_composer_state()
                    if hash_text(after_ins.get("text")) == prompt_hash:
                        result["phases"]["2_text_inserted"] = True
                        result["phases"]["3_text_verified"] = True
                    await client.clear_composer()
                    result["status"] = "DRY_RUN_WRITE_PROBE_SUCCESS"
                else:
                    result["status"] = "DRY_RUN_READ_ONLY_SUCCESS"
                
                result["dry_run_summary"] = {
                    "mode": "READ_ONLY" if not args.probe_composer_write else "WRITE_PROBE",
                    "composer_located": True,
                    "target_uuid_verified": target.get("uuid"),
                    "duplicate_status": duplicate_status,
                    "send_button_detected": comp_state.get("sendButton", {}).get("found", False)
                }
                return result

            attempt_rec = journal.start_recovery_attempt(target["uuid"], prompt_text)
            attempt_id = attempt_rec["attempt_id"]
            result["recovery_attempt_id"] = attempt_id

            baseline = scoped_state

            await client.clear_composer()
            await client.insert_prompt_text(prompt_text)
            result["phases"]["2_text_inserted"] = True

            verified_comp = await client.inspect_composer_state()
            if hash_text(verified_comp.get("text")) != prompt_hash:
                journal.transition_state(target["uuid"], attempt_id, STATE_FAILED, "Text insertion verification failed")
                result["status"] = "TEXT_INSERTION_FAILED"
                result["errors"].append("Composer text does not match intended prompt.")
                return result
            result["phases"]["3_text_verified"] = True

            journal.transition_state(target["uuid"], attempt_id, STATE_SUBMISSION_ATTEMPTED)

            dispatch_res = await client.dispatch_submission_input()
            if not dispatch_res.get("dispatched"):
                journal.transition_state(target["uuid"], attempt_id, STATE_FAILED, "Input dispatch failed")
                result["status"] = "SEND_INPUT_DISPATCH_FAILED"
                result["errors"].append("Failed to dispatch send button or Enter key.")
                return result
            result["phases"]["4_send_input_dispatched"] = True

            turn_res = await client.wait_for_user_and_assistant_turn(target["uuid"], prompt_hash, baseline, timeout=args.timeout)
            
            if turn_res.get("user_message_observed"):
                result["phases"]["5_user_message_observed"] = True
                journal.transition_state(target["uuid"], attempt_id, STATE_MESSAGE_OBSERVED)

            if turn_res.get("assistant_turn_started"):
                result["phases"]["6_assistant_turn_started"] = True
                journal.transition_state(target["uuid"], attempt_id, STATE_TURN_STARTED)
                result["status"] = "TURN_STARTED"
            elif turn_res.get("user_message_observed"):
                result["status"] = "USER_MESSAGE_OBSERVED_ASSISTANT_PENDING"
            else:
                journal.transition_state(target["uuid"], attempt_id, STATE_FAILED, "Turn confirmation timeout")
                result["status"] = "TURN_START_TIMEOUT"
                result["errors"].append("Turn confirmation timed out.")

    except Exception as e:
        result["status"] = "EXCEPTION"
        result["errors"].append(str(e))

    return result

def main():
    parser = argparse.ArgumentParser(description="Antigravity Desktop Resume Tool (T03 Prototype)")
    parser.add_argument("--conversation-id", "--uuid", dest="conversation_id", help="Target conversation UUID")
    parser.add_argument("--title", help="Target conversation exact title")
    parser.add_argument("--prompt", default=DEFAULT_RESUME_PROMPT, help="Resume prompt text")
    parser.add_argument("--send", action="store_true", help="Execute real submission (default: DRY RUN)")
    parser.add_argument("--probe-composer-write", action="store_true", help="Allow dry-run write probe (mutates/cleans composer)")
    parser.add_argument("--dangerous-manual-override-do-not-use-in-production", dest="dangerous_manual_override", action="store_true", help="Manual test override for duplicate protection")
    parser.add_argument("--cdp-endpoint", help="Custom CDP endpoint URL")
    parser.add_argument("--journal-path", help="Custom recovery journal path")
    parser.add_argument("--timeout", type=int, default=30, help="Operation timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument("--verbose-private-data", action="store_true", help="Expose raw titles and message contents")

    args = parser.parse_args()

    result = asyncio.run(execute_resume_pipeline(args))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\n=======================================================")
        print(f"[*] Antigravity Resume Tool - Result: {result['status']}")
        print(f"[*] Mode: {'REAL SEND' if not result['dry_run'] else 'DRY RUN'}")
        print(f"[*] CDP Endpoint: {result['endpoint']}")
        if result['target_conversation']:
            tc = result['target_conversation']
            print(f"[*] Target UUID: {tc.get('uuid')} (active: {tc.get('isActive')})")
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

    if result["status"] in ["DRY_RUN_READ_ONLY_SUCCESS", "DRY_RUN_WRITE_PROBE_SUCCESS", "TURN_STARTED"]:
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == '__main__':
    main()

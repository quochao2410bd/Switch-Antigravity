#!/usr/bin/env python3
"""
Antigravity Desktop Resume Adapter (T03 Research Prototype)

Implements safe, deterministic conversation location, duplicate prevention,
crash-safe recovery journaling, and resume submission via Chrome DevTools Protocol (CDP).

Review Round 6 Hardening:
- Structural non-reentrant locking: execute_resume_pipeline explicitly calls unlocked journal
  methods (_start_recovery_attempt_unlocked, _transition_state_unlocked, _reconcile_existing_attempt_unlocked)
  strictly inside with journal.exclusive_lock(...).
- Atomic renderer-side prompt identity verification before click:
  Normalized composer text is verified to equal expected prompt inside JS before clicking Send.
  Zero irreversible clicks on mismatched prompt or route.
- Send control validation: requires exactly 1 verified visible enabled send button inside target root.
  0 buttons -> SEND_CONTROL_NOT_FOUND; >1 buttons -> SEND_CONTROL_AMBIGUOUS (DO NOT SEND).
  Enter fallback completely removed from autonomous supervisor safe path.
- Clear separation of targetRoot, messageContainer, and composerContainer.
- Re-reads and re-evaluates all state from disk inside lock.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from recovery_journal import (
    RecoveryJournal,
    JournalDurabilityError,
    STATE_NOT_SENT,
    STATE_SUBMISSION_ATTEMPTED,
    STATE_DISPATCHED_UNCONFIRMED,
    STATE_MESSAGE_OBSERVED,
    STATE_TURN_STARTED,
    STATE_TURN_ACTIVE,
    STATE_FAILED,
    DECISION_NEW_ATTEMPT_ALLOWED,
    DECISION_RESUME_ALREADY_OBSERVED,
    DECISION_TURN_ALREADY_ACTIVE,
    DECISION_PREVIOUS_SUBMISSION_UNCONFIRMED,
    DECISION_RECOVERY_STATE_UNKNOWN,
    DECISION_JOURNAL_CORRUPTED,
    DECISION_JOURNAL_SCHEMA_UNSUPPORTED,
    DECISION_MANUAL_RECONCILIATION_REQUIRED,
    DECISION_BLOCKED_DRAFT_PRESENT,
    evaluate_recovery_permission,
    validate_uuid,
    hash_prompt
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
    if not text:
        return ""
    return " ".join(text.strip().split())

def hash_text(text):
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()

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
        
        req = urllib.request.Request(f"{endpoint}/json/version", headers={"User-Agent": "SwitchAntigravity-T03/6.0"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            ver_data = json.loads(resp.read().decode("utf-8"))
            if "Browser" not in ver_data:
                return None, "CDP_ENDPOINT_UNREACHABLE"
        return endpoint, "OK"
    except Exception:
        return None, "CDP_ENDPOINT_UNREACHABLE"

def classify_duplicate_state(user_messages, prompt_hash, has_unknown_role=False, last_message_is_unknown=False):
    if last_message_is_unknown:
        return "DUPLICATE_STATE_UNKNOWN", None

    if not user_messages:
        return "RESUME_NOT_PRESENT" if not has_unknown_role else "DUPLICATE_STATE_UNKNOWN", None

    last_user_text = user_messages[-1]
    last_user_hash = hash_text(last_user_text)

    if last_user_hash == prompt_hash:
        return "RESUME_MESSAGE_PRESENT", last_user_hash
    return "RESUME_NOT_PRESENT", last_user_hash

def correlate_turn_status(dom_state, baseline_state=None, external_error_hook=None):
    if not dom_state:
        return "TURN_STATUS_UNKNOWN"

    if external_error_hook and callable(external_error_hook):
        ext_res = external_error_hook(dom_state, baseline_state)
        if ext_res:
            return ext_res

    new_quota_error = dom_state.get("newQuotaError", False)
    new_generic_error = dom_state.get("newGenericError", False)

    if new_quota_error:
        return "QUOTA_ERROR_OBSERVED"
    if new_generic_error:
        return "ERROR_RESPONSE_OBSERVED"
    if dom_state.get("isMainTurnActive"):
        return "ASSISTANT_GENERATION_ACTIVE"
    if dom_state.get("assistantMessageDelta", 0) > 0:
        return "ASSISTANT_GENERATION_COMPLETED"

    return "NO_ASSISTANT_TURN"

DOM_QUALIFICATION_JS = r"""
function qualifyTargetSurface(targetUuid) {
    if (targetUuid) {
        const pathname = window.location.pathname.toLowerCase();
        const expected = '/c/' + targetUuid.toLowerCase();
        if (pathname !== expected) {
            return { surface: null, error: 'WRONG_CONVERSATION_ACTIVE', currentPathname: pathname };
        }
    }

    const candidateElements = new Set();
    const candidateVariants = new Map();

    const mains = Array.from(document.querySelectorAll('main')).filter(m => !!m.offsetParent && !m.closest('[data-testid="conversation-list-sidebar"]'));
    for (const m of mains) {
        candidateElements.add(m);
        candidateVariants.set(m, 'LEGACY_MAIN');
    }

    const convViews = Array.from(document.querySelectorAll('[data-testid="conversation-view"]')).filter(c => !!c.offsetParent && !c.closest('[data-testid="conversation-list-sidebar"]'));
    for (const cv of convViews) {
        candidateElements.add(cv);
        if (!candidateVariants.has(cv)) {
            candidateVariants.set(cv, 'CONVERSATION_VIEW');
        }
    }

    if (candidateElements.size === 0) {
        return { surface: null, error: 'TARGET_SURFACE_NOT_FOUND' };
    }
    if (candidateElements.size > 1) {
        return { surface: null, error: 'TARGET_SURFACE_AMBIGUOUS', count: candidateElements.size };
    }

    const targetSurface = Array.from(candidateElements)[0];
    return { surface: targetSurface, variant: candidateVariants.get(targetSurface), error: null };
}

function qualifyComposerSurface(targetSurface) {
    if (!targetSurface) return { editor: null, inputBox: null, sendButton: { found: false }, sendBtnElement: null, error: 'TARGET_SURFACE_NOT_FOUND' };

    const rawInputBoxes = new Set();
    const explicitTestIdBoxes = Array.from(targetSurface.querySelectorAll('[data-testid="agent-input-box"]')).filter(b => !!b.offsetParent);
    for (const b of explicitTestIdBoxes) rawInputBoxes.add(b);

    const explicitIdBoxes = Array.from(targetSurface.querySelectorAll('#antigravity\\.agentSidePanelInputBox')).filter(b => !!b.offsetParent);
    for (const b of explicitIdBoxes) rawInputBoxes.add(b);

    if (rawInputBoxes.size === 0) {
        return { editor: null, inputBox: null, sendButton: { found: false }, sendBtnElement: null, error: 'COMPOSER_SURFACE_NOT_FOUND' };
    }

    // Filter out ancestor containers if an inner candidate container is nested inside it
    const allBoxArray = Array.from(rawInputBoxes);
    const leafInputBoxes = allBoxArray.filter(box => {
        return !allBoxArray.some(other => other !== box && box.contains(other));
    });

    if (leafInputBoxes.length === 0) {
        return { editor: null, inputBox: null, sendButton: { found: false }, sendBtnElement: null, error: 'COMPOSER_SURFACE_NOT_FOUND' };
    }
    if (leafInputBoxes.length > 1) {
        return { editor: null, inputBox: null, sendButton: { found: false }, sendBtnElement: null, error: 'COMPOSER_SURFACE_AMBIGUOUS', count: leafInputBoxes.length };
    }

    const inputBox = leafInputBoxes[0];

    const targetEditors = Array.from(targetSurface.querySelectorAll('[data-lexical-editor="true"]')).filter(e => !!e.offsetParent);
    if (targetEditors.length === 0) return { editor: null, inputBox: inputBox, sendButton: { found: false }, sendBtnElement: null, error: 'COMPOSER_NOT_FOUND' };
    if (targetEditors.length > 1) return { editor: null, inputBox: inputBox, sendButton: { found: false }, sendBtnElement: null, error: 'COMPOSER_AMBIGUOUS', count: targetEditors.length };
    const targetEditor = targetEditors[0];

    const boxEditors = Array.from(inputBox.querySelectorAll('[data-lexical-editor="true"]')).filter(e => !!e.offsetParent);
    if (boxEditors.length === 0) {
        return { editor: null, inputBox: inputBox, sendButton: { found: false }, sendBtnElement: null, error: 'COMPOSER_NOT_FOUND' };
    }
    if (boxEditors.length > 1) {
        return { editor: null, inputBox: inputBox, sendButton: { found: false }, sendBtnElement: null, error: 'COMPOSER_AMBIGUOUS', count: boxEditors.length };
    }
    const boxEditor = boxEditors[0];

    if (targetEditor !== boxEditor) {
        return { editor: null, inputBox: inputBox, sendButton: { found: false }, sendBtnElement: null, error: 'COMPOSER_AMBIGUOUS' };
    }

    const sendBtnCandidates = new Set();
    const testIdSendBtns = Array.from(inputBox.querySelectorAll('button[data-testid="send-button"]')).filter(b => !!b.offsetParent);
    for (const b of testIdSendBtns) sendBtnCandidates.add(b);

    const ariaSendBtns = Array.from(inputBox.querySelectorAll('button[aria-label="Send message"]')).filter(b => !!b.offsetParent);
    for (const b of ariaSendBtns) sendBtnCandidates.add(b);

    let sendBtnState = { found: false };
    let sendBtnElement = null;
    if (sendBtnCandidates.size === 0) {
        sendBtnState = { found: false, error: 'SEND_CONTROL_NOT_FOUND' };
    } else if (sendBtnCandidates.size > 1) {
        sendBtnState = { found: false, error: 'SEND_CONTROL_AMBIGUOUS', count: sendBtnCandidates.size };
    } else {
        const b = Array.from(sendBtnCandidates)[0];
        sendBtnElement = b;
        sendBtnState = {
            found: true,
            disabled: b.disabled || b.getAttribute('aria-disabled') === 'true',
            label: b.getAttribute('aria-label') || 'Send'
        };
    }

    return { editor: boxEditor, inputBox: inputBox, sendButton: sendBtnState, sendBtnElement: sendBtnElement, error: null };
}

function qualifyMessageSurface(targetSurface) {
    if (!targetSurface) return { container: null, error: 'TARGET_SURFACE_NOT_FOUND' };

    const explicitContainers = Array.from(targetSurface.querySelectorAll('[data-testid="conversation-messages"]')).filter(c => !!c.offsetParent);
    if (explicitContainers.length === 1) {
        return { container: explicitContainers[0], variant: 'EXPLICIT_DATA_TESTID', error: null };
    } else if (explicitContainers.length > 1) {
        return { container: null, error: 'MESSAGE_CONTAINER_AMBIGUOUS', count: explicitContainers.length };
    }

    const structuralCandidates = Array.from(targetSurface.children).filter(c => {
        if (!c.offsetParent) return false;
        if (c.matches('[data-testid="agent-input-box"], #antigravity\\.agentSidePanelInputBox')) return false;
        if (c.querySelector('[data-lexical-editor="true"]')) return false;
        const articleCount = c.querySelectorAll('article, [role="article"]').length;
        return articleCount >= 1;
    });

    if (structuralCandidates.length === 1) {
        return { container: structuralCandidates[0], variant: 'LIVE_STRUCTURAL_DIRECT_CHILD', error: null };
    } else if (structuralCandidates.length > 1) {
        return { container: null, error: 'MESSAGE_CONTAINER_AMBIGUOUS', count: structuralCandidates.length };
    }

    return { container: null, error: 'MESSAGE_CONTAINER_NOT_FOUND' };
}

function qualifyStopControl(targetSurface) {
    if (!targetSurface) return { found: false };
    const stopBtn = Array.from(targetSurface.querySelectorAll('button')).find(b => {
        if (b.closest('[data-testid="conversation-list-sidebar"]')) return false;
        const label = (b.getAttribute('aria-label') || '').toLowerCase();
        const text = (b.textContent || '').toLowerCase();
        return (label.includes('stop') || text.includes('stop')) && !!b.offsetParent;
    });
    if (stopBtn) {
        return { found: true, label: stopBtn.getAttribute('aria-label') || 'Stop' };
    }
    return { found: false };
}
"""

class QualifiedAntigravityClient:
    def __init__(self, endpoint, timeout=30):
        self.endpoint = endpoint
        self.timeout = timeout
        self.ws = None
        self.target = None
        self._msg_id = 0
        self._connection_count = 0

    async def connect_and_qualify(self):
        if self.ws:
            return self.target, "APP_PAGE_QUALIFIED"

        self._connection_count += 1
        try:
            url = f"{self.endpoint}/json/list"
            req = urllib.request.Request(url, headers={"User-Agent": "SwitchAntigravity-T03/6.0"})
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
                    probe_id = 1001
                    probe_payload = {
                        "id": probe_id,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": """(() => {
                                const hasSidebar = !!document.querySelector('[data-testid="conversation-list-sidebar"]');
                                const hasComposer = !!document.querySelector('[data-lexical-editor="true"]');
                                const hasAppConfig = !!(window.__APP_CONFIG__ && window.__APP_CONFIG__.productName === "antigravity");
                                const pathname = window.location.pathname;
                                const urlHasConvo = pathname.startsWith('/c/');
                                return {
                                    hasSidebar: hasSidebar,
                                    hasComposer: hasComposer,
                                    hasAppConfig: hasAppConfig,
                                    urlHasConvo: urlHasConvo,
                                    pathname: pathname
                                };
                            })()""",
                            "returnByValue": True
                        }
                    }
                    await test_ws.send(json.dumps(probe_payload))
                    
                    res_val = None
                    start_recv = time.time()
                    while time.time() - start_recv < 3.0:
                        raw_resp = await asyncio.wait_for(test_ws.recv(), timeout=3.0)
                        msg = json.loads(raw_resp)
                        if msg.get("id") == probe_id:
                            res_val = msg.get("result", {}).get("result", {}).get("value", {})
                            break

                    if not res_val:
                        continue

                    signals = sum([
                        1 if res_val.get("hasSidebar") else 0,
                        1 if res_val.get("hasComposer") else 0,
                        1 if res_val.get("hasAppConfig") else 0
                    ])
                    if signals >= 2 or (signals >= 1 and res_val.get("urlHasConvo")):
                        qualified.append((candidate, res_val))
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
        if not self.ws:
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
        script = r"""
        (() => {
            const rows = Array.from(document.querySelectorAll('[data-testid="conversation-row-sidebar"]'));
            return rows.map((row, idx) => {
                const link = row.closest('a') || row.querySelector('a') || (row.parentElement && row.parentElement.querySelector('a'));
                let href = link ? link.getAttribute('href') : null;
                const titleTrigger = row.querySelector('[data-testid="lifted-context-menu-trigger"]');
                const title = titleTrigger ? titleTrigger.textContent.trim() : row.textContent.trim();
                
                let uuid = null;
                if (href) {
                    try {
                        const parsedUrl = new URL(href, window.location.origin);
                        const match = parsedUrl.pathname.match(/^\/c\/([0-9a-fA-F-]{36})$/);
                        if (match) uuid = match[1].toLowerCase();
                    } catch (e) {
                        const match = href.match(/^\/c\/([0-9a-fA-F-]{36})$/);
                        if (match) uuid = match[1].toLowerCase();
                    }
                }
                const pathname = window.location.pathname.toLowerCase();
                const isActive = !!(uuid && pathname === '/c/' + uuid);
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
        val_uuid = validate_uuid(target_uuid)
        click_script = f"""
        ((targetUuid) => {{
            const rows = Array.from(document.querySelectorAll('[data-testid="conversation-row-sidebar"]'));
            for (const row of rows) {{
                const link = row.closest('a') || row.querySelector('a') || (row.parentElement && row.parentElement.querySelector('a'));
                if (link) {{
                    const href = link.getAttribute('href') || '';
                    let rowUuid = null;
                    try {{
                        const parsed = new URL(href, window.location.origin);
                        const match = parsed.pathname.match(/^\\/c\\/([0-9a-fA-F-]{{36}})$/);
                        if (match) rowUuid = match[1].toLowerCase();
                    }} catch(e) {{
                        const match = href.match(/^\\/c\\/([0-9a-fA-F-]{{36}})$/);
                        if (match) rowUuid = match[1].toLowerCase();
                    }}
                    if (rowUuid === targetUuid.toLowerCase()) {{
                        link.click();
                        return {{ clicked: true, method: "sidebar_row_click" }};
                    }}
                }}
            }}
            window.location.pathname = '/c/' + targetUuid.toLowerCase();
            return {{ clicked: true, method: "pathname_nav" }};
        }})({json.dumps(val_uuid)})
        """
        await self.evaluate(click_script)

        start = time.time()
        while time.time() - start < timeout:
            check = await self.evaluate(f"""
            ((targetUuid) => {{
                {DOM_QUALIFICATION_JS}
                const targetRes = qualifyTargetSurface(targetUuid);
                const compRes = qualifyComposerSurface(targetRes.surface);
                const isExactMatch = !targetRes.error;
                const composerMounted = !compRes.error && !!compRes.editor;
                return {{
                    isExactMatch: isExactMatch,
                    composerMounted: composerMounted,
                    currentPathname: window.location.pathname,
                    targetError: targetRes.error,
                    composerError: compRes.error
                }};
            }})({json.dumps(val_uuid)})
            """)
            if check.get("isExactMatch") and check.get("composerMounted"):
                return {"status": "CONVERSATION_SWITCH_VERIFIED", "elapsed": round(time.time() - start, 2)}
            elif check.get("composerMounted") and not check.get("isExactMatch") and check.get("currentPathname", "").startswith('/c/'):
                if time.time() - start > 2.0:
                    return {"status": "CONVERSATION_SWITCH_WRONG_TARGET", "currentPathname": check.get("currentPathname")}
            await asyncio.sleep(0.2)

        return {"status": "CONVERSATION_SWITCH_TIMEOUT"}

    async def inspect_scoped_conversation_state(self, target_uuid, prompt_hash, baseline_article_count=0):
        val_uuid = validate_uuid(target_uuid)
        script = rf"""
        ((targetUuid, baselineCount) => {{
            {DOM_QUALIFICATION_JS}
            const targetRes = qualifyTargetSurface(targetUuid);
            if (targetRes.error) {{
                return {{ error: targetRes.error, currentPathname: window.location.pathname }};
            }}
            const targetSurface = targetRes.surface;

            const msgRes = qualifyMessageSurface(targetSurface);
            if (msgRes.error) {{
                return {{ error: msgRes.error, count: msgRes.count }};
            }}
            const messageContainer = msgRes.container;
            const articles = Array.from(messageContainer.querySelectorAll('article, [role="article"]'));
            
            const stopBtn = qualifyStopControl(targetSurface);
            const isMainTurnActive = stopBtn.found;

            const userMessages = [];
            const assistantMessages = [];
            let hasUnknownRole = false;
            let lastMessageIsUnknown = false;
            let newQuotaError = false;
            let newGenericError = false;

            articles.forEach((art, idx) => {{
                const authorAttr = (art.getAttribute('data-author') || '').toLowerCase();
                const isUserClass = art.classList.contains('user-message') || !!art.querySelector('[data-testid="user-message"], [data-testid="user-turn"]');
                const isAssistClass = art.classList.contains('assistant-message') || !!art.querySelector('[data-testid="assistant-message"], [data-testid="assistant-turn"]');
                const text = (art.textContent || '').trim();

                const isNewNode = (idx >= baselineCount);
                if (isNewNode) {{
                    const textLower = text.toLowerCase();
                    if (textLower.includes('quota exceeded') || textLower.includes('rate limit') || textLower.includes('resource_exhausted')) {{
                        newQuotaError = true;
                    }}
                    if (art.querySelector('.markdown-alert-danger, [data-testid="error-banner"]')) {{
                        newGenericError = true;
                    }}
                }}

                if (authorAttr === 'user' || isUserClass) {{
                    userMessages.push(text);
                    if (idx === articles.length - 1) lastMessageIsUnknown = false;
                }} else if (authorAttr === 'assistant' || isAssistClass) {{
                    assistantMessages.push(text);
                    if (idx === articles.length - 1) lastMessageIsUnknown = false;
                }} else {{
                    hasUnknownRole = true;
                    if (idx === articles.length - 1) lastMessageIsUnknown = true;
                }}
            }});

            const lastUserMsg = userMessages.length > 0 ? userMessages[userMessages.length - 1] : null;
            const lastAssistantMsg = assistantMessages.length > 0 ? assistantMessages[assistantMessages.length - 1] : null;

            return {{
                totalArticles: articles.length,
                userMessageCount: userMessages.length,
                assistantMessageCount: assistantMessages.length,
                userMessages: userMessages,
                lastUserMessageText: lastUserMsg,
                lastAssistantMessageText: lastAssistantMsg,
                hasUnknownRole: hasUnknownRole,
                lastMessageIsUnknown: lastMessageIsUnknown,
                newQuotaError: newQuotaError,
                newGenericError: newGenericError,
                isMainTurnActive: isMainTurnActive,
                mainStopButtonCount: isMainTurnActive ? 1 : 0,
                isConversationEmptyOrIdle: (articles.length === 0 || (userMessages.length === 0 && !isMainTurnActive))
            }};
        }})({json.dumps(val_uuid)}, {baseline_article_count})
        """
        raw_state = await self.evaluate(script)
        if raw_state.get("error"):
            return raw_state

        dup_status, last_hash = classify_duplicate_state(
            raw_state.get("userMessages", []),
            prompt_hash,
            raw_state.get("hasUnknownRole", False),
            raw_state.get("lastMessageIsUnknown", False)
        )
        raw_state["duplicateStatus"] = dup_status
        raw_state["lastUserMessageHash"] = last_hash
        return raw_state

    async def inspect_composer_state(self, target_uuid=None):
        val_uuid = validate_uuid(target_uuid) if target_uuid else None
        script = f"""
        ((targetUuid) => {{
            {DOM_QUALIFICATION_JS}
            const targetRes = qualifyTargetSurface(targetUuid);
            if (targetRes.error) {{
                return {{ found: false, error: targetRes.error, currentPathname: window.location.pathname }};
            }}
            const targetSurface = targetRes.surface;

            const compRes = qualifyComposerSurface(targetSurface);
            if (compRes.error) {{
                return {{ found: false, error: compRes.error, count: compRes.count }};
            }}
            const editor = compRes.editor;
            const stopBtn = qualifyStopControl(targetSurface);

            const text = (editor.innerText || editor.textContent || '').trim();
            return {{
                found: true,
                role: editor.getAttribute('role'),
                ariaLabel: editor.getAttribute('aria-label'),
                text: text,
                draftPresent: text.length > 0,
                isFocused: document.activeElement === editor,
                sendButton: compRes.sendButton,
                stopButton: stopBtn
            }};
        }})({json.dumps(val_uuid)})
        """
        return await self.evaluate(script)

    async def clear_composer(self, target_uuid):
        val_uuid = validate_uuid(target_uuid)
        focus_res = await self.evaluate(f"""
        ((targetUuid) => {{
            {DOM_QUALIFICATION_JS}
            const targetRes = qualifyTargetSurface(targetUuid);
            if (targetRes.error) {{
                return {{ focused: false, error: targetRes.error }};
            }}
            const compRes = qualifyComposerSurface(targetRes.surface);
            if (compRes.error) {{
                return {{ focused: false, error: compRes.error }};
            }}
            const editor = compRes.editor;
            editor.focus();
            return {{ focused: (document.activeElement === editor) }};
        }})({json.dumps(val_uuid)})
        """)
        if not focus_res.get("focused"):
            raise RuntimeError(f"Could not focus composer: {focus_res.get('error')}")

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

    async def insert_prompt_text(self, target_uuid, text):
        val_uuid = validate_uuid(target_uuid)
        focus_res = await self.evaluate(f"""
        ((targetUuid) => {{
            {DOM_QUALIFICATION_JS}
            const targetRes = qualifyTargetSurface(targetUuid);
            if (targetRes.error) {{
                return {{ focused: false, error: targetRes.error }};
            }}
            const compRes = qualifyComposerSurface(targetRes.surface);
            if (compRes.error) {{
                return {{ focused: false, error: compRes.error }};
            }}
            const editor = compRes.editor;
            editor.focus();
            return {{ focused: (document.activeElement === editor) }};
        }})({json.dumps(val_uuid)})
        """)
        if not focus_res.get("focused"):
            raise RuntimeError(f"Could not focus composer: {focus_res.get('error')}")

        await asyncio.sleep(0.05)
        await self.send_command("Input.insertText", {"text": text})
        await asyncio.sleep(0.1)

    async def dispatch_submission_input(self, target_uuid, expected_prompt_text):
        """
        Atomic renderer-side verification and send dispatch.
        Verifies exact route, targetSurface, single composer, matching normalized prompt text,
        and exactly ONE enabled send button inside renderer BEFORE clicking.
        Zero irreversible actions on mismatch.
        """
        val_uuid = validate_uuid(target_uuid)
        norm_expected = normalize_text(expected_prompt_text)

        dispatch_res = await self.evaluate(f"""
        ((targetUuid, expectedNormText) => {{
            {DOM_QUALIFICATION_JS}
            const targetRes = qualifyTargetSurface(targetUuid);
            if (targetRes.error) {{
                if (targetRes.error === 'WRONG_CONVERSATION_ACTIVE') {{
                    return {{ safe: false, error: "ROUTE_MUTATED_BEFORE_DISPATCH" }};
                }}
                return {{ safe: false, error: targetRes.error }};
            }}
            const targetSurface = targetRes.surface;
            
            const stopBtn = qualifyStopControl(targetSurface);
            if (stopBtn.found) {{
                return {{ safe: false, error: "TURN_ALREADY_ACTIVE_BEFORE_DISPATCH" }};
            }}

            const compRes = qualifyComposerSurface(targetSurface);
            if (compRes.error) {{
                return {{ safe: false, error: compRes.error }};
            }}
            const editor = compRes.editor;
            const sendBtnState = compRes.sendButton;
            const sendBtn = compRes.sendBtnElement;

            const rawText = (editor.innerText || editor.textContent || '').trim();
            const currentNorm = rawText.split(/\\s+/).filter(Boolean).join(' ');

            if (currentNorm !== expectedNormText) {{
                return {{ safe: false, error: "PROMPT_IDENTITY_MISMATCH", currentText: currentNorm }};
            }}

            if (!sendBtnState || !sendBtnState.found || !sendBtn) {{
                return {{ safe: false, error: sendBtnState.error || "SEND_CONTROL_NOT_FOUND" }};
            }}

            if (sendBtnState.disabled) {{
                return {{ safe: false, error: "SEND_CONTROL_DISABLED" }};
            }}

            // Pre-dispatch assertions passed; execute atomic click
            sendBtn.click();
            return {{ safe: true, dispatched: true, method: "button_click" }};
        }})({json.dumps(val_uuid)}, {json.dumps(norm_expected)})
        """)

        if not dispatch_res.get("safe"):
            return {"dispatched": False, "error": dispatch_res.get("error")}

        return {"dispatched": True, "method": dispatch_res.get("method", "button_click")}

    async def wait_for_user_and_assistant_turn(self, target_uuid, prompt_hash, baseline, timeout=12, external_error_hook=None):
        start = time.time()
        user_msg_observed = False
        assistant_turn_type = "NO_ASSISTANT_TURN"
        baseline_articles = baseline.get("totalArticles", 0)

        while time.time() - start < timeout:
            state = await self.inspect_scoped_conversation_state(target_uuid, prompt_hash, baseline_article_count=baseline_articles)
            
            if not user_msg_observed:
                last_user_hash = state.get("lastUserMessageHash")
                user_msg_delta = state.get("userMessageCount", 0) > baseline.get("userMessageCount", 0)
                if last_user_hash == prompt_hash and user_msg_delta:
                    user_msg_observed = True

            state["assistantMessageDelta"] = state.get("assistantMessageCount", 0) - baseline.get("assistantMessageCount", 0)
            assistant_turn_type = correlate_turn_status(state, baseline, external_error_hook)

            if user_msg_observed and assistant_turn_type in ["ASSISTANT_GENERATION_ACTIVE", "ASSISTANT_GENERATION_COMPLETED", "QUOTA_ERROR_OBSERVED", "ERROR_RESPONSE_OBSERVED"]:
                return {
                    "user_message_observed": True,
                    "assistant_turn_type": assistant_turn_type,
                    "elapsed_seconds": round(time.time() - start, 2),
                    "state": state
                }
            await asyncio.sleep(0.3)

        return {
            "user_message_observed": user_msg_observed,
            "assistant_turn_type": assistant_turn_type,
            "timeout": timeout,
            "elapsed_seconds": round(time.time() - start, 2)
        }

async def execute_resume_pipeline(args, client_override=None, journal_override=None, external_error_hook=None, pre_lock_barrier=None, in_lock_pause_event=None):
    """
    Main execution pipeline for conversation restore and resume submission.
    Guarantees that all authoritative send decisions and forward reconciliations
    occur strictly inside the exclusive cross-process lock using explicit unlocked methods.
    """
    result = {
        "status": "INIT",
        "dry_run": not getattr(args, "send", False),
        "endpoint": None,
        "target_conversation": None,
        "recovery_attempt_id": None,
        "recovery_decision": None,
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

    journal = journal_override or RecoveryJournal(getattr(args, "journal_path", None))

    if getattr(args, "cdp_endpoint", None):
        endpoint = args.cdp_endpoint
        status = "OK"
    else:
        endpoint, status = discover_cdp_endpoint()

    result["endpoint"] = endpoint
    if status != "OK":
        result["status"] = status
        result["errors"].append(f"CDP discovery failed: {status}")
        return result

    prompt_text = getattr(args, "prompt", "").strip() if getattr(args, "prompt", None) else ""
    if not prompt_text:
        result["status"] = "ERROR_EMPTY_PROMPT"
        result["errors"].append("Prompt text cannot be empty.")
        return result

    prompt_hash = hash_text(prompt_text)

    try:
        client = client_override or QualifiedAntigravityClient(endpoint=endpoint, timeout=getattr(args, "timeout", 30))
        _, qual_status = await client.connect_and_qualify()
        if qual_status != "APP_PAGE_QUALIFIED":
            result["status"] = qual_status
            result["errors"].append(f"Target page qualification failed: {qual_status}")
            return result

        async with client:
            convos = await client.list_conversations()

            target = None
            is_send_mode = getattr(args, "send", False)

            if is_send_mode:
                if not getattr(args, "conversation_id", None):
                    result["status"] = "UUID_REQUIRED_FOR_SEND"
                    result["errors"].append("Real send mode requires an explicit, verified conversation UUID (--conversation-id/--uuid). Title-only and implicit active conversation selection are strictly forbidden for send.")
                    return result
                val_target_uuid = validate_uuid(args.conversation_id)
                matches = [c for c in convos if c.get("uuid") and c.get("uuid") == val_target_uuid]
                if not matches:
                    result["status"] = "CONVERSATION_NOT_FOUND"
                    result["errors"].append(f"Conversation UUID '{args.conversation_id}' not found in active UI.")
                    return result
                target = matches[0]
            else:
                if getattr(args, "conversation_id", None):
                    val_target_uuid = validate_uuid(args.conversation_id)
                    matches = [c for c in convos if c.get("uuid") and c.get("uuid") == val_target_uuid]
                    if not matches:
                        result["status"] = "CONVERSATION_NOT_FOUND"
                        result["errors"].append(f"Conversation UUID '{args.conversation_id}' not found in active UI.")
                        return result
                    target = matches[0]
                elif getattr(args, "title", None):
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
            if getattr(args, "verbose_private_data", False):
                target_display["title"] = target.get("title")
            result["target_conversation"] = target_display

            original_active = next((c for c in convos if c.get("isActive")), None)
            original_uuid = original_active.get("uuid") if original_active else None

            if not target.get("isActive") and target.get("uuid"):
                switch_res = await client.switch_conversation_verified(target["uuid"])
                switch_status = switch_res.get("status")
                if switch_status != "CONVERSATION_SWITCH_VERIFIED":
                    result["status"] = switch_status
                    result["errors"].append(f"Failed to navigate to conversation {target['uuid']}: {switch_status}")
                    return result

            comp_state = await client.inspect_composer_state(target["uuid"])
            if not comp_state.get("found"):
                result["status"] = comp_state.get("error", "COMPOSER_NOT_FOUND")
                result["errors"].append(f"Lexical composer not found in target pane: {comp_state.get('error')}")
                return result
            result["phases"]["1_composer_located"] = True

            scoped_state = await client.inspect_scoped_conversation_state(target["uuid"], prompt_hash)
            if scoped_state.get("error"):
                result["status"] = scoped_state["error"]
                result["errors"].append(f"Scoped state check failed: {scoped_state['error']}")
                return result

            scoped_state["draftPresent"] = comp_state.get("draftPresent", False)

            if not getattr(args, "send", False):
                latest_rec, j_status = journal.get_latest_record(target["uuid"])
                is_first_attempt = (latest_rec is None and j_status == "NOT_FOUND")
                decision_code, decision_reason = evaluate_recovery_permission(
                    latest_record=latest_rec,
                    live_dom_state=scoped_state,
                    prompt_hash=prompt_hash,
                    journal_status=j_status,
                    is_first_attempt=is_first_attempt
                )
                result["recovery_decision"] = {"code": decision_code, "reason": decision_reason}

                dry_run_type = "READ_ONLY"
                if getattr(args, "probe_composer_write", False):
                    if scoped_state.get("draftPresent"):
                        result["status"] = "COMPOSER_DRAFT_PRESENT"
                        result["errors"].append("Composer contains unsubmitted user draft. Probe refused.")
                        return result
                    await client.clear_composer(target["uuid"])
                    await client.insert_prompt_text(target["uuid"], prompt_text)
                    after_ins = await client.inspect_composer_state(target["uuid"])
                    if hash_text(after_ins.get("text")) == prompt_hash:
                        result["phases"]["2_text_inserted"] = True
                        result["phases"]["3_text_verified"] = True
                    await client.clear_composer(target["uuid"])
                    dry_run_type = "WRITE_PROBE"
                    result["status"] = "DRY_RUN_WRITE_PROBE_SUCCESS"
                else:
                    result["status"] = "DRY_RUN_READ_ONLY_SUCCESS"

                if original_uuid and original_uuid != target.get("uuid"):
                    restore_res = await client.switch_conversation_verified(original_uuid)
                    if restore_res.get("status") == "CONVERSATION_SWITCH_VERIFIED":
                        result["dry_run_navigation_restored"] = True
                    else:
                        result["status"] = "DRY_RUN_RESTORE_FAILED"
                        result["dry_run_navigation_restored"] = False
                        result["errors"].append(f"Failed to restore original active conversation: {restore_res.get('status')}")
                elif original_uuid is None:
                    result["dry_run_navigation_restored"] = "NOT_APPLICABLE_NO_PRIOR_ACTIVE"

                result["dry_run_summary"] = {
                    "mode": dry_run_type,
                    "target_uuid_verified": target.get("uuid"),
                    "recovery_decision": decision_code,
                    "duplicate_status": scoped_state.get("duplicateStatus"),
                    "draft_present": scoped_state.get("draftPresent")
                }
                return result

            # Optional synchronization barrier for deterministic race testing
            if pre_lock_barrier is not None:
                await pre_lock_barrier.wait()

            # Production Send Mode: Authoritative decision, reconciliation, reservation & dispatch INSIDE ASYNC LOCK
            async with journal.async_exclusive_lock(conversation_uuid=target["uuid"]):
                # 1. Re-read fresh journal state from disk
                latest_rec, j_status = journal.get_latest_record(target["uuid"])

                # 2. Re-inspect target route & fresh live DOM
                fresh_scoped_state = await client.inspect_scoped_conversation_state(target["uuid"], prompt_hash)
                if fresh_scoped_state.get("error"):
                    result["status"] = fresh_scoped_state["error"]
                    result["errors"].append(f"Scoped DOM re-check failed inside lock: {fresh_scoped_state['error']}")
                    return result

                fresh_comp_state = await client.inspect_composer_state(target["uuid"])
                if not fresh_comp_state.get("found"):
                    result["status"] = fresh_comp_state.get("error", "COMPOSER_NOT_FOUND")
                    result["errors"].append("Composer missing on re-check inside lock.")
                    return result

                fresh_scoped_state["draftPresent"] = fresh_comp_state.get("draftPresent", False)

                # 3. Forward reconcile unconfirmed prior attempt if live DOM confirms it (explicit unlocked method)
                latest_rec, was_reconciled = journal._reconcile_existing_attempt_unlocked(
                    target["uuid"], latest_rec, fresh_scoped_state, prompt_hash
                )
                if was_reconciled:
                    latest_rec, j_status = journal.get_latest_record(target["uuid"])

                is_first_attempt = (latest_rec is None and j_status == "NOT_FOUND")

                # 4. Authoritative recovery permission evaluation
                decision_code, decision_reason = evaluate_recovery_permission(
                    latest_record=latest_rec,
                    live_dom_state=fresh_scoped_state,
                    prompt_hash=prompt_hash,
                    journal_status=j_status,
                    is_first_attempt=is_first_attempt
                )
                result["recovery_decision"] = {"code": decision_code, "reason": decision_reason}

                if decision_code != DECISION_NEW_ATTEMPT_ALLOWED:
                    result["status"] = decision_code
                    result["errors"].append(f"Recovery blocked by safety decision ({decision_code}): {decision_reason}")
                    return result

                if fresh_comp_state.get("draftPresent"):
                    result["status"] = "COMPOSER_DRAFT_PRESENT"
                    result["errors"].append("Composer contains unsubmitted user draft. Send refused.")
                    return result

                if in_lock_pause_event is not None:
                    await in_lock_pause_event.wait()

                # 5. Reserve attempt in NOT_SENT state (explicit unlocked method)
                attempt_rec = journal._start_recovery_attempt_unlocked(target["uuid"], prompt_text)
                attempt_id = attempt_rec["attempt_id"]
                result["recovery_attempt_id"] = attempt_id

                baseline = fresh_scoped_state

                # 6. Insert and verify prompt text
                await client.clear_composer(target["uuid"])
                await client.insert_prompt_text(target["uuid"], prompt_text)
                result["phases"]["2_text_inserted"] = True

                verified_comp = await client.inspect_composer_state(target["uuid"])
                if hash_text(verified_comp.get("text")) != prompt_hash:
                    journal._transition_state_unlocked(target["uuid"], attempt_id, STATE_FAILED, failure_stage="PRE_IRREVERSIBLE", detail="Text insertion verification failed")
                    result["status"] = "TEXT_INSERTION_FAILED"
                    result["errors"].append("Composer text does not match intended prompt.")
                    return result
                result["phases"]["3_text_verified"] = True

                # 7. Durably transition journal to SUBMISSION_ATTEMPTED before dispatch (explicit unlocked method)
                journal._transition_state_unlocked(target["uuid"], attempt_id, STATE_SUBMISSION_ATTEMPTED)

                # 8. Atomic renderer-side dispatch with pre-dispatch revalidation
                try:
                    dispatch_res = await client.dispatch_submission_input(target["uuid"], prompt_text)
                    if not dispatch_res.get("dispatched"):
                        journal._transition_state_unlocked(target["uuid"], attempt_id, STATE_FAILED, failure_stage="PRE_IRREVERSIBLE", detail=f"Dispatch failed pre-irreversible: {dispatch_res.get('error')}")
                        result["status"] = "SEND_INPUT_DISPATCH_FAILED"
                        result["errors"].append(f"Pre-dispatch validation failed: {dispatch_res.get('error')}")
                        return result
                    result["phases"]["4_send_input_dispatched"] = True
                except Exception as de:
                    # CDP disconnect, renderer crash, or exception during dispatch:
                    # State remains SUBMISSION_ATTEMPTED or transitions to FAILED with POST_IRREVERSIBLE_UNKNOWN
                    # to strictly forbid blind resend.
                    journal._transition_state_unlocked(target["uuid"], attempt_id, STATE_FAILED, failure_stage="POST_IRREVERSIBLE_UNKNOWN", detail=f"Dispatch exception: {str(de)}")
                    result["status"] = "SEND_INPUT_DISPATCH_EXCEPTION"
                    result["errors"].append(f"Exception during send dispatch: {str(de)}")
                    return result

            # Outside lock: Observe post-dispatch turns with public locked transitions
            turn_res = await client.wait_for_user_and_assistant_turn(
                target["uuid"], prompt_hash, baseline, timeout=getattr(args, "timeout", 30), external_error_hook=external_error_hook
            )
            
            if turn_res.get("user_message_observed"):
                result["phases"]["5_user_message_observed"] = True
                await journal.transition_state_async(target["uuid"], attempt_id, STATE_MESSAGE_OBSERVED)

            assistant_type = turn_res.get("assistant_turn_type", "NO_ASSISTANT_TURN")
            if assistant_type in ["ASSISTANT_GENERATION_ACTIVE", "ASSISTANT_GENERATION_COMPLETED"]:
                result["phases"]["6_assistant_turn_started"] = True
                await journal.transition_state_async(target["uuid"], attempt_id, STATE_TURN_STARTED)
                result["status"] = "TURN_STARTED"
            elif assistant_type == "QUOTA_ERROR_OBSERVED":
                await journal.transition_state_async(target["uuid"], attempt_id, STATE_FAILED, failure_stage="POST_IRREVERSIBLE_UNKNOWN", detail="API quota exhausted")
                result["status"] = "QUOTA_ERROR_OBSERVED"
                result["errors"].append("Turn started but immediately hit API quota limits.")
            elif assistant_type == "ERROR_RESPONSE_OBSERVED":
                await journal.transition_state_async(target["uuid"], attempt_id, STATE_FAILED, failure_stage="POST_IRREVERSIBLE_UNKNOWN", detail="API error received")
                result["status"] = "ERROR_RESPONSE_OBSERVED"
            elif turn_res.get("user_message_observed"):
                result["status"] = "USER_MESSAGE_OBSERVED_ASSISTANT_PENDING"
            else:
                await journal.transition_state_async(target["uuid"], attempt_id, STATE_DISPATCHED_UNCONFIRMED, failure_stage="POST_IRREVERSIBLE_UNKNOWN", detail="Post-dispatch confirmation timeout")
                result["status"] = "DISPATCHED_UNCONFIRMED"
                result["errors"].append("Send input was dispatched, but DOM confirmation timed out. Resend strictly blocked.")

    except JournalDurabilityError as jde:
        result["status"] = "JOURNAL_DURABILITY_FAILED"
        result["errors"].append(str(jde))
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

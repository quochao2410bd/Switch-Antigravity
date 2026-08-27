#!/usr/bin/env python3
"""
Persistent Recovery Journal for Antigravity Resume Adapter (T03 Prototype)

Manages crash-recovery state for conversation resume attempts.
Stores strictly non-sensitive metadata (UUIDs, state, prompt SHA-256, timestamps).

Review Round 6 Hardening:
- Non-reentrant explicit locking architecture:
    * Public methods (start_recovery_attempt, transition_state) acquire exclusive lock.
    * Explicit unlocked methods (_start_recovery_attempt_unlocked, _transition_state_unlocked, _reconcile_unlocked)
      are strictly used when the lock is already held by execute_resume_pipeline.
    * No unsafe thread-blind/depth-counter reentrancy bypasses.
- Tri-state process liveness checker (PROCESS_ALIVE, PROCESS_DEAD_CONFIRMED, PROCESS_LIVENESS_UNKNOWN).
- PID reuse mitigation via process creation timestamp / identity metadata and lock nonce.
- Stale locks from dead PIDs reclaimed only after dead status is confirmed; always re-reads journal state.
- Durability barriers (fsync failure raises JournalDurabilityError).
"""

import asyncio
import contextlib
import ctypes
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid

SCHEMA_VERSION = 2

STATE_NOT_SENT = "NOT_SENT"
STATE_SUBMISSION_ATTEMPTED = "SUBMISSION_ATTEMPTED"
STATE_DISPATCHED_UNCONFIRMED = "DISPATCHED_UNCONFIRMED"
STATE_MESSAGE_OBSERVED = "MESSAGE_OBSERVED"
STATE_TURN_STARTED = "TURN_STARTED"
STATE_TURN_ACTIVE = "TURN_ACTIVE"
STATE_FAILED = "FAILED"

VALID_STATES = {
    STATE_NOT_SENT,
    STATE_SUBMISSION_ATTEMPTED,
    STATE_DISPATCHED_UNCONFIRMED,
    STATE_MESSAGE_OBSERVED,
    STATE_TURN_STARTED,
    STATE_TURN_ACTIVE,
    STATE_FAILED
}

VALID_FAILURE_STAGES = {
    "PRE_IRREVERSIBLE",
    "POST_IRREVERSIBLE_UNKNOWN"
}

ALLOWED_TRANSITIONS = {
    STATE_NOT_SENT: {STATE_SUBMISSION_ATTEMPTED, STATE_FAILED},
    STATE_SUBMISSION_ATTEMPTED: {STATE_MESSAGE_OBSERVED, STATE_DISPATCHED_UNCONFIRMED, STATE_FAILED},
    STATE_DISPATCHED_UNCONFIRMED: {STATE_MESSAGE_OBSERVED, STATE_FAILED},
    STATE_MESSAGE_OBSERVED: {STATE_TURN_STARTED, STATE_TURN_ACTIVE, STATE_FAILED},
    STATE_TURN_STARTED: {STATE_TURN_ACTIVE, STATE_FAILED},
    STATE_TURN_ACTIVE: {STATE_FAILED},
    STATE_FAILED: set()
}

DECISION_NEW_ATTEMPT_ALLOWED = "NEW_ATTEMPT_ALLOWED"
DECISION_RESUME_ALREADY_OBSERVED = "RESUME_ALREADY_OBSERVED"
DECISION_TURN_ALREADY_ACTIVE = "TURN_ALREADY_ACTIVE"
DECISION_PREVIOUS_SUBMISSION_UNCONFIRMED = "PREVIOUS_SUBMISSION_UNCONFIRMED"
DECISION_RECOVERY_STATE_UNKNOWN = "RECOVERY_STATE_UNKNOWN"
DECISION_JOURNAL_CORRUPTED = "JOURNAL_CORRUPTED"
DECISION_JOURNAL_SCHEMA_UNSUPPORTED = "JOURNAL_SCHEMA_UNSUPPORTED"
DECISION_MANUAL_RECONCILIATION_REQUIRED = "MANUAL_RECONCILIATION_REQUIRED"
DECISION_BLOCKED_DRAFT_PRESENT = "BLOCKED_DRAFT_PRESENT"
DECISION_CONCURRENT_LOCK_ACTIVE = "CONCURRENT_LOCK_ACTIVE"

LIVENESS_ALIVE = "PROCESS_ALIVE"
LIVENESS_DEAD_CONFIRMED = "PROCESS_DEAD_CONFIRMED"
LIVENESS_UNKNOWN = "PROCESS_LIVENESS_UNKNOWN"

UUID_REGEX = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
SHA256_REGEX = re.compile(r'^[0-9a-fA-F]{64}$')

class JournalDurabilityError(RuntimeError):
    """Raised when the journal durability barrier (fsync/write) fails."""
    pass

class JournalSchemaError(ValueError):
    """Raised when the journal content violates strict semantic schema rules."""
    pass

def get_process_start_identity(pid):
    """
    Returns a unique identifier or creation timestamp for a process PID to mitigate PID reuse.
    On Windows, uses GetProcessTimes. On Unix, reads /proc/<pid>/stat.
    """
    if pid <= 0:
        return None
    if os.name == 'nt':
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle == 0:
            return None
        try:
            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", ctypes.c_uint), ("dwHighDateTime", ctypes.c_uint)]
            creation_time = FILETIME()
            exit_time = FILETIME()
            kernel_time = FILETIME()
            user_time = FILETIME()
            if kernel32.GetProcessTimes(handle, ctypes.byref(creation_time), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return (creation_time.dwHighDateTime << 32) + creation_time.dwLowDateTime
            return None
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            stat_file = f"/proc/{pid}/stat"
            if os.path.exists(stat_file):
                with open(stat_file, "r") as f:
                    parts = f.read().split()
                    if len(parts) > 21:
                        return parts[21]
        except Exception:
            pass
        return None

def check_process_liveness(pid, expected_start_identity=None):
    """
    Tri-state process liveness checker.
    Returns:
      PROCESS_ALIVE - Process is definitely running and start identity matches.
      PROCESS_DEAD_CONFIRMED - Process does not exist (confirmed dead, safe to reclaim).
      PROCESS_LIVENESS_UNKNOWN - Cannot determine status (e.g. Access Denied), fail closed.
    """
    if pid <= 0:
        return LIVENESS_DEAD_CONFIRMED

    if os.name == 'nt':
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle == 0:
            err = kernel32.GetLastError()
            ERROR_INVALID_PARAMETER = 87
            ERROR_NOT_FOUND = 1168
            ERROR_ACCESS_DENIED = 5
            if err in (ERROR_INVALID_PARAMETER, ERROR_NOT_FOUND):
                return LIVENESS_DEAD_CONFIRMED
            elif err == ERROR_ACCESS_DENIED:
                return LIVENESS_UNKNOWN
            return LIVENESS_DEAD_CONFIRMED

        try:
            # Check exit code
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                STILL_ACTIVE = 259
                if exit_code.value != STILL_ACTIVE:
                    return LIVENESS_DEAD_CONFIRMED

            # Check PID reuse if start identity was recorded
            if expected_start_identity is not None:
                current_start = get_process_start_identity(pid)
                if current_start is not None and current_start != expected_start_identity:
                    return LIVENESS_DEAD_CONFIRMED

            return LIVENESS_ALIVE
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            if expected_start_identity is not None:
                current_start = get_process_start_identity(pid)
                if current_start is not None and current_start != expected_start_identity:
                    return LIVENESS_DEAD_CONFIRMED
            return LIVENESS_ALIVE
        except ProcessLookupError:
            return LIVENESS_DEAD_CONFIRMED
        except PermissionError:
            return LIVENESS_UNKNOWN
        except OSError:
            return LIVENESS_UNKNOWN

def validate_uuid(convo_uuid):
    if not convo_uuid or not isinstance(convo_uuid, str) or not UUID_REGEX.match(convo_uuid.strip()):
        raise ValueError(f"Invalid UUID format: {convo_uuid}")
    return convo_uuid.strip().lower()

def validate_prompt_sha(sha_str):
    if not sha_str or not isinstance(sha_str, str) or not SHA256_REGEX.match(sha_str.strip()):
        raise ValueError(f"Invalid SHA-256 hash: {sha_str}")
    return sha_str.strip().lower()

def get_default_journal_path():
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        base_dir = os.path.join(local_appdata, "SwitchAntigravity")
    else:
        base_dir = os.path.join(os.path.expanduser("~"), ".switch_antigravity")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "t03_recovery_journal.json")

def hash_prompt(prompt_text):
    if not prompt_text:
        return ""
    normalized = " ".join(prompt_text.strip().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

def validate_journal_schema(data):
    if not isinstance(data, dict):
        raise JournalSchemaError("Journal root must be a JSON object")

    ver = data.get("version")
    if ver != SCHEMA_VERSION:
        raise JournalSchemaError(f"Unsupported journal schema version: {ver} (expected {SCHEMA_VERSION})")

    records = data.get("records")
    if not isinstance(records, dict):
        raise JournalSchemaError("Journal 'records' must be a dictionary")

    for convo_key, record_list in records.items():
        val_key = validate_uuid(convo_key)
        if not isinstance(record_list, list):
            raise JournalSchemaError(f"Record list for conversation '{convo_key}' must be an array")

        for r in record_list:
            if not isinstance(r, dict):
                raise JournalSchemaError(f"Record in '{convo_key}' must be a JSON object")

            attempt_id = r.get("attempt_id")
            validate_uuid(attempt_id)

            r_convo_uuid = r.get("conversation_uuid")
            if validate_uuid(r_convo_uuid) != val_key:
                raise JournalSchemaError(f"Record conversation_uuid mismatch: {r_convo_uuid} vs key {val_key}")

            state = r.get("state")
            if state not in VALID_STATES:
                raise JournalSchemaError(f"Record state '{state}' is invalid. Must be one of {VALID_STATES}")

            prompt_sha = r.get("prompt_sha256")
            validate_prompt_sha(prompt_sha)

            created_at = r.get("created_at_utc")
            updated_at = r.get("updated_at_utc")
            if not isinstance(created_at, (int, float)) or created_at <= 0:
                raise JournalSchemaError(f"Invalid created_at_utc timestamp: {created_at}")
            if not isinstance(updated_at, (int, float)) or updated_at < created_at:
                raise JournalSchemaError(f"Invalid updated_at_utc timestamp: {updated_at}")

            failure_stage = r.get("failure_stage")
            if failure_stage is not None and failure_stage not in VALID_FAILURE_STAGES:
                raise JournalSchemaError(f"Invalid failure_stage: {failure_stage}")

            history = r.get("history")
            if not isinstance(history, list):
                raise JournalSchemaError(f"Record history for {attempt_id} must be a list")

            for h in history:
                if not isinstance(h, dict):
                    raise JournalSchemaError("History entry must be a dictionary")
                h_state = h.get("state")
                if h_state not in VALID_STATES:
                    raise JournalSchemaError(f"Invalid history state: {h_state}")
                h_ts = h.get("timestamp")
                if not isinstance(h_ts, (int, float)) or h_ts <= 0:
                    raise JournalSchemaError(f"Invalid history timestamp: {h_ts}")

    return True

def validate_lock_metadata(lock_meta):
    """Strictly validates lock file metadata structure and types."""
    if not isinstance(lock_meta, dict):
        return False, "Lock metadata root must be an object"
    owner_pid = lock_meta.get("owner_pid")
    if not isinstance(owner_pid, int) or owner_pid <= 0:
        return False, "Invalid or missing owner_pid"
    nonce = lock_meta.get("lock_nonce")
    if not nonce or not isinstance(nonce, str):
        return False, "Invalid or missing lock_nonce"
    created_at = lock_meta.get("created_at")
    if not isinstance(created_at, (int, float)) or created_at <= 0:
        return False, "Invalid or missing created_at"
    return True, "OK"

def evaluate_recovery_permission(latest_record, live_dom_state, prompt_hash, journal_status="OK", is_first_attempt=False):
    """
    Authoritative evaluation of recovery permission.
    Returns (decision_code, explanation).
    """
    if journal_status == "SCHEMA_UNSUPPORTED":
        return DECISION_JOURNAL_SCHEMA_UNSUPPORTED, "Journal schema version is unsupported. Fail closed."
    if journal_status in ["CORRUPTED", "SCHEMA_INVALID"]:
        return DECISION_JOURNAL_CORRUPTED, "Recovery journal is corrupted or semantically invalid. Fail closed."

    if not live_dom_state or not isinstance(live_dom_state, dict):
        return DECISION_RECOVERY_STATE_UNKNOWN, "Live DOM state could not be inspected."

    if live_dom_state.get("error"):
        return DECISION_RECOVERY_STATE_UNKNOWN, f"Live DOM inspection error: {live_dom_state.get('error')}"

    if live_dom_state.get("isMainTurnActive"):
        return DECISION_TURN_ALREADY_ACTIVE, "Target conversation has an active turn executing (Stop button present)."

    if live_dom_state.get("draftPresent"):
        return DECISION_BLOCKED_DRAFT_PRESENT, "Composer contains an existing unsubmitted user draft. Overwriting refused."

    dom_duplicate_status = live_dom_state.get("duplicateStatus")
    last_user_hash = live_dom_state.get("lastUserMessageHash")

    if dom_duplicate_status == "RESUME_MESSAGE_PRESENT" or (last_user_hash and last_user_hash == prompt_hash):
        return DECISION_RESUME_ALREADY_OBSERVED, "Intended resume prompt is already present in conversation history."

    if latest_record:
        prev_state = latest_record.get("state")
        prev_hash = latest_record.get("prompt_sha256")
        same_prompt = (prev_hash == prompt_hash)

        if prev_state in [STATE_SUBMISSION_ATTEMPTED, STATE_DISPATCHED_UNCONFIRMED]:
            if same_prompt and last_user_hash == prompt_hash:
                return DECISION_RESUME_ALREADY_OBSERVED, "Previous attempt was submitted and confirmed in DOM."
            return DECISION_PREVIOUS_SUBMISSION_UNCONFIRMED, (
                f"Previous recovery attempt was in state '{prev_state}'. "
                "DOM state is unconfirmed. Blind resend is strictly blocked."
            )

        if prev_state == STATE_MESSAGE_OBSERVED:
            return DECISION_RESUME_ALREADY_OBSERVED, "Previous recovery message was already confirmed observed in DOM."

        if prev_state in [STATE_TURN_STARTED, STATE_TURN_ACTIVE]:
            return DECISION_TURN_ALREADY_ACTIVE, f"Previous recovery turn was recorded in active state '{prev_state}'."

        if prev_state == STATE_FAILED:
            failure_stage = latest_record.get("failure_stage")
            if failure_stage == "PRE_IRREVERSIBLE" and dom_duplicate_status == "RESUME_NOT_PRESENT":
                return DECISION_NEW_ATTEMPT_ALLOWED, "Previous failure was confirmed pre-irreversible and target is clean."
            return DECISION_MANUAL_RECONCILIATION_REQUIRED, (
                f"Previous attempt is FAILED with unconfirmed failure stage '{failure_stage}'. "
                "Manual reconciliation required; blind resend forbidden."
            )

    if dom_duplicate_status == "DUPLICATE_STATE_UNKNOWN":
        if is_first_attempt and not latest_record and live_dom_state.get("isConversationEmptyOrIdle"):
            return DECISION_NEW_ATTEMPT_ALLOWED, "First attempt on proven idle conversation with clean journal."
        return DECISION_RECOVERY_STATE_UNKNOWN, "Conversation duplicate status is unknown. Fail closed."

    if dom_duplicate_status == "RESUME_NOT_PRESENT":
        return DECISION_NEW_ATTEMPT_ALLOWED, "Verified target is idle, no draft, and resume prompt not present in history."

    return DECISION_RECOVERY_STATE_UNKNOWN, f"Unhandled duplicate status '{dom_duplicate_status}'. Fail closed."

class RecoveryJournal:
    def __init__(self, journal_path=None):
        self.journal_path = journal_path or get_default_journal_path()
        self.lock_path = f"{self.journal_path}.lock"
        self._is_corrupted = False
        self._schema_unsupported = False

    @contextlib.asynccontextmanager
    async def async_exclusive_lock(self, timeout=5.0, conversation_uuid=None):
        """
        Async-safe non-blocking cross-process advisory lock.
        Uses await asyncio.sleep() to yield control to the event loop on contention.
        """
        target_dir = os.path.dirname(os.path.abspath(self.lock_path))
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)
        start_time = time.time()
        lock_fd = None
        current_pid = os.getpid()
        current_start_id = get_process_start_identity(current_pid)
        nonce = str(uuid.uuid4())

        while time.time() - start_time < timeout:
            try:
                lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                meta = {
                    "owner_pid": current_pid,
                    "start_identity": current_start_id,
                    "lock_nonce": nonce,
                    "created_at": time.time(),
                    "conversation_uuid": conversation_uuid
                }
                os.write(lock_fd, json.dumps(meta).encode("utf-8"))
                break
            except OSError:
                try:
                    if os.path.exists(self.lock_path):
                        with open(self.lock_path, "r", encoding="utf-8") as f:
                            lock_meta = json.load(f)
                        valid, _ = validate_lock_metadata(lock_meta)
                        if valid:
                            owner_pid = lock_meta.get("owner_pid")
                            start_id = lock_meta.get("start_identity")
                            liveness = check_process_liveness(owner_pid, start_id)

                            if liveness == LIVENESS_DEAD_CONFIRMED:
                                try:
                                    os.remove(self.lock_path)
                                    continue
                                except OSError:
                                    pass
                except Exception:
                    pass
                await asyncio.sleep(0.05)

        if lock_fd is None:
            raise TimeoutError(f"Could not acquire journal lock '{self.lock_path}' within {timeout}s")

        try:
            yield
        finally:
            try:
                os.close(lock_fd)
                if os.path.exists(self.lock_path):
                    os.remove(self.lock_path)
            except OSError:
                pass

    @contextlib.contextmanager
    def exclusive_lock(self, timeout=5.0, conversation_uuid=None):
        """
        Non-reentrant cross-process advisory lock.
        Writes ownership metadata (owner_pid, start_identity, lock_nonce, timestamp, conversation_uuid).
        Safely detects and reclaims stale locks strictly when owner PID is confirmed dead.
        """
        target_dir = os.path.dirname(os.path.abspath(self.lock_path))
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)
        start_time = time.time()
        lock_fd = None
        current_pid = os.getpid()
        current_start_id = get_process_start_identity(current_pid)
        nonce = str(uuid.uuid4())

        while time.time() - start_time < timeout:
            try:
                lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                meta = {
                    "owner_pid": current_pid,
                    "start_identity": current_start_id,
                    "lock_nonce": nonce,
                    "created_at": time.time(),
                    "conversation_uuid": conversation_uuid
                }
                os.write(lock_fd, json.dumps(meta).encode("utf-8"))
                break
            except OSError:
                # Lock file exists; verify owner liveness
                try:
                    if os.path.exists(self.lock_path):
                        with open(self.lock_path, "r", encoding="utf-8") as f:
                            lock_meta = json.load(f)
                        owner_pid = lock_meta.get("owner_pid")
                        start_id = lock_meta.get("start_identity")
                        liveness = check_process_liveness(owner_pid, start_id)

                        if liveness == LIVENESS_DEAD_CONFIRMED:
                            try:
                                os.remove(self.lock_path)
                                continue
                            except OSError:
                                pass
                except Exception:
                    pass
                time.sleep(0.05)

        if lock_fd is None:
            raise TimeoutError(f"Could not acquire journal lock '{self.lock_path}' within {timeout}s")

        try:
            yield
        finally:
            try:
                os.close(lock_fd)
                if os.path.exists(self.lock_path):
                    os.remove(self.lock_path)
            except OSError:
                pass

    def _read_raw(self):
        """Read, validate schema, and return (data_dict, status_str)."""
        if self._schema_unsupported:
            return {"version": SCHEMA_VERSION, "records": {}, "error": "Journal schema version is unsupported"}, "SCHEMA_UNSUPPORTED"
        if self._is_corrupted:
            return {"version": SCHEMA_VERSION, "records": {}, "error": "Journal is marked corrupted"}, "CORRUPTED"

        if not os.path.exists(self.journal_path):
            return {"version": SCHEMA_VERSION, "records": {}}, "NOT_FOUND"

        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, dict) and data.get("version") != SCHEMA_VERSION:
                self._schema_unsupported = True
                return data, "SCHEMA_UNSUPPORTED"

            validate_journal_schema(data)
            return data, "OK"
        except JournalSchemaError as jse:
            self._is_corrupted = True
            corrupt_path = f"{self.journal_path}.corrupt.{int(time.time())}"
            try:
                os.replace(self.journal_path, corrupt_path)
            except Exception:
                pass
            return {"version": SCHEMA_VERSION, "records": {}, "corrupted_backup": corrupt_path, "error": str(jse)}, "SCHEMA_INVALID"
        except Exception as e:
            self._is_corrupted = True
            corrupt_path = f"{self.journal_path}.corrupt.{int(time.time())}"
            try:
                os.replace(self.journal_path, corrupt_path)
            except Exception:
                pass
            return {"version": SCHEMA_VERSION, "records": {}, "corrupted_backup": corrupt_path, "error": str(e)}, "CORRUPTED"

    def _write_atomic(self, data):
        """
        Durably persist data using temp file, flush, fsync, and atomic replace.
        Fsync failures raise JournalDurabilityError and abort before send.
        """
        validate_journal_schema(data)
        target_dir = os.path.dirname(os.path.abspath(self.journal_path))
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix="t03_journal_", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError as ose:
                    raise JournalDurabilityError(f"JOURNAL_DURABILITY_FAILED: fsync failed: {ose}") from ose
            os.replace(temp_path, self.journal_path)
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

    def get_latest_record(self, conversation_uuid):
        val_uuid = validate_uuid(conversation_uuid)
        data, status = self._read_raw()
        if status in ["CORRUPTED", "SCHEMA_INVALID", "SCHEMA_UNSUPPORTED"]:
            return None, status
        records = data.get("records", {})
        convo_records = records.get(val_uuid, [])
        if not convo_records:
            return None, status
        return convo_records[-1], status

    # Explicit unlocked methods for use strictly inside with exclusive_lock():
    def _start_recovery_attempt_unlocked(self, conversation_uuid, prompt_text):
        val_uuid = validate_uuid(conversation_uuid)
        data, status = self._read_raw()
        if status in ["CORRUPTED", "SCHEMA_INVALID", "SCHEMA_UNSUPPORTED"]:
            raise RuntimeError(f"Cannot start recovery attempt: journal is in invalid state '{status}'. Manual reconciliation required.")

        attempt_id = str(uuid.uuid4())
        prompt_sha = hash_prompt(prompt_text)
        now = time.time()

        record = {
            "attempt_id": attempt_id,
            "conversation_uuid": val_uuid,
            "state": STATE_NOT_SENT,
            "prompt_sha256": prompt_sha,
            "created_at_utc": now,
            "updated_at_utc": now,
            "failure_stage": None,
            "history": [
                {"state": STATE_NOT_SENT, "timestamp": now}
            ]
        }

        if val_uuid not in data["records"]:
            data["records"][val_uuid] = []
        data["records"][val_uuid].append(record)

        self._write_atomic(data)
        return record

    def _transition_state_unlocked(self, conversation_uuid, attempt_id, new_state, failure_stage=None, detail=None):
        val_uuid = validate_uuid(conversation_uuid)
        if new_state not in VALID_STATES:
            raise ValueError(f"Invalid recovery state: {new_state}")
        if failure_stage is not None and failure_stage not in VALID_FAILURE_STAGES:
            raise ValueError(f"Invalid failure stage: {failure_stage}")

        data, status = self._read_raw()
        if status in ["CORRUPTED", "SCHEMA_INVALID", "SCHEMA_UNSUPPORTED"]:
            raise RuntimeError(f"Cannot transition state: journal is in invalid state '{status}'.")

        records = data.get("records", {}).get(val_uuid, [])
        target_record = None
        for r in reversed(records):
            if r.get("attempt_id") == attempt_id:
                target_record = r
                break

        if not target_record:
            raise KeyError(f"Recovery attempt {attempt_id} not found for {val_uuid}")

        current_state = target_record["state"]
        allowed = ALLOWED_TRANSITIONS.get(current_state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Illegal state transition from '{current_state}' to '{new_state}'. "
                f"Allowed transitions: {list(allowed)}"
            )

        now = time.time()
        target_record["state"] = new_state
        target_record["updated_at_utc"] = now
        if failure_stage:
            target_record["failure_stage"] = failure_stage
        hist_entry = {"state": new_state, "timestamp": now}
        if detail:
            hist_entry["detail"] = detail
        target_record["history"].append(hist_entry)

        self._write_atomic(data)
        return target_record

    def _reconcile_existing_attempt_unlocked(self, conversation_uuid, latest_record, live_dom_state, prompt_hash):
        if not latest_record or not live_dom_state:
            return latest_record, False

        attempt_id = latest_record.get("attempt_id")
        current_state = latest_record.get("state")
        last_user_hash = live_dom_state.get("lastUserMessageHash")
        is_turn_active = live_dom_state.get("isMainTurnActive", False)

        reconciled = False
        if current_state in [STATE_SUBMISSION_ATTEMPTED, STATE_DISPATCHED_UNCONFIRMED]:
            if last_user_hash == prompt_hash:
                self._transition_state_unlocked(
                    conversation_uuid, attempt_id, STATE_MESSAGE_OBSERVED,
                    detail="Forward reconciled: prompt confirmed in live DOM"
                )
                reconciled = True
                current_state = STATE_MESSAGE_OBSERVED

        if current_state == STATE_MESSAGE_OBSERVED and is_turn_active:
            self._transition_state_unlocked(
                conversation_uuid, attempt_id, STATE_TURN_STARTED,
                detail="Forward reconciled: active turn detected in live DOM"
            )
            reconciled = True

        updated_rec, _ = self.get_latest_record(conversation_uuid)
        return updated_rec, reconciled

    # Async public method for async pipelines:
    async def transition_state_async(self, conversation_uuid, attempt_id, new_state, failure_stage=None, detail=None):
        async with self.async_exclusive_lock(conversation_uuid=conversation_uuid):
            return self._transition_state_unlocked(conversation_uuid, attempt_id, new_state, failure_stage, detail)

    # Public locked methods:
    def start_recovery_attempt(self, conversation_uuid, prompt_text):
        with self.exclusive_lock(conversation_uuid=conversation_uuid):
            return self._start_recovery_attempt_unlocked(conversation_uuid, prompt_text)

    def transition_state(self, conversation_uuid, attempt_id, new_state, failure_stage=None, detail=None):
        with self.exclusive_lock(conversation_uuid=conversation_uuid):
            return self._transition_state_unlocked(conversation_uuid, attempt_id, new_state, failure_stage, detail)

    def reconcile_existing_attempt(self, conversation_uuid, latest_record, live_dom_state, prompt_hash):
        with self.exclusive_lock(conversation_uuid=conversation_uuid):
            return self._reconcile_existing_attempt_unlocked(conversation_uuid, latest_record, live_dom_state, prompt_hash)

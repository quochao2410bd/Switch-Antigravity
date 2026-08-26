import os
import re
import sys
import json
import time
import hashlib
import argparse
from typing import Dict, Any, Optional, List, Tuple

SCHEMA_VERSION = "1.0.0"

INDIVIDUAL_QUOTA_PATTERN = re.compile(
    r'(?:ERROR:\s+logging\s+before\s+google\.Init:\s+[IE](?P<timestamp>\d{4}\s+[\d:.]+)\s+(?P<log_thread_id>\d+)\s+(?P<source_location>[\w.]+:\d+)\]\s+)?'
    r'.*?(?:agent\s+executor\s+error:\s+)?(?:calling\s+model:\s+)?'
    r'RESOURCE_EXHAUSTED\s+\(code\s+429\):\s+'
    r'Individual\s+quota\s+reached\.\s+Please\s+upgrade\s+your\s+subscription\s+to\s+increase\s+your\s+limits\.\s+'
    r'Resets\s+in\s+(?P<resets_in>[^.)]+)',
    re.IGNORECASE
)

def get_file_identity(file_path: str) -> Dict[str, Any]:
    stat = os.stat(file_path)
    ctime_ns = int(getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1e9)))
    return {
        "dev": int(stat.st_dev),
        "ino": int(stat.st_ino),
        "ctime_ns": ctime_ns,
        "size_at_creation": int(stat.st_size)
    }

def create_baseline(
    log_path: str,
    ls_pid: Optional[int] = None,
    supervisor_session_id: Optional[str] = None
) -> Tuple[Dict[str, Any], int]:
    canonical_path = os.path.abspath(log_path)
    if not os.path.exists(canonical_path):
        return {
            "status": "LOG_UNAVAILABLE",
            "schema_version": SCHEMA_VERSION,
            "canonical_log_path": canonical_path,
            "error": f"Log file does not exist: {canonical_path}",
            "committed_byte_offset": 0,
            "file_size": 0,
            "file_identity": None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "language_server_process_id": ls_pid,
            "supervisor_session_id": supervisor_session_id
        }, 3

    try:
        identity = get_file_identity(canonical_path)
        stat = os.stat(canonical_path)
        return {
            "status": "BASELINE_INITIALIZED",
            "schema_version": SCHEMA_VERSION,
            "canonical_log_path": canonical_path,
            "committed_byte_offset": stat.st_size,
            "file_size": stat.st_size,
            "file_identity": identity,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "language_server_process_id": ls_pid,
            "supervisor_session_id": supervisor_session_id
        }, 0
    except Exception as e:
        return {
            "status": "LOG_UNAVAILABLE",
            "schema_version": SCHEMA_VERSION,
            "canonical_log_path": canonical_path,
            "error": f"Failed inspecting log file: {e}",
            "committed_byte_offset": 0,
            "file_size": 0,
            "file_identity": None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "language_server_process_id": ls_pid,
            "supervisor_session_id": supervisor_session_id
        }, 3

def validate_baseline_schema(
    baseline: Any,
    current_log_path: str,
    current_ls_pid: Optional[int] = None,
    current_session_id: Optional[str] = None
) -> Tuple[bool, str]:
    if not isinstance(baseline, dict) or not baseline:
        return False, "Baseline must be a non-empty JSON dictionary"

    if baseline.get("schema_version") != SCHEMA_VERSION:
        return False, f"Unsupported or missing schema_version: expected {SCHEMA_VERSION}, got {baseline.get('schema_version')}"

    canonical_current = os.path.abspath(current_log_path)
    baseline_path = baseline.get("canonical_log_path")
    if not isinstance(baseline_path, str) or not baseline_path or os.path.abspath(baseline_path) != canonical_current:
        return False, f"Log path mismatch: baseline is for {baseline_path}, current target is {canonical_current}"

    offset = baseline.get("committed_byte_offset")
    if offset is None or type(offset) is not int or offset < 0:
        return False, f"Invalid committed_byte_offset (must be non-negative integer): {offset}"

    file_size = baseline.get("file_size")
    if file_size is not None and (type(file_size) is not int or file_size < 0):
        return False, f"Invalid file_size (must be non-negative integer): {file_size}"

    identity = baseline.get("file_identity")
    if not isinstance(identity, dict):
        return False, "Missing or malformed file_identity in baseline (must be a dictionary)"

    for k in ["dev", "ino", "ctime_ns", "size_at_creation"]:
        val = identity.get(k)
        if val is None or type(val) is not int or val < 0:
            return False, f"Invalid or missing file_identity field '{k}': {val}"

    bound_pid = baseline.get("language_server_process_id")
    if bound_pid is not None:
        if type(bound_pid) is not int or bound_pid <= 0:
            return False, f"Invalid bound language_server_process_id in baseline: {bound_pid}"
        if current_ls_pid is None:
            return False, f"Baseline is bound to LS PID {bound_pid}, but current_ls_pid was omitted by caller"
        if type(current_ls_pid) is not int or current_ls_pid != bound_pid:
            return False, f"Language server process changed (bound: {bound_pid}, current: {current_ls_pid})"

    bound_session = baseline.get("supervisor_session_id")
    if bound_session is not None:
        if not isinstance(bound_session, str) or not bound_session.strip():
            return False, f"Invalid bound supervisor_session_id in baseline: {bound_session}"
        if current_session_id is None:
            return False, f"Baseline is bound to supervisor session '{bound_session}', but current_session_id was omitted by caller"
        if not isinstance(current_session_id, str) or current_session_id != bound_session:
            return False, f"Supervisor session changed (bound: '{bound_session}', current: '{current_session_id}')"

    return True, "VALID"

def poll_new_events(
    baseline: Any,
    log_path: Optional[str] = None,
    current_ls_pid: Optional[int] = None,
    current_session_id: Optional[str] = None,
    include_raw_log: bool = False
) -> Tuple[Dict[str, Any], int]:
    if not isinstance(baseline, dict) or not baseline:
        return {
            "status": "BASELINE_REQUIRED",
            "event_poll_status": "BASELINE_REQUIRED",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "error": "Valid baseline dictionary is required for incremental polling",
            "cursor": 0
        }, 5

    target_path = log_path or baseline.get("canonical_log_path")
    if not target_path or not os.path.exists(target_path):
        return {
            "status": "LOG_UNAVAILABLE",
            "event_poll_status": "LOG_UNAVAILABLE",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "error": f"Log file unavailable: {target_path}",
            "cursor": baseline.get("committed_byte_offset", 0) if isinstance(baseline, dict) else 0
        }, 3

    valid, reason = validate_baseline_schema(baseline, target_path, current_ls_pid, current_session_id)
    if not valid:
        return {
            "status": "BASELINE_INVALID",
            "event_poll_status": "BASELINE_INVALID",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "error": f"Baseline validation failed: {reason}",
            "rebaseline_required": True,
            "cursor": 0
        }, 2

    canonical_target = os.path.abspath(target_path)
    try:
        current_identity = get_file_identity(canonical_target)
        stat = os.stat(canonical_target)
    except Exception as e:
        return {
            "status": "LOG_UNAVAILABLE",
            "event_poll_status": "LOG_UNAVAILABLE",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "error": f"Failed stat on log file: {e}",
            "cursor": baseline["committed_byte_offset"]
        }, 3

    prev_identity = baseline["file_identity"]
    if (current_identity["dev"] != prev_identity["dev"] or
        current_identity["ino"] != prev_identity["ino"] or
        current_identity["ctime_ns"] != prev_identity["ctime_ns"]):
        return {
            "status": "BASELINE_INVALID",
            "event_poll_status": "BASELINE_INVALID",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "error": "Log file replaced with new file identity (rotation or replacement detected)",
            "rebaseline_required": True,
            "cursor": stat.st_size
        }, 2

    prev_offset = baseline["committed_byte_offset"]
    current_size = stat.st_size

    if current_size < prev_offset:
        return {
            "status": "BASELINE_INVALID",
            "event_poll_status": "BASELINE_INVALID",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "error": f"Log file truncated (previous offset {prev_offset} > current size {current_size})",
            "rebaseline_required": True,
            "cursor": current_size
        }, 2

    if current_size == prev_offset:
        return {
            "status": "NO_NEW_EVENT",
            "event_poll_status": "NO_NEW_EVENT",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "new_events_count": 0,
            "events": [],
            "cursor": prev_offset
        }, 1

    try:
        with open(canonical_target, "rb") as f:
            f.seek(prev_offset)
            raw_bytes = f.read()
    except Exception as e:
        return {
            "status": "PARSE_ERROR",
            "event_poll_status": "PARSE_ERROR",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "error": f"Failed reading raw log bytes: {e}",
            "cursor": prev_offset
        }, 4

    last_newline_idx = raw_bytes.rfind(b"\n")
    if last_newline_idx == -1:
        return {
            "status": "NO_NEW_EVENT",
            "event_poll_status": "NO_NEW_EVENT",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "new_events_count": 0,
            "events": [],
            "cursor": prev_offset,
            "trailing_partial_bytes_count": len(raw_bytes)
        }, 1

    complete_bytes = raw_bytes[:last_newline_idx + 1]
    new_committed_offset = prev_offset + len(complete_bytes)

    matches = []
    current_byte_pos = prev_offset

    lines_raw = complete_bytes.splitlines(keepends=True)
    for line_bytes in lines_raw:
        line_start = current_byte_pos
        line_end = line_start + len(line_bytes)
        current_byte_pos = line_end

        line_str = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
        m = INDIVIDUAL_QUOTA_PATTERN.search(line_str)
        if m:
            resets_in = m.group("resets_in").strip() if m.group("resets_in") else None
            timestamp = m.group("timestamp").strip() if m.group("timestamp") else None
            log_thread_id = m.group("log_thread_id").strip() if m.group("log_thread_id") else None
            source_location = m.group("source_location").strip() if m.group("source_location") else None

            record_sha256 = hashlib.sha256(line_bytes).hexdigest()
            event_id = f"evt_{current_identity['ino']}_{line_start}_{line_end}_{record_sha256[:16]}"

            event_obj = {
                "event_id": event_id,
                "event_sha256": record_sha256,
                "event_record_sha256": record_sha256,
                "code": 429,
                "resets_in": resets_in,
                "log_timestamp": timestamp,
                "log_thread_id": log_thread_id,
                "source_location": source_location,
                "event_start_offset": line_start,
                "event_end_offset": line_end,
                "account_attribution": "UNKNOWN_AT_T01_LAYER",
                "evidence_class": "OBSERVED_FORMAT"
            }
            if include_raw_log:
                event_obj["raw_line"] = line_str

            matches.append(event_obj)

    if matches:
        latest = matches[-1]
        return {
            "status": "NEW_CONFIRMED_QUOTA_EVENT",
            "event_poll_status": "NEW_CONFIRMED_QUOTA_EVENT",
            "quota_state_effect": "EXHAUSTED",
            "current_session_quota_state": "CONFIRMED",
            "signature_confidence": 1.0,
            "account_attribution": "UNKNOWN_AT_T01_LAYER",
            "new_events_count": len(matches),
            "latest_event": latest,
            "resets_in": latest["resets_in"],
            "events": matches,
            "cursor": new_committed_offset
        }, 0
    else:
        return {
            "status": "NO_NEW_EVENT",
            "event_poll_status": "NO_NEW_EVENT",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "new_events_count": 0,
            "events": [],
            "cursor": new_committed_offset
        }, 1

def detect_historical_events(
    log_path: str,
    include_raw_log: bool = False
) -> Tuple[Dict[str, Any], int]:
    canonical_path = os.path.abspath(log_path)
    if not os.path.exists(canonical_path):
        return {
            "status": "LOG_UNAVAILABLE",
            "error": f"File not found: {canonical_path}",
            "total_matches": 0
        }, 3

    try:
        identity = get_file_identity(canonical_path)
        with open(canonical_path, "rb") as f:
            content = f.read()
    except Exception as e:
        return {
            "status": "PARSE_ERROR",
            "error": f"Failed reading log: {e}",
            "total_matches": 0
        }, 4

    lines_raw = content.splitlines(keepends=True)
    matches = []
    current_pos = 0

    for idx, line_bytes in enumerate(lines_raw, 1):
        line_start = current_pos
        line_end = line_start + len(line_bytes)
        current_pos = line_end

        line_str = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
        m = INDIVIDUAL_QUOTA_PATTERN.search(line_str)
        if m:
            resets_in = m.group("resets_in").strip() if m.group("resets_in") else None
            timestamp = m.group("timestamp").strip() if m.group("timestamp") else None
            log_thread_id = m.group("log_thread_id").strip() if m.group("log_thread_id") else None
            source_location = m.group("source_location").strip() if m.group("source_location") else None

            record_sha256 = hashlib.sha256(line_bytes).hexdigest()
            event_id = f"evt_{identity['ino']}_{line_start}_{line_end}_{record_sha256[:16]}"

            event_obj = {
                "event_id": event_id,
                "event_sha256": record_sha256,
                "event_record_sha256": record_sha256,
                "line_number": idx,
                "code": 429,
                "resets_in": resets_in,
                "log_timestamp": timestamp,
                "log_thread_id": log_thread_id,
                "source_location": source_location,
                "event_start_offset": line_start,
                "event_end_offset": line_end,
                "account_attribution": "UNKNOWN_AT_T01_LAYER",
                "evidence_class": "OBSERVED_FORMAT"
            }
            if include_raw_log:
                event_obj["raw_line"] = line_str

            matches.append(event_obj)

    if matches:
        return {
            "status": "HISTORICAL_QUOTA_EVENT_FOUND",
            "event_poll_status": "HISTORICAL_SCAN_COMPLETE",
            "quota_state_effect": "UNKNOWN_HISTORICAL",
            "current_session_quota_state": "UNKNOWN_HISTORICAL_ONLY",
            "signature_confidence": 1.0,
            "total_matches": len(matches),
            "latest_event": matches[-1],
            "events": matches
        }, 0
    else:
        return {
            "status": "NO_HISTORICAL_QUOTA_EVENT",
            "event_poll_status": "HISTORICAL_SCAN_COMPLETE",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "total_matches": 0,
            "events": []
        }, 1

def main():
    parser = argparse.ArgumentParser(description="Antigravity Desktop Robust Incremental Quota Detector")
    parser.add_argument("--file", help="Path to language_server.log", default=None)
    parser.add_argument("--historical", action="store_true", help="Run full historical diagnostic scan")
    parser.add_argument("--baseline", help="JSON string or path to baseline JSON file", default=None)
    parser.add_argument("--init-baseline", action="store_true", help="Initialize and output a fresh baseline at EOF")
    parser.add_argument("--ls-pid", type=int, help="Language server process PID to bind/enforce", default=None)
    parser.add_argument("--session-id", help="Supervisor session identifier to bind/enforce", default=None)
    parser.add_argument("--include-raw-log", action="store_true", help="Include raw log lines in diagnostic output")
    args = parser.parse_args()

    log_path = args.file or os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")

    if args.init_baseline:
        base, exit_code = create_baseline(log_path, ls_pid=args.ls_pid, supervisor_session_id=args.session_id)
        print(json.dumps(base, indent=2))
        sys.exit(exit_code)

    if args.historical:
        res, exit_code = detect_historical_events(log_path, include_raw_log=args.include_raw_log)
        print(json.dumps(res, indent=2))
        sys.exit(exit_code)

    if not args.baseline:
        err_res = {
            "status": "BASELINE_REQUIRED",
            "event_poll_status": "BASELINE_REQUIRED",
            "quota_state_effect": "UNCHANGED",
            "current_session_quota_state": "UNKNOWN_OR_UNCHANGED",
            "signature_confidence": 0.0,
            "error": "Incremental polling requires a validated baseline. Use --init-baseline to create one, or --historical for diagnostic scan."
        }
        print(json.dumps(err_res, indent=2))
        sys.exit(5)

    try:
        if os.path.exists(args.baseline):
            with open(args.baseline, "r", encoding="utf-8") as bf:
                base_dict = json.load(bf)
        else:
            base_dict = json.loads(args.baseline)
    except Exception as e:
        err_res = {
            "status": "BASELINE_INVALID",
            "event_poll_status": "BASELINE_INVALID",
            "error": f"Failed parsing baseline argument: {e}",
            "rebaseline_required": True
        }
        print(json.dumps(err_res, indent=2))
        sys.exit(2)

    res, exit_code = poll_new_events(
        baseline=base_dict,
        log_path=log_path,
        current_ls_pid=args.ls_pid,
        current_session_id=args.session_id,
        include_raw_log=args.include_raw_log
    )
    print(json.dumps(res, indent=2))
    sys.exit(exit_code)

if __name__ == "__main__":
    main()

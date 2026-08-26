import os
import re
import sys
import json
import time
import argparse
from typing import Dict, Any, Optional, List

INDIVIDUAL_QUOTA_PATTERN = re.compile(
    r'(?:ERROR: logging before google\.Init:\s+[IE](?P<timestamp>\d{4}\s+[\d:.]+)\s+(?P<pid>\d+)\s+(?P<source>[\w.]+:\d+)\]\s+)?'
    r'.*?(?:agent executor error:\s+)?(?:calling model:\s+)?'
    r'RESOURCE_EXHAUSTED\s+\(code\s+429\):\s+'
    r'Individual quota reached\.\s+Please upgrade your subscription to increase your limits\.\s+'
    r'Resets in\s+(?P<resets_in>[^.)]+)',
    re.IGNORECASE
)

def create_baseline(log_path: str, ls_pid: Optional[int] = None) -> Dict[str, Any]:
    if not os.path.exists(log_path):
        return {
            "status": "LOG_UNAVAILABLE",
            "log_path": log_path,
            "error": f"Log file does not exist: {log_path}",
            "byte_offset": 0,
            "file_size": 0,
            "file_mtime": 0.0,
            "baseline_timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ls_pid": ls_pid
        }
    stat = os.stat(log_path)
    return {
        "status": "BASELINE_INITIALIZED",
        "log_path": os.path.abspath(log_path),
        "byte_offset": stat.st_size,
        "file_size": stat.st_size,
        "file_mtime": stat.st_mtime,
        "baseline_timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ls_pid": ls_pid
    }

def poll_new_events(baseline: Dict[str, Any], log_path: Optional[str] = None) -> Dict[str, Any]:
    target_path = log_path or baseline.get("log_path")
    if not target_path or not os.path.exists(target_path):
        return {
            "status": "LOG_UNAVAILABLE",
            "event_scope": "NONE",
            "current_session_quota_state": "UNKNOWN",
            "signature_confidence": 0.0,
            "error": f"Log file unavailable: {target_path}",
            "cursor": baseline.get("byte_offset", 0)
        }

    try:
        stat = os.stat(target_path)
    except Exception as e:
        return {
            "status": "LOG_UNAVAILABLE",
            "event_scope": "NONE",
            "current_session_quota_state": "UNKNOWN",
            "signature_confidence": 0.0,
            "error": f"Failed to stat log file: {e}",
            "cursor": baseline.get("byte_offset", 0)
        }

    prev_offset = baseline.get("byte_offset", 0)
    current_size = stat.st_size

    if current_size < prev_offset:
        return {
            "status": "BASELINE_INVALID",
            "event_scope": "NONE",
            "current_session_quota_state": "UNKNOWN",
            "signature_confidence": 0.0,
            "error": f"Log file truncated or rotated (previous offset {prev_offset} > current size {current_size})",
            "rebaseline_required": True,
            "cursor": current_size
        }

    if current_size == prev_offset:
        return {
            "status": "NO_NEW_EVENT",
            "event_scope": "NONE",
            "current_session_quota_state": "NORMAL",
            "signature_confidence": 0.0,
            "new_events_count": 0,
            "events": [],
            "cursor": prev_offset
        }

    try:
        with open(target_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(prev_offset)
            new_chunk = f.read()
    except Exception as e:
        return {
            "status": "PARSE_ERROR",
            "event_scope": "NONE",
            "current_session_quota_state": "UNKNOWN",
            "signature_confidence": 0.0,
            "error": f"Failed reading new log bytes: {e}",
            "cursor": prev_offset
        }

    matches = []
    lines = new_chunk.splitlines()
    for line in lines:
        m = INDIVIDUAL_QUOTA_PATTERN.search(line)
        if m:
            matches.append({
                "timestamp": m.group("timestamp") if "timestamp" in m.groupdict() else None,
                "pid": m.group("pid") if "pid" in m.groupdict() else None,
                "source": m.group("source") if "source" in m.groupdict() else None,
                "code": 429,
                "resets_in": m.group("resets_in").strip() if "resets_in" in m.groupdict() and m.group("resets_in") else None,
                "raw_line": line.strip()
            })

    if matches:
        latest = matches[-1]
        return {
            "status": "NEW_CONFIRMED_QUOTA_EVENT",
            "event_scope": "NEW_SINCE_BASELINE",
            "current_session_quota_state": "CONFIRMED",
            "signature_confidence": 1.0,
            "new_events_count": len(matches),
            "latest_event": latest,
            "resets_in": latest["resets_in"],
            "cursor": current_size
        }
    else:
        return {
            "status": "NO_NEW_EVENT",
            "event_scope": "NONE",
            "current_session_quota_state": "NORMAL",
            "signature_confidence": 0.0,
            "new_events_count": 0,
            "events": [],
            "cursor": current_size
        }

def detect_historical_events(log_path: str) -> Dict[str, Any]:
    if not os.path.exists(log_path):
        return {
            "status": "LOG_UNAVAILABLE",
            "event_scope": "NONE",
            "current_session_quota_state": "UNKNOWN",
            "signature_confidence": 0.0,
            "error": f"File not found: {log_path}"
        }
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    
    lines = content.splitlines()
    matches = []
    for idx, line in enumerate(lines, 1):
        m = INDIVIDUAL_QUOTA_PATTERN.search(line)
        if m:
            matches.append({
                "line_number": idx,
                "timestamp": m.group("timestamp") if "timestamp" in m.groupdict() else None,
                "pid": m.group("pid") if "pid" in m.groupdict() else None,
                "source": m.group("source") if "source" in m.groupdict() else None,
                "code": 429,
                "resets_in": m.group("resets_in").strip() if "resets_in" in m.groupdict() and m.group("resets_in") else None,
                "raw_line": line.strip()
            })

    if matches:
        return {
            "status": "HISTORICAL_QUOTA_EVENT_FOUND",
            "event_scope": "HISTORICAL",
            "current_session_quota_state": "UNKNOWN_HISTORICAL_ONLY",
            "signature_confidence": 1.0,
            "total_matches": len(matches),
            "latest_event": matches[-1]
        }
    else:
        return {
            "status": "NO_HISTORICAL_QUOTA_EVENT",
            "event_scope": "NONE",
            "current_session_quota_state": "NORMAL",
            "signature_confidence": 0.0,
            "total_matches": 0
        }

def main():
    parser = argparse.ArgumentParser(description="Antigravity Desktop Incremental Quota Detector")
    parser.add_argument("--file", help="Path to language_server.log", default=None)
    parser.add_argument("--historical", action="store_true", help="Run full historical diagnostic scan")
    parser.add_argument("--baseline", help="JSON string or file of previous baseline", default=None)
    parser.add_argument("--init-baseline", action="store_true", help="Initialize and output a fresh baseline at EOF")
    args = parser.parse_args()

    log_path = args.file or os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")

    if args.init_baseline:
        base = create_baseline(log_path)
        print(json.dumps(base, indent=2))
        sys.exit(0)

    if args.historical:
        res = detect_historical_events(log_path)
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("status") == "HISTORICAL_QUOTA_EVENT_FOUND" else 1)

    if args.baseline:
        try:
            if os.path.exists(args.baseline):
                with open(args.baseline, "r", encoding="utf-8") as bf:
                    base_dict = json.load(bf)
            else:
                base_dict = json.loads(args.baseline)
        except Exception as e:
            err_res = {"status": "PARSE_ERROR", "error": f"Invalid baseline input: {e}"}
            print(json.dumps(err_res, indent=2))
            sys.exit(4)
    else:
        base_dict = {"log_path": log_path, "byte_offset": 0}

    res = poll_new_events(base_dict, log_path)
    print(json.dumps(res, indent=2))

    if res.get("status") == "NEW_CONFIRMED_QUOTA_EVENT":
        sys.exit(0)
    elif res.get("status") == "NO_NEW_EVENT":
        sys.exit(1)
    elif res.get("status") == "BASELINE_INVALID":
        sys.exit(2)
    elif res.get("status") == "LOG_UNAVAILABLE":
        sys.exit(3)
    else:
        sys.exit(4)

if __name__ == "__main__":
    main()

import json
import os

fixtures_dir = "tests/fixtures/t01"
os.makedirs(fixtures_dir, exist_ok=True)

SYNTH_CASCADE_ID = "00000000-0000-4000-8000-000000000001"
SYNTH_TRAJECTORY_ID = "10000000-0000-4000-8000-000000000001"
SYNTH_PROJECT_ID = "90000000-0000-4000-8000-000000000001"

process_fixture = [
    {
        "ProcessId": 10000,
        "ParentProcessId": 1000,
        "Name": "Antigravity.exe",
        "Role": "Main Electron Browser Process",
        "CommandLine": "\"C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\Antigravity.exe\""
    },
    {
        "ProcessId": 10001,
        "ParentProcessId": 10000,
        "Name": "Antigravity.exe",
        "Role": "GPU Process",
        "CommandLine": "\"C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\Antigravity.exe\" --type=gpu-process --user-data-dir=\"C:\\Users\\<USER>\\AppData\\Roaming\\Antigravity\""
    },
    {
        "ProcessId": 10002,
        "ParentProcessId": 10000,
        "Name": "Antigravity.exe",
        "Role": "Utility NetworkService",
        "CommandLine": "\"C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\Antigravity.exe\" --type=utility --utility-sub-type=network.mojom.NetworkService --user-data-dir=\"C:\\Users\\<USER>\\AppData\\Roaming\\Antigravity\""
    },
    {
        "ProcessId": 10003,
        "ParentProcessId": 10000,
        "Name": "Antigravity.exe",
        "Role": "Renderer Process (UI Webview)",
        "CommandLine": "\"C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\Antigravity.exe\" --type=renderer --user-data-dir=\"C:\\Users\\<USER>\\AppData\\Roaming\\Antigravity\" --app-path=\"C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\resources\\app.asar\""
    },
    {
        "ProcessId": 10004,
        "ParentProcessId": 10000,
        "Name": "language_server.exe",
        "Role": "Language Server / Agent Core RPC Service",
        "CommandLine": "C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\resources\\bin\\language_server.exe --standalone --override_ide_name antigravity --subclient_type hub --override_ide_version 2.10.0 --override_user_agent_name antigravity --https_server_port 0 --csrf_token <REDACTED_CSRF_TOKEN> --app_data_dir antigravity --api_server_url https://generativelanguage.googleapis.com --cloud_code_endpoint https://daily-cloudcode-pa.googleapis.com --enable_sidecars --host_bridge_url=http://127.0.0.1:58860 --host_bridge_token=<REDACTED_HOST_BRIDGE_TOKEN>"
    }
]
with open(os.path.join(fixtures_dir, "sample_process_tree.json"), "w", encoding="utf-8") as f:
    json.dump(process_fixture, f, indent=2)

trajectory_fixture = {
    "tables": {
        "trajectory_meta": {
            "schema": "CREATE TABLE `trajectory_meta` (`trajectory_id` text,`cascade_id` text,`trajectory_type` integer,`source` integer,PRIMARY KEY (`trajectory_id`))",
            "columns": ["trajectory_id", "cascade_id", "trajectory_type", "source"],
            "sample_row": {
                "trajectory_id": SYNTH_TRAJECTORY_ID,
                "cascade_id": SYNTH_CASCADE_ID,
                "trajectory_type": 4,
                "source": 1
            }
        },
        "steps": {
            "schema": "CREATE TABLE `steps` (`idx` integer,`step_type` integer NOT NULL DEFAULT 0,`status` integer NOT NULL DEFAULT 0,`has_subtrajectory` numeric NOT NULL DEFAULT false,`metadata` blob,`error_details` blob,`permissions` blob,`task_details` blob,`render_info` blob,`step_payload` blob,`step_format` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`))"
        },
        "trajectory_metadata_blob": {
            "schema": "CREATE TABLE `trajectory_metadata_blob` (`id` text DEFAULT 'main',`data` blob,PRIMARY KEY (`id`))"
        }
    }
}
with open(os.path.join(fixtures_dir, "sample_trajectory_meta.json"), "w", encoding="utf-8") as f:
    json.dump(trajectory_fixture, f, indent=2)

positive_quota = """ERROR: logging before google.Init: I0826 17:40:05.441110  181166 run.go:367] Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m54s.), retrying in 1s
ERROR: logging before google.Init: E0826 17:40:12.155991  181166 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m48s.
"""
with open(os.path.join(fixtures_dir, "quota_positive.txt"), "w", encoding="utf-8") as f:
    f.write(positive_quota)
with open(os.path.join(fixtures_dir, "sample_quota_error_log.txt"), "w", encoding="utf-8") as f:
    f.write(positive_quota)

neg_generic_exhausted = """ERROR: logging before google.Init: E0826 18:10:00.123456  123456 run.go:100] RPC failed: RESOURCE_EXHAUSTED: Server side worker capacity temporarily exhausted.
ERROR: logging before google.Init: E0826 18:10:05.123456  123456 run.go:100] Backend out of memory: RESOURCE_EXHAUSTED.
"""
with open(os.path.join(fixtures_dir, "quota_negative_generic_resource_exhausted.txt"), "w", encoding="utf-8") as f:
    f.write(neg_generic_exhausted)

neg_429_other = """ERROR: logging before google.Init: I0826 19:00:00.000000  100000 http_helpers.go:100] HTTP 429 Too Many Requests: Rate limit exceeded (15 requests per minute).
ERROR: logging before google.Init: E0826 19:00:05.000000  100000 errorreport.go:50] 429 concurrency limit reached, backing off.
"""
with open(os.path.join(fixtures_dir, "quota_negative_429_other.txt"), "w", encoding="utf-8") as f:
    f.write(neg_429_other)

neg_code_503 = """ERROR: logging before google.Init: E0826 17:40:12.155991  181166 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 503): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h.
"""
with open(os.path.join(fixtures_dir, "quota_negative_code_503.txt"), "w", encoding="utf-8") as f:
    f.write(neg_code_503)

neg_normal_log = """ERROR: logging before google.Init: I0826 16:27:33.749312       1 server.go:1478] Starting language server process with pid 7520
ERROR: logging before google.Init: I0826 16:27:33.766902       1 server.go:590] Language server listening on random port at 58861 for HTTPS (gRPC)
ERROR: logging before google.Init: I0826 16:27:35.721529       1 http_helpers.go:246] URL: https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist Trace: 0x1554b1e4b060bde2
"""
with open(os.path.join(fixtures_dir, "quota_negative_normal_log.txt"), "w", encoding="utf-8") as f:
    f.write(neg_normal_log)

host_bridge_log = """[2026-08-26 16:27:33.565] [info] Starting app (v2.10.0) with dynamic port.
[2026-08-26 16:27:33.585] [info] Host bridge server listening on http://127.0.0.1:58860
[2026-08-26 16:27:33.587] [info] Spawning: C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\resources\\bin\\language_server.exe --standalone --override_ide_name antigravity --subclient_type hub --override_ide_version 2.10.0 --override_user_agent_name antigravity --https_server_port 0 --csrf_token <REDACTED_CSRF> --app_data_dir antigravity --api_server_url https://generativelanguage.googleapis.com --cloud_code_endpoint https://daily-cloudcode-pa.googleapis.com --enable_sidecars --host_bridge_url=http://127.0.0.1:58860 --host_bridge_token=<REDACTED_TOKEN>
[2026-08-26 16:27:33.767] [info] [Auto-Restart] Port changed! Reloading all windows with URL: https://127.0.0.1:58861/
"""
with open(os.path.join(fixtures_dir, "sample_host_bridge_log.txt"), "w", encoding="utf-8") as f:
    f.write(host_bridge_log)

sse_log = """ERROR: logging before google.Init: I0826 21:56:08.887617  546948 http_helpers.go:246] URL: https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse Trace: 0x3a059134385a685c ResponseID: hv6OatrMKZqB1e8Pq4LUqAg
ERROR: logging before google.Init: I0826 21:56:09.710346  546936 http_helpers.go:246] URL: https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse Trace: 0xcf18df115095001f ResponseID: hf6OavyNGPPUqfkPlbHicA
"""
with open(os.path.join(fixtures_dir, "sample_sse_stream_log.txt"), "w", encoding="utf-8") as f:
    f.write(sse_log)

cdp_targets = [
    {
        "id": "PAGE_TARGET_0001",
        "type": "page",
        "title": "Sample Active Task",
        "url": f"https://127.0.0.1:58861/c/{SYNTH_CASCADE_ID}?section={SYNTH_PROJECT_ID}",
        "webSocketDebuggerUrl": "ws://127.0.0.1:58859/devtools/page/PAGE_TARGET_0001"
    }
]
with open(os.path.join(fixtures_dir, "sample_cdp_targets.json"), "w", encoding="utf-8") as f:
    json.dump(cdp_targets, f, indent=2)

proto_fixture = {
    "conversation_id": SYNTH_CASCADE_ID,
    "title": "Sample Active Task",
    "step_count": 100,
    "trajectory_id": SYNTH_TRAJECTORY_ID,
    "workspace_uri": "file:///c:/Users/<USER>/Documents/workspace",
    "git_branch": "main",
    "project_id": SYNTH_PROJECT_ID
}
with open(os.path.join(fixtures_dir, "sample_proto_summary.json"), "w", encoding="utf-8") as f:
    json.dump(proto_fixture, f, indent=2)

devtools_fixture = "58859\n/devtools/browser/00000000-0000-4000-8000-000000000000\n"
with open(os.path.join(fixtures_dir, "sample_devtools_active_port.txt"), "w", encoding="utf-8") as f:
    f.write(devtools_fixture)

cross_corr = {
    "local_four_way_correlation": {
        "status": "VERIFIED_RUNTIME",
        "matching_conversations_count": 9,
        "total_conversations_count": 9,
        "sources": [
            "sqlite_db_filename (%USERPROFILE%\\.gemini\\antigravity\\conversations\\<cascade_id>.db)",
            "sqlite_trajectory_meta_table (cascade_id column)",
            "brain_directory (%USERPROFILE%\\.gemini\\antigravity\\brain\\<cascade_id>\\)",
            "proto_summaries_index (agyhub_summaries_proto.pb Field 17 Subfield 6)"
        ],
        "sample_synthetic_correlation": {
            "cascade_id": SYNTH_CASCADE_ID,
            "trajectory_id": SYNTH_TRAJECTORY_ID,
            "db_filename_matches_table": True,
            "brain_dir_exists": True,
            "proto_index_matches": True
        }
    },
    "active_cdp_correlation": {
        "status": "VERIFIED_RUNTIME",
        "active_renderer_targets_count": 1,
        "active_target_matched_cascade_id": True,
        "sample_active_target_url": f"https://127.0.0.1:58861/c/{SYNTH_CASCADE_ID}?section={SYNTH_PROJECT_ID}"
    }
}
with open(os.path.join(fixtures_dir, "sample_cross_correlation.json"), "w", encoding="utf-8") as f:
    json.dump(cross_corr, f, indent=2)

print("Generated all sanitized fixtures.")

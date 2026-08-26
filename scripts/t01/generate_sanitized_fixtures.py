import json
import os

fixtures_dir = "tests/fixtures/t01"
os.makedirs(fixtures_dir, exist_ok=True)

# 1. Process tree fixture
process_fixture = [
    {
        "ProcessId": 14472,
        "ParentProcessId": 3500,
        "Name": "Antigravity.exe",
        "Role": "Main Electron Browser Process",
        "CommandLine": "\"C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\Antigravity.exe\""
    },
    {
        "ProcessId": 6008,
        "ParentProcessId": 14472,
        "Name": "Antigravity.exe",
        "Role": "GPU Process",
        "CommandLine": "\"C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\Antigravity.exe\" --type=gpu-process --user-data-dir=\"C:\\Users\\<USER>\\AppData\\Roaming\\Antigravity\""
    },
    {
        "ProcessId": 7380,
        "ParentProcessId": 14472,
        "Name": "Antigravity.exe",
        "Role": "Utility NetworkService",
        "CommandLine": "\"C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\Antigravity.exe\" --type=utility --utility-sub-type=network.mojom.NetworkService --user-data-dir=\"C:\\Users\\<USER>\\AppData\\Roaming\\Antigravity\""
    },
    {
        "ProcessId": 12992,
        "ParentProcessId": 14472,
        "Name": "Antigravity.exe",
        "Role": "Renderer Process (UI Webview)",
        "CommandLine": "\"C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\Antigravity.exe\" --type=renderer --user-data-dir=\"C:\\Users\\<USER>\\AppData\\Roaming\\Antigravity\" --app-path=\"C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\resources\\app.asar\""
    },
    {
        "ProcessId": 7520,
        "ParentProcessId": 14472,
        "Name": "language_server.exe",
        "Role": "Language Server / Agent Core RPC Service",
        "CommandLine": "C:\\Users\\<USER>\\AppData\\Local\\Programs\\antigravity\\resources\\bin\\language_server.exe --standalone --override_ide_name antigravity --subclient_type hub --override_ide_version 2.10.0 --override_user_agent_name antigravity --https_server_port 0 --csrf_token <REDACTED_CSRF_TOKEN> --app_data_dir antigravity --api_server_url https://generativelanguage.googleapis.com --cloud_code_endpoint https://daily-cloudcode-pa.googleapis.com --enable_sidecars --host_bridge_url=http://127.0.0.1:58860 --host_bridge_token=<REDACTED_HOST_BRIDGE_TOKEN>"
    }
]
with open(os.path.join(fixtures_dir, "sample_process_tree.json"), "w", encoding="utf-8") as f:
    json.dump(process_fixture, f, indent=2)

# 2. Trajectory meta schema & row fixture
trajectory_fixture = {
    "tables": {
        "trajectory_meta": {
            "schema": "CREATE TABLE `trajectory_meta` (`trajectory_id` text,`cascade_id` text,`trajectory_type` integer,`source` integer,PRIMARY KEY (`trajectory_id`))",
            "columns": ["trajectory_id", "cascade_id", "trajectory_type", "source"],
            "sample_row": {
                "trajectory_id": "83a53ccd-8127-463e-a422-205d5c34cf0c",
                "cascade_id": "4674ef3b-d559-4a90-87e2-c30b11f03250",
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

# 3. Quota error log fixture
quota_log_fixture = """Line 4134: ERROR: logging before google.Init: I0826 17:40:05.441110  181166 run.go:367] Run: attempt 1 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m54s.), retrying in 1s
Line 4135: ERROR: logging before google.Init: I0826 17:40:08.537815  181166 run.go:367] Run: attempt 2 failed (RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m51s.), retrying in 1.85516771s
Line 4137: ERROR: logging before google.Init: E0826 17:40:12.155991  181166 errorreport.go:223] agent executor error: calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m48s.
Line 4138: ERROR: logging before google.Init: E0826 17:40:12.178117  181166 errorreport.go:223] calling model: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 3h24m48s."""
with open(os.path.join(fixtures_dir, "sample_quota_error_log.txt"), "w", encoding="utf-8") as f:
    f.write(quota_log_fixture)

# 4. DevTools fixture
devtools_fixture = "58859\n/devtools/browser/c5732c07-ab94-433e-94f0-7e6713b0bbba\n"
with open(os.path.join(fixtures_dir, "sample_devtools_active_port.txt"), "w", encoding="utf-8") as f:
    f.write(devtools_fixture)

# 5. Proto summary fixture
proto_fixture = {
    "conversation_id": "4674ef3b-d559-4a90-87e2-c30b11f03250",
    "title": "T01",
    "step_count": 128,
    "trajectory_id": "83a53ccd-8127-463e-a422-205d5c34cf0c",
    "workspace_uri": "file:///c:/Users/<USER>/Documents/antigravity/excited-oppenheimer",
    "git_branch": "master",
    "project_id": "f9401a86-c599-4395-b076-31829765c539"
}
with open(os.path.join(fixtures_dir, "sample_proto_summary.json"), "w", encoding="utf-8") as f:
    json.dump(proto_fixture, f, indent=2)

print("Generated fixtures successfully in", fixtures_dir)

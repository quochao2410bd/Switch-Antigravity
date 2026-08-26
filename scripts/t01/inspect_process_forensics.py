import subprocess
import json
import time
import os

def run_powershell(cmd):
    res = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    return res.stdout.strip()

def collect_process_snapshot(iteration):
    print(f"=== Process Snapshot Iteration {iteration} ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===")
    
    # 1. Process tree
    ps_cmd = """
    Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'antigravity|language_server' } |
    Select-Object ProcessId, ParentProcessId, Name, CommandLine | ConvertTo-Json
    """
    proc_json = run_powershell(ps_cmd)
    try:
        procs = json.loads(proc_json)
        if isinstance(procs, dict):
            procs = [procs]
        print(f"Found {len(procs)} Antigravity/LanguageServer processes:")
        for p in procs:
            pid = p.get('ProcessId')
            ppid = p.get('ParentProcessId')
            name = p.get('Name')
            cmd = p.get('CommandLine', '')
            print(f"  PID: {pid:<6} PPID: {ppid:<6} Name: {name:<20} Cmd: {cmd[:120]}...")
    except Exception as e:
        print("Failed to parse process json:", e)

    # 2. Listening Ports
    net_cmd = """
    Get-NetTCPConnection | Where-Object { $_.State -eq 'Listen' } |
    Select-Object OwningProcess, LocalAddress, LocalPort | ConvertTo-Json
    """
    net_json = run_powershell(net_cmd)
    try:
        conns = json.loads(net_json)
        if isinstance(conns, dict):
            conns = [conns]
        antigravity_pids = {p.get('ProcessId') for p in procs}
        print(f"\nListening ports owned by Antigravity processes:")
        for c in conns:
            if c.get('OwningProcess') in antigravity_pids:
                print(f"  PID: {c.get('OwningProcess')} listening on {c.get('LocalAddress')}:{c.get('LocalPort')}")
    except Exception as e:
        print("Failed to parse net connections:", e)

    # 3. DevTools Active Port
    dt_path = os.path.expandvars(r"%APPDATA%\Antigravity\DevToolsActivePort")
    if os.path.exists(dt_path):
        with open(dt_path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines()]
        print(f"\nDevToolsActivePort file content: {lines}")

    print("\n" + "="*60 + "\n")

if __name__ == "__main__":
    for i in range(1, 4):
        collect_process_snapshot(i)
        if i < 3:
            time.sleep(2)

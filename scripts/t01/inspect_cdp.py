import urllib.request
import json
import os

def inspect_cdp():
    dev_tools_path = os.path.expandvars(r"%APPDATA%\Antigravity\DevToolsActivePort")
    if not os.path.exists(dev_tools_path):
        print(f"DevToolsActivePort not found at {dev_tools_path}")
        return

    with open(dev_tools_path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]

    if not lines:
        print("DevToolsActivePort is empty")
        return

    port = lines[0]
    browser_ws = lines[1] if len(lines) > 1 else ""
    print(f"DevToolsActivePort Port: {port}, WS path: {browser_ws}")

    try:
        url = f"http://127.0.0.1:{port}/json/version"
        with urllib.request.urlopen(url) as resp:
            data = json.loads(resp.read().decode())
            print(f"\n/json/version response:")
            for k, v in data.items():
                print(f"  {k}: {v}")
    except Exception as e:
        print(f"Error querying /json/version: {e}")

    try:
        url = f"http://127.0.0.1:{port}/json/list"
        with urllib.request.urlopen(url) as resp:
            targets = json.loads(resp.read().decode())
            print(f"\n/json/list response ({len(targets)} targets):")
            for i, t in enumerate(targets):
                print(f"Target #{i+1}:")
                print(f"  id: {t.get('id')}")
                print(f"  type: {t.get('type')}")
                print(f"  title: {t.get('title')}")
                print(f"  url: {t.get('url')}")
                print(f"  webSocketDebuggerUrl: {t.get('webSocketDebuggerUrl')}")
    except Exception as e:
        print(f"Error querying /json/list: {e}")

if __name__ == "__main__":
    inspect_cdp()

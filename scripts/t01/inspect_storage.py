import json
import os
import sys

def inspect_app_storage():
    path = os.path.expandvars(r"%APPDATA%\Antigravity\app_storage.json")
    if not os.path.exists(path):
        print(f"File not found: {path}")
        return

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"Top-level keys in {path}:")
    for k in sorted(data.keys()):
        val = data[k]
        val_type = type(val).__name__
        if isinstance(val, dict):
            print(f"  - {k} (dict, {len(val)} keys): {list(val.keys())[:10]}")
        elif isinstance(val, list):
            print(f"  - {k} (list, {len(val)} items)")
        else:
            print(f"  - {k} ({val_type})")

if __name__ == "__main__":
    inspect_app_storage()

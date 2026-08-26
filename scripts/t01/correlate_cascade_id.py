import os
import re
import glob
import sqlite3
import json
import urllib.request
import urllib.parse

def parse_proto_simple(data):
    idx = 0
    length = len(data)
    fields = {}
    while idx < length:
        shift = 0
        key = 0
        while True:
            if idx >= length: break
            byte = data[idx]
            idx += 1
            key |= (byte & 0x7F) << shift
            if not (byte & 0x80): break
            shift += 7
        if idx > length: break
        field_num = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            val = 0
            shift = 0
            while True:
                if idx >= length: break
                byte = data[idx]
                idx += 1
                val |= (byte & 0x7F) << shift
                if not (byte & 0x80): break
                shift += 7
            fields.setdefault(field_num, []).append(("varint", val))
        elif wire_type == 2:
            val_len = 0
            shift = 0
            while True:
                if idx >= length: break
                byte = data[idx]
                idx += 1
                val_len |= (byte & 0x7F) << shift
                if not (byte & 0x80): break
                shift += 7
            val_bytes = data[idx:idx+val_len]
            idx += val_len
            fields.setdefault(field_num, []).append(("bytes", val_bytes))
        elif wire_type == 1:
            val_bytes = data[idx:idx+8]
            idx += 8
            fields.setdefault(field_num, []).append(("64bit", val_bytes))
        elif wire_type == 5:
            val_bytes = data[idx:idx+4]
            idx += 4
            fields.setdefault(field_num, []).append(("32bit", val_bytes))
        else:
            break
    return fields

def extract_proto_cascade_ids():
    pb_path = os.path.join(os.environ.get("USERPROFILE", ""), ".gemini", "antigravity", "agyhub_summaries_proto.pb")
    if not os.path.exists(pb_path):
        return {}
    with open(pb_path, "rb") as f:
        raw = f.read()
    top = parse_proto_simple(raw)
    summaries = {}
    for item in top.get(1, []):
        f = parse_proto_simple(item[1])
        cid_field1 = f.get(1, [[None, b""]])[0][1].decode('utf-8', errors='ignore')
        sub2_bytes = f.get(2, [[None, b""]])[0][1]
        sub2 = parse_proto_simple(sub2_bytes)
        title = ""
        if 1 in sub2:
            try: title = sub2[1][0][1].decode('utf-8', errors='ignore')
            except: pass
        sub17_list = sub2.get(17, [])
        cascade_id_in_sub17 = None
        if sub17_list:
            sub17 = parse_proto_simple(sub17_list[0][1])
            if 6 in sub17:
                try: cascade_id_in_sub17 = sub17[6][0][1].decode('utf-8', errors='ignore')
                except: pass
        summaries[cid_field1] = {
            "title": title,
            "cascade_id_in_sub17": cascade_id_in_sub17
        }
    return summaries

CONVERSATION_ROUTE_EXACT_PATTERN = re.compile(r"^/c/([0-9a-fA-F-]{36})$")

def extract_cdp_conversation_targets(raw_targets=None):
    if raw_targets is None:
        dt_path = os.path.join(os.environ.get("APPDATA", ""), "Antigravity", "DevToolsActivePort")
        if not os.path.exists(dt_path):
            return []
        with open(dt_path, "r", encoding="utf-8") as f:
            port = f.readline().strip()
        try:
            url = f"http://127.0.0.1:{port}/json/list"
            with urllib.request.urlopen(url, timeout=2) as resp:
                raw_targets = json.loads(resp.read().decode())
        except Exception:
            return []

    eligible_pages = []
    for t in raw_targets:
        if t.get("type") == "page":
            raw_url = t.get("url", "")
            parsed = urllib.parse.urlparse(raw_url)
            m = CONVERSATION_ROUTE_EXACT_PATTERN.match(parsed.path)
            if m:
                eligible_pages.append({
                    "target_id": t.get("id"),
                    "cascade_id": m.group(1).lower(),
                    "raw_url": raw_url,
                    "title": t.get("title", "")
                })
    return eligible_pages

def correlate_all():
    user_prof = os.environ.get("USERPROFILE", "")
    convos_dir = os.path.join(user_prof, ".gemini", "antigravity", "conversations")
    brain_dir = os.path.join(user_prof, ".gemini", "antigravity", "brain")
    
    db_files = glob.glob(os.path.join(convos_dir, "*.db"))
    proto_summaries = extract_proto_cascade_ids()
    cdp_conversation_targets = extract_cdp_conversation_targets()
    
    print("=== Conversation Identifier Correlation Analysis ===")
    print(f"Total SQLite DBs found: {len(db_files)}")
    print(f"Total Proto Summaries entries: {len(proto_summaries)}")
    print(f"Total Eligible CDP Conversation Page Targets: {len(cdp_conversation_targets)}\n")

    local_four_way_matches = 0
    active_cdp_matches = 0
    
    for db_path in sorted(db_files):
        filename = os.path.basename(db_path)
        cid = os.path.splitext(filename)[0].lower()
        
        cid_from_meta = None
        tid_from_meta = None
        try:
            conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
            c = conn.cursor()
            c.execute("SELECT cascade_id, trajectory_id FROM trajectory_meta")
            row = c.fetchone()
            if row:
                cid_from_meta = row[0].lower() if row[0] else None
                tid_from_meta = row[1]
            conn.close()
        except Exception as e:
            cid_from_meta = f"ERROR: {e}"

        brain_exists = os.path.isdir(os.path.join(brain_dir, cid))
        proto_entry = proto_summaries.get(cid)
        proto_matched = (proto_entry is not None and (proto_entry.get("cascade_id_in_sub17") or "").lower() == cid)
        cdp_matched = any(t["cascade_id"] == cid for t in cdp_conversation_targets)

        local_4way = (cid == cid_from_meta and brain_exists and proto_matched)
        if local_4way:
            local_four_way_matches += 1
        if cdp_matched:
            active_cdp_matches += 1
            
        status = "[LOCAL-4WAY-PASS]" if local_4way else "[LOCAL-MISMATCH]"
        cdp_status = "ACTIVE_FOREGROUND_PAGE" if cdp_matched else "background_or_closed"
        print(f"{status} Cascade ID: {cid}")
        print(f"  - DB Filename: {filename}")
        print(f"  - trajectory_meta: cascade_id={cid_from_meta}, trajectory_id={tid_from_meta}")
        print(f"  - Brain Dir: {'EXISTS' if brain_exists else 'MISSING'}")
        print(f"  - Proto Index: {'MATCHED' if proto_matched else 'NOT MATCHED'}")
        print(f"  - CDP Foreground Status: {cdp_status}")
        print()

    print("--- Correlation Summary ---")
    print(f"1. LOCAL FOUR-WAY CORRELATION (DB filename + trajectory_meta + brain dir + proto index): {local_four_way_matches}/{len(db_files)}")
    print(f"2. ACTIVE FOREGROUND CDP CORRELATION (Exact Conversation Page Target UUID vs Cascade ID): {active_cdp_matches}/{len(cdp_conversation_targets)} eligible target(s)")

if __name__ == "__main__":
    correlate_all()

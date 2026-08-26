import os
import glob
import sqlite3
import json
import urllib.request
import re

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
    pb_path = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb")
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

def extract_cdp_active_urls():
    dt_path = os.path.expandvars(r"%APPDATA%\Antigravity\DevToolsActivePort")
    if not os.path.exists(dt_path):
        return []
    with open(dt_path, "r", encoding="utf-8") as f:
        port = f.readline().strip()
    try:
        url = f"http://127.0.0.1:{port}/json/list"
        with urllib.request.urlopen(url, timeout=2) as resp:
            targets = json.loads(resp.read().decode())
            return [t.get("url", "") for t in targets if "url" in t]
    except Exception as e:
        return []

def correlate_all():
    convos_dir = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity\conversations")
    brain_dir = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity\brain")
    
    db_files = glob.glob(os.path.join(convos_dir, "*.db"))
    proto_summaries = extract_proto_cascade_ids()
    cdp_urls = extract_cdp_active_urls()
    
    print("=== Cross-Source Cascade ID Correlation Matrix ===")
    print(f"Total SQLite DBs found: {len(db_files)}")
    print(f"Total Proto Summaries entries: {len(proto_summaries)}")
    print(f"Total CDP Targets URLs: {len(cdp_urls)}\n")

    correlated_results = []
    
    for db_path in sorted(db_files):
        filename = os.path.basename(db_path)
        cid_from_filename = os.path.splitext(filename)[0]
        
        # 1. SQLite trajectory_meta
        cid_from_meta = None
        tid_from_meta = None
        try:
            conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
            c = conn.cursor()
            c.execute("SELECT cascade_id, trajectory_id FROM trajectory_meta")
            row = c.fetchone()
            if row:
                cid_from_meta = row[0]
                tid_from_meta = row[1]
            conn.close()
        except Exception as e:
            cid_from_meta = f"ERROR: {e}"

        # 2. Brain directory existence
        brain_path = os.path.join(brain_dir, cid_from_filename)
        brain_exists = os.path.isdir(brain_path)

        # 3. Proto summaries existence
        proto_entry = proto_summaries.get(cid_from_filename)
        proto_matched = (proto_entry is not None and proto_entry.get("cascade_id_in_sub17") == cid_from_filename)

        # 4. CDP URL match
        cdp_matched = any(f"/c/{cid_from_filename}" in u for u in cdp_urls)

        # Validation
        all_match = (cid_from_filename == cid_from_meta and brain_exists and proto_matched)
        
        record = {
            "cascade_id": cid_from_filename,
            "filename_matches_table": (cid_from_filename == cid_from_meta),
            "brain_dir_exists": brain_exists,
            "proto_index_matches": proto_matched,
            "cdp_active": cdp_matched,
            "trajectory_id": tid_from_meta,
            "title": proto_entry.get("title") if proto_entry else None
        }
        correlated_results.append(record)
        
        status_sym = "[PASS]" if all_match else "[MISMATCH]"
        print(f"{status_sym} Cascade ID: {cid_from_filename}")
        print(f"  - DB Filename: {filename}")
        print(f"  - trajectory_meta: cascade_id={cid_from_meta}, trajectory_id={tid_from_meta}")
        print(f"  - Brain Directory: {'EXISTS' if brain_exists else 'MISSING'}")
        print(f"  - Proto Index: {'MATCHED' if proto_matched else 'NOT MATCHED'}")
        print(f"  - Active CDP URL: {'ACTIVE IN RENDERER' if cdp_matched else 'Not active in foreground'}")
        print()

    total_pass = sum(1 for r in correlated_results if r['filename_matches_table'] and r['brain_dir_exists'] and r['proto_index_matches'])
    print(f"Total Fully Correlated Across Filesystem, DB & Proto: {total_pass}/{len(correlated_results)}")

if __name__ == "__main__":
    correlate_all()

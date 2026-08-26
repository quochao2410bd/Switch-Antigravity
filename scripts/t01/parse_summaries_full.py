import os
import struct

def parse_proto(data):
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

def decode_entry(bytes_val):
    fields = parse_proto(bytes_val)
    res = {}
    for fnum, items in fields.items():
        for ftype, fval in items:
            if ftype == "bytes":
                try:
                    res[fnum] = fval.decode('utf-8')
                except:
                    # try recursive
                    res[fnum] = f"nested({len(fval)}b)"
            elif ftype == "varint":
                res[fnum] = fval
    return res

pb_path = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb")
with open(pb_path, "rb") as f:
    raw = f.read()

top = parse_proto(raw)
print(f"Total entries in summaries proto: {len(top.get(1, []))}")
for item in top.get(1, []):
    val = item[1]
    f = parse_proto(val)
    cid = f.get(1, [[None, b""]])[0][1].decode('utf-8')
    sub2_bytes = f.get(2, [[None, b""]])[0][1]
    sub2 = parse_proto(sub2_bytes)
    print(f"\n--- Conversation: {cid} ---")
    for k, vlist in sub2.items():
        for t, v in vlist:
            if t == "bytes":
                try:
                    txt = v.decode('utf-8')
                    print(f"  Field {k}: {txt}")
                except:
                    sub3 = parse_proto(v)
                    print(f"  Field {k} (nested {len(v)}b): subkeys {list(sub3.keys())}")
                    for sk, svlist in sub3.items():
                        for st, sv in svlist:
                            if st == "bytes":
                                try:
                                    print(f"    Subfield {sk}: {sv.decode('utf-8')}")
                                except:
                                    print(f"    Subfield {sk}: <binary {len(sv)}b>")
                            else:
                                print(f"    Subfield {sk}: {sv}")
            else:
                print(f"  Field {k}: {v}")


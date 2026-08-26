import os
import struct

def parse_proto_tags(data, indent=0):
    idx = 0
    length = len(data)
    fields = []
    while idx < length:
        # read varint for key
        shift = 0
        key = 0
        while True:
            if idx >= length: break
            byte = data[idx]
            idx += 1
            key |= (byte & 0x7F) << shift
            if not (byte & 0x80): break
            shift += 7
        
        field_num = key >> 3
        wire_type = key & 0x07
        if wire_type == 0: # Varint
            val = 0
            shift = 0
            while True:
                if idx >= length: break
                byte = data[idx]
                idx += 1
                val |= (byte & 0x7F) << shift
                if not (byte & 0x80): break
                shift += 7
            fields.append((field_num, "varint", val))
        elif wire_type == 2: # Length-delimited
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
            fields.append((field_num, "bytes", val_bytes))
        elif wire_type == 1: # 64-bit
            val_bytes = data[idx:idx+8]
            idx += 8
            fields.append((field_num, "64bit", val_bytes))
        elif wire_type == 5: # 32-bit
            val_bytes = data[idx:idx+4]
            idx += 4
            fields.append((field_num, "32bit", val_bytes))
        else:
            break
    return fields

pb_path = os.path.expandvars(r"%USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb")
with open(pb_path, "rb") as f:
    raw = f.read()

top_fields = parse_proto_tags(raw)
print(f"Top-level proto fields count: {len(top_fields)}")
for num, wtype, val in top_fields[:5]:
    print(f"Field {num} ({wtype}): length={len(val) if isinstance(val, bytes) else val}")
    if isinstance(val, bytes) and len(val) > 0:
        sub = parse_proto_tags(val)
        for snum, swtype, sval in sub[:10]:
            if swtype == "bytes":
                try:
                    text = sval.decode('utf-8')
                    print(f"   Sub {snum} (str): {text}")
                except:
                    print(f"   Sub {snum} (bytes len {len(sval)})")
            else:
                print(f"   Sub {snum} ({swtype}): {sval}")

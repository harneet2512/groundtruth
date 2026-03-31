import json, sys

bl = json.load(open(sys.argv[1]))
print(f"Total: {len(bl)}")

# Show first 3
for k, v in list(bl.items())[:3]:
    print(f"\n--- {k} ---")
    if isinstance(v, dict):
        print(f"keys: {list(v.keys())}")
        mp = v.get("model_patch", "")
        print(f"model_patch ({len(mp)} chars): {mp[:200]}")
    else:
        print(f"type: {type(v).__name__}, len: {len(str(v))}")
        print(str(v)[:200])

# Count categories
valid = 0
empty = 0
has_patch_key = 0
for k, v in bl.items():
    if isinstance(v, dict):
        has_patch_key += 1
        mp = v.get("model_patch", "")
        if mp and mp.strip():
            valid += 1
        else:
            empty += 1
    elif isinstance(v, str) and v.strip():
        valid += 1
    else:
        empty += 1

print(f"\nDict format: {has_patch_key}")
print(f"Valid (non-empty content): {valid}")
print(f"Empty: {empty}")

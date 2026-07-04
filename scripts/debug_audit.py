#!/usr/bin/env python3
import json
import os

OUTPUT_FILES = [
    "benchmarks/openhands/cal20_live_lite/output.jsonl",
    ".tmp_oh_smoke_output.jsonl"
]

def main():
    for out_file in OUTPUT_FILES:
        if os.path.exists(out_file):
            print(f"--- {out_file} ---")
            with open(out_file, "r", encoding="utf-8") as f:
                for line in f:
                    rec = json.loads(line)
                    iid = rec.get("instance_id")
                    if iid == "aws-cloudformation__cfn-lint-3817":
                        print(f"ID: {iid}")
                        print(f"Keys: {rec.keys()}")
                        print(f"Brief: {repr(rec.get('gt_brief'))}")
                        print(f"Instruction[:200]: {repr(rec.get('instruction')[:200])}")
                        break

if __name__ == "__main__":
    main()

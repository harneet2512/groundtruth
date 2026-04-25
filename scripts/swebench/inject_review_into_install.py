#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inject gt_review_patch.py call into GT install.sh presubmit wrapper."""

INSTALL_PATH = "/tmp/SWE-agent/tools/groundtruth/install.sh"

OLD_PATTERN = 'rm -f "$ATTEMPTS_FILE" 2>/dev/null || true\nexec "/root/tools/review_on_submit_m/bin/submit.real" "$@"'

NEW_BLOCK = '''rm -f "$ATTEMPTS_FILE" 2>/dev/null || true

# GT vNext review_patch - runs before final submit
if [ -f /tmp/gt_review_patch.py ]; then
    python3 /tmp/gt_review_patch.py
    GT_RP_RC=$?
    if [ "$GT_RP_RC" -eq 0 ]; then
        # review_patch printed findings and wants to pause submit
        exit 0
    fi
fi

exec "/root/tools/review_on_submit_m/bin/submit.real" "$@"'''

def main():
    with open(INSTALL_PATH) as f:
        content = f.read()

    if "gt_review_patch" in content:
        print("ALREADY INJECTED")
        return

    if OLD_PATTERN in content:
        content = content.replace(OLD_PATTERN, NEW_BLOCK)
        with open(INSTALL_PATH, "w") as f:
            f.write(content)
        print("INJECTED: gt_review_patch.py call into install.sh")
        return

    # Try to find the exec line
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if "exec" in line and "submit.real" in line:
            print("Found exec at line %d: %s" % (i, line))
            print("Injecting before it...")
            inject = [
                "",
                "# GT vNext review_patch - runs before final submit",
                "if [ -f /tmp/gt_review_patch.py ]; then",
                "    python3 /tmp/gt_review_patch.py",
                "    GT_RP_RC=$?",
                '    if [ "$GT_RP_RC" -eq 0 ]; then',
                "        exit 0",
                "    fi",
                "fi",
                "",
            ]
            lines = lines[:i] + inject + lines[i:]
            with open(INSTALL_PATH, "w") as f:
                f.write("\n".join(lines))
            print("INJECTED via line-number fallback")
            return

    print("ERROR: could not find injection point")

if __name__ == "__main__":
    main()

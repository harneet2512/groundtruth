"""Debug: check what on_continue would see in tool messages."""
import glob
import re
from inspect_ai.log import read_eval_log
from inspect_ai.model import ChatMessageTool

evals = sorted(glob.glob("/tmp/inspect_t2_gt/*.eval"))
log = read_eval_log(evals[-1])
s = log.samples[0]

for i, m in enumerate(s.messages):
    if not isinstance(m, ChatMessageTool):
        continue
    fn = getattr(m, "function", "")
    c = str(getattr(m, "content", ""))
    if "groundtruth" in fn:
        continue

    if fn == "text_editor":
        if "has been edited" in c:
            match = re.search(r"(?:file\s+)(/\S+\.\w+)", c[:300])
            fp = match.group(1) if match else "NO_MATCH"
            print(f"MSG[{i}] EDIT: {fp}")
            print(f"  content[:150]: {c[:150]}")
        elif "cat -n" in c[:200]:
            match = re.search(r"cat -n[`]?\s+(?:on\s+)?(/\S+\.\w+)", c[:300])
            fp = match.group(1) if match else "NO_MATCH"
            print(f"MSG[{i}] VIEW: {fp}")
        else:
            print(f"MSG[{i}] TEXT_EDITOR_OTHER: {c[:100]}")
    elif fn == "bash_session":
        match = re.search(r"(/testbed/\S+\.\w+)", c[:500])
        if match:
            print(f"MSG[{i}] BASH: {match.group(1)}")

print(f"\nTotal messages: {len(s.messages)}")
print(f"Tool messages: {sum(1 for m in s.messages if isinstance(m, ChatMessageTool))}")

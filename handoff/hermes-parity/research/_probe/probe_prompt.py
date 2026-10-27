import sqlite3, re, io, sys

p = r'E:\Hermes Agent CN Desktop\data\hermes-home\state.db'
soul = open(r'E:\Hermes Agent CN Desktop\data\hermes-home\SOUL.md', encoding='utf-8').read()
c = sqlite3.connect('file:' + p + '?mode=ro&immutable=1', uri=True)

rows = c.execute("select hash, prompt from system_prompts").fetchall()
print('n prompts =', len(rows))
for h, pr in rows:
    print(h[:12], 'len=%d' % len(pr), '| soul_is_prefix=%s' % pr.startswith(soul.strip()[:120]))

# pick the largest (most complete) prompt and dump headings
h, pr = max(rows, key=lambda r: len(r[1]))
print('\n=== chosen prompt', h[:12], 'len', len(pr))
out = r'E:\bbbooottt\handoff\hermes-parity\research\_probe\system_prompt_sample.txt'
open(out, 'w', encoding='utf-8').write(pr)
print('written to', out)

print('\n=== markdown/heading-ish lines (first 120) ===')
n = 0
for i, line in enumerate(pr.splitlines()):
    s = line.strip()
    if not s:
        continue
    if s.startswith('#') or s.startswith('==') or s.endswith(':') and len(s) < 60 or re.match(r'^[A-Z][A-Za-z0-9 /&()\-]{2,48}$', s):
        print('%5d | %s' % (i, s[:100]))
        n += 1
        if n > 120:
            break

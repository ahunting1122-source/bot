import re, io

p = r'E:\bbbooottt\handoff\hermes-parity\research\_probe\system_prompt_sample.txt'
txt = open(p, encoding='utf-8').read()
lines = txt.splitlines()
out = []
out.append('total chars=%d lines=%d' % (len(txt), len(lines)))
out.append('--- section markers ---')
for i, l in enumerate(lines, 1):
    s = l.strip()
    if (s.startswith('# ') or s.startswith('## ')
            or s.startswith('<available_skills') or s == '</available_skills>'
            or s.startswith('════') or re.match(r'^[A-Z][A-Z ()/&-]{4,60}$', s)):
        out.append('%5d | %s' % (i, s[:110]))
# char offsets of key blocks
for marker in ['<available_skills>', 'MEMORY (your personal notes)', 'USER PROFILE (who the user is)',
               '# Project Context', 'Conversation started:', 'Skills', 'SKILL']:
    idx = txt.find(marker)
    out.append('offset %-34s = %s' % (marker, idx))
open(r'E:\bbbooottt\handoff\hermes-parity\research\_probe\outline.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out[:8]))
print('outline lines =', len(out))

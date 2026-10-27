import json

p = r'E:\Hermes Agent CN Desktop\data\hermes-home\.skills_prompt_snapshot.json'
d = json.load(open(p, encoding='utf-8'))
print('version =', d['version'])
man = d['manifest']
print('manifest keys =', list(man.keys()))
for k, v in man.items():
    print('  manifest.%s = %r' % (k, (v if not isinstance(v, (list, dict)) else ('len=%d' % len(v)))))

sk = d['skills']
print('skills entries =', len(sk))
print('skill[0] keys =', list(sk[0].keys()))
print(json.dumps(sk[0], ensure_ascii=False, indent=1)[:1600])
print('--- category_descriptions ---')
for k, v in d['category_descriptions'].items():
    print('  ', k, '->', (v[:90] if isinstance(v, str) else v))

# measure the rendered size of the skills index the prompt would carry
names = [s.get('name') for s in sk]
print('names count', len(names))
total = 0
for s in sk:
    total += len(json.dumps(s, ensure_ascii=False))
print('sum of per-entry json chars =', total)
print('raw file bytes =', len(open(p, 'rb').read()))

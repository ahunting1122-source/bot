import json, os

d0 = r'E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18\_internal\hermes_cli\observability\schemas'
out = []
for f in ('hermes.shared_metrics.v1.schema.json', 'hermes.shared_metrics.v2.schema.json'):
    d = json.load(open(os.path.join(d0, f), encoding='utf-8'))
    out.append('=== ' + f)
    out.append(' title: %s | $id: %s' % (d.get('title'), d.get('$id')))
    out.append(' required: %s' % d.get('required'))
    out.append(' resource.properties: %s' % list(d['properties']['resource'].get('properties', {}).keys()))
    mi = d['properties']['metrics'].get('items', {})
    out.append(' metric item required: %s' % mi.get('required'))
    out.append(' metric item properties: %s' % list(mi.get('properties', {}).keys()))
    defs = d.get('$defs', {})
    out.append(' $defs: %s' % list(defs.keys()))
    for k, v in list(defs.items())[:6]:
        props = list(v.get('properties', {}).keys())
        out.append('   def %s: props=%s required=%s' % (k, props, v.get('required')))
    # metric names enum if present
    p = mi.get('properties', {})
    for key in ('name', 'metric', 'metric_name', 'kind'):
        if key in p:
            out.append('   metric.%s = %s' % (key, json.dumps(p[key])[:600]))
open(r'E:\bbbooottt\handoff\hermes-parity\research\_probe\schemas.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('ok', len(out))

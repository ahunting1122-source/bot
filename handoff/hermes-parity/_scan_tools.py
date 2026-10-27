import pathlib
import re

root = pathlib.Path(r"E:\bbbooottt\backend")
files = [
    "app/tools.py",
    "app/browser.py",
    "app/connectors.py",
    "app/graph/nodes.py",
    "app/mcp.py",
    "app/policies.py",
]
pat = re.compile(r'Tool\(\s*"([a-z_0-9]+)"')
seen: dict[str, list[str]] = {}
for rel in files:
    p = root / rel
    if not p.exists():
        continue
    text = p.read_text(encoding="utf-8")
    for m in pat.finditer(text):
        seen.setdefault(m.group(1), []).append(rel)
for name in sorted(seen):
    print(f"{name:28s} {' '.join(sorted(set(seen[name])))}")
print("total:", len(seen))

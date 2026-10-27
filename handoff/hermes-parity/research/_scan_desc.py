import ast, json
from pathlib import Path

T = Path(r"E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18\_internal\tools")

def strval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            else:
                parts.append("{...}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        l, r = strval(node.left), strval(node.right)
        if l and r:
            return l + r
    return None

found = {}
for p in sorted(T.glob("*.py")):
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        kv = {}
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                kv[k.value] = v
        if "name" not in kv:
            continue
        name = strval(kv["name"])
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            continue
        desc = strval(kv.get("description")) if "description" in kv else None
        params = []
        pnode = kv.get("parameters")
        if isinstance(pnode, ast.Dict):
            for pk, pv in zip(pnode.keys, pnode.values):
                if isinstance(pk, ast.Constant) and pk.value == "properties" and isinstance(pv, ast.Dict):
                    for ak in pv.keys:
                        if isinstance(ak, ast.Constant):
                            params.append(ak.value)
        cur = found.get(name)
        if cur is None or (desc and len(desc) > len(cur.get("desc") or "")):
            found[name] = {"name": name, "desc": desc, "params": params,
                           "file": p.name, "line": node.lineno}

Path(r"E:\bbbooottt\handoff\hermes-parity\research\_tool_desc.json").write_text(
    json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
print(len(found))
for n, v in sorted(found.items()):
    print("%-38s %-26s %s | params=%s" % (n, v["file"], (v["desc"] or "")[:110].replace("\n", " "), ",".join(v["params"][:8])))

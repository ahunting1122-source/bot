import ast, json, os, sys, io
from pathlib import Path

T = Path(r"E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18\_internal\tools")

def consts_of(tree):
    d = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                d[node.targets[0].id] = node.value.value
    return d

def resolve(node, consts):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    if isinstance(node, ast.JoinedStr):
        return None
    return None

rows = []
dynamic = []

for p in sorted(T.glob("*.py")):
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except Exception as e:
        dynamic.append({"file": p.name, "err": str(e)})
        continue
    consts = consts_of(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = None
        if isinstance(fn, ast.Attribute):
            fname = fn.attr
        elif isinstance(fn, ast.Name):
            fname = fn.id
        if fname != "register":
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        name = resolve(kw.get("name"), consts) if "name" in kw else None
        toolset = resolve(kw.get("toolset"), consts) if "toolset" in kw else None
        desc = resolve(kw.get("description"), consts) if "description" in kw else None
        schema = None
        if "schema" in kw:
            schema = resolve(kw["schema"], consts)
        rows.append({
            "name": name, "toolset": toolset, "description": desc,
            "schema_const": schema, "file": p.name, "line": node.lineno,
        })

# schema dict descriptions: find dict literals assigned to name "X_SCHEMA" and read description
schemas = {}
for p in sorted(T.glob("*.py")):
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            tgt = node.targets[0].id
            if tgt.endswith("_SCHEMA") and isinstance(node.value, ast.Dict):
                d = {}
                for k, v in zip(node.value.keys, node.value.values):
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                        d[k.value] = v.value
                    elif isinstance(k, ast.Constant) and k.value == "parameters" and isinstance(v, ast.Dict):
                        props = {}
                        for pk, pv in zip(v.keys, v.values):
                            if isinstance(pk, ast.Constant) and pk.value == "properties" and isinstance(pv, ast.Dict):
                                for ak, av in zip(pv.keys, pv.values):
                                    if isinstance(ak, ast.Constant):
                                        props[ak.value] = True
                        d["_params"] = list(props.keys())
                d["_file"] = p.name
                schemas[tgt] = d

out = {"tools": rows, "schemas": schemas, "errors": dynamic}
Path(r"E:\bbbooottt\handoff\hermes-parity\research\_tools_raw.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("register calls:", len(rows))
print("named:", sum(1 for r in rows if r["name"]))
print("schemas:", len(schemas))
for r in rows:
    if not r["name"]:
        print("DYNAMIC:", r["file"], r["line"])

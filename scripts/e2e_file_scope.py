"""文件访问级别 —— 真实接口 e2e。

两段，各证明一件事：

    A. 打**真实运行中的后端**（127.0.0.1:6185）：走前端同款链路
       `GET /api/session → PUT /api/connectors → GET /api/connectors → /api/health`，
       并核对磁盘上的 settings.json 真的落盘、坏值被 422 拒绝。

  B. 用**真实代码 + 真实工作目录**直接驱动工具层与系统提示，验证两档确实
     "一个放行、一个收窄"：文件读写、命令执行（cwd 与不可逆拦截表）都逐条对到底。
"""

import asyncio
import http.cookiejar
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

BASE = "http://127.0.0.1:6185"
PROBE_READ = Path("E:/_orbit_scope_probe.txt")
PROBE_WRITE = Path("E:/_orbit_scope_write.txt")
FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


# --------------------------------------------------------------------------- A
class Client:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, payload: object | None = None):
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            BASE + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            try:
                parsed: object = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            return error.code, parsed


def part_a() -> str:
    print("\n=== A. 真实后端 HTTP 往返 ===")
    client = Client()
    status, _ = client.call("GET", "/api/session")
    check("建立本地会话", status == 200, f"status={status}")

    status, health = client.call("GET", "/api/health")
    check("健康检查暴露 file_scope", status == 200 and "file_scope" in (health or {}), str(health))
    check("健康检查暴露命令执行能力", (health or {}).get("command_execution") is True, str(health))
    original = (health or {}).get("file_scope", "workspace")

    status, before = client.call("GET", "/api/connectors")
    check("读取初始配置", status == 200 and before.get("file_scope") == original,
          f"scope={before.get('file_scope')} root={before.get('file_root')!r}")

    status, after = client.call("PUT", "/api/connectors", {"file_scope": "machine"})
    check("切到完全访问权限", status == 200 and after.get("file_scope") == "machine", str(after.get("file_scope")))

    _, reread = client.call("GET", "/api/connectors")
    check("回读为 machine", reread.get("file_scope") == "machine")
    check("machine 档 file_root 为空串", reread.get("file_root") == "", repr(reread.get("file_root")))

    _, health2 = client.call("GET", "/api/health")
    check("健康检查同步为 machine", health2.get("file_scope") == "machine", str(health2.get("file_scope")))

    disk = REPO / "data" / "settings.json"
    on_disk = json.loads(disk.read_text(encoding="utf-8")) if disk.exists() else {}
    check("settings.json 已落盘 machine", on_disk.get("file_scope") == "machine", str(on_disk.get("file_scope")))

    status, bad = client.call("PUT", "/api/connectors", {"file_scope": "full"})
    check("非法档位被 422 拒绝", status == 422, f"status={status} detail={bad}")

    _, restored = client.call("PUT", "/api/connectors", {"file_scope": original})
    check("恢复原始档位", restored.get("file_scope") == original, str(restored.get("file_scope")))
    return original


# --------------------------------------------------------------------------- B
async def part_b(workspace: Path) -> None:
    print("\n=== B. 工具层 + 系统提示（真实工作目录） ===")
    from app.config import Settings
    from app.context import build_prefix
    from app.main import migrate
    from app.storage import Conversation, Store, Task
    from app.tools import ToolRegistry, machine_roots

    for stale in (PROBE_READ, PROBE_WRITE):
        stale.unlink(missing_ok=True)
    PROBE_READ.write_text("orbit-scope-probe", encoding="utf-8")
    outside = PROBE_READ
    check("探针文件在工作目录之外", not str(outside).startswith(str(workspace)),
          f"{outside} vs {workspace}")

    tmp = Path(tempfile.mkdtemp(prefix="orbit_e2e_"))
    settings = Settings(data_dir=tmp, workspace=workspace, file_scope="machine")
    settings.prepare()
    migrate(settings)
    store = Store(tmp / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="e2e"))
    machine = ToolRegistry(settings, store)

    # --- 完全访问档 ---
    doc = await machine.local("local_read_document", {"path": str(outside)}, task.id)
    check("machine 读到工作目录外的文件", doc.get("text") == "orbit-scope-probe", str(doc.get("path")))

    listing = await machine.local("local_list_files", {"path": "."}, task.id)
    drives = machine_roots()
    check("machine 列目录给出盘符入口", [e["path"] for e in listing] == [str(d) for d in drives],
          f"{len(listing)} 项")

    wrote = await machine.local(
        "local_write_file", {"path": str(PROBE_WRITE), "content": "ok"}, task.id
    )
    check("machine 写入工作目录外成功", PROBE_WRITE.exists() and wrote.get("verified") is True)

    refused_dir = False
    try:
        await machine.local("local_write_file", {"path": str(tmp), "content": "x"}, task.id)
    except ValueError:
        refused_dir = True
    check("machine 拒绝把目录当文件写", refused_dir)

    # 完全访问档"不设限"：覆盖既有文件无需 overwrite，工作台自身配置也一并交出去。
    again = await machine.local(
        "local_write_file", {"path": str(PROBE_WRITE), "content": "改写"}, task.id
    )
    check(
        "machine 覆盖既有文件无需 overwrite",
        again.get("verified") is True
        and PROBE_WRITE.read_text(encoding="utf-8") == "改写",
    )
    own = await machine.local(
        "local_write_file",
        {"path": str(tmp / "settings.json"), "content": "{}"},
        task.id,
    )
    check("machine 可写工作台自身配置（不设限）", own.get("verified") is True)

    prompt_machine = await build_prefix(store, task, settings)
    check("系统提示声明完全访问", "完全访问权限" in prompt_machine and "仅工作文件夹" not in prompt_machine)

    # --- 仅工作文件夹档 ---
    narrow_settings = Settings(data_dir=tmp, workspace=workspace, file_scope="workspace")
    narrow_settings.prepare()
    narrow = ToolRegistry(narrow_settings, store)

    blocked = False
    try:
        await narrow.local("local_read_document", {"path": str(outside)}, task.id)
    except ValueError:
        blocked = True
    check("workspace 拒绝读取工作目录外", blocked)

    blocked_write = False
    try:
        await narrow.local(
            "local_write_file", {"path": str(PROBE_WRITE), "content": "z"}, task.id
        )
    except ValueError:
        blocked_write = True
    check("workspace 拒绝写工作目录外", blocked_write)

    inside_write = await narrow.local(
        "local_write_file", {"path": "scope-inside.txt", "content": "目录内"}, task.id
    )
    check(
        "workspace 可写工作目录内的普通文件",
        inside_write.get("verified") is True
        and (workspace / "scope-inside.txt").read_text(encoding="utf-8") == "目录内",
    )
    (workspace / "scope-inside.txt").unlink(missing_ok=True)

    inside = await narrow.local("local_list_files", {"path": "."}, task.id)
    # 有限档下 display_path 能相对授权目录说清楚就相对说；只有越界条目才会以绝对
    # 路径回传。所以"没有绝对路径条目"即"没有一条越出工作目录"。
    check("workspace 列目录不越界", all(
        not Path(e["path"]).is_absolute()
        and (workspace / e["path"]).resolve().is_relative_to(workspace.resolve())
        for e in inside
    ), f"{len(inside)} 项")

    prompt_narrow = await build_prefix(store, task, narrow_settings)
    check("系统提示声明仅工作文件夹并带上授权目录",
          "仅工作文件夹" in prompt_narrow and str(workspace) in prompt_narrow)
    check("系统提示声明命令能力", "run_command" in prompt_machine and "run_command" in prompt_narrow)

    # --- 命令执行（run_command）---
    ran = await machine.local(
        "run_command", {"command": "echo orbit-machine", "cwd": str(outside.parent)}, task.id
    )
    check(
        "machine 在任意工作目录执行命令",
        "orbit-machine" in ran.get("stdout", "") and ran.get("cwd") == str(outside.parent.resolve()),
        f"exit={ran.get('exit_code')}",
    )

    narrow_ran = await narrow.local("run_command", {"command": "echo orbit-narrow"}, task.id)
    check(
        "workspace 命令默认在授权目录执行",
        "orbit-narrow" in narrow_ran.get("stdout", "")
        and narrow_ran.get("cwd") == str(workspace.resolve()),
        str(narrow_ran.get("cwd")),
    )

    escape_denied = False
    try:
        await narrow.local(
            "run_command", {"command": "echo x", "cwd": str(outside.parent)}, task.id
        )
    except ValueError:
        escape_denied = True
    check("workspace 命令 cwd 不能越界", escape_denied)

    # 同一条命令：窄档被不可逆拦截表挡下，完全访问档照跑——这就是"档位 = 开关"的实证。
    # 用 `echo mkfs`（命中拦截表但本身无害）验证，绝不真跑破坏性命令。
    guard_blocked = False
    try:
        await narrow.local("run_command", {"command": "echo mkfs"}, task.id)
    except ValueError as exc:
        guard_blocked = "完全访问权限" in str(exc)
    check("workspace 拦下不可逆命令并提示切档", guard_blocked)

    guard_bypassed = await machine.local("run_command", {"command": "echo mkfs"}, task.id)
    check(
        "machine 完全绕过不可逆拦截表",
        "mkfs" in guard_bypassed.get("stdout", ""),
        f"exit={guard_bypassed.get('exit_code')}",
    )

    # 收尾（sqlite 连接仍开着，删不掉就留着，系统临时目录会自行回收）
    PROBE_READ.unlink(missing_ok=True)
    PROBE_WRITE.unlink(missing_ok=True)
    for leftover in tmp.rglob("*"):
        if leftover.is_file():
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass


async def main() -> None:
    original = part_a()
    client = Client()
    client.call("GET", "/api/session")
    _, config = client.call("GET", "/api/connectors")
    workspace = Path(config.get("workspace") or (REPO / "data" / "workspace"))
    await part_b(workspace)

    print("\n" + ("=" * 48))
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败")
        for name in FAILS:
            print("  - " + name)
        raise SystemExit(1)
    print("结果：全部通过")
    print(f"（原始档位 {original} 已恢复）")


if __name__ == "__main__":
    asyncio.run(main())

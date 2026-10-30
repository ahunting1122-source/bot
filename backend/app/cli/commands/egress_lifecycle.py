"""egress start/stop/status 的真实现（4.8 CLI 接线，第 90 轮）。

## 设计：不用 pid，不用信号

第 88 轮踩到：Windows 上 os.kill(pid, 0) 会真的 TerminateProcess。
所以这一版完全不碰进程管理：
- start 前台 serve_forever()，只把**端口**写进 <data>/egress/port；
- status 读端口文件 → TCP 连一下；
- stop 向代理 POST /__shutdown，**代理自己退出**。

**判据：能不用 pid 就不用 —— pid 会被复用，而端口不会在同一时刻属于两个进程。**
"""

from __future__ import annotations

import http.server
import pathlib
import socket
from typing import Any

from app.egress import EgressPolicy
from app.egress_proxy import EgressProxy, _Handler


def _port_path(data_dir: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(data_dir) / 'egress' / 'port'


def _read_port(data_dir: str | pathlib.Path) -> int:
    path = _port_path(data_dir)
    if not path.is_file():
        return 0
    try:
        return int(path.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return 0


def _probe(port: int, *, host: str = '127.0.0.1', timeout: float = 0.5) -> bool:
    if port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_egress_start(context: Any, args: Any) -> int:
    data_dir = context.data_dir
    policy = EgressPolicy.load(data_dir)
    proxy = EgressProxy(policy)
    port = int(getattr(args, 'port', 0) or 0)
    server = http.server.HTTPServer(('127.0.0.1', port), _Handler)
    server.proxy = proxy  # type: ignore[attr-defined]
    actual = server.server_port
    path = _port_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(actual), encoding='utf-8')
    context.note(
        f'egress 代理已在前台启动：http://127.0.0.1:{actual}\n'
        f'停止：orbit egress stop（或 Ctrl-C）。'
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        path.unlink(missing_ok=True)
        context.note('egress 代理已停止。')
    return 0


def run_egress_stop(context: Any, args: Any) -> int:
    import httpx

    port = _read_port(context.data_dir)
    if not port or not _probe(port):
        _port_path(context.data_dir).unlink(missing_ok=True)
        context.note('egress 代理没在跑（没有端口文件，或端口上没人听）。')
        return 0
    try:
        response = httpx.post(f'http://127.0.0.1:{port}/__shutdown', timeout=10)
    except httpx.HTTPError as exc:
        raise RuntimeError(f'停不掉（端口 {port}）：{exc}') from exc
    if response.status_code != 200:
        raise RuntimeError(f'代理拒绝停止：HTTP {response.status_code}')
    _port_path(context.data_dir).unlink(missing_ok=True)
    context.note(f'已让 egress 代理（端口 {port}）自行停止。')
    return 0


def describe_state(data_dir: str | pathlib.Path) -> dict[str, Any]:
    port = _read_port(data_dir)
    if not port:
        return {'running': False, 'reason': '没有端口文件（没起过，或已正常停止）'}
    if not _probe(port):
        return {'running': False, 'reason': f'端口文件写着 {port}，但那个端口上没人听（陈旧记录）'}
    policy = EgressPolicy.load(data_dir)
    return {'running': True, 'port': port, 'rules': len(getattr(policy, 'rules', ()) or ())}

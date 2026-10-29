"""MATRIX 14.11：用户可配 URL 的出站闸（connectors / peer_client）。

## 按「危害面」扫，不按「调用点」扫

上次我按 `httpx.Client(` 出现次数数出 4 处，**那是错的数法**：

| 位置 | URL 来源 | 要闸吗 |
|---|---|---|
| `connectors.py` 模型连通性检查 | `settings.model_base_url`（**用户可配**） | **要** |
| `connectors.py` GitHub 客户端 | GitHub 固定 API（硬编码） | 不要 |
| `peer_client.py` | `orbit peer add <name> --url …`（**用户可配**） | **要** |
| `platforms.py` | 平台配置（webhook 等用户可配） | 要（下一批） |

**数调用点会把固定 URL 的客户端也算进来，而漏掉真正危险的用户可配 URL。**
"""

from __future__ import annotations

import pathlib

import pytest

from app.egress import refuse_cloud_metadata


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/v1",
        "http://169.254.170.2/v1",
        "http://100.100.100.200/v1",
        "http://metadata.google.internal/v1",
        "http://instance-data/latest",
    ],
)
def test_the_shared_gate_refuses_cloud_metadata(url):
    with pytest.raises(ValueError) as excinfo:
        refuse_cloud_metadata(url)
    assert "云元数据" in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        # **本地 provider 必须放行**——一刀切会打死「配一个 provider 就能用」
        "http://127.0.0.1:11434/v1",
        "http://192.168.1.50:8000/v1",
        "",
    ],
)
def test_normal_and_local_urls_pass(url):
    """**成对**：只测「被拒」的话，一个「永远抛异常」的实现也能过。"""
    refuse_cloud_metadata(url)


def test_the_gate_names_the_call_site():
    """错误信息要指出**是哪条出站路径**被拒——否则用户不知道去哪改配置。"""
    with pytest.raises(ValueError) as excinfo:
        refuse_cloud_metadata("http://169.254.169.254/v1", where="peer 请求")
    assert "peer 请求" in str(excinfo.value)


def test_connectors_and_peer_client_really_call_the_gate():
    """**防回归**：三个文件里必须真的出现调用（不是只 import）。

    14.11 的虚报形状是「闸在别处、这条路没接」——只测 `refuse_cloud_metadata`
    本身不够，要钉住**调用点存在**。
    """
    app = pathlib.Path(__file__).resolve().parent.parent / "app"
    for name in ("connectors.py", "peer_client.py", "platforms.py"):
        source = (app / name).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        assert "refuse_cloud_metadata(" in code, f"{name} 没有调用闸"

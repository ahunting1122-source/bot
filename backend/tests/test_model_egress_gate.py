"""模型出站处的云元数据闸（MATRIX 4.8 邻域，第 52 轮）。

## 为什么这条是「闸门」而不是「说明」

上一轮我在 `EgressPolicy.check` 里加了云元数据硬拦——但**模型这条路根本不查它**：

```
providers.py:1295   from langchain_openai import ChatOpenAI
providers.py:1311   return ChatOpenAI(**kwargs)      ← 一次都没查过 egress
```

`EgressPolicy` 之前只有两个非 CLI 消费者：shell 命令的 host 检查（`tools.py`）
和 `egress check` / `egress_status`（**说明性**命令）。

**而模型这条路才是 API key 真正发出去的地方**：一个恶意或被污染的 `base_url`
（例如指向 `169.254.169.254`）会在这里把凭证直接送出去。
**判据：闸门要放在危害发生的那一步。**
"""

from __future__ import annotations

import pytest

from app.providers import _refuse_cloud_metadata_endpoint


@pytest.mark.parametrize(
    "base_url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://169.254.170.2/v1",
        "http://100.100.100.200/v1",
        "http://metadata.google.internal/v1",
        "http://instance-data/latest",
    ],
)
def test_a_cloud_metadata_base_url_is_refused(base_url):
    """**在发出第一个请求之前就停下**——凭证不该有机会离开进程。"""
    with pytest.raises(ValueError) as excinfo:
        _refuse_cloud_metadata_endpoint(base_url, "openai")
    message = str(excinfo.value)
    assert "云元数据" in message, message
    assert "openai" in message, "错误信息要指出是哪个 provider 的配置"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com/v1",
        "https://api.anthropic.com",
        # **本地 provider 必须放行**：一刀切会打死「配一个 provider 就能用」
        "http://127.0.0.1:11434/v1",
        "http://localhost:1234/v1",
        "http://192.168.1.50:8000/v1",
    ],
)
def test_normal_and_local_base_urls_pass(base_url):
    """本地模型（Ollama / LM Studio / 局域网 gateway）**必须能用**。

    这条与上一条成对：只测「元数据被拒」的话，一个「永远抛异常」的实现也能过。
    """
    _refuse_cloud_metadata_endpoint(base_url, "openai")  # 不该抛


def test_an_empty_base_url_is_not_a_failure():
    """没配 `base_url` 是正常情况（用 provider 默认值），不该被当成配置错误。"""
    _refuse_cloud_metadata_endpoint("", "openai")
    _refuse_cloud_metadata_endpoint(None, "openai")  # type: ignore[arg-type]

"""`proxy.py` 转发点的云元数据闸（第 56 轮）。

`proxy.py` 是**本地 OpenAI 兼容代理**——它可以被任何客户端打到
（curl、第三方 SDK、别的进程），**那条路根本不经过 `providers.py` 的模型客户端构造**。
两条路互不覆盖，所以两处都要有闸。
"""

from __future__ import annotations

import pytest

from app.proxy import ProxyError, _refuse_cloud_metadata


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://169.254.170.2/creds",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://100.100.100.200/latest/meta-data",
    ],
)
def test_the_proxy_refuses_cloud_metadata_targets(url):
    with pytest.raises(ProxyError) as excinfo:
        _refuse_cloud_metadata(url)
    assert "云元数据" in str(excinfo.value), str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.anthropic.com/v1/messages",
        "https://generativelanguage.googleapis.com/v1beta/models",
        # 本地上游必须放行——代理的用途就是转发到这些地方
        "http://127.0.0.1:11434/v1/chat/completions",
    ],
)
def test_normal_upstreams_pass(url):
    """**成对**：只测「被拒」的话，一个「永远抛异常」的实现也能过。"""
    _refuse_cloud_metadata(url)

"""可观测性 exporter 插件（MATRIX 15.5）。

Hermes 把 langfuse / nemo_relay 做成 `plugins/observability/` 下的插件，靠一组
生命周期钩子收集 trace：`pre_api_request` / `post_api_request` /
`api_request_error` / `pre_llm_call` / `post_llm_call` / `pre_tool_call` /
`post_tool_call` / `on_session_finalize` / `on_session_end` /
`subagent_start` / `subagent_stop`。

## 本机没有网络，这一层怎么才算"真的做了"

假装能上报是最坏的选择：配置好之后什么都收不到，而日志里全是"发送成功"。
所以这里的做法是：

1. **钩子订阅是真的**——`hooks()` 返回的清单与 Hermes 的 `plugin.yaml` 一致，
   事件真的会进来；
2. **落盘是真的**——`emit()` 把事件写进本地 spool（`<data>/exports/spool/<name>.jsonl`），
   可离线验证、可回放、可事后补发；
3. **网络发送是明确不做的**——`flush()` 抛 `NetworkUnavailable` 并说明缺什么。
   这比"静默成功"诚实得多。

`JsonlExporter` 是唯一完整可用的：它只落盘，不需要网络，因此本机可以端到端验证。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: Hermes `plugins/observability/langfuse/plugin.yaml` 里的 hooks 清单（11 条）。
#: 抄下来而不是"意思差不多"——订阅错了钩子，trace 就缺段，而且很难发现。
LANGFUSE_HOOKS: tuple[str, ...] = (
    "pre_api_request",
    "post_api_request",
    "api_request_error",
    "pre_llm_call",
    "post_llm_call",
    "pre_tool_call",
    "post_tool_call",
    "on_session_finalize",
    "on_session_end",
    "subagent_start",
    "subagent_stop",
)

#: nemo_relay 订阅的是同一组生命周期点（它和 langfuse 都挂在"调用前后"）。
NEMO_RELAY_HOOKS: tuple[str, ...] = LANGFUSE_HOOKS


class ExportError(RuntimeError):
    """导出失败的基类。"""


class NetworkUnavailable(ExportError):
    """需要联网但本机不可用。**必须带上是哪个 exporter、缺什么**。"""


@runtime_checkable
class Exporter(Protocol):
    """一个 exporter 的最小面。"""

    name: str

    def hooks(self) -> tuple[str, ...]:
        """订阅哪些生命周期事件。"""
        ...

    def available(self) -> bool:
        """此刻能不能真的把数据送出去。"""
        ...

    def describe(self) -> dict[str, Any]:
        """自曝：能做什么、不能做什么、缺什么。"""
        ...

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        """收一条事件。**不联网的 exporter 也必须真的存下来**。"""
        ...

    def flush(self) -> int:
        """把攒下的送出去，返回送出的条数。"""
        ...


@dataclass
class _Spooling:
    """把事件追加到本地 JSONL 的共用部分（spool = 待发队列）。

    为什么要 spool 而不是直接丢：没网的时候直接丢，等于这段时间的 trace
    永久缺失；spool 让"事后补发"成为可能，也让测试能验证事件真的进来了。
    """

    data_dir: Path
    name: str
    _spool_path: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._spool_path = Path(self.data_dir) / "exports" / "spool" / f"{self.name}.jsonl"

    @property
    def spool_path(self) -> Path:
        return self._spool_path

    def write(self, event: str, payload: dict[str, Any]) -> None:
        self._spool_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "exporter": self.name,
            "event": event,
            "payload": payload,
        }
        # 追加写 + 单行 JSON：断电最多丢最后一行，不会毁掉整个文件。
        with self._spool_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def pending(self) -> list[dict[str, Any]]:
        if not self._spool_path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self._spool_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # 坏行（比如断电截断）跳过而不是整批失败——丢一条好过丢全部。
                continue
        return records

    def clear(self) -> None:
        if self._spool_path.exists():
            self._spool_path.unlink()


class JsonlExporter(_Spooling):
    """只落盘的 exporter。**本机唯一能端到端跑通的**，也是自建看板的取数口。"""

    def __init__(self, data_dir: str | Path, *, name: str = "jsonl") -> None:
        self.data_dir = Path(data_dir)
        self.name = name
        super().__init__(self.data_dir, name)

    def hooks(self) -> tuple[str, ...]:
        return LANGFUSE_HOOKS

    def available(self) -> bool:
        return True

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": "local",
            "available": True,
            "spool": str(self.spool_path),
            "note": "只落盘，不联网。可以直接被 jq / 自建看板消费。",
        }

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        self.write(event, payload)

    def flush(self) -> int:
        # 已经落盘了，"发送"就是"确认已落盘"。返回条数便于调用方对账。
        return len(self.pending())


class _RemoteExporter(_Spooling):
    """远端 exporter 的共用骨架：**收得下、存得住、发不出去**。

    子类只需给出 `env_vars`（缺哪些凭证就不可用）与 `endpoint`（发到哪）。
    """

    #: 需要哪些环境变量才算配置好。照 Hermes 的 `requires_env`。
    env_vars: tuple[str, ...] = ()
    endpoint: str = ""

    def __init__(self, data_dir: str | Path, *, name: str | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.name = name or type(self).__name__.lower()
        super().__init__(self.data_dir, self.name)

    def hooks(self) -> tuple[str, ...]:
        return LANGFUSE_HOOKS

    def missing_env(self) -> list[str]:
        return [key for key in self.env_vars if not os.environ.get(key)]

    def available(self) -> bool:
        # **恒 False**：即使凭证齐了，本机也没有网络。假装可用会让上层以为
        # "trace 在传了"，而实际上一条也没出去。
        return False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": "remote",
            "available": False,
            "endpoint": self.endpoint,
            "missing_env": self.missing_env(),
            "spool": str(self.spool_path),
            "note": (
                "本机无网络，远端上报不可用。事件已 spool 到本地，"
                "联网后可用 `orbit observability flush` 补发。"
            ),
        }

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        # 关键：**发不出去也要存下来**。否则"没网"就等于"这段时间的 trace 没了"。
        self.write(event, payload)

    def flush(self) -> int:
        missing = self.missing_env()
        if missing:
            raise NetworkUnavailable(
                f"{self.name}：缺环境变量 {'、'.join(missing)}，无法上报；"
                f"事件仍在本地 spool（{self.spool_path}）"
            )
        raise NetworkUnavailable(
            f"{self.name}：本机无网络，无法上报到 {self.endpoint or '远端'}；"
            f"事件仍在本地 spool（{self.spool_path}），联网后可补发"
        )


class LangfuseExporter(_RemoteExporter):
    """Langfuse：`requires_env: HERMES_LANGFUSE_PUBLIC_KEY / HERMES_LANGFUSE_SECRET_KEY`。"""

    env_vars = ("HERMES_LANGFUSE_PUBLIC_KEY", "HERMES_LANGFUSE_SECRET_KEY")
    endpoint = "https://cloud.langfuse.com/api/public/ingestion"


class NemoRelayExporter(_RemoteExporter):
    """NeMo Relay：与 langfuse 同一组钩子，端点由用户配置。"""

    env_vars = ("HERMES_NEMO_RELAY_API_KEY", "HERMES_NEMO_RELAY_ENDPOINT")
    endpoint = "（由 HERMES_NEMO_RELAY_ENDPOINT 指定）"

    def __init__(self, data_dir: str | Path, *, name: str | None = None) -> None:
        super().__init__(data_dir, name=name or "nemo_relay")
        self.endpoint = os.environ.get("HERMES_NEMO_RELAY_ENDPOINT", "") or self.endpoint


def default_exporters(data_dir: str | Path) -> dict[str, Exporter]:
    """默认 exporter 池：1 个可用（jsonl）+ 2 个只能 spool 的远端。"""
    return {
        "jsonl": JsonlExporter(data_dir),
        # 显式传 name：`_RemoteExporter` 的默认名是类名小写（`langfuseexporter`），
        # 与池里的键（`langfuse`）对不上——用户看到两个名字会以为是两个东西。
        "langfuse": LangfuseExporter(data_dir, name="langfuse"),
        "nemo_relay": NemoRelayExporter(data_dir),
    }

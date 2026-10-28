"""TTS / STT：11 家 TTS + 8 家 STT，**能离线真跑的有 3 条**（MATRIX 4.12）。

## 先说清楚这台机器上能做什么

Hermes 内置 11 个 TTS provider 与 8 个 STT provider（调研 06 §6.3 / §6.4）。
其中云的那 8 家 TTS（edge / openai / elevenlabs / gemini / xai / minimax / mistral /
deepinfra）与 6 家 STT **全部**需要联网，本机无网络、且本层禁止安装依赖，所以它们
`available() = False`，每一家都带"缺哪个包 / 缺哪个环境变量 / 装什么"。

**本机真能跑的 TTS 是 3~4 家**（命令通道 + SAPI + flite，另加条件可用的 OneCore）：

1. `CommandTts`（`command`，单列在最前面）：用户配的外部命令；
2. `LocalTtsProvider`（`sapi`，见 `app/voice_local.py`）：Windows 自带的
   `System.Speech.Synthesis`，**真合成出合法 RIFF/WAVE**，完全离线；
3. `FfmpegFliteTts`（`flite`，见 `app/voice_piper.py`）：ffmpeg 的 libflite 滤镜，
   **本机实测合成出 126,798 字节 wav**（英文），零额外安装 —— 但它**只支持英文**，
   中文会合成出接近静音，所以 `synthesize()` 对非 ASCII 文本当场拒绝并指路；
4. `LocalOneCoreTts`（`onecore`，见 `app/voice_local.py`）：WinRT 的
   `Windows.Media.SpeechSynthesis`，**另一套音色库**（本机列出 Huihui / Yaoyao /
   Kangkang，SAPI 那套只有 Huihui Desktop / Zira），高完整性级别下实测合成出
   **41,966 字节 wav**。

**关于 2 与 4 的一个必须说清的环境事实**：在本机的 venv python 子进程里，SAPI 与
OneCore 的探测都是 `False` —— 那些子进程的完整性级别是 `Mandatory Label\\Low`，
语音 token 不对 Low IL 开放（`app/voice_local.py` 的 docstring 有完整实测记录）。
所以 `available()` 一律**实测一次最小合成**，而不是"看装了没"；两者在本机
从普通终端跑是真能出声的，在被降权的沙箱里则如实报不可用。

STT 那边有 2 条本地通道：

* `LocalSttProvider`（`sapi`）：`System.Speech.Recognition` + `DictationGrammar`
  配 `SetInputToWaveFile`，**真能转写**，但实测质量差（置信度 0.35 级别），
  所以照实标成 `quality="poor"` 并推荐 `stt_command`（faster-whisper）；
* `FfmpegWhisperStt`（`whisper_cpp`，见 `app/voice_piper.py`）：ffmpeg 的 `whisper`
  filter（whisper.cpp）。本机 **filter 在、模型不在**，所以如实 `False` 并给出
  ggml 模型的下载地址 —— 装上模型就能用，不需要改代码。

`piper` / `kittentts` / `neutts` 三家本来就是纯本地的。`kittentts` / `neutts`
用 `app/voice_local.py::BinaryTts` 做真检测（`find_spec`）；`piper` 换成了
`app/voice_piper.py::PiperTts` —— 它比 `BinaryTts` 多两段阶梯：**找模型** 与
**实测一次最小合成**。理由很具体：只有二进制没有 `.onnx` 模型时 piper 一跑就报
"no model"，而 `shutil.which` 却说"可用"——这和 SAPI 那边"装了音色 ≠ 能用音色"
是同一类坑，判据必须落到"真跑一次"。

`espeak` / `espeak-ng` / `festival` / `say` / `pico2wave` 五家常见本地 TTS 本机
**实测全部不在 PATH**（`Get-Command` 逐个 MISS），所以它们在
`app/voice_piper.py::EXTRA_LOCAL_TTS` 里如实登记：`available()` 走
`shutil.which` + 真跑一次最小合成，`describe()` 给各自的安装方式。

**不假装**：每个不可用的 provider 都给出**具体原因**（缺哪个包 / 缺哪个环境变量 /
本机无网络）与**安装提示**（真要有网时该 `pip install` 什么）。一句"不可用"会让
用户去猜是不是自己配错了。

## 命令通道

`CommandTts` / `CommandStt` 走用户配置的外部命令（`<data>/voice.json` 的
`tts_command` / `stt_command`），用子进程 + 临时文件。这条**可离线验证**：
测试里用 `python -c "print(...)"` 当命令就够了。

命令的两种写法（`_run_command` 都支持）：

1. **占位符式**：命令里出现 `{text_file}` / `{audio_file}` / `{out_file}` 时，
   先把输入落到临时文件、把占位符替换成真实路径，再跑命令。适合 `piper -m x.onnx
   -f {audio_file}`、`whisper {audio_file}` 这类真 CLI。
2. **管道式**：没有占位符时，输入走 stdin、输出走 stdout。适合
   `python -c "..."`、`cat`、`sox -t raw` 这类流式工具。

## 为什么 TTS 的 command 不在那 11 家里

Hermes 的 TTS 命令通道是**另一套注册表**（`tts.providers.<name>: type: command`，
调研 06 §6.4 末尾那句），不占 11 个内置 provider 的名额；而 STT 的 `local_command`
**是**内置 8 家之一（调研 §6.3 的表）。本模块照抄这条边界：
`TTS_PROVIDERS` 是那 11 家，`COMMAND_TTS` 单独暴露；`STT_PROVIDERS` 里
`local_command` 就是 `CommandStt`。SAPI 的两家同理单列（`kind="local"`），
因为它们是 **Orbit 自己的**本地通道，不是 Hermes 清单里的第 12/9 家。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.voice_local import (
    LOCAL_ONECORE_TTS,
    LOCAL_SAPI_STT,
    LOCAL_SAPI_TTS,
    LOCAL_TTS_PROVIDERS,
)
from app.voice_piper import (
    EXTRA_LOCAL_TTS,
    FLITE_TTS,
    PIPER_TTS,
    WHISPER_STT,
)

#: 语音配置文件名（落在 `<data>/voice.json`）。与 `settings.json` 分开：
#: 语音配置是"这台机器上装了什么命令"，跟模型选择不是一类东西，混在一起会让
#: `orbit model` 的读写碰到无关字段。
VOICE_CONFIG_NAME = "voice.json"

#: 命令执行的默认超时（秒）。比模型调用的 60s 宽一点：本地 TTS 合成一段长文本
#: 可能真的要几十秒，超时太短会把"慢"误报成"坏"。
DEFAULT_TIMEOUT = 120.0


class VoiceError(RuntimeError):
    """语音层的可预期失败（消息要能直接给人看）。"""


class ProviderUnavailable(VoiceError):
    """该 provider 在这台机器上用不了。消息里必须带**原因 + 安装提示**。"""


class CommandNotConfigured(VoiceError):
    """没配 `tts_command` / `stt_command`，所以命令通道无从调用。"""


# ── 配置 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VoiceConfig:
    """`<data>/voice.json` 的内容。

    只放"外部命令"这一类键：云 provider 的凭证在系统 keyring（`orbit login`），
    不在这里——把 key 写进一个普通 JSON 文件等于把它交给任何能读文件的人。
    """

    tts_command: str = ""
    stt_command: str = ""
    #: 传给 `synthesize(text, voice)` 的默认音色（命令自己解释这个字符串）。
    voice: str = ""
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def load(cls, data_dir: str | Path | None = None) -> VoiceConfig:
        """读配置。**坏文件当空**：语音配置坏了只该让命令通道"未配置"，
        不该让整个 `orbit model --voice` 崩掉。"""
        path = config_path(data_dir)
        if path is None or not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        try:
            timeout = float(data.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        return cls(
            tts_command=str(data.get("tts_command") or "").strip(),
            stt_command=str(data.get("stt_command") or "").strip(),
            voice=str(data.get("voice") or "").strip(),
            timeout=timeout,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "tts_command": self.tts_command,
            "stt_command": self.stt_command,
            "voice": self.voice,
            "timeout": self.timeout,
        }


def config_path(data_dir: str | Path | None = None) -> Path | None:
    """`<data>/voice.json` 的落点。

    `data_dir` 为空时退回 `ORBIT_DATA_DIR` 环境变量，再退回仓库相对的 `data/`：
    CLI 一定会传 `data_dir`，但模块被脚本直接调用时不该因为缺参数就崩。
    """
    if data_dir is not None:
        return Path(data_dir) / VOICE_CONFIG_NAME
    env = os.environ.get("ORBIT_DATA_DIR", "").strip()
    return Path(env) / VOICE_CONFIG_NAME if env else Path("data") / VOICE_CONFIG_NAME


def save_config(data_dir: str | Path, config: VoiceConfig) -> Path:
    """写配置（`orbit model --voice-set ...` 用）。"""
    path = config_path(data_dir)
    assert path is not None  # data_dir 非空时必然有路径
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


# ── Protocol ────────────────────────────────────────────────────────────────


@runtime_checkable
class TtsProvider(Protocol):
    """一个 TTS 后端。

    `synthesize` 返回**字节**而不是路径：调用方（CLI / 聊天平台）自己决定落盘还是
    直接发出去。返回路径会把"临时文件谁来清"这个坑留给每个调用点。
    """

    #: 名字声明成**只读属性**而不是可写变量：实现方大多是 frozen dataclass，
    #: 可写变量成员会让 mypy 判定"expected settable variable, got read-only
    #: attribute"——协议该描述"能读到名字"，不该要求"能改名字"。
    @property
    def name(self) -> str: ...

    def available(self) -> bool:
        """这台机器上现在能不能用。**宁可说不能，也不要试了才知道**。"""
        ...

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """合成语音。不可用时抛 `ProviderUnavailable`（不是返回空字节）。"""
        ...

    def describe(self) -> str:
        """一句话说清"是什么 / 能不能用 / 不能用怎么办"。"""
        ...


@runtime_checkable
class SttProvider(Protocol):
    """一个 STT 后端。`transcribe` 吃字节、吐文本。"""

    @property
    def name(self) -> str: ...

    def available(self) -> bool: ...

    def transcribe(self, audio: bytes) -> str:
        """转写。不可用时抛 `ProviderUnavailable`。"""
        ...

    def describe(self) -> str: ...


# ── 不可用的 18 家 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UnavailableTts:
    """登记在册、但这台机器上用不了的 TTS 后端。

    存在的意义是**把"有这个东西"和"现在能用"分开**：`orbit model --voice` 必须
    列全 11 家（否则用户会以为 Orbit 只支持命令通道），但每一家都要老实说
    "缺什么、要装什么"。
    """

    name: str
    #: 缺什么（依赖 / 凭证 / 网络）。
    requires: str = ""
    #: 真要有网时该怎么做。
    hint: str = ""

    def available(self) -> bool:
        return False

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        raise ProviderUnavailable(self._reason())

    def describe(self) -> str:
        """三件事必须**同时**出现：`reason` / `requires` / `install`。

        为什么把它们写成带标签的固定字段：这张清单会同时列出 19 家，而其中 8 家
        云端永远不可用。只写"不可用 —— 需要 OPENAI_API_KEY"读起来像"配一下就能用"，
        而真相是"配了也没用，本机无网络"。三个字段分开写，读者一眼能看出缺的是
        **网络**还是**安装**。
        """
        return (
            f"{self.name}：不可用 —— reason: 本机无网络（本层不发真实请求）；"
            f"requires: {self.requires or '（未声明）'}；"
            f"install: {self.hint or '（未声明）'}"
        )

    def _reason(self) -> str:
        parts = ["本机无网络（本层不发真实请求）"]
        if self.requires:
            parts.append(f"需要 {self.requires}")
        reason = "；".join(parts)
        return f"{reason}。{self.hint}" if self.hint else reason


@dataclass(frozen=True)
class UnavailableStt:
    """同上，STT 版。"""

    name: str
    requires: str = ""
    hint: str = ""

    def available(self) -> bool:
        return False

    def transcribe(self, audio: bytes) -> str:
        raise ProviderUnavailable(self._reason())

    def describe(self) -> str:
        """同 TTS 版：`reason` / `requires` / `install` 三件套。"""
        return (
            f"{self.name}：不可用 —— reason: 本机无网络（本层不发真实请求）；"
            f"requires: {self.requires or '（未声明）'}；"
            f"install: {self.hint or '（未声明）'}"
        )

    def _reason(self) -> str:
        parts = ["本机无网络（本层不发真实请求）"]
        if self.requires:
            parts.append(f"需要 {self.requires}")
        reason = "；".join(parts)
        return f"{reason}。{self.hint}" if self.hint else reason


# ── 本地但依赖 Python 包的 STT：faster-whisper ───────────────────────────────


@dataclass(frozen=True)
class FasterWhisperStt:
    """STT 清单里那一家 `local`（Hermes 的首选：faster-whisper）。

    为什么它不再是 `UnavailableStt`：那个类的 `available()` **恒 False**，于是
    "没装"与"装了但没人检测"在输出里长得一模一样 —— 用户 `pip install faster-whisper`
    之后回来一看还是"不可用"，只会以为自己装错了。换成真检测（`find_spec`）之后，
    装上就真的变成可用，不用等下一次改代码。

    判据只到"包在不在"这一层，**没有**做"试转一段"：faster-whisper 的一次最小转写
    要加载几十 MB 权重（首次还要联网下载），而 `describe_all()` 会把每家问一遍。
    这个取舍写在明面上，而不是假装判据比它实际更硬 —— 真正的转写质量由
    `transcribe()` 那次调用回答。
    """

    name: str = "local"
    #: Python 包名（`importlib.util.find_spec` 的目标）。
    module: str = "faster_whisper"
    #: 权重缓存目录（`download_root` 的默认位置；不判断内容，只用来告诉用户去哪看）。
    weights_hint: str = "~/.cache/huggingface/hub（或 ORBIT_WHISPER_DOWNLOAD_ROOT）"

    def available(self) -> bool:
        """包在就算可用。**不联网**：`find_spec` 只查已安装的包。"""
        try:
            return importlib.util.find_spec(self.module) is not None
        except (ImportError, ValueError):  # 损坏的包 / 非法的 spec —— 都算没有
            return False

    def transcribe(self, audio: bytes) -> str:
        """真转写。包不在时抛 `ProviderUnavailable`（带装什么）。

        这条通道的实现是 faster-whisper 的 Python API（不是外部命令）：
        `WhisperModel(...).transcribe(...)` 返回一个生成器，要遍历才拿到段。
        """
        if not self.available():
            raise ProviderUnavailable(self._reason())
        if not audio:
            raise VoiceError("音频是空的：没什么可转写的。")
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - available() 已经挡过一次
            raise ProviderUnavailable(self._reason()) from exc
        with tempfile.TemporaryDirectory(prefix="orbit-fw-") as tmp:
            path = Path(tmp) / "in.wav"
            path.write_bytes(audio)
            # `int8` + CPU：本层不假设有 GPU，也不假设用户愿意为一次转写下载 fp16 权重。
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments, _info = model.transcribe(str(path))
            text = " ".join(segment.text.strip() for segment in segments).strip()
        if not text:
            raise VoiceError(
                "faster-whisper 没有转写出任何内容（音频是静音、太短，或与语言不符）。"
            )
        return text

    def _reason(self) -> str:
        return (
            f"Python 包 `{self.module}` 未安装（本机 find_spec 为 None）；"
            "本机无网络（本层不发真实请求），装不了也下不了权重"
        )

    def describe(self) -> str:
        """同 TTS 版：`reason` / `requires` / `install` 三件套。"""
        if self.available():
            return (
                f"local：可用 —— 本机已装 `{self.module}`（faster-whisper）；"
                "requires: 已满足；install: 已安装；"
                f"reason: 纯本地 CTranslate2 推理，权重缓存在 {self.weights_hint}。"
            )
        return (
            f"local：不可用 —— reason: {self._reason()}；"
            "requires: `faster-whisper` 包 + 模型权重（tiny/base/small/medium/large-v3）；"
            "install: 有网后 `pip install faster-whisper`，首次 `transcribe()` 会自动下 "
            "base 权重（约 145MB）。"
        )


# ── 唯一能真跑的通道 ────────────────────────────────────────────────────────

#: 命令模板里的占位符。出现任意一个就走"临时文件"模式，否则走 stdin/stdout。
TEXT_PLACEHOLDER = "{text_file}"
AUDIO_PLACEHOLDER = "{audio_file}"


def split_command(command: str) -> list[str]:
    """把配置里的命令串拆成 argv。

    **不用 `shell=True`**：那等于把 `<data>/voice.json` 变成任意代码执行入口，而
    这个文件会被各种脚本写。`shlex.split(posix=False)` 保留 Windows 路径里的反斜杠，
    再把成对引号剥掉（`"C:\\Program Files\\python.exe"` → 可执行路径本身）。
    """
    parts = shlex.split(command, posix=False)
    argv: list[str] = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
            part = part[1:-1]
        if part:
            argv.append(part)
    return argv


def _run_command(
    command: str,
    *,
    stdin_bytes: bytes | None = None,
    text: str | None = None,
    audio: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    env_extra: dict[str, str] | None = None,
    prefer_stdout: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """跑一条外部命令，返回完整结果（stdout/stderr/returncode）。

    两种模式（见模块 docstring）：有占位符 → 临时文件；没有 → stdin/stdout。
    非零退出**抛 `VoiceError` 并带上 stderr 尾部**——只报"命令失败"等于让用户去猜。
    """
    argv = split_command(command)
    if not argv:
        raise CommandNotConfigured("命令是空的：先在 `<data>/voice.json` 里配 `tts_command`。")
    uses_files = TEXT_PLACEHOLDER in command or AUDIO_PLACEHOLDER in command
    with tempfile.TemporaryDirectory(prefix="orbit-voice-") as tmp:
        cwd = Path(tmp)
        expanded = list(argv)
        stdin_data = stdin_bytes
        if uses_files:
            text_path = cwd / "input.txt"
            audio_path = cwd / "input.audio"
            out_path = cwd / "output.bin"
            if text is not None:
                text_path.write_text(text, encoding="utf-8")
            if audio is not None:
                audio_path.write_bytes(audio)
            out_path.write_bytes(b"")
            expanded = [
                part.replace(TEXT_PLACEHOLDER, str(text_path))
                .replace(AUDIO_PLACEHOLDER, str(audio_path))
                .replace("{out_file}", str(out_path))
                for part in argv
            ]
            stdin_data = None
        try:
            result = subprocess.run(  # noqa: S603 —— argv 已拆分，不经 shell
                expanded,
                input=stdin_data if stdin_data is not None else b"",
                capture_output=True,
                timeout=timeout,
                cwd=tmp,
                env={**os.environ, **env_extra} if env_extra else None,
            )
        except FileNotFoundError as exc:
            raise VoiceError(
                f"命令找不到：`{expanded[0]}`。检查 `<data>/voice.json` 里的路径，"
                "或改用绝对路径。"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise VoiceError(f"命令超时（{timeout}s）：`{command[:80]}`") from exc
        if result.returncode != 0:
            tail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            raise VoiceError(
                f"命令退出码 {result.returncode}：`{command[:80]}`"
                + (f"\nstderr: {tail[-500:]}" if tail else "")
            )
        if uses_files:
            # 两种来源：命令写到 `{out_file}` / 覆盖 `{audio_file}`，或者直接打到
            # stdout。谁优先由调用方定（TTS 认文件、STT 认 stdout）——同一个辅助
            # 函数服务两种语义，只能把这个选择显式化。
            data = result.stdout or b""
            if not prefer_stdout or not data:
                from_file = out_path.read_bytes() if out_path.exists() else b""
                if not from_file:
                    for candidate in (audio_path, text_path):
                        if candidate.exists() and candidate.stat().st_size:
                            from_file = candidate.read_bytes()
                            break
                data = from_file or data
            return subprocess.CompletedProcess(expanded, result.returncode, data, result.stderr)
        return result


class CommandTts:
    """走外部命令的 TTS：**唯一能在本机离线验证的 TTS 实现**。

    `available()` 只看"配没配命令"，不试跑：试跑会在每次列清单时 fork 一个进程，
    而"配了但命令是坏的"应该由 `synthesize()` 报错（带 stderr），不该让清单页卡住。
    """

    name = "command"

    def __init__(
        self, config: VoiceConfig | None = None, *, data_dir: str | Path | None = None
    ) -> None:
        self._config = config
        self._data_dir = data_dir

    @property
    def config(self) -> VoiceConfig:
        """每次现读：`orbit model --voice` 可能在用户改完配置后立刻再跑一次。"""
        if self._config is not None:
            return self._config
        return VoiceConfig.load(self._data_dir)

    def available(self) -> bool:
        return bool(self.config.tts_command.strip())

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        command = self.config.tts_command.strip()
        if not command:
            raise CommandNotConfigured(
                "没有配置 TTS 命令。在 `<data>/voice.json` 里写 "
                '`{"tts_command": "piper -m zh_CN.onnx -f {audio_file}"}`，'
                "或用 `orbit model --voice-set tts_command=<命令>`。"
            )
        chosen = (voice or self.config.voice or "").strip()
        # 音色通过环境变量交给命令：命令的 argv 由用户写死，塞参数进去会破坏他们的
        # 引号；而"音色"本来就是每个 provider 各一套字符串，没有统一的位置参数。
        env_extra = {"ORBIT_TTS_VOICE": chosen} if chosen else None
        result = _run_command(
            command,
            stdin_bytes=text.encode("utf-8"),
            text=text,
            timeout=self.config.timeout,
            env_extra=env_extra,
            # TTS 认文件：`piper -f {audio_file}` 这类命令不会往 stdout 写音频。
            prefer_stdout=False,
        )
        data = result.stdout or b""
        if not data:
            raise VoiceError(
                "命令没有输出音频数据。管道式命令要把音频写到 stdout；"
                "占位符式命令要写 `{out_file}` 或 `{audio_file}`。"
            )
        return data

    def describe(self) -> str:
        command = self.config.tts_command.strip()
        if not command:
            return (
                "command：不可用 —— reason: 未配置 `tts_command`（`<data>/voice.json`）；"
                "requires: 一条外部 TTS 命令；install: 写 "
                '`{"tts_command": "piper -m zh_CN.onnx -f {audio_file}"}` 或 '
                "`orbit model --voice-set tts_command=<命令>`。这是唯一**不需要联网**"
                "且不依赖语音包的通道，配了就能用。"
            )
        return (
            f"command：可用 —— 外部命令 `{command[:80]}`（{self.config.timeout:g}s 超时）；"
            "requires: 已满足；install: 已配置；reason: 走本机子进程，不需要联网。"
        )


class CommandStt:
    """走外部命令的 STT：对应 Hermes 的 `local_command` provider。"""

    name = "local_command"

    def __init__(
        self, config: VoiceConfig | None = None, *, data_dir: str | Path | None = None
    ) -> None:
        self._config = config
        self._data_dir = data_dir

    @property
    def config(self) -> VoiceConfig:
        if self._config is not None:
            return self._config
        return VoiceConfig.load(self._data_dir)

    def available(self) -> bool:
        return bool(self.config.stt_command.strip())

    def transcribe(self, audio: bytes) -> str:
        command = self.config.stt_command.strip()
        if not command:
            raise CommandNotConfigured(
                "没有配置 STT 命令。在 `<data>/voice.json` 里写 "
                '`{"stt_command": "whisper --output-txt {audio_file}"}`，'
                "或用 `orbit model --voice-set stt_command=<命令>`。"
            )
        if not audio:
            raise VoiceError("音频是空的：没什么可转写的。")
        result = _run_command(
            command, stdin_bytes=audio, audio=audio, timeout=self.config.timeout
        )
        text = (result.stdout or b"").decode("utf-8", errors="replace").strip()
        if not text:
            raise VoiceError(
                "命令没有输出文本。管道式命令要把转写结果写到 stdout；"
                "占位符式命令要打印到 stdout。"
            )
        return text

    def describe(self) -> str:
        command = self.config.stt_command.strip()
        if not command:
            return (
                "local_command：不可用 —— reason: 未配置 `stt_command`（`<data>/voice.json`）；"
                "requires: 一条外部 STT 命令；install: 写 "
                '`{"stt_command": "whisper --output-txt {audio_file}"}` 或 '
                "`orbit model --voice-set stt_command=<命令>`。配了就能离线转写"
                "（faster-whisper / whisper.cpp 的 CLI 都行）。"
            )
        return (
            f"local_command：可用 —— 外部命令 `{command[:80]}`（{self.config.timeout:g}s 超时）；"
            "requires: 已满足；install: 已配置；reason: 走本机子进程，不需要联网。"
        )


# ── 登记表 ──────────────────────────────────────────────────────────────────

#: 三家纯本地 TTS 的实现（真检测，见 `app/voice_local.py::BinaryTts`），
#: 按名字索引：登记表里**只写名字**，实现留在 voice_local —— 这样"这家能不能用"
#: 只有一个事实源（`available()`），不会出现"登记表说不可用、检测说可用"的分叉。
_LOCAL_TTS: dict[str, TtsProvider] = {
    provider.name: provider for provider in LOCAL_TTS_PROVIDERS
}

#: **不在 11 家里**、但本机真能跑（或真装了就能跑）的 TTS：`flite` 打头，
#: 后面跟 espeak / espeak-ng / festival / say / pico2wave。
#:
#: 为什么要一个显式的类型注解而不是直接写元组字面量：`(FLITE_TTS, *EXTRA_LOCAL_TTS)`
#: 里两个类没有共同基类，mypy 会把元素的联合类型退化成 `object`，于是
#: `provider.name` / `.available()` / `.describe()` 全部报 attr-defined —— 注解成
#: `tuple[TtsProvider, ...]` 之后，结构类型检查才真正生效（这两个类都满足协议）。
EXTRA_TTS: tuple[TtsProvider, ...] = (FLITE_TTS, *EXTRA_LOCAL_TTS)

#: 11 个 TTS provider（调研 06 §6.4 的清单，顺序照抄）。云的那 8 家不可用并带
#: "缺什么 + 装什么"；本地那 3 家（neutts / kittentts / piper）走真检测。
TTS_PROVIDERS: tuple[TtsProvider, ...] = (
    UnavailableTts(
        "edge",
        requires="`edge-tts` 包（未安装）",
        hint="有网后 `pip install edge-tts`；它是免费默认 provider，字符上限 5000。",
    ),
    UnavailableTts(
        "openai",
        requires="OPENAI_API_KEY + `openai` SDK",
        hint="模型 gpt-4o-mini-tts，字符上限 4096。",
    ),
    UnavailableTts(
        "xai",
        requires="XAI_API_KEY",
        hint="字符上限 15000。",
    ),
    UnavailableTts(
        "minimax",
        requires="MINIMAX_API_KEY",
        hint="模型 speech-02-hd，字符上限 10000。",
    ),
    UnavailableTts(
        "mistral",
        requires="MISTRAL_API_KEY + `mistralai` 包",
        hint="模型 voxtral-mini-tts-2603，字符上限 4000。",
    ),
    UnavailableTts(
        "gemini",
        requires="GOOGLE_API_KEY",
        hint="字符上限 32000（32k token 上下文）。",
    ),
    UnavailableTts(
        "elevenlabs",
        requires="ELEVENLABS_API_KEY",
        hint="字符上限随 model_id 变化（eleven_v3 5000 / multilingual_v2 10000）。",
    ),
    # ── 下面三家**本来就是纯本地**的：neutts / kittentts 走 `voice_local.BinaryTts`，
    #    piper 走 `voice_piper.PiperTts`（多两段阶梯：找模型 + 实测一次最小合成）──
    #
    # 为什么不是 `UnavailableTts`：那个类的 `available()` 恒 `False`，于是"没装"与
    # "装了也没人检测"在输出里长得一模一样。换成真检测之后，装上 piper 就真的可用，
    # 不用等下一次改代码。名字与顺序**逐字不变**（对齐调研 06 §6.4 的清单）。
    _LOCAL_TTS["neutts"],
    _LOCAL_TTS["kittentts"],
    PIPER_TTS,
    UnavailableTts(
        "deepinfra",
        requires="DEEPINFRA_API_KEY",
        hint="字符上限由 `tts.deepinfra.max_text_length` 覆盖。",
    ),
)

#: 8 个 STT provider（调研 06 §6.3 的清单，顺序 = Hermes 的 auto-detect 优先级）。
#: 其中 `local`（faster-whisper，**真检测**包在不在）与 `local_command`（用户配的命令）
#: 是两条活的实现；其余 6 家是云服务，本机无网络所以不可用并带原因。
STT_PROVIDERS: tuple[SttProvider, ...] = (
    # Hermes 的首选：faster-whisper。上一轮这里是 `UnavailableStt`（恒 False），
    # 于是"装了 faster-whisper 也永远显示不可用"——换成真检测后装上就能用。
    FasterWhisperStt(),
    # Hermes 的 `local_command`：用户自己配的命令通道。这是唯一离线可用的那一家。
    CommandStt(),
    UnavailableStt(
        "groq",
        requires="GROQ_API_KEY + `openai` SDK",
        hint="模型 whisper-large-v3-turbo。",
    ),
    UnavailableStt(
        "openai",
        requires="OPENAI_API_KEY（audio 凭证）",
        hint="模型 whisper-1。",
    ),
    UnavailableStt(
        "mistral",
        requires="MISTRAL_API_KEY + `mistralai` 包",
        hint="模型 voxtral-mini-latest。",
    ),
    UnavailableStt(
        "xai",
        requires="XAI_API_KEY",
        hint="走 xAI 的 HTTP 转写面。",
    ),
    UnavailableStt(
        "elevenlabs",
        requires="ELEVENLABS_API_KEY",
        hint="模型 scribe_v2（支持 diarize / 音频事件标记）。",
    ),
    UnavailableStt(
        "deepinfra",
        requires="DEEPINFRA_API_KEY",
        hint="Hermes 的 auto-detect 里排最后，避免误抢 xAI / ElevenLabs。",
    ),
)

#: TTS 的命令通道。**不在那 11 家里**（Hermes 把它做成独立 registry，见模块
#: docstring 末段）。`data_dir=None` 时按 `ORBIT_DATA_DIR` / 仓库 `data/` 找配置。
COMMAND_TTS: CommandTts = CommandTts()


def tts_provider(name: str, *, data_dir: str | Path | None = None) -> TtsProvider:
    """按名字取 TTS provider（`command` / `sapi` / `flite` / `espeak` … 都可取）。

    查找顺序：命令通道 → SAPI → 11 家登记表 → **本机二进制类**（flite / espeak /
    espeak-ng / festival / say / pico2wave）。最后那一组不在 11 家里（见模块
    docstring 末段），但它们是真正能在本机跑的实现，不能只出现在清单里取不到。
    """
    key = (name or "").strip().lower()
    if key in {"command", "local_command"}:
        return CommandTts(data_dir=data_dir) if data_dir is not None else COMMAND_TTS
    # SAPI 是 Orbit 自己的本地通道（不在那 11 家里），单独认一个名字。
    # 别名 `local_sapi` 是给"本地 / 云端"这类习惯叫法留的入口。
    if key in {"sapi", "local_sapi"}:
        return LOCAL_SAPI_TTS
    # OneCore（WinRT）是另一套音色库 + 另一套引擎，单独一个名字（见 voice_local 的注释）。
    if key in {"onecore", "winrt"}:
        return LOCAL_ONECORE_TTS
    for provider in TTS_PROVIDERS:
        if provider.name == key:
            return provider
    for extra in EXTRA_TTS:
        if extra.name == key:
            return extra
    names = " / ".join([p.name for p in TTS_PROVIDERS] + [p.name for p in EXTRA_TTS])
    raise VoiceError(
        f"未知 TTS provider：{name}（可用：command / sapi / onecore / {names}）"
    )


def stt_provider(name: str, *, data_dir: str | Path | None = None) -> SttProvider:
    """按名字取 STT provider。`local_command` 会用传入的 `data_dir` 现读配置。

    `whisper_cpp` 是 ffmpeg 的 `whisper` filter（whisper.cpp），不在 8 家清单里 ——
    同 TTS 侧 `flite` 的理由：它是本机真实存在的一条本地通道，必须有名字可取。
    """
    key = (name or "").strip().lower()
    if key in {"local_command", "command"}:
        return CommandStt(data_dir=data_dir) if data_dir is not None else STT_PROVIDERS[1]
    if key in {"sapi", "local_sapi"}:
        return LOCAL_SAPI_STT
    if key in {"whisper_cpp", "whisper.cpp"}:
        return WHISPER_STT
    for provider in STT_PROVIDERS:
        if provider.name == key:
            return provider
    raise VoiceError(
        f"未知 STT provider：{name}"
        f"（可用：sapi / whisper_cpp / {' / '.join(p.name for p in STT_PROVIDERS)}）"
    )


def describe_all(*, data_dir: str | Path | None = None) -> list[dict[str, object]]:
    """19 家 + 本地通道的可用性清单（CLI 表格 + `--json` 用）。

    顺序固定：`command` 单列最前（`kind="command"`）→ 11 家 TTS（`kind="tts"`）→
    8 家 STT（`kind="stt"`）→ 两条 SAPI 本地通道（`kind="local"` / `kind="local-stt"`）→
    **本机二进制类通道**：`flite` + espeak/festival/say/pico2wave（`kind="local-tts"`）
    与 whisper.cpp（`kind="local-stt-bin"`）。

    为什么 SAPI / flite / whisper 单列成新的 `kind` 而不是混进 11/8：那两张表是
    **Hermes 的清单**（调研 06 §6.3/§6.4 的条数被测试逐字钉住），而这些是
    Orbit 自己的本地通道。混进去会让"对齐 Hermes 的账"算不清 —— 而这张清单的
    用途恰恰是"对齐到了哪"。反过来说：**不在 11/8 里，不等于不可用**，
    `flite` 就是本机真能合成的那一家，它必须出现在这张清单上。
    """
    config = VoiceConfig.load(data_dir)
    tts_command = CommandTts(config, data_dir=data_dir)
    rows: list[dict[str, object]] = [
        {
            "kind": "command",
            "name": "command",
            "available": tts_command.available(),
            "describe": tts_command.describe(),
        }
    ]
    for provider in TTS_PROVIDERS:
        rows.append(
            {
                "kind": "tts",
                "name": provider.name,
                "available": provider.available(),
                "describe": provider.describe(),
            }
        )
    for stt_entry in STT_PROVIDERS:
        # `local_command` 用现读的配置（`STT_PROVIDERS` 里那个是空配置的占位实例）。
        instance: SttProvider = (
            CommandStt(config, data_dir=data_dir)
            if stt_entry.name == "local_command"
            else stt_entry
        )
        rows.append(
            {
                "kind": "stt",
                "name": instance.name,
                "available": instance.available(),
                "describe": instance.describe(),
            }
        )
    # 本地通道：SAPI 的两条。`available()` 内部只做一次（缓存过的）探测，
    # 列清单不会因此变慢 —— 这是"如实报能不能用"的代价，值得付。
    rows.append(
        {
            "kind": "local",
            "name": LOCAL_SAPI_TTS.name,
            "available": LOCAL_SAPI_TTS.available(),
            "describe": LOCAL_SAPI_TTS.describe(),
        }
    )
    rows.append(
        {
            "kind": "local-stt",
            "name": LOCAL_SAPI_STT.name,
            "available": LOCAL_SAPI_STT.available(),
            "describe": LOCAL_SAPI_STT.describe(),
            "quality": LOCAL_SAPI_STT.quality,
        }
    )
    # OneCore（WinRT）合成器：与 SAPI **并列的第四家**，不是它的别名。
    # 音色库不同（OneCore 多出 Yaoyao / Kangkang），引擎也不同 —— 一边坏了另一边
    # 可能还是好的，所以它必须单独一行。
    rows.append(
        {
            "kind": "local",
            "name": LOCAL_ONECORE_TTS.name,
            "available": LOCAL_ONECORE_TTS.available(),
            "describe": LOCAL_ONECORE_TTS.describe(),
        }
    )
    # 本机二进制类通道：flite 打头（本机唯一一个零安装就能真合成的），
    # 后面跟 5 家常见本地 TTS（本机全都不在 PATH，如实登记 + 安装方式）。
    for extra in EXTRA_TTS:
        rows.append(
            {
                "kind": "local-tts",
                "name": extra.name,
                "available": extra.available(),
                "describe": extra.describe(),
            }
        )
    rows.append(
        {
            "kind": "local-stt-bin",
            "name": WHISPER_STT.name,
            "available": WHISPER_STT.available(),
            "describe": WHISPER_STT.describe(),
        }
    )
    return rows


__all__ = [
    "AUDIO_PLACEHOLDER",
    "COMMAND_TTS",
    "DEFAULT_TIMEOUT",
    "EXTRA_LOCAL_TTS",
    "FLITE_TTS",
    "LOCAL_ONECORE_TTS",
    "LOCAL_SAPI_STT",
    "LOCAL_SAPI_TTS",
    "LOCAL_TTS_PROVIDERS",
    "PIPER_TTS",
    "STT_PROVIDERS",
    "TEXT_PLACEHOLDER",
    "TTS_PROVIDERS",
    "VOICE_CONFIG_NAME",
    "WHISPER_STT",
    "CommandNotConfigured",
    "CommandStt",
    "CommandTts",
    "FasterWhisperStt",
    "ProviderUnavailable",
    "SttProvider",
    "TtsProvider",
    "UnavailableStt",
    "UnavailableTts",
    "VoiceConfig",
    "VoiceError",
    "config_path",
    "describe_all",
    "save_config",
    "split_command",
    "stt_provider",
    "tts_provider",
]

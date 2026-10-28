"""MATRIX 4.12 的「本地二进制 TTS / STT」：piper 的真发现链 + 其它本地语音工具的真检测。

## 这个文件要解决的具体问题

`app/voice.py` 的 11 家 TTS 里 `piper` 上一轮已经换成了 `app/voice_local.py::BinaryTts`
——它的 `available()` 只做 `shutil.which("piper")`。**那仍然会说谎**：piper 是个
"二进制 + 声音模型"两件套，光有可执行文件而没有任何 `.onnx` 模型时，`piper` 一跑就报
"no model"，而清单上却写着"可用"。这和 SAPI 那边踩过的坑是同一类（"装了"≠"能用"），
所以这里把判据改成**三段阶梯**：

1. **找二进制**：`PIPER_BIN` 环境变量 → `PATH`（`shutil.which`）→ 几个已知目录；
2. **找模型**：`PIPER_MODEL` 环境变量 → `<data>/voice/models/*.onnx` → `%LOCALAPPDATA%\\piper`、
   `~/.piper` 这类 piper 自己的默认位置；
3. **实测一次最小合成**：把「OK」喂给 piper，拿到**合法 RIFF/WAVE** 才算可用
   （与 `app/voice_local.py::_tts_probe` 完全同一个判据，理由见那边的 docstring）。

任何一步断了，`available()` 就返回 `False`，并把**断在哪一步**写进 `describe()`
的 `reason` 里 —— "不可用"三个字会让用户去猜是自己装错了还是代码没接。

## 本机的实测结论（写进代码，因为它决定了哪些路径真的被验证过）

| 后端 | 二进制 | 模型 | 实测结果 |
|---|---|---|---|
| `flite`（走 ffmpeg 的 libflite） | ✅ `ffmpeg` 在 PATH（gyan.dev full build，`--enable-libflite`） | 不需要（模型编进二进制） | ✅ **真合成出 126,798 字节 wav**（英文）；中文合成出 5,998 字节≈静音 |
| `piper` | ❌ 不在 PATH，也不在已知目录 | ❌ 无 `.onnx` | 如实 `False` + `pip install piper-tts` + 模型下载地址 |
| `espeak` / `espeak-ng` / `festival` / `say` / `pico2wave` | ❌ 全部不在 PATH | — | 如实 `False` + 各自的安装方式 |
| `whisper.cpp`（走 ffmpeg 的 `whisper` filter） | ✅ ffmpeg 带 `--enable-whisper`（filter 存在） | ❌ 无 `ggml-*.bin` | 如实 `False` + 模型下载地址 |

所以这一轮**真可用从 2 家推到 3 家**：命令通道、SAPI（高完整性级别下）、
以及新增的 **`flite`**。flite 的限制写在 `FfmpegFliteTts.describe()` 里：
libflite 只带 6 个**英文**音色，中文会合成出接近静音的东西，所以 `synthesize()`
对非 ASCII 文本**当场拒绝**并指路（`sapi` 或 `tts_command`）—— 悄悄吐一段静音
比报错坏得多。

## 为什么纯函数与"跑一次"要分开

`piper_argv()` / `flite_argv()` / `whisper_argv()` 是**纯函数**：本机没有 piper 也没有
whisper 模型，这两个后端的"跑一次"在这里无法实测。把命令行拼装抽成纯函数之后，
"参数对不对"这件事仍然可离线断言（测试直接比对 argv），而"能不能真跑"留给
`probe()` 在本机如实回答。**没有把没验过的路径写成"已完成"。**

## 关于 `available()` 的缓存

`describe_all()` 会把每家都问一遍，而一次探测要 fork 一个 ffmpeg（本机约 300ms）。
结论缓存在模块级（`_flite_probe` / `_piper_probe` / `_commandline_probe`），
并用 :func:`reset_probe_cache` 给"刚装完想立刻看到"的场景留口子 —— 缓存不可重置的话，
"装上了仍显示不可用"会变成第二个查不出来的问题（同 `app/voice_local.py` 的理由）。
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# `Probe` / `_failure` / `_unavailable` 从 `app.voice_local` 复用而不是重写一份：
# 这两个模块的 `describe()` 会被同一张清单并列打印，判据与异常文案必须是同一套，
# 否则同一台机器上会出现"voice_local 说不可用、voice_piper 说可用"的分叉。
from app.voice_local import Probe, _failure, _unavailable

# ── 常量：装什么、去哪下（每一家的 describe() 都会原样带出去）────────────────

#: 显式指定 piper 可执行文件 / 模型的入口。**优先于一切自动发现**：
#: 自动发现只能覆盖"常见位置"，用户的 piper 完全可能在一个我们猜不到的地方。
PIPER_BIN_ENV = "PIPER_BIN"
PIPER_MODEL_ENV = "PIPER_MODEL"
#: 显式指定 ffmpeg（flite / whisper 两个后端都走它）。
FFMPEG_BIN_ENV = "ORBIT_FFMPEG"
#: 显式指定 whisper.cpp 的 ggml 模型。
WHISPER_MODEL_ENV = "ORBIT_WHISPER_MODEL"

PIPER_INSTALL = (
    "`pip install piper-tts`（装完会有 `piper` 控制台脚本），"
    "或从 https://github.com/rhasspy/piper/releases 下对应平台的 release 包"
)
PIPER_MODEL_INSTALL = (
    "下模型（并放在同一个目录，piper 会自己找同名 .onnx.json）："
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx"
)
FLITE_INSTALL = (
    "ffmpeg 需要带 `--enable-libflite`（gyan.dev 的 full build 自带）；"
    "`ffmpeg -filters | findstr flite` 能查到就说明有"
)
WHISPER_INSTALL = (
    "下 whisper.cpp 模型并指给 `ORBIT_WHISPER_MODEL`："
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
)

#: 探测合成用的最短文本。**只求"能出合法 wav"**，不求好听。
PROBE_TEXT = "OK"

#: 探测超时（秒）。冷启动 ffmpeg + flite 初始化，本机实测 < 1s；60s 是防卡死的上限。
PROBE_TIMEOUT = 60.0
#: 合成超时（秒）。一段话的量级，不打算让一次工具调用合成整本有声书。
SYNTH_TIMEOUT = 180.0


# ── 公共小工具 ──────────────────────────────────────────────────────────────


def is_wave(data: bytes) -> bool:
    """是不是一段**能用**的 RIFF/WAVE：前 12 字节对得上，且长过 44 字节头。

    为什么两个条件都要：`b"RIFF"` + `b"WAVE"` 只能证明"头写对了"，
    0 帧的 wav 也是合法头 —— 而那正是"命令跑了但没合成出东西"的样子。
    """
    return len(data) > 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _tail(raw: bytes | None, limit: int = 500) -> str:
    """取 stderr 尾部（命令的报错信息通常都在最后几行）。"""
    return (raw or b"").decode("utf-8", errors="replace").strip()[-limit:]


def run_tool(
    argv: list[str],
    *,
    cwd: str | Path,
    stdin_bytes: bytes | None = None,
    timeout: float = SYNTH_TIMEOUT,
) -> subprocess.CompletedProcess[bytes]:
    """跑一条外部命令，返回完整结果。非零退出**抛错并带 stderr 尾部**。

    **不用 `shell=True`**：argv 全是本文件拼出来的常量 + 临时文件路径，
    没有任何来自模型/用户的字符串进命令行（文本一律走文件或 stdin）。
    """
    try:
        result = subprocess.run(  # noqa: S603 —— argv 已拆好，不经 shell
            argv,
            input=stdin_bytes if stdin_bytes is not None else b"",
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd),
            check=False,
        )
    except FileNotFoundError as exc:
        raise _unavailable(
            f"命令找不到：`{argv[0]}`。检查 PATH，或用环境变量指定绝对路径"
            f"（{FFMPEG_BIN_ENV} / {PIPER_BIN_ENV}）。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _failure(f"命令超时（{timeout:g}s）：`{argv[0]}`") from exc
    if result.returncode != 0:
        tail = _tail(result.stderr)
        raise _failure(
            f"命令退出码 {result.returncode}：`{argv[0]}`"
            + (f"\nstderr: {tail}" if tail else "（stderr 为空）")
        )
    return result


# ── 发现：二进制 + 模型 ─────────────────────────────────────────────────────


def find_binary(name: str, *, env_var: str = "", extra: tuple[str, ...] = ()) -> str:
    """按 环境变量 → `PATH` → 已知绝对路径 的顺序找一个可执行文件。

    找不到返回**空串**（不抛）：`available()` 的契约是"能用吗"，不是"必须能用"。
    探测路径上抛异常会让 `orbit model --voice` 因为一台没装 piper 的机器而崩掉，
    而它本来只该显示"这一家不可用"。
    """
    if env_var:
        explicit = os.environ.get(env_var, "").strip()
        if explicit and Path(explicit).exists():
            return explicit
    found = shutil.which(name)
    if found:
        return found
    for candidate in extra:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def _home_dirs() -> tuple[Path, ...]:
    """piper 常见的"自己放模型"的几个目录（不存在的会在调用点被跳过）。"""
    dirs: list[Path] = []
    for env in ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME"):
        base = os.environ.get(env, "").strip()
        if not base:
            continue
        dirs.extend((Path(base) / "piper", Path(base) / ".piper"))
    return tuple(dirs)


def piper_candidate_dirs(*, data_dir: str | Path | None = None) -> tuple[Path, ...]:
    """找 piper 的二进制与模型时会扫的目录（顺序即优先级）。

    `<data>/voice/models` 排最前：那是 Orbit 自己的落点，用户把模型丢进去就能用，
    不用去设环境变量。
    """
    dirs: list[Path] = []
    if data_dir:
        dirs.append(Path(data_dir) / "voice" / "models")
    dirs.extend(_home_dirs())
    dirs.append(Path.cwd() / "models")
    return tuple(dirs)


def _binary_candidates() -> tuple[str, ...]:
    """piper 的已知绝对路径候选（Windows 带 .exe，POSIX 不带）。"""
    out: list[str] = []
    for folder in _home_dirs():
        out.extend((str(folder / "piper.exe"), str(folder / "piper")))
    return tuple(out)


def find_piper_models(*, data_dir: str | Path | None = None) -> tuple[Path, ...]:
    """找 piper 的 `.onnx` 声音模型。`PIPER_MODEL` 显式给了就只认它。

    去重保序：同一个目录可能通过 `LOCALAPPDATA` 与 `USERPROFILE` 两条路径被扫两次，
    而"扫出两份同样的模型"会让用户以为装了两遍。
    """
    explicit = os.environ.get(PIPER_MODEL_ENV, "").strip()
    if explicit:
        path = Path(explicit)
        return (path,) if path.is_file() else ()
    found: list[Path] = []
    for folder in piper_candidate_dirs(data_dir=data_dir):
        if not folder.is_dir():
            continue
        try:
            found.extend(sorted(folder.glob("*.onnx")))
        except OSError:  # 权限不足之类的目录直接跳过，不该让发现链崩掉
            continue
    seen: set[str] = set()
    unique: list[Path] = []
    for path in found:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return tuple(unique)


@dataclass(frozen=True)
class Discovery:
    """一次"找二进制 + 找模型"的结论。``gaps`` 非空即不可用。

    为什么把 ``searched`` 也带出来：用户说"我明明装了 piper"，我们得能回答
    "我在这 5 个目录里没找到" —— 只回一句"不可用"，下一个问题一定是"你找哪了"。
    """

    binary: str = ""
    model: str = ""
    gaps: tuple[str, ...] = ()
    searched: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.gaps

    def as_dict(self) -> dict[str, Any]:
        return {
            "binary": self.binary,
            "model": self.model,
            "gaps": list(self.gaps),
            "searched": list(self.searched),
            "ok": self.ok,
        }


def discover_piper(*, data_dir: str | Path | None = None) -> Discovery:
    """piper 的两段发现：二进制 → 模型。**这是 `available()` 的前两步**。"""
    binary = find_binary("piper", env_var=PIPER_BIN_ENV, extra=_binary_candidates())
    models = find_piper_models(data_dir=data_dir)
    searched = tuple(str(folder) for folder in piper_candidate_dirs(data_dir=data_dir))
    gaps: list[str] = []
    if not binary:
        gaps.append(f"可执行文件 `piper` 不在 PATH，也不在已知目录（{PIPER_BIN_ENV} 可指定）")
    if not models:
        gaps.append(f"没有任何 `.onnx` 声音模型（{PIPER_MODEL_ENV} 可指定；找过 {len(searched)} 个目录）")
    return Discovery(
        binary=binary,
        model=str(models[0]) if models else "",
        gaps=tuple(gaps),
        searched=searched,
    )


def discover_ffmpeg() -> str:
    """找 ffmpeg（`ORBIT_FFMPEG` → PATH）。flite 与 whisper 两个后端都靠它。"""
    return find_binary("ffmpeg", env_var=FFMPEG_BIN_ENV)


@functools.lru_cache(maxsize=4)
def ffmpeg_filters(ffmpeg: str) -> frozenset[str]:
    """`ffmpeg -filters` 里的滤镜名集合（用来判"这个 build 到底带不带 flite/whisper"）。

    为什么要真跑一次 `-filters` 而不是只判 ffmpeg 存在：**flite 与 whisper 都是
    `--enable-*` 编进去的可选件**。本机这份 gyan.dev full build 两个都带，
    但一个精简 build 就都没有 —— 那时 `ffmpeg` 在 PATH 上照样能跑，只是没有 TTS。
    """
    if not ffmpeg:
        return frozenset()
    try:
        result = run_tool(
            [ffmpeg, "-hide_banner", "-filters"], cwd=tempfile.gettempdir(), timeout=PROBE_TIMEOUT
        )
    except Exception:  # noqa: BLE001 —— 探测失败 = 没有滤镜，不该把调用方带崩
        return frozenset()
    names: set[str] = set()
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        pieces = line.split()
        # 形如 ` .. flite             |->A       Synthesize voice from text using libflite.`
        if len(pieces) >= 2 and pieces[0].startswith(("..", "T.", ".T", "TT")):
            names.add(pieces[1])
    return frozenset(names)


def find_whisper_models(*, data_dir: str | Path | None = None) -> tuple[Path, ...]:
    """找 whisper.cpp 的 ggml 模型（`ORBIT_WHISPER_MODEL` → 几个已知目录）。"""
    explicit = os.environ.get(WHISPER_MODEL_ENV, "").strip()
    if explicit:
        path = Path(explicit)
        return (path,) if path.is_file() else ()
    found: list[Path] = []
    for folder in piper_candidate_dirs(data_dir=data_dir):
        if not folder.is_dir():
            continue
        try:
            found.extend(sorted(folder.glob("ggml-*.bin")))
        except OSError:
            continue
    return tuple(found)


# ── 纯函数：命令行拼装（**离线可逐字断言**，见模块 docstring 末段）────────────


def piper_argv(binary: str, model: str, out_file: str) -> list[str]:
    """piper 的命令行：文本走 stdin，wav 走 `--output_file`。

    用 `--output_file` 而不是 `-f`：短选项在不同版本里撞过别的含义，
    长选项是 piper 官方 README 的写法。
    """
    return [binary, "--model", model, "--output_file", out_file]


def flite_argv(ffmpeg: str, text_file: str, *, voice: str = "slt") -> list[str]:
    """flite 的命令行：**文本走文件**（`textfile=`），wav 打到 stdout。

    三个刻意的选择：
    1. `textfile=` 而不是 `text=`：`text=` 的值里出现 `,` / `:` 会被当成滤镜分隔符
       截断（本机实测：一句带逗号的英文直接让 ffmpeg 以非零码退出）。走文件之后
       文本内容对 ffmpeg 的滤镜语法**完全不可见**，没有注入面也没有转义问题。
    2. 文件名必须是**相对路径**（调用方 `cwd` 到临时目录）：Windows 绝对路径里的
       `C:` 与 `\\` 同样会被滤镜语法吃掉。
    3. wav 打 stdout：少一个临时文件，也少一条"文件没写出来"的失败路径。
    """
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"flite=textfile={text_file}:voice={voice}",
        "-f",
        "wav",
        "-",
    ]


def whisper_argv(
    ffmpeg: str,
    audio_file: str,
    *,
    model: str,
    destination: str,
    language: str = "auto",
) -> list[str]:
    """whisper.cpp（ffmpeg 的 `whisper` filter）的命令行：转写结果写到 `destination`。

    同样全部用**相对文件名**（调用方 `cwd` 到临时目录），理由同 :func:`flite_argv`。
    `-f null -` 是"只要副作用不要输出流"的标准写法：这个 filter 是 A->A，
    真正的产物是 `destination` 里那份文本。
    """
    graph = f"whisper=model={model}:language={language}:destination={destination}"
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        audio_file,
        "-af",
        graph,
        "-f",
        "null",
        "-",
    ]


# ── 探测：真跑一次最小合成 ──────────────────────────────────────────────────


@functools.lru_cache(maxsize=8)
def _piper_probe(binary: str, model: str) -> Probe:
    """piper 的第三段阶梯：喂「OK」，拿到合法 wav 才算可用。

    缓存键是 `(binary, model)` 而不是无参：换了模型必须重新测（模型坏掉是
    piper 最常见的故障，而它长得和"二进制坏了"一模一样）。
    """
    with tempfile.TemporaryDirectory(prefix="orbit-piper-probe-") as tmp:
        out_path = Path(tmp) / "probe.wav"
        try:
            run_tool(
                piper_argv(binary, model, str(out_path)),
                cwd=tmp,
                stdin_bytes=PROBE_TEXT.encode("utf-8"),
                timeout=PROBE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 —— 探测失败就是不可用，原因原样带上
            return Probe(False, " ".join(str(exc).split())[:400], {"model": model})
        if not out_path.exists():
            return Probe(False, "piper 没有产出 wav 文件（探测合成失败）", {"model": model})
        data = out_path.read_bytes()
    if not is_wave(data):
        return Probe(False, "piper 产出的不是合法 RIFF/WAVE（探测合成失败）", {"model": model})
    return Probe(True, "", {"model": model, "probe_wav_bytes": len(data)})


@functools.lru_cache(maxsize=4)
def _flite_probe(ffmpeg: str) -> Probe:
    """flite 的探测：真合成一次「OK」，判据与 SAPI / piper 完全一致。"""
    if "flite" not in ffmpeg_filters(ffmpeg):
        return Probe(False, "这个 ffmpeg build 没有 flite 滤镜（缺 --enable-libflite）")
    with tempfile.TemporaryDirectory(prefix="orbit-flite-probe-") as tmp:
        (Path(tmp) / "probe.txt").write_text(PROBE_TEXT, encoding="utf-8")
        try:
            result = run_tool(flite_argv(ffmpeg, "probe.txt"), cwd=tmp, timeout=PROBE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return Probe(False, " ".join(str(exc).split())[:400])
    data = result.stdout or b""
    if not is_wave(data):
        return Probe(False, f"flite 产出的不是合法 RIFF/WAVE（{len(data)} 字节）")
    return Probe(True, "", {"probe_wav_bytes": len(data), "voice": "slt"})


@functools.lru_cache(maxsize=16)
def _commandline_probe(name: str, binary: str, command: str) -> Probe:
    """通用外部 TTS 的探测：**二进制在**且**真跑出一条命令拿到 wav** 才算可用。

    没有命令模板时只到第一段阶梯（二进制在就算可用），并如实说明
    "还缺一条命令模板" —— 这类工具（pico2wave）从 argv 取文本，
    本层的文件/stdin 通道喂不进去，只能由用户在 `tts_command` 里手写。
    """
    if not shutil.which(binary):
        return Probe(False, f"可执行文件 `{binary}` 不在 PATH")
    if not command.strip():
        return Probe(True, "", {"note": "二进制在，但没有命令模板（见 describe 的 install）"})
    from app.voice import _run_command  # 延迟 import：复用占位符/超时的同一套语义

    try:
        result = _run_command(command, stdin_bytes=PROBE_TEXT.encode("utf-8"), text=PROBE_TEXT)
    except Exception as exc:  # noqa: BLE001
        return Probe(False, " ".join(str(exc).split())[:400])
    data = result.stdout or b""
    if not is_wave(data):
        return Probe(False, f"`{binary}` 没有产出合法 wav（{len(data)} 字节）")
    return Probe(True, "", {"probe_wav_bytes": len(data)})


def reset_probe_cache() -> None:
    """清掉全部探测缓存（测试用；也留给"刚装完想立刻看到"的场景）。"""
    _piper_probe.cache_clear()
    _flite_probe.cache_clear()
    _commandline_probe.cache_clear()
    ffmpeg_filters.cache_clear()


# ── TTS：piper ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PiperTts:
    """piper（本地神经 TTS）：二进制 + 模型 + **实测合成**三段判据。

    `synthesize()` 是真实现：把文本从 stdin 喂给 piper、读它写出的 wav。
    装上 piper 和任一 `.onnx` 模型之后，这一家**不需要改代码**就会变成可用。
    """

    name: str = "piper"
    #: 让发现链能扫到 `<data>/voice/models`。空 = 只扫用户目录与 cwd。
    data_dir: str = ""

    def discovery(self) -> Discovery:
        return discover_piper(data_dir=self.data_dir or None)

    def probe(self) -> Probe:
        """三段阶梯的结论（二进制 → 模型 → 实测合成）。"""
        found = self.discovery()
        if found.gaps:
            return Probe(False, "；".join(found.gaps), found.as_dict())
        return _piper_probe(found.binary, found.model)

    def available(self) -> bool:
        return self.probe().ok

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """真跑 piper。不可用时抛 `ProviderUnavailable`（不是返回空字节）。"""
        body = str(text or "").strip()
        if not body:
            raise _failure("文本是空的：没什么可合成的。")
        found = self.discovery()
        if not found.ok:
            raise _unavailable(self._reason())
        with tempfile.TemporaryDirectory(prefix="orbit-piper-") as tmp:
            out_path = Path(tmp) / "out.wav"
            run_tool(
                piper_argv(found.binary, found.model, str(out_path)),
                cwd=tmp,
                stdin_bytes=body.encode("utf-8"),
            )
            if not out_path.exists() or out_path.stat().st_size <= 44:
                raise _failure(
                    f"piper 没有产出音频（文件不存在或只有 44 字节头）。模型 {found.model!r} "
                    "可能和文本语言不匹配 —— piper 的中文模型只吃中文，英文模型只吃英文。"
                )
            data = out_path.read_bytes()
        if not is_wave(data):
            raise _failure("piper 产出的不是 RIFF/WAVE（前 12 字节对不上），不能当音频用。")
        return data

    def _reason(self) -> str:
        found = self.discovery()
        if found.gaps:
            return "；".join(found.gaps)
        probe = self.probe()
        return f"二进制与模型都在，但**探测合成失败**：{probe.reason}"

    def _requires(self) -> str:
        found = self.discovery()
        missing_binary = not found.binary
        missing_model = not found.model
        if missing_binary and missing_model:
            return "piper 可执行文件 + 声音模型（.onnx，需与文本语言匹配）"
        if missing_binary:
            return "piper 可执行文件"
        if missing_model:
            return "声音模型（.onnx）"
        return "piper + 可用的 .onnx 模型（已满足）"

    def _install(self) -> str:
        found = self.discovery()
        steps: list[str] = []
        if not found.binary:
            steps.append(PIPER_INSTALL)
        if not found.model:
            steps.append(PIPER_MODEL_INSTALL)
        if not steps:
            steps.append("已装好；若合成失败，换一个与文本语言匹配的模型")
        return "；".join(steps)

    def describe(self) -> str:
        """三件套（reason / requires / install）与 19 家云端那批保持同一排版。"""
        if not self.available():
            return (
                f"piper：不可用 —— reason: {self._reason()}（本机无网络，本层不发真实请求）；"
                f"requires: {self._requires()}；install: {self._install()}"
            )
        probe = self.probe()
        found = self.discovery()
        size = probe.detail.get("probe_wav_bytes") if probe.detail else "?"
        return (
            f"piper：可用 —— 本机 piper `{found.binary}` + 模型 `{found.model}`"
            f"（探测合成 {size} 字节 wav）；requires: 已满足；install: 已安装；"
            "reason: 纯本地 VITS，不需要联网。"
        )


# ── TTS：flite（走 ffmpeg 的 libflite）──────────────────────────────────────

#: libflite 自带的 6 个音色（英文）。写出来是为了让"换音色"有明确取值域。
FLITE_VOICES: tuple[str, ...] = ("slt", "kal", "awb", "rms", "kal16")


@dataclass(frozen=True)
class FfmpegFliteTts:
    """flite：**本机唯一一个不装任何东西就能真跑的额外 TTS**。

    它不是一个独立的二进制，而是 ffmpeg 的一个 source 滤镜（`--enable-libflite`）。
    本机实测（gyan.dev full build）：

    * 「Hello, orbit: this is a test; 123.」→ **126,798 字节**合法 wav；
    * 「你好，世界。」→ 5,998 字节，也就是**接近静音**。

    所以它的能力边界必须写死：**只支持英文**。非 ASCII 文本 `synthesize()` 直接拒绝
    并指路（`sapi` 中文音色 / `tts_command` 接 piper 中文模型）—— 悄悄吐一段静音，
    用户只会以为"TTS 坏了"。
    """

    name: str = "flite"
    #: 默认音色（`slt` = 女声，libflite 的默认之外最常用的一条）。
    voice: str = "slt"

    def ffmpeg(self) -> str:
        return discover_ffmpeg()

    def probe(self) -> Probe:
        exe = self.ffmpeg()
        if not exe:
            return Probe(False, f"找不到 ffmpeg（{FFMPEG_BIN_ENV} 可指定绝对路径）")
        return _flite_probe(exe)

    def available(self) -> bool:
        return self.probe().ok

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        body = str(text or "").strip()
        if not body:
            raise _failure("文本是空的：没什么可合成的。")
        if not body.isascii():
            raise _failure(
                "flite 只支持英文（libflite 只带 slt/kal/awb/rms/kal16 六个英文音色），"
                f"这段文本里有非 ASCII 字符，合成出来会是接近静音的 {len(body)} 字。"
                "中文请用 `sapi`（Windows 自带音色）或配 `tts_command` 接 piper 的中文模型。"
            )
        probe = self.probe()
        if not probe.ok:
            raise _unavailable(self._reason())
        chosen = (voice or self.voice or "slt").strip()
        exe = self.ffmpeg()
        with tempfile.TemporaryDirectory(prefix="orbit-flite-") as tmp:
            (Path(tmp) / "text.txt").write_text(body, encoding="utf-8")
            result = run_tool(flite_argv(exe, "text.txt", voice=chosen), cwd=tmp)
        data = result.stdout or b""
        if not is_wave(data):
            raise _failure(
                f"flite 没有产出合法 wav（{len(data)} 字节）。音色 {chosen!r} 可能不存在，"
                f"可选：{'/'.join(FLITE_VOICES)}。"
            )
        return data

    def _reason(self) -> str:
        exe = self.ffmpeg()
        if not exe:
            return f"找不到 ffmpeg（{FFMPEG_BIN_ENV} 可指定绝对路径）"
        if "flite" not in ffmpeg_filters(exe):
            return (
                f"ffmpeg 在（{exe}），但这个 build **没有 flite 滤镜**"
                "（编译时缺 --enable-libflite），所以它没有 TTS 能力"
            )
        return f"ffmpeg 与 flite 滤镜都在，但**探测合成失败**：{self.probe().reason}"

    def _requires(self) -> str:
        exe = self.ffmpeg()
        if not exe:
            return "ffmpeg"
        if "flite" not in ffmpeg_filters(exe):
            return "带 --enable-libflite 的 ffmpeg build"
        return "ffmpeg + libflite（已满足）"

    def describe(self) -> str:
        if not self.available():
            return (
                f"flite：不可用 —— reason: {self._reason()}；"
                f"requires: {self._requires()}；install: {FLITE_INSTALL}"
            )
        probe = self.probe()
        size = probe.detail.get("probe_wav_bytes") if probe.detail else "?"
        return (
            f"flite：可用（**仅英文**）—— ffmpeg 的 libflite 滤镜，音色 "
            f"{'/'.join(FLITE_VOICES)}（当前 {self.voice}；探测合成 {size} 字节 wav）；"
            f"requires: 已满足；install: 已安装（{FLITE_INSTALL}）；"
            "reason: 纯本地、零额外安装；**中文不支持**，中文请用 `sapi` 或 `tts_command`。"
        )


# ── TTS：其它常见本地工具（本机一个都没有，如实登记 + 安装方式）──────────────


@dataclass(frozen=True)
class CommandLineTts:
    """espeak / espeak-ng / festival / say / pico2wave 这类"一个可执行文件就是全部"的 TTS。

    与 `app/voice_local.py::BinaryTts` 的分工：那个只做 `shutil.which`（"装了没"），
    这个在二进制在的时候**再真跑一次最小合成**（"能不能用"）。本机这五个都不在 PATH
    （实测 `Get-Command espeak` / `espeak-ng` / `festival` / `say` / `pico2wave` 全部 MISS），
    所以它们在这里是**如实登记 + 安装方式**，不是"假装支持"。
    """

    name: str
    #: 可执行文件名（`shutil.which` 的目标）。
    binary: str
    #: 合成命令模板（占位符式，见 `app/voice.py::_run_command`）。空 = 只做检测。
    command: str = ""
    requires: str = ""
    install: str = ""
    #: 这条通道的已知限制（写进 describe，别让用户自己撞）。
    note: str = ""

    def probe(self) -> Probe:
        return _commandline_probe(self.name, self.binary, self.command)

    def available(self) -> bool:
        return self.probe().ok

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        body = str(text or "").strip()
        if not body:
            raise _failure("文本是空的：没什么可合成的。")
        if not self.available():
            raise _unavailable(self._reason())
        if not self.command.strip():
            raise _unavailable(
                f"{self.name}：二进制在，但没有合成命令模板（本层不猜它的 argv）。"
                "在 `<data>/voice.json` 的 `tts_command` 里写一条即可。"
            )
        from app.voice import _run_command

        result = _run_command(
            self.command,
            stdin_bytes=body.encode("utf-8"),
            text=body,
            prefer_stdout=False,
        )
        data = result.stdout or b""
        if not is_wave(data):
            raise _failure(f"{self.name} 没有产出合法 wav（{len(data)} 字节）。")
        return data

    def _reason(self) -> str:
        probe = self.probe()
        if not shutil.which(self.binary):
            return f"可执行文件 `{self.binary}` 不在 PATH（本机实测 MISS）"
        return f"二进制在，但**探测合成失败**：{probe.reason}"

    def describe(self) -> str:
        if not self.available():
            note = f"（{self.note}）" if self.note else ""
            return (
                f"{self.name}：不可用 —— reason: {self._reason()}{note}；"
                f"requires: {self.requires or f'`{self.binary}` 可执行文件'}；"
                f"install: {self.install or '装好这个工具并让它进 PATH'}"
            )
        note = f"；注意: {self.note}" if self.note else ""
        return (
            f"{self.name}：可用 —— 本机 `{shutil.which(self.binary) or self.binary}`；"
            f"requires: 已满足；install: 已安装；"
            f"reason: 纯本地，不需要联网{note}。"
        )


#: 本机**一个都没有**的 5 家常驻本地 TTS。名字与各自的 CLI 形状都写实：
#: 有 `-w`/`-o` 这类"写 wav"选项的给出真模板，只从 argv 取文本的（pico2wave）留空。
EXTRA_LOCAL_TTS: tuple[CommandLineTts, ...] = (
    CommandLineTts(
        name="espeak",
        binary="espeak",
        # espeak 的 `-f` 读文本文件、`-w` 写 wav —— 正好是本层的占位符式契约。
        command="espeak -f {text_file} -w {audio_file}",
        requires="`espeak` 可执行文件（Debian: espeak-ng；Windows: eSpeak NG 安装包）",
        install=(
            "Linux: `sudo apt install espeak`；Windows: 装 eSpeak NG 并把安装目录加进 PATH；"
            "macOS: `brew install espeak`"
        ),
        note="eSpeak 是共振峰合成，音质明显不如 piper，但中文（-v zh）能听懂",
    ),
    CommandLineTts(
        name="espeak-ng",
        binary="espeak-ng",
        command="espeak-ng -f {text_file} -w {audio_file}",
        requires="`espeak-ng` 可执行文件",
        install="Linux: `sudo apt install espeak-ng`；Windows: https://github.com/espeak-ng/espeak-ng/releases",
        note="espeak 的活跃分支，命令行与 espeak 兼容",
    ),
    CommandLineTts(
        name="festival",
        binary="festival",
        # festival 的 wav 输出要走 Scheme，不是一条命令行能拼出来的；不猜。
        command="",
        requires="`festival` 可执行文件 + 至少一个 voice（如 cmu_us_slt_arctic）",
        install=(
            "Linux: `sudo apt install festival festvox-us-slt-html`；"
            "wav 输出需要 Scheme 脚本，装好后在 `tts_command` 里手写"
        ),
        note="festival 默认只往音频设备说话，落 wav 需要 `(utt.save.wave ...)`",
    ),
    CommandLineTts(
        name="say",
        binary="say",
        # macOS 的 say：`-o` 写文件、`-f` 读文本文件。只在 macOS 上有。
        command="say -f {text_file} -o {audio_file} --data-format=LEI16@22050",
        requires="macOS 自带的 `say`（Windows / Linux 上没有）",
        install="只能在 macOS 上用；本机是 Windows，所以这一家必然不可用",
        note="macOS 自带，中文要配一个中文音色（`say -v '?'` 列出）",
    ),
    CommandLineTts(
        name="pico2wave",
        binary="pico2wave",
        # pico2wave **只从 argv 取文本**（`pico2wave -w out.wav "text"`），
        # 既没有 `-f` 也不读 stdin，所以本层的文件/stdin 通道喂不进去。
        command="",
        requires="`pico2wave`（SVOX Pico TTS）+ `libttspico-utils`",
        install=(
            "Debian: `sudo apt install libttspico-utils`；"
            "它只从 argv 取文本，装好后在 `tts_command` 里手写 `pico2wave -w {out_file} ...`"
        ),
        note="Pico 是超轻量嵌入式 TTS，只支持 en-GB/en-US/it/de/es/fr",
    ),
)


# ── STT：whisper.cpp（走 ffmpeg 的 whisper filter）──────────────────────────


@dataclass(frozen=True)
class FfmpegWhisperStt:
    """whisper.cpp：ffmpeg 的 `whisper` filter（`--enable-whisper`）。

    **本机实测结论：filter 在，模型不在** —— `ffmpeg -filters` 里能查到
    `whisper  A->A  Transcribe audio using whisper.cpp.`，但全盘没有 `ggml-*.bin`。
    所以 `available()` 如实返回 `False`，并把"下哪个模型、指给哪个环境变量"写清楚。

    这条通道**没能在本机实测**（没有模型就没法转写），如实标在 `describe()` 里；
    命令行拼装（:func:`whisper_argv`）是纯函数，测试逐字断言过。
    """

    name: str = "whisper_cpp"
    #: 让发现链能扫到 `<data>/voice/models/ggml-*.bin`。
    data_dir: str = ""
    #: 识别语言。`auto` 让 whisper 自己判；中文可以钉成 `zh`。
    language: str = "auto"

    def discovery(self) -> Discovery:
        exe = discover_ffmpeg()
        models = find_whisper_models(data_dir=self.data_dir or None)
        gaps: list[str] = []
        if not exe:
            gaps.append(f"找不到 ffmpeg（{FFMPEG_BIN_ENV} 可指定）")
        elif "whisper" not in ffmpeg_filters(exe):
            gaps.append("这个 ffmpeg build 没有 whisper 滤镜（缺 --enable-whisper）")
        if not models:
            gaps.append(f"没有任何 ggml 模型（{WHISPER_MODEL_ENV} 可指定）")
        return Discovery(
            binary=exe,
            model=str(models[0]) if models else "",
            gaps=tuple(gaps),
            searched=tuple(str(f) for f in piper_candidate_dirs(data_dir=self.data_dir or None)),
        )

    def available(self) -> bool:
        """判据是**齐备性**（ffmpeg+filter+模型），不是"试转一段"。

        为什么不试转：whisper 的一次最小转写要加载几十 MB 模型、本机冷启动数秒，
        而 `describe_all()` 会把每家问一遍。**这是与 TTS 那几家刻意的差异**，
        理由写在这里而不是藏起来：模型在不在是确定的事实，转写质量不是。
        """
        return self.discovery().ok

    def transcribe(self, audio: bytes) -> str:
        if not audio:
            raise _failure("音频是空的：没什么可转写的。")
        if not audio.startswith(b"RIFF"):
            raise _failure(
                "whisper 走 ffmpeg，但本层只把 wav 直接喂进去。"
                f"这段数据前 4 字节是 {audio[:4]!r}，先转成 wav："
                "`ffmpeg -i in.ogg -ar 16000 -ac 1 out.wav`。"
            )
        found = self.discovery()
        if not found.ok:
            raise _unavailable(self._reason())
        with tempfile.TemporaryDirectory(prefix="orbit-whisper-") as tmp:
            (Path(tmp) / "in.wav").write_bytes(audio)
            run_tool(
                whisper_argv(
                    found.binary,
                    "in.wav",
                    model=Path(found.model).name,
                    destination="out.txt",
                    language=self.language,
                ),
                cwd=tmp,
            )
            out_path = Path(tmp) / "out.txt"
            if not out_path.exists():
                raise _failure(
                    "whisper 没有写出转写结果（destination 没生成）。"
                    "检查模型文件是否与 ffmpeg 的 whisper filter 版本匹配。"
                )
            text = out_path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            raise _failure("whisper 转写结果为空（音频是静音，或与 language 不符）。")
        return text

    def _reason(self) -> str:
        return "；".join(self.discovery().gaps) or "未知原因"

    def describe(self) -> str:
        found = self.discovery()
        if not found.ok:
            return (
                f"{self.name}：不可用 —— reason: {self._reason()}；"
                "requires: 带 --enable-whisper 的 ffmpeg + whisper.cpp 的 ggml 模型；"
                f"install: {WHISPER_INSTALL}"
            )
        return (
            f"{self.name}：可用 —— ffmpeg `{found.binary}` + 模型 `{found.model}`"
            f"（language={self.language}）；requires: 已满足；install: 已安装；"
            "reason: 纯本地 whisper.cpp；**本条通道未在本机实测过转写**（本机无模型）。"
        )


# ── 登记实例 ────────────────────────────────────────────────────────────────

#: piper 的真实现。`app/voice.py` 的 11 家里那一条 `piper` 用的就是它。
PIPER_TTS: PiperTts = PiperTts()

#: flite（本机真可用的那一家）。
FLITE_TTS: FfmpegFliteTts = FfmpegFliteTts()

#: STT 侧的 whisper.cpp。
WHISPER_STT: FfmpegWhisperStt = FfmpegWhisperStt()

#: 全部"本机二进制"类 TTS（flite 打头：它是本机唯一真可用的那一家）。
BINARY_LOCAL_TTS: tuple[Any, ...] = (FLITE_TTS, *EXTRA_LOCAL_TTS)


def local_tts_catalog() -> list[dict[str, Any]]:
    """本机二进制类 TTS + whisper.cpp STT 的可用性清单（CLI / JSON / 测试共用）。

    每条都带 `available` / `reason` / `requires` / `install` 的可判定字段：
    这张清单的用途就是"到底哪家能用、不能用是缺什么"，只给 `bool` 是不够的。
    """
    rows: list[dict[str, Any]] = []
    for provider in BINARY_LOCAL_TTS:
        rows.append(
            {
                "kind": "binary-tts",
                "name": provider.name,
                "available": provider.available(),
                "describe": provider.describe(),
            }
        )
    rows.append(
        {
            "kind": "binary-stt",
            "name": WHISPER_STT.name,
            "available": WHISPER_STT.available(),
            "describe": WHISPER_STT.describe(),
        }
    )
    return rows


__all__ = [
    "BINARY_LOCAL_TTS",
    "EXTRA_LOCAL_TTS",
    "FFMPEG_BIN_ENV",
    "FLITE_TTS",
    "FLITE_VOICES",
    "PIPER_BIN_ENV",
    "PIPER_INSTALL",
    "PIPER_MODEL_ENV",
    "PIPER_MODEL_INSTALL",
    "PIPER_TTS",
    "WHISPER_INSTALL",
    "WHISPER_MODEL_ENV",
    "WHISPER_STT",
    "CommandLineTts",
    "Discovery",
    "FfmpegFliteTts",
    "FfmpegWhisperStt",
    "PiperTts",
    "discover_ffmpeg",
    "discover_piper",
    "ffmpeg_filters",
    "find_binary",
    "find_piper_models",
    "find_whisper_models",
    "flite_argv",
    "is_wave",
    "local_tts_catalog",
    "piper_argv",
    "piper_candidate_dirs",
    "reset_probe_cache",
    "run_tool",
    "whisper_argv",
]

"""MATRIX 4.12 的「本地那半」：Windows SAPI 真合成 / 真识别 + 三家本地 provider 的真检测。

## 这个文件要解决的两个具体问题

### 1. 三家「本来就是纯本地」的 TTS 之前是恒 `False`

`app/voice.py` 的 11 家 TTS 里 `piper` / `kittentts` / `neutts` **本来就是纯本地**的，
但上一轮把它们写成 `UnavailableTts` —— 那个类 `available()` 恒 `False`，于是
"没装" 与 "装了也没人检测" 在输出里长得一模一样。用户装好 piper 后回来一看还是
"不可用"，只会以为自己装错了。本文件把这三家换成 :class:`BinaryTts`：
`available()` 真的去 `shutil.which()` / `importlib.util.find_spec()`，装了就真能用。

### 2. Windows 自带的 SAPI 一直没被用起来

`:class:`LocalTtsProvider`（名字 `sapi`）走 `System.Speech.Synthesis`，
**在本机能真的合成出合法 RIFF/WAVE**。实测证据（本机 PowerShell 5.1，
`Microsoft Huihui Desktop` / zh-CN）：

* 「Orbit test 123」→ 121,484 字节 wav；
* 「你好，世界。Orbit 语音合成测试。」→ 220,252 字节 wav。

`:class:`LocalSttProvider`（名字 `sapi`）走 `System.Speech.Recognition`。
任务书里写的是"SAPI 做不了自由听写（只支持语法）"，**实测不成立**：
`DictationGrammar` 配 `SetInputToWaveFile` 能对着 wav 做听写。本机（识别器
`MS-2052-80-DESK` / zh-CN）实测「你好，这是 Orbit 的语音测试。今天天气不错。」
→ `你好这些个别的语音特许`，置信度 0.35、368ms。质量确实差，所以它**照实标注**
`quality="poor"` 并推荐 `stt_command`（faster-whisper），而不是假装好用。

## 为什么走 `powershell -File`，而不是 pywin32 / comtypes / `-Command`

1. **不许装依赖**（本轮铁律）。`pywin32` / `comtypes` 都要 `pip install`，而
   `System.Speech` 是 .NET Framework 自带的、`powershell.exe` 是 Windows 自带的 ——
   这条路径**零安装**。
2. **`-File` 而不是 `-Command`**：`-Command` 要把脚本体拼进一个字符串里，中文、引号、
   `$`、反引号全要转义，转错一处就是"合成出一段空白音频"这种不报错的坏结果。
   `-File` 把脚本落在临时 `.ps1`、**文本也落在临时文件里**（脚本用
   `Get-Content -Raw -Encoding UTF8` 读），于是命令行参数只剩路径 —— 没有注入面。
3. **脚本必须写成 UTF-8 **带 BOM**（`utf-8-sig`）**：Windows PowerShell 5.1 读没有
   BOM 的 `.ps1` 时按系统 ANSI 代码页（本机 GBK）解码，脚本里任何非 ASCII 字符都会
   变成乱码 —— 而乱码里可能出现 `0x27`（单引号），于是报出来的是
   "The string is missing the terminator" 这种**看起来与内容无关**的解析错误。
4. **`param(...)` 必须是脚本的第一个语句**：放第二行之后 PowerShell 会把它当命令去
   执行，于是参数全空、脚本以"静默的错误"收场（实测踩过）。
5. **`$ErrorActionPreference = 'Stop'` 必须在**：实测有一次脚本报了一屏错误但
   **退出码仍是 0**。所以"靠退出码判断成败"在这条链路上不成立，两道闸都要有：
   脚本里让错误变成终止错误，Python 侧再核对产物（文件在不在、是不是 RIFF）。

## `available()` 为什么要**实测一次最小合成**，而不是只看"音色装没装"

这是本文件里最容易被写歪的一处，也是本轮实测撞上的真问题：

- 在本机，`GetInstalledVoices()` 会列出 2 个音色且 `Enabled=True`；
- 但从 **venv python 的子进程**里调 SAPI，`SelectVoice` / `Speak` 一律报
  「系统上未安装语音，或没有当前安全设置可用的语音」；
- 查 `whoami /groups` 才发现：那个 powershell 进程的完整性级别是
  **`Mandatory Label\\Low`**（S-1-16-4096）。SAPI 的音色 token 不对 Low IL 开放，
  于是"装了音色"与"能用音色"在这里分叉。
- 同一个脚本从高完整性级别的进程（本机的 PowerShell 通道）跑，完全正常
  （就是上面那 121KB / 220KB 的证据）。

结论：`available()` 只看"音色在不在"会**说谎**。所以它现在真的合成一次最短的
「OK」（结果缓存），拿不到合法 wav 就返回 `False` 并给出**实测到的报错原文**。
宁可说不能用，也不要在用户按下"朗读"之后才炸。

## 探测结果为什么要缓存

`available()` 会被 `describe_all()` 在列清单时对每一家各调一次，而一次探测要 fork
一个 powershell（本机冷启动约 300ms）。所以结果缓存在模块级
（:func:`_tts_probe` / :func:`_stt_probe`），并用 :func:`reset_probe_cache()` 给
"刚装完语音包想立刻看到"的场景留一个口子 —— 缓存不可重置的话，
"装上了仍显示不可用"会变成第二个查不出来的问题。
"""

from __future__ import annotations

import functools
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── 与 app/voice.py 的关系 ──────────────────────────────────────────────────
#
# 反向 import（`from app.voice import VoiceError`）放在函数里，是为了避开循环导入：
# `app/voice.py` 在模块顶层要用本文件的 `BinaryTts` / `LOCAL_SAPI_TTS` 来组装登记表，
# 而本文件只在**抛错时**才需要那两个异常类型。"用到才需要"的东西延迟到调用点 import
# 没有代价，却让两边都能被单独 import（测试直接 import `app.voice_local` 也不会把
# 整条装配链拖进来）。


def _errors() -> tuple[type[Exception], type[Exception]]:
    """取 `app.voice` 的 `(VoiceError, ProviderUnavailable)`。延迟 import，见上。"""
    from app.voice import ProviderUnavailable, VoiceError

    return VoiceError, ProviderUnavailable


def _unavailable(message: str) -> Exception:
    """造一个 `ProviderUnavailable`（消息要能直接给人看：缺什么 + 怎么办）。"""
    _voice_error, provider_unavailable = _errors()
    return provider_unavailable(message)


def _failure(message: str) -> Exception:
    """造一个 `VoiceError`（"这台机器能做，但这次没做成"）。"""
    voice_error, _provider_unavailable = _errors()
    return voice_error(message)


# ── powershell 定位 ─────────────────────────────────────────────────────────

#: 依次尝试的解释器。`powershell` = Windows PowerShell 5.1（`System.Speech` 随
#: .NET Framework 一起装好，本机就是它）；`pwsh` = PowerShell 7，留给装了新版的环境。
POWERSHELL_CANDIDATES: tuple[str, ...] = ("powershell", "pwsh")

#: 只读探测的超时（秒）。列音色 / 列识别器都是毫秒级动作，20 秒已经很宽。
PROBE_TIMEOUT = 20.0

#: 一次"最小合成"探测的超时。冷启动 powershell + SAPI 初始化 + 说一个词，实测 ~1s。
PROBE_SYNTH_TIMEOUT = 30.0

#: 合成/识别超时（秒）。口径是"一句话到一段话"；整本有声书不该由一次工具调用完成。
RUN_TIMEOUT = 120.0


@functools.lru_cache(maxsize=1)
def powershell_path() -> str:
    """本机 powershell 的绝对路径；没有就返回空串（**不抛异常**）。

    为什么返回空串而不是抛：`available()` 的契约是"能用吗"，不是"必须能用"。
    探测路径上抛异常会让 `voice --list` 因为一台没装 powershell 的机器而崩掉，
    而它本来只该显示"这一家不可用"。
    """
    for name in POWERSHELL_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return ""


# ── 脚本模板 ────────────────────────────────────────────────────────────────
#
# 共同规则见模块 docstring 第 3~5 条：UTF-8 **带 BOM** 写文件、`param` 必须在第一行、
# 脚本里 `$ErrorActionPreference = 'Stop'`、输出编码钉成 UTF-8。
#
# 输出编码这一条单独说：Windows PowerShell 5.1 的 `[Console]::OutputEncoding` 默认是
# 控制台代码页（本机 936/GBK），音色名与识别文本里的中文会变成乱码 —— 而乱码不报错，
# 只会"看起来识别失败"，是最难查的一类故障。

_HEAD = (
    "$ErrorActionPreference = 'Stop'\n"
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
)

#: 列音色。制表符分隔：`名字 \t culture \t 性别 \t enabled`。
#:
#: **不要碰 `$synth.Voice`**：实测这一句会让后续 `GetInstalledVoices()` 把两个音色都
#: 报成 `Enabled=False`（拿 `$s.Voice.VoiceInfo.Name` 试一次就能复现，删掉它立刻恢复）。
#: 音色一律从 `GetInstalledVoices()` 里按名字挑。
SAPI_VOICES_SCRIPT = (
    _HEAD
    + """Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
foreach ($voice in $synth.GetInstalledVoices()) {
  $info = $voice.VoiceInfo
  Write-Output ("{0}`t{1}`t{2}`t{3}" -f $info.Name, $info.Culture.Name, $info.Gender, $voice.Enabled)
}
$synth.Dispose()
"""
)

#: 文本 → wav。文本从文件读（不拼进命令行），音色可选。
SAPI_TTS_SCRIPT = (
    """param([string]$TextFile, [string]$OutFile, [string]$Voice)
"""
    + _HEAD
    + """Add-Type -AssemblyName System.Speech
$text = Get-Content -Raw -Encoding UTF8 -LiteralPath $TextFile
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($Voice) { $synth.SelectVoice($Voice) }
$synth.SetOutputToWaveFile($OutFile)
$synth.Speak($text)
$synth.Dispose()
Write-Output "SAPI_OK"
"""
)

#: 列已安装的识别器。制表符分隔：`名字 \t culture`。
SAPI_RECOGNIZERS_SCRIPT = (
    _HEAD
    + """Add-Type -AssemblyName System.Speech
$all = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
foreach ($rec in $all) { Write-Output ("{0}`t{1}" -f $rec.Name, $rec.Culture.Name) }
"""
)

#: wav → 文本（听写）。循环 `Recognize()` 直到没有结果，最多 8 段 ——
#: 一段长音频会被切成多个 utterance，只读第一段会静默丢内容。
SAPI_STT_SCRIPT = (
    """param([string]$AudioFile, [string]$Culture)
"""
    + _HEAD
    + """Add-Type -AssemblyName System.Speech
$cultureInfo = New-Object System.Globalization.CultureInfo($Culture)
$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($cultureInfo)
$engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$engine.SetInputToWaveFile($AudioFile)
$pieces = @()
for ($i = 0; $i -lt 8; $i++) {
  $result = $engine.Recognize()
  if ($result -eq $null) { break }
  $pieces += $result.Text
}
$engine.Dispose()
[Console]::Out.Write(($pieces -join ' '))
"""
)

#: **引擎自检**：只验证"能不能造出识别引擎并喂一个 wav"，不要求识别出内容。
#: 用它当 `available()` 的判据：静音 wav 识别不出东西是正常的（stdout 空），
#: 而引擎造不出来会抛错 —— 这两种情况必须分得开。
SAPI_STT_PROBE_SCRIPT = (
    """param([string]$AudioFile, [string]$Culture)
"""
    + _HEAD
    + """Add-Type -AssemblyName System.Speech
$cultureInfo = New-Object System.Globalization.CultureInfo($Culture)
$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($cultureInfo)
$engine.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$engine.SetInputToWaveFile($AudioFile)
$null = $engine.Recognize()
$engine.Dispose()
Write-Output "ENGINE_OK"
"""
)


def run_powershell(
    script: str,
    *,
    args: dict[str, str] | None = None,
    timeout: float = RUN_TIMEOUT,
) -> subprocess.CompletedProcess[bytes]:
    """把一段脚本落到临时 `.ps1` 里跑一遍，返回完整结果（stdout/stderr/退出码）。

    **不用 `shell=True`**：脚本是本文件的常量，参数是路径，没有任何来自模型/用户的
    字符串进命令行。这也是"文本走文件"的第二个理由（第一个是转义）。

    非零退出**抛错并带 stderr 尾部**：只说"合成失败"会让用户去猜是音色名错了、
    还是这台机器没装语音包 —— powershell 的 stderr 恰好把这两件事说得清清楚楚。
    """
    exe = powershell_path()
    if not exe:
        raise _unavailable(
            "找不到 powershell（Windows PowerShell 5.1 或 pwsh）。SAPI 走的就是它，"
            "没有它就调不到 System.Speech。install: 装回 Windows 自带的 PowerShell；"
            "requires: powershell + System.Speech；**不需要联网**。"
        )
    with tempfile.TemporaryDirectory(prefix="orbit-sapi-") as tmp:
        script_path = Path(tmp) / "sapi.ps1"
        script_path.write_text(script, encoding="utf-8-sig")  # BOM 是必须的，见 docstring
        argv = [
            exe,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ]
        for key, value in (args or {}).items():
            argv.extend([f"-{key}", str(value)])
        try:
            result = subprocess.run(  # noqa: S603 —— argv 已拆好，脚本是本文件的常量
                argv,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:  # pragma: no cover - 上面已经 which 过
            raise _unavailable(f"powershell 不可执行：{exe}") from exc
        except subprocess.TimeoutExpired as exc:
            raise _failure(
                f"powershell 超时（{timeout:g}s）：这段文本/音频太长，或 SAPI 引擎卡住了。"
            ) from exc
        if result.returncode != 0:
            stderr = decode(result.stderr).strip()
            raise _failure(
                f"powershell 退出码 {result.returncode}"
                + (f"\nstderr: {stderr[-800:]}" if stderr else "（stderr 为空）")
            )
        return result


def decode(raw: bytes | None) -> str:
    """powershell 的输出解码：先 UTF-8，再 GBK（脚本里已设 UTF-8，GBK 是兜底）。"""
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def silent_wav(seconds: float = 0.2, *, rate: int = 16000) -> bytes:
    """造一段**静音 wav**（16-bit 单声道 PCM）。

    为什么要它：`LocalSttProvider.available()` 需要一个"合法的 wav 输入"来验证识别
    引擎能不能初始化，而这时候我们**不能**依赖 SAPI 合成（那正是可能坏掉的那一半）。
    用标准库 `wave` 直接写一段静音，判据就只剩"识别引擎能不能起来"这一件事。
    """
    frames = int(rate * max(0.01, float(seconds)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


# ── 探测：音色 / 识别器 / 最小合成 / 引擎自检 ───────────────────────────────


@dataclass(frozen=True)
class SapiVoice:
    """一个已安装的 SAPI 音色（真数据，不是声明表）。"""

    name: str
    culture: str = ""
    gender: str = ""
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "culture": self.culture,
            "gender": self.gender,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class Probe:
    """一次可用性实测的结论。``ok=False`` 时 ``reason`` 必须是**实测到的原文**。

    为什么不用裸 `bool`：调用方（`describe()` / 异常消息）需要把"为什么不行"原样
    转给人看。丢掉原因，用户就只会看到"不可用"，然后去猜。
    """

    ok: bool
    reason: str = ""
    detail: dict[str, Any] | None = None


def _parse_rows(text: str, fields: int) -> list[list[str]]:
    """解析制表符分隔的探测输出，丢掉字段数不对的行（探测输出不该让整层崩）。"""
    rows: list[list[str]] = []
    for line in text.splitlines():
        pieces = line.rstrip("\r").split("\t")
        if len(pieces) >= fields and pieces[0].strip():
            rows.append(pieces)
    return rows


@functools.lru_cache(maxsize=1)
def _voices_cached() -> tuple[SapiVoice, ...]:
    """音色表（进程内只探一次，见模块 docstring「探测结果为什么要缓存」）。"""
    if not powershell_path():
        return ()
    try:
        result = run_powershell(SAPI_VOICES_SCRIPT, timeout=PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001 —— 探测失败 = 没有音色，不该把调用方带崩
        return ()
    voices: list[SapiVoice] = []
    for pieces in _parse_rows(decode(result.stdout), 3):
        enabled = len(pieces) < 4 or pieces[3].strip().lower() != "false"
        voices.append(
            SapiVoice(
                name=pieces[0].strip(),
                culture=pieces[1].strip(),
                gender=pieces[2].strip(),
                enabled=enabled,
            )
        )
    return tuple(voices)


@functools.lru_cache(maxsize=1)
def _recognizers_cached() -> tuple[tuple[str, str], ...]:
    """已安装识别器表 `(名字, culture)`（进程内只探一次）。"""
    if not powershell_path():
        return ()
    try:
        result = run_powershell(SAPI_RECOGNIZERS_SCRIPT, timeout=PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001
        return ()
    return tuple(
        (pieces[0].strip(), pieces[1].strip())
        for pieces in _parse_rows(decode(result.stdout), 2)
    )


def _first_error(exc: Exception) -> str:
    """把异常压成一行（SAPI 的报错原文常常夹在 PowerShell 的包装里）。"""
    text = " ".join(str(exc).split())
    return text[:400]


@functools.lru_cache(maxsize=1)
def _tts_probe() -> Probe:
    """TTS 可用性：**真的合成一次**「OK」，拿到合法 wav 才算可用（见模块 docstring）。"""
    if not powershell_path():
        return Probe(False, "找不到 powershell（Windows PowerShell 5.1 / pwsh）")
    voices = [item for item in _voices_cached() if item.enabled]
    if not voices:
        return Probe(False, "没有任何启用的 SAPI 音色（语音包缺失）")
    chosen = voices[0].name
    try:
        with tempfile.TemporaryDirectory(prefix="orbit-sapi-probe-") as tmp:
            text_path = Path(tmp) / "probe.txt"
            wav_path = Path(tmp) / "probe.wav"
            text_path.write_text("OK", encoding="utf-8")
            run_powershell(
                SAPI_TTS_SCRIPT,
                args={"TextFile": str(text_path), "OutFile": str(wav_path), "Voice": chosen},
                timeout=PROBE_SYNTH_TIMEOUT,
            )
            if not wav_path.exists():
                return Probe(False, "SAPI 没有产出 wav 文件（探测合成失败）", {"voice": chosen})
            data = wav_path.read_bytes()
    except Exception as exc:  # noqa: BLE001 —— 探测失败就是不可用，原因原样带上
        return Probe(False, _first_error(exc), {"voice": chosen})
    if len(data) <= 44 or not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        return Probe(False, "SAPI 产出的不是合法 RIFF/WAVE（探测合成失败）", {"voice": chosen})
    return Probe(True, "", {"voice": chosen, "probe_wav_bytes": len(data)})


@functools.lru_cache(maxsize=1)
def _stt_probe() -> Probe:
    """STT 可用性：造一个识别引擎、喂一段静音 wav。**不要求识别出内容**。"""
    if not powershell_path():
        return Probe(False, "找不到 powershell（Windows PowerShell 5.1 / pwsh）")
    recognizers = _recognizers_cached()
    if not recognizers:
        return Probe(False, "没有任何已安装的 SAPI 识别器（识别语言包缺失）")
    culture = recognizers[0][1]
    try:
        with tempfile.TemporaryDirectory(prefix="orbit-sapi-probe-") as tmp:
            wav_path = Path(tmp) / "silence.wav"
            wav_path.write_bytes(silent_wav())
            result = run_powershell(
                SAPI_STT_PROBE_SCRIPT,
                args={"AudioFile": str(wav_path), "Culture": culture},
                timeout=PROBE_SYNTH_TIMEOUT,
            )
    except Exception as exc:  # noqa: BLE001
        return Probe(False, _first_error(exc), {"culture": culture})
    if "ENGINE_OK" not in decode(result.stdout):
        return Probe(False, "识别引擎自检没有回 ENGINE_OK", {"culture": culture})
    return Probe(True, "", {"culture": culture, "recognizer": recognizers[0][0]})


def reset_probe_cache() -> None:
    """清掉全部探测缓存（测试用；也留给"刚装完语音包想立刻看到"的场景）。"""
    _voices_cached.cache_clear()
    _recognizers_cached.cache_clear()
    _tts_probe.cache_clear()
    _stt_probe.cache_clear()
    _onecore_scan.cache_clear()
    _onecore_voices_cached.cache_clear()
    _onecore_probe.cache_clear()


def installed_voices() -> tuple[SapiVoice, ...]:
    """已安装音色（公开入口，缓存见 :func:`_voices_cached`）。"""
    return _voices_cached()


def installed_recognizers() -> tuple[tuple[str, str], ...]:
    """已安装识别器 `(名字, culture)`。"""
    return _recognizers_cached()


def tts_probe() -> Probe:
    """TTS 可用性实测结论（公开入口，缓存）。"""
    return _tts_probe()


def stt_probe() -> Probe:
    """STT 可用性实测结论（公开入口，缓存）。"""
    return _stt_probe()


def _pick_voice(voice: str | None) -> str:
    """选定音色名：优先调用方给的，其次 `ORBIT_SAPI_VOICE`，最后第一个可用音色。

    为什么要挑一个默认值：不选音色时 `Speak()` 用系统默认音色，而默认音色在中文机器上
    可能根本不存在（那会合成出空音频）。显式挑一个已安装的，才谈得上"能跑"。
    """
    wanted = (voice or os.environ.get("ORBIT_SAPI_VOICE", "") or "").strip()
    voices = [item for item in installed_voices() if item.enabled]
    if not wanted:
        return voices[0].name if voices else ""
    names = {item.name for item in voices}
    if wanted in names:
        return wanted
    raise _failure(
        f"没有这个 SAPI 音色：{wanted}。本机已装："
        + ("、".join(sorted(names)) or "（一个都没有）")
        + "。留空会用第一个可用音色。"
    )


# ── TTS：Windows SAPI ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class LocalTtsProvider:
    """Windows SAPI 合成器：这台机器上真正能离线跑通的第二家 TTS。

    `synthesize()` 返回 wav 字节（与 `app/voice.py::TtsProvider` 协议一致），
    `synthesize_to_file()` 直接落盘（工具层要路径，不想再写一遍文件）。
    """

    #: 名字用 `sapi` 而不是 `local`：`local` 在 STT 那边已经被 Hermes 的
    #: faster-whisper 占了（见 `app/voice.py::STT_PROVIDERS`），同名会让人以为是
    #: 同一个东西，而它们的依赖、语言、质量完全不同。
    name: str = "sapi"
    #: 默认音色（空 = 取第一个可用音色）。
    voice: str = ""

    def available(self) -> bool:
        """**实测一次最小合成**。见模块 docstring「为什么要实测一次最小合成」。"""
        return _tts_probe().ok

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """合成成 wav 字节。不可用时抛 `ProviderUnavailable`（不是返回空字节）。"""
        body = str(text or "").strip()
        if not body:
            raise _failure("文本是空的：没什么可合成的。")
        probe = _tts_probe()
        if not probe.ok:
            raise _unavailable(self._reason())
        chosen = _pick_voice(voice if voice is not None else self.voice)
        with tempfile.TemporaryDirectory(prefix="orbit-sapi-tts-") as tmp:
            text_path = Path(tmp) / "text.txt"
            wav_path = Path(tmp) / "out.wav"
            text_path.write_text(body, encoding="utf-8")
            run_powershell(
                SAPI_TTS_SCRIPT,
                args={"TextFile": str(text_path), "OutFile": str(wav_path), "Voice": chosen},
            )
            if not wav_path.exists() or wav_path.stat().st_size <= 44:
                raise _failure(
                    "SAPI 没有产出音频（文件不存在或只有 44 字节头）。"
                    f"音色 {chosen!r} 可能不可用；用 `orbit model --voice` 看已装音色。"
                )
            data = wav_path.read_bytes()
        if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
            raise _failure("SAPI 产出的不是 RIFF/WAVE（前 12 字节对不上），不能当音频用。")
        return data

    def synthesize_to_file(
        self, text: str, path: str | Path, voice: str | None = None
    ) -> Path:
        """合成并落到 `path`（父目录自动建）。返回**落盘后的真实路径**。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.synthesize(text, voice))
        return target

    def _reason(self) -> str:
        """**为什么不可用**（只说 why；三件套的拼装在 :meth:`describe` 里）。"""
        probe = _tts_probe()
        if not powershell_path():
            return "找不到 powershell（Windows PowerShell 5.1 / pwsh），调不到 System.Speech"
        if not [item for item in installed_voices() if item.enabled]:
            return "powershell 在，但没有任何启用的 SAPI 音色（语音包缺失）"
        return (
            f"powershell 在、{len(installed_voices())} 个音色也装着，但**探测合成失败**："
            f"{probe.reason}（常见原因：进程完整性级别太低 —— `whoami /groups` 里是 "
            "`Mandatory Label\\Low`，SAPI 的音色 token 不对 Low IL 开放）"
        )

    def _requires(self) -> str:
        if not powershell_path():
            return "powershell + System.Speech"
        if not [item for item in installed_voices() if item.enabled]:
            return "System.Speech + 至少一个已启用音色"
        return "powershell + System.Speech + 可用的音色 token（非 Low 完整性级别）"

    def _install(self) -> str:
        if not powershell_path():
            return "装回系统自带的 Windows PowerShell（**不需要联网**）"
        if not [item for item in installed_voices() if item.enabled]:
            return "设置 → 时间和语言 → 语音 → 添加语音（中文语音包）"
        return "从普通终端（非沙箱）运行，或改用 `tts_command` 指一条外部 TTS"

    def describe(self) -> str:
        """一句话说清"是什么 / 能不能用 / 不能用怎么办"（含已装音色清单）。"""
        if not self.available():
            return (
                f"sapi：不可用 —— reason: {self._reason()}；"
                f"requires: {self._requires()}；install: {self._install()}"
            )
        voices = [item for item in installed_voices() if item.enabled]
        names = "、".join(item.name for item in voices[:4]) or "（未列出）"
        probe = _tts_probe()
        return (
            f"sapi：可用 —— Windows SAPI 离线合成（{len(voices)} 个音色：{names}；"
            f"探测合成 {probe.detail.get('probe_wav_bytes') if probe.detail else '?'} 字节 wav）；"
            "requires: powershell + System.Speech；install: 系统自带，无需安装；"
            "reason: 不联网也能跑，中文音色取决于已装语音包。"
        )


# ── STT：Windows SAPI 听写 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class LocalSttProvider:
    """Windows SAPI 听写：**真能转写，但质量不足以做正经自由听写**。

    实测数字留在 docstring 里（本机 zh-CN 识别器 `MS-2052-80-DESK`）：拿 SAPI 自己
    合成的「你好，这是 Orbit 的语音测试。今天天气不错。」做听写，得到
    `你好这些个别的语音特许`，置信度 0.35、耗时 368ms。

    所以 `available()` 判的是"引擎能不能跑"（用静音 wav 自检，不要求听出内容），
    而 `quality="poor"` 与 `describe()` 把"质量差"写在明面上。**不建议**把它当默认
    STT：真正的自由听写请配 `stt_command`（faster-whisper / whisper.cpp），
    那条路径的准确率高一个数量级。
    """

    name: str = "sapi"
    #: 识别语言。空 = 用第一个已安装识别器的 culture。
    culture: str = ""
    #: 质量自评。`poor` 是实测结论，不是谦辞。
    quality: str = "poor"

    def _recognizer_culture(self) -> str:
        """选定 culture：显式给的优先，否则取第一个已安装识别器的。"""
        installed = installed_recognizers()
        wanted = (self.culture or "").strip()
        if wanted:
            cultures = {culture for _name, culture in installed}
            if wanted not in cultures:
                raise _unavailable(
                    f"没有 {wanted} 的 SAPI 识别器。本机已装："
                    + ("、".join(sorted(cultures)) or "（一个都没有）")
                    + "。install: 设置 → 时间和语言 → 语音 → 添加识别语言包。"
                )
            return wanted
        return installed[0][1] if installed else ""

    def available(self) -> bool:
        """**实测**造一次识别引擎（见 :func:`_stt_probe`）。"""
        return _stt_probe().ok

    def transcribe(self, audio: bytes) -> str:
        """把 wav 字节转成文本。不可用 / 不是 wav 时**抛错**，不返回空串。"""
        if not audio:
            raise _failure("音频是空的：没什么可转写的。")
        if not audio.startswith(b"RIFF"):
            raise _failure(
                "SAPI 只吃 RIFF/WAVE 的 PCM 音频（它用 SetInputToWaveFile）。"
                f"这段数据的前 4 字节是 {audio[:4]!r}，不是 RIFF。"
                "先转成 wav：`ffmpeg -i in.ogg -ar 16000 -ac 1 out.wav`。"
            )
        if not self.available():
            raise _unavailable(self._reason())
        culture = self._recognizer_culture()
        with tempfile.TemporaryDirectory(prefix="orbit-sapi-stt-") as tmp:
            wav_path = Path(tmp) / "in.wav"
            wav_path.write_bytes(audio)
            result = run_powershell(
                SAPI_STT_SCRIPT,
                args={"AudioFile": str(wav_path), "Culture": culture},
            )
        text = decode(result.stdout).strip()
        if not text:
            raise _failure(
                "识别器没有听出任何内容（音频是静音、太长、或与识别器语言不符）。"
                f"当前识别语言 {culture!r}；换 `stt_command`（faster-whisper）会稳得多。"
            )
        return text

    def _reason(self) -> str:
        probe = _stt_probe()
        if not powershell_path():
            return "找不到 powershell（Windows PowerShell 5.1 / pwsh），调不到 System.Speech.Recognition"
        if not installed_recognizers():
            return "powershell 在，但没有任何已安装的 SAPI 识别器（识别语言包缺失）"
        return (
            f"识别器装着（{installed_recognizers()[0][0]}），但**引擎自检失败**：{probe.reason}"
            "（与 TTS 同源：Low 完整性级别下 SAPI 语音 token 不可用）"
        )

    def _requires(self) -> str:
        if not powershell_path():
            return "powershell + System.Speech.Recognition"
        if not installed_recognizers():
            return "System.Speech.Recognition + 识别语言包"
        return "powershell + System.Speech.Recognition + 可用的识别器 token（非 Low 完整性级别）"

    def _install(self) -> str:
        if not powershell_path():
            return "装回系统自带的 Windows PowerShell（**不需要联网**）"
        if not installed_recognizers():
            return "设置 → 时间和语言 → 语音 → 添加识别语言"
        return "从普通终端（非沙箱）运行，或改用 `stt_command`（faster-whisper）"

    def describe(self) -> str:
        if not self.available():
            return (
                f"sapi：不可用 —— reason: {self._reason()}；"
                f"requires: {self._requires()}；install: {self._install()}"
            )
        recognizers = installed_recognizers()
        cultures = "、".join(sorted({culture for _name, culture in recognizers}))
        return (
            f"sapi：可用（质量 poor）—— Windows SAPI 离线听写，识别器 culture={cultures}。"
            "实测「你好，这是 Orbit 的语音测试。今天天气不错。」→"
            "「你好这些个别的语音特许」（置信度 0.35）——能跑，但**别拿它做正经听写**；"
            "requires: powershell + 识别语言包；install: 系统自带；"
            "reason: 想要准确率高一个数量级，配 `stt_command`（faster-whisper / whisper.cpp）。"
        )


# ── 第四家：Windows OneCore（WinRT）合成器 ──────────────────────────────────
#
# 为什么值得单列一家：`System.Speech`（SAPI5 桌面音色）与 WinRT 的
# `Windows.Media.SpeechSynthesis`（OneCore 音色）是**两套音色库、两套引擎**。
# 本机实测两边列出来的东西就不一样：
#
# * SAPI 桌面音色：`Microsoft Huihui Desktop`(zh-CN) / `Microsoft Zira Desktop`(en-US)
# * OneCore 音色：`Microsoft Huihui` / `Microsoft Yaoyao` / `Microsoft Kangkang`（都是 zh-CN）
#
# 也就是说 OneCore 这条路能拿到 **Yaoyao / Kangkang 两个男/女声**，而 SAPI 那条拿不到。
# 本机从高完整性级别的 PowerShell 实测合成「OK」→ **41,966 字节**合法 wav
# （`52 49 46 46 … 57 41 56 45`）。
#
# 但**它和 SAPI 受同一个限制**：从 venv python 的子进程（Low 完整性级别）里
# `New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer` 会报
# 「Internal Speech Error」（实测），所以 `available()` 同样必须实测、不能看"装没装"。
#
# ## WinRT 的异步在这里怎么处理
#
# `SynthesizeTextToStreamAsync` 返回的是 `IAsyncOperation<SpeechSynthesisStream>`，
# PowerShell 5.1 不能直接 `.Result`（那是 .NET 的 Task）。要先用反射拿到
# `WindowsRuntimeSystemExtensions.AsTask` 的泛型重载，再用 `MakeGenericMethod`
# 造一个 `AsTask<SpeechSynthesisStream>`，`Wait()` 之后取 `Result`。
# 这就是脚本里那个 `Await` 函数 —— 它不是风格问题，是这条路上**唯一**能等异步结果的办法。

#: 列 OneCore 音色。制表符分隔：`显示名 \t 语言`。
ONECORE_VOICES_SCRIPT = (
    _HEAD
    + """$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media, ContentType=WindowsRuntime]
foreach ($voice in [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices) {
  Write-Output ("{0}`t{1}" -f $voice.DisplayName, $voice.Language)
}
"""
)

#: 文本 → wav（OneCore 引擎）。文本从文件读，音色按 `DisplayName` 选。
ONECORE_TTS_SCRIPT = (
    """param([string]$TextFile, [string]$OutFile, [string]$Voice)
"""
    + _HEAD
    + """Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.DataReader, Windows.Storage, ContentType=WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($WinRtTask, $ResultType) {
  $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
  $netTask = $asTask.Invoke($null, @($WinRtTask))
  $null = $netTask.Wait(-1)
  $netTask.Result
}
$text = Get-Content -Raw -Encoding UTF8 -LiteralPath $TextFile
$synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
if ($Voice) {
  $pick = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices |
    Where-Object { $_.DisplayName -eq $Voice } | Select-Object -First 1
  if ($pick) { $synth.Voice = $pick }
}
$stream = Await ($synth.SynthesizeTextToStreamAsync($text)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
$size = [uint32]$stream.Size
$reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
$null = Await ($reader.LoadAsync($size)) ([uint32])
$buffer = New-Object 'byte[]' $size
$reader.ReadBytes($buffer)
[System.IO.File]::WriteAllBytes($OutFile, $buffer)
Write-Output "ONECORE_OK"
"""
)


@functools.lru_cache(maxsize=1)
def _onecore_scan() -> tuple[tuple[SapiVoice, ...], str]:
    """扫 OneCore 音色，**同时把失败原因带出来**。

    为什么不用"空元组 = 没有音色"这一种表达：本机实测在 Low 完整性级别的子进程里，
    连 `SpeechSynthesizer::AllVoices` 这一句都会报「Internal Speech Error」——
    如果把它和"语音包没装"混成同一个空元组，`describe()` 就会说错原因，
    用户会去装语音包，而真正的问题是进程被降权了。
    """
    if not powershell_path():
        return (), "找不到 powershell（Windows PowerShell 5.1 / pwsh）"
    try:
        result = run_powershell(ONECORE_VOICES_SCRIPT, timeout=PROBE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 —— 探测失败 = 没有音色，原因原样带上
        return (), _first_error(exc)
    voices = tuple(
        SapiVoice(name=pieces[0].strip(), culture=pieces[1].strip())
        for pieces in _parse_rows(decode(result.stdout), 2)
    )
    if not voices:
        return (), "脚本跑通了，但一个 OneCore 音色都没列出来（Windows 语音包缺失）"
    return voices, ""


@functools.lru_cache(maxsize=1)
def _onecore_voices_cached() -> tuple[SapiVoice, ...]:
    """OneCore 音色表（进程内只探一次）。复用 :class:`SapiVoice`：字段完全一样。"""
    return _onecore_scan()[0]


@functools.lru_cache(maxsize=1)
def _onecore_probe() -> Probe:
    """OneCore 可用性：**真的合成一次**「OK」（判据与 SAPI 完全一致）。"""
    if not powershell_path():
        return Probe(False, "找不到 powershell（Windows PowerShell 5.1 / pwsh）")
    voices = _onecore_voices_cached()
    if not voices:
        _found, scan_error = _onecore_scan()
        return Probe(False, f"列不出 OneCore 音色：{scan_error}")
    chosen = voices[0].name
    try:
        with tempfile.TemporaryDirectory(prefix="orbit-onecore-probe-") as tmp:
            text_path = Path(tmp) / "probe.txt"
            wav_path = Path(tmp) / "probe.wav"
            text_path.write_text("OK", encoding="utf-8")
            run_powershell(
                ONECORE_TTS_SCRIPT,
                args={"TextFile": str(text_path), "OutFile": str(wav_path), "Voice": chosen},
                timeout=PROBE_SYNTH_TIMEOUT,
            )
            if not wav_path.exists():
                return Probe(False, "OneCore 没有产出 wav 文件（探测合成失败）", {"voice": chosen})
            data = wav_path.read_bytes()
    except Exception as exc:  # noqa: BLE001 —— 探测失败就是不可用，原因原样带上
        return Probe(False, _first_error(exc), {"voice": chosen})
    if len(data) <= 44 or not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        return Probe(False, "OneCore 产出的不是合法 RIFF/WAVE（探测合成失败）", {"voice": chosen})
    return Probe(True, "", {"voice": chosen, "probe_wav_bytes": len(data)})


@dataclass(frozen=True)
class LocalOneCoreTts:
    """Windows OneCore（WinRT）合成器：与 SAPI 并列的**第四家**本地 TTS。

    与 `sapi` 的分工写在这里，因为它是这张清单里最容易被误读的一处：

    * 两者都走 Windows 自带的东西，都不需要联网，都不需要安装；
    * 但音色库不同（OneCore 多出 Yaoyao / Kangkang），引擎也不同 —— 一边坏掉时
      另一边**可能**还是好的，所以它们是两家，不是一家的两个名字；
    * 两者受同一个限制：Low 完整性级别的子进程里都拿不到语音 token。

    `quality` 不做自评：OneCore 的音色比 SAPI 桌面音色新（Narrator 用的就是它），
    但音质好坏是主观的，这里只报"能不能跑"。
    """

    name: str = "onecore"
    #: 默认音色（`DisplayName`）。空 = 取第一个 OneCore 音色。
    voice: str = ""

    def available(self) -> bool:
        """**实测一次最小合成**（同 `LocalTtsProvider` 的理由）。"""
        return _onecore_probe().ok

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        body = str(text or "").strip()
        if not body:
            raise _failure("文本是空的：没什么可合成的。")
        probe = _onecore_probe()
        if not probe.ok:
            raise _unavailable(self._reason())
        chosen = (voice or self.voice or "").strip() or _onecore_default_voice()
        with tempfile.TemporaryDirectory(prefix="orbit-onecore-") as tmp:
            text_path = Path(tmp) / "text.txt"
            wav_path = Path(tmp) / "out.wav"
            text_path.write_text(body, encoding="utf-8")
            run_powershell(
                ONECORE_TTS_SCRIPT,
                args={"TextFile": str(text_path), "OutFile": str(wav_path), "Voice": chosen},
            )
            if not wav_path.exists() or wav_path.stat().st_size <= 44:
                raise _failure(
                    "OneCore 没有产出音频（文件不存在或只有 44 字节头）。"
                    f"音色 {chosen!r} 可能不存在；`orbit model --voice` 会列出全部音色。"
                )
            data = wav_path.read_bytes()
        if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
            raise _failure("OneCore 产出的不是 RIFF/WAVE（前 12 字节对不上），不能当音频用。")
        return data

    def _reason(self) -> str:
        probe = _onecore_probe()
        if not powershell_path():
            return "找不到 powershell（Windows PowerShell 5.1 / pwsh），调不到 WinRT 合成器"
        voices, scan_error = _onecore_scan()
        if not voices:
            return (
                f"列不出任何 OneCore 音色：{scan_error}"
                "（本机实测：Low 完整性级别的子进程里连 `AllVoices` 都会报 "
                "`Internal Speech Error`，这与'语音包没装'是两件事）"
            )
        return (
            f"powershell 在、{len(voices)} 个 OneCore 音色也装着，"
            f"但**探测合成失败**：{probe.reason}（与 SAPI 同源：Low 完整性级别下"
            "语音 token 不可用，`whoami /groups` 里会看到 `Mandatory Label\\Low`）"
        )

    def describe(self) -> str:
        if not self.available():
            return (
                f"onecore：不可用 —— reason: {self._reason()}；"
                "requires: powershell + System.Runtime.WindowsRuntime + OneCore 语音包；"
                "install: 设置 → 时间和语言 → 语音 → 添加语音（或从普通终端运行，"
                "不要在被降权的沙箱里跑）"
            )
        voices = _onecore_voices_cached()
        names = "、".join(item.name for item in voices[:4]) or "（未列出）"
        probe = _onecore_probe()
        return (
            f"onecore：可用 —— Windows OneCore（WinRT）离线合成（{len(voices)} 个音色：{names}；"
            f"探测合成 {probe.detail.get('probe_wav_bytes') if probe.detail else '?'} 字节 wav）；"
            "requires: powershell + WinRT 合成器；install: 系统自带，无需安装；"
            "reason: 不联网也能跑，音色库与 `sapi` 那套**不同**（OneCore 多出 Yaoyao / Kangkang）。"
        )


def _onecore_default_voice() -> str:
    """第一个 OneCore 音色（取不到返回空串，由脚本回退到系统默认）。"""
    voices = _onecore_voices_cached()
    return voices[0].name if voices else ""


def onecore_voices() -> tuple[SapiVoice, ...]:
    """已安装的 OneCore 音色（公开入口，缓存）。"""
    return _onecore_voices_cached()


def onecore_probe() -> Probe:
    """OneCore 可用性实测结论（公开入口，缓存）。"""
    return _onecore_probe()


#: OneCore 合成器实例。与 `LOCAL_SAPI_TTS` 并列，**不是它的别名**（见类 docstring）。
LOCAL_ONECORE_TTS: LocalOneCoreTts = LocalOneCoreTts()


# ── 三家「本来就是本地」的 TTS：把恒 False 换成真检测 ───────────────────────


@dataclass(frozen=True)
class BinaryTts:
    """靠**本机已有二进制 / Python 包**工作的 TTS（piper / kittentts / neutts）。

    与 `app/voice.py::UnavailableTts` 的区别只有一条，但很关键：
    `available()` **真的去查**（`shutil.which` / `importlib.util.find_spec`），
    而不是恒 `False`。这样"没装"与"装了但没人检测"才区分得开。

    `command` 非空时 `synthesize()` 会真的把文本喂给那个可执行文件（占位符式，
    见 `app/voice.py::_run_command`）—— 装上就能用，不是占位实现。
    """

    name: str
    #: 可执行文件名（`shutil.which` 的目标）。空 = 这家是纯 Python 包。
    binary: str = ""
    #: 纯 Python 包的 import 名（`importlib.util.find_spec` 的目标）。
    module: str = ""
    #: 合成命令模板（含 `{text_file}` / `{audio_file}` / `{out_file}` 占位符）。
    command: str = ""
    #: 缺什么（给人看）。
    requires: str = ""
    #: 装好之后怎么做。
    hint: str = ""

    def missing(self) -> tuple[str, ...]:
        """缺的组件清单（空 = 齐了）。**这是 `available()` 的唯一依据**。"""
        gaps: list[str] = []
        if self.binary and not shutil.which(self.binary):
            gaps.append(f"可执行文件 `{self.binary}` 不在 PATH")
        if self.module and importlib.util.find_spec(self.module) is None:
            gaps.append(f"Python 包 `{self.module}` 未安装")
        if not self.binary and not self.module:
            gaps.append("没有声明依赖（这条登记本身有问题）")
        return tuple(gaps)

    def available(self) -> bool:
        return not self.missing()

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """真的跑那条命令；缺依赖时抛 `ProviderUnavailable`（带缺什么 + 怎么办）。"""
        if self.missing():
            raise _unavailable(self._reason())
        if not self.command.strip():
            raise _unavailable(
                f"{self.name}：二进制/包在，但没有合成命令模板（本层不猜它的 CLI 参数）。"
                "在 `<data>/voice.json` 的 `tts_command` 里写一条即可（例如 "
                f"`{self.name} -f {{audio_file}}`）。"
            )
        body = str(text or "").strip()
        if not body:
            raise _failure("文本是空的：没什么可合成的。")
        from app.voice import _run_command  # 延迟 import：复用它的占位符/超时语义

        result = _run_command(
            self.command,
            stdin_bytes=body.encode("utf-8"),
            text=body,
            prefer_stdout=False,
        )
        data = result.stdout or b""
        if not data:
            raise _failure(
                f"{self.name} 没有输出音频。占位符式命令要写 `{{out_file}}` 或 "
                "`{audio_file}`；管道式命令要把音频写到 stdout。"
            )
        return data

    def _reason(self) -> str:
        gaps = "；".join(self.missing()) or "未知原因"
        parts = [f"缺 {gaps}"]
        if self.requires:
            parts.append(f"需要 {self.requires}")
        parts.append("本机无网络（本层不发真实请求）")
        reason = "；".join(parts)
        return f"{reason}。{self.hint}" if self.hint else reason

    def describe(self) -> str:
        gaps = self.missing()
        if gaps:
            # 三个标签与 19 家云端那批保持一致（reason / requires / install）：
            # 同一张清单里两种排版会让人以为它们不是同一类东西。
            return (
                f"{self.name}：不可用 —— reason: {'；'.join(gaps)}"
                "（本机无网络，本层不发真实请求）；"
                f"requires: {self.requires or '（未声明）'}；"
                f"install: {self.hint or '（未声明）'}"
            )
        return (
            f"{self.name}：可用 —— 本机已装（{self.binary or self.module}）；"
            f"requires: {self.requires or '已满足'}；install: 已安装；"
            "reason: 纯本地，不需要联网。"
        )


#: 三家本地 TTS 的真实现。名字与 `app/voice.py::TTS_PROVIDERS` 里那三条**逐字一致**
#: （顺序也一致），因为它们替换的就是那三条 —— 换名字会让登记表与调研清单对不上。
LOCAL_TTS_PROVIDERS: tuple[BinaryTts, ...] = (
    BinaryTts(
        name="neutts",
        module="neutts",
        requires="本地权重 `neutts-air-q4-gguf` + `neutts` 包",
        hint="字符上限 2000；纯本地，装好权重即可离线跑。",
    ),
    BinaryTts(
        name="kittentts",
        module="kittentts",
        requires="本地 25MB 模型 + `kittentts` 包",
        hint="字符上限 2000；纯本地。",
    ),
    BinaryTts(
        name="piper",
        binary="piper",
        # 刻意**不猜** piper 的 argv：`--model` 的路径因机器而异，写死一个错的模板
        # 比没有模板更坏（它会以"退出码 2"的形式失败，看起来像 piper 坏了）。
        # 于是 `command` 留空，装上 piper 后由用户在 `<data>/voice.json` 里写一行。
        requires="piper 可执行文件 + 声音模型（.onnx）",
        hint="字符上限 5000；纯本地 VITS。装好后把模型路径写进 `<data>/voice.json` 的 "
        "`tts_command`（例如 `piper -m zh_CN-huayan-medium.onnx -f {audio_file}`）。",
    ),
)


def local_tts_provider(name: str) -> BinaryTts | None:
    """按名字取三家本地 TTS 之一（找不到返回 `None`，由调用方决定报什么错）。"""
    key = (name or "").strip().lower()
    for provider in LOCAL_TTS_PROVIDERS:
        if provider.name == key:
            return provider
    return None


#: SAPI 的两家。**单列**而不是塞进 `TTS_PROVIDERS` / `STT_PROVIDERS`：
#: 那两张表是 Hermes 的 11 家 / 8 家清单（测试逐字钉住），而 SAPI 是 Orbit 自己的
#: 本地通道 —— 混进去会让"对齐 Hermes"的账算不清。
LOCAL_SAPI_TTS: LocalTtsProvider = LocalTtsProvider()
LOCAL_SAPI_STT: LocalSttProvider = LocalSttProvider()


def local_providers() -> list[dict[str, Any]]:
    """本地通道的可用性清单（`orbit model --voice` 与测试共用）。

    四条：`sapi`（TTS）、`sapi`（STT，kind 不同），以及三家二进制 TTS 的真检测结果。
    为什么把三家二进制 TTS 也列在这里：它们的 `available()` 刚被改成"真检测"，
    这份清单就是"检测到底查出了什么"的可见面 —— 否则改了也没人看得见。
    """
    rows: list[dict[str, Any]] = [
        {
            "kind": "local",
            "name": LOCAL_SAPI_TTS.name,
            "available": LOCAL_SAPI_TTS.available(),
            "describe": LOCAL_SAPI_TTS.describe(),
        },
        {
            "kind": "local-stt",
            "name": LOCAL_SAPI_STT.name,
            "available": LOCAL_SAPI_STT.available(),
            "describe": LOCAL_SAPI_STT.describe(),
            "quality": LOCAL_SAPI_STT.quality,
        },
    ]
    for provider in LOCAL_TTS_PROVIDERS:
        rows.append(
            {
                "kind": "binary-tts",
                "name": provider.name,
                "available": provider.available(),
                "missing": list(provider.missing()),
                "describe": provider.describe(),
            }
        )
    return rows


__all__ = [
    "LOCAL_ONECORE_TTS",
    "LOCAL_SAPI_STT",
    "LOCAL_SAPI_TTS",
    "LOCAL_TTS_PROVIDERS",
    "ONECORE_TTS_SCRIPT",
    "ONECORE_VOICES_SCRIPT",
    "POWERSHELL_CANDIDATES",
    "SAPI_RECOGNIZERS_SCRIPT",
    "SAPI_STT_PROBE_SCRIPT",
    "SAPI_STT_SCRIPT",
    "SAPI_TTS_SCRIPT",
    "SAPI_VOICES_SCRIPT",
    "BinaryTts",
    "LocalOneCoreTts",
    "LocalSttProvider",
    "LocalTtsProvider",
    "Probe",
    "SapiVoice",
    "decode",
    "installed_recognizers",
    "installed_voices",
    "local_providers",
    "local_tts_provider",
    "onecore_probe",
    "onecore_voices",
    "powershell_path",
    "reset_probe_cache",
    "run_powershell",
    "silent_wav",
    "stt_probe",
    "tts_probe",
]

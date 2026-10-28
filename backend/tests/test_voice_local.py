"""MATRIX 4.12 的「本地那半」：SAPI 真合成 / 真识别 + 三家本地 TTS 的真检测。

这份文件的存在理由，是把上一轮的一个**偏保守结论**钉成可复算的事实：
"19 家里只有命令通道能离线跑"。本机实测的真相是：

* Windows SAPI **能**离线合成出合法 wav（`:class:`LocalTtsProvider`）；
* SAPI 的 `DictationGrammar` + `SetInputToWaveFile` **能**对 wav 做听写
  （`:class:`LocalSttProvider`，质量差，所以标 `quality="poor"`）；
* `piper` / `kittentts` / `neutts` 三家的 `available()` 从**恒 False** 换成了
  **真检测**（`shutil.which` / `find_spec`）。

## 一个必须如实写下来的环境事实

在本机的 **venv python 子进程**里，SAPI 一律报
「系统上未安装语音，或没有当前安全设置可用的语音」，尽管 `GetInstalledVoices()`
列得出 2 个音色。查 `whoami /groups` 才知道原因：这些子进程的完整性级别是
**`Mandatory Label\\Low`**（S-1-16-4096），而 SAPI 的音色 token 不对 Low IL 开放。
同一段脚本从高完整性级别的进程跑则完全正常（本机实测产出 121,484 与 220,252
字节的 wav）。

所以这份测试**不假设 SAPI 可用**：它断言的是"`available()` 是实测结论，不是猜的"——
可用时必须真的产出合法 wav，不可用时必须带**实测到的报错原文**。真合成那条路径
用 `pytest.skip` 明确标出来（跳过原因写清是 Low IL，而不是含糊的"环境不支持"）。
"""

from __future__ import annotations

import sys
import wave

import pytest

from app import voice as vc
from app import voice_local as vl

#: 本机有没有 powershell。没有就整条 SAPI 路径不可用（但"真检测"的逻辑照样要测）。
HAS_POWERSHELL = bool(vl.powershell_path())


def _py(code: str) -> str:
    """把一段 Python 代码包成一条外部命令（代码里**不许出现双引号**，同 test_voice.py）。"""
    return f'"{sys.executable}" -c "{code}"'


@pytest.fixture(autouse=True)
def _fresh_probe():
    """每条用例前后都清一次探测缓存：缓存是性能优化，不该让用例互相影响。"""
    vl.reset_probe_cache()
    yield
    vl.reset_probe_cache()


# ── 1. 探测本身：可用必须真能产出，不可用必须带原文 ──────────────────────────


def test_tts_availability_is_measured_not_guessed():
    """`available()` 必须是**实测**结论：可用 → 真产出 wav；不可用 → 带报错原文。

    这条用例是这一层的核心判据。它允许两种结果，但不允许第三种（"说不清"）：
    说可用却拿不出 wav，或说不可用却给不出原因，都是红。
    """
    probe = vl.tts_probe()
    assert vl.LOCAL_SAPI_TTS.available() is probe.ok, "available() 必须与实测结论一致"
    if not HAS_POWERSHELL:
        assert probe.ok is False
        assert "powershell" in probe.reason.lower()
        return
    if probe.ok:
        assert probe.detail is not None
        assert probe.detail["probe_wav_bytes"] > 44, "说可用就必须真合成出超过文件头的 wav"
        assert probe.detail["voice"], "要说清用的是哪个音色"
    else:
        assert probe.reason.strip(), "不可用时必须给出实测原因"
        # 本机（Low 完整性级别）报的是"没有当前安全设置可用的语音"；别的机器可能是
        # 语音包缺失或 powershell 不可执行。三者都算"说清了"。
        lowered = probe.reason.lower()
        assert (
            "安全设置" in probe.reason
            or "语音" in probe.reason
            or "powershell" in lowered
            or "stt" in lowered
        ), f"原因看不出是哪种不可用：{probe.reason}"


def test_stt_availability_is_measured_not_guessed():
    """STT 的判据同 TTS，但允许"引擎起来了但没听出内容"（静音自检的预期结果）。"""
    probe = vl.stt_probe()
    assert vl.LOCAL_SAPI_STT.available() is probe.ok
    if not HAS_POWERSHELL:
        assert probe.ok is False
        return
    if probe.ok:
        assert probe.detail is not None
        assert probe.detail["culture"], "说可用就要说清用哪个识别语言"
    else:
        assert probe.reason.strip()


def test_reset_probe_cache_forces_a_new_probe():
    """缓存必须可重置：否则"刚装完语音包仍显示不可用"会变成一个查不出来的问题。"""
    first = vl.tts_probe()
    vl.reset_probe_cache()
    second = vl.tts_probe()
    # 结论可以相同（同一台机器），但**必须是重新算出来的**（cache_info 的 miss 增加）。
    assert first.ok == second.ok
    if HAS_POWERSHELL:
        assert vl._tts_probe.cache_info().misses == 1


# ── 2. 真合成：有 SAPI 就必须产出合法 wav ───────────────────────────────────


@pytest.mark.skipif(
    not HAS_POWERSHELL, reason="本机没有 powershell，SAPI 路径不可用（见 test_voice_local 文件头）"
)
def test_sapi_synthesizes_a_real_wav_file(tmp_path):
    """真合成：字节是 RIFF/WAVE、`wave` 模块能解析、落盘文件与返回值一致。"""
    if not vl.LOCAL_SAPI_TTS.available():
        pytest.skip(
            "SAPI 探测失败（本机实测原因见下），跳过真合成："
            f"{vl.tts_probe().reason[:200]}"
        )
    data = vl.LOCAL_SAPI_TTS.synthesize("你好，世界。")
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    assert len(data) > 44

    # 用标准库 `wave` 再验一遍：能被解析、帧数 > 0 —— 只看文件头还不够。
    path = vl.LOCAL_SAPI_TTS.synthesize_to_file("你好，世界。", tmp_path / "out.wav")
    assert path.exists()
    with wave.open(str(path), "rb") as handle:
        assert handle.getnframes() > 0
        assert handle.getframerate() > 0
    assert path.stat().st_size == len(path.read_bytes())


@pytest.mark.skipif(not HAS_POWERSHELL, reason="本机没有 powershell")
def test_sapi_stt_round_trip_synthesize_then_transcribe():
    """真识别：把 SAPI 自己合成的 wav 再喂回去，必须拿到非空文本。

    这是"STT 不是纸面功能"的唯一硬证据。本机在 venv python 里会因完整性级别
    被跳过（原因写在 skip 里，不含糊）。
    """
    if not (vl.LOCAL_SAPI_TTS.available() and vl.LOCAL_SAPI_STT.available()):
        pytest.skip(
            "SAPI 探测失败，跳过合成→识别闭环："
            f"tts={vl.tts_probe().reason[:120]} / stt={vl.stt_probe().reason[:120]}"
        )
    audio = vl.LOCAL_SAPI_TTS.synthesize("你好，这是测试。")
    text = vl.LOCAL_SAPI_STT.transcribe(audio)
    assert text.strip(), "识别器必须给出非空文本（空串会被当成'识别成功但没内容'）"


# ── 3. 坏输入：必须当场拒绝并说下一步 ───────────────────────────────────────


def test_sapi_tts_refuses_empty_text():
    with pytest.raises(vc.VoiceError, match="文本是空的"):
        vl.LOCAL_SAPI_TTS.synthesize("   ")


def test_sapi_stt_refuses_empty_and_non_wav():
    with pytest.raises(vc.VoiceError, match="音频是空的"):
        vl.LOCAL_SAPI_STT.transcribe(b"")
    with pytest.raises(vc.VoiceError, match="RIFF"):
        vl.LOCAL_SAPI_STT.transcribe(b"OggS\x00\x02 not a wav")
    # 报错要给出下一步（转成 wav 的具体命令），而不是只说"格式不对"。
    with pytest.raises(vc.VoiceError) as excinfo:
        vl.LOCAL_SAPI_STT.transcribe(b"ID3 mp3")
    assert "ffmpeg" in str(excinfo.value)


def test_silent_wav_is_a_valid_wave_file():
    """STT 自检用的静音 wav 必须是**真的合法 wav**（否则自检判的是别的东西）。"""
    data = vl.silent_wav(0.1)
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    import io

    with wave.open(io.BytesIO(data), "rb") as handle:
        assert handle.getnframes() == 1600  # 0.1s × 16000Hz


# ── 4. 三家本地 TTS：available() 真的在检测 ────────────────────────────────


def test_binary_tts_detects_a_real_binary(monkeypatch):
    """`piper` 的 `available()` 必须跟着 `shutil.which` 走，不能恒 False。"""
    piper = vl.local_tts_provider("piper")
    assert piper is not None

    monkeypatch.setattr(vl.shutil, "which", lambda name: None)
    assert piper.available() is False
    assert any("不在 PATH" in gap for gap in piper.missing())
    text = piper.describe()
    assert "不可用" in text and "install:" in text and "requires:" in text

    monkeypatch.setattr(vl.shutil, "which", lambda name: rf"C:\fake\{name}.exe")
    assert piper.available() is True
    assert piper.missing() == ()
    assert "可用" in piper.describe()


def test_binary_tts_detects_a_real_module(monkeypatch):
    """`kittentts` / `neutts` 是纯 Python 包：检测走 `importlib.util.find_spec`。"""
    kittentts = vl.local_tts_provider("kittentts")
    neutts = vl.local_tts_provider("neutts")
    assert kittentts is not None and neutts is not None

    monkeypatch.setattr(vl.importlib.util, "find_spec", lambda name: None)
    assert kittentts.available() is False
    assert neutts.available() is False
    assert any("未安装" in gap for gap in kittentts.missing())

    monkeypatch.setattr(vl.importlib.util, "find_spec", lambda name: object())
    assert kittentts.available() is True
    assert neutts.available() is True


def test_binary_tts_runs_the_configured_command(monkeypatch):
    """装上之后 `synthesize()` **真的跑那条命令**（不是占位实现）。

    这里用 `python -c` 当"那个二进制"：它把固定字节打到 stdout，于是断言的是
    "文本真的喂进去了、输出真的取回来了"这条链路，而不是某个具体 TTS 的行为。
    """
    monkeypatch.setattr(vl.shutil, "which", lambda name: rf"C:\fake\{name}.exe")
    provider = vl.BinaryTts(
        name="piper",
        binary="piper",
        command=_py("import sys; sys.stdout.buffer.write(b'RIFFfakeWAVE')"),
    )
    assert provider.available() is True
    assert provider.synthesize("你好") == b"RIFFfakeWAVE"


def test_binary_tts_without_a_command_says_what_to_write(monkeypatch):
    """装上了但没配命令模板时：抛错并**指名**该在哪个文件写哪一行。"""
    monkeypatch.setattr(vl.shutil, "which", lambda name: rf"C:\fake\{name}.exe")
    provider = vl.BinaryTts(name="piper", binary="piper", command="")
    assert provider.available() is True
    with pytest.raises(vc.ProviderUnavailable) as excinfo:
        provider.synthesize("你好")
    assert "tts_command" in str(excinfo.value)


# ── 5. 清单：19 家 + 本地通道，每家都要有 install / requires / reason ────────


def test_every_provider_describes_install_requires_and_reason():
    """**19 家登记 + 本地通道**，每一家的 `describe()` 都要有这三样。

    这是"别让'登记了 19 家'读起来像'19 家都能用'"的实现点：不可用的必须写清
    `reason`（为什么不行）/ `requires`（缺什么）/ `install`（怎么办）。
    """
    rows = vc.describe_all(data_dir="data")
    assert len(rows) >= 21, "11 家 TTS + 8 家 STT + command + 两条 SAPI 本地通道"
    for row in rows:
        text = str(row["describe"])
        assert "requires:" in text, f"{row['name']} 的 describe 缺 requires"
        assert "install:" in text, f"{row['name']} 的 describe 缺 install"
        assert "reason:" in text, f"{row['name']} 的 describe 缺 reason"
        if not row["available"]:
            assert "不可用" in text, f"{row['name']} 不可用却没写'不可用'"


def test_local_providers_lists_sapi_twice_and_the_three_binaries():
    """本地清单：SAPI 的 TTS / STT 各一条（kind 不同），外加三家二进制 TTS。"""
    rows = vl.local_providers()
    kinds = [row["kind"] for row in rows]
    assert kinds.count("local") == 1 and kinds.count("local-stt") == 1
    assert [row["name"] for row in rows if row["kind"] == "binary-tts"] == [
        "neutts",
        "kittentts",
        "piper",
    ]
    # 每条都要有可判定字段，且不可用的要带缺什么（不是空列表）。
    for row in rows:
        assert isinstance(row["available"], bool)
        assert str(row["describe"]).strip()
        if not row["available"] and row["kind"] == "binary-tts":
            assert row["missing"], f"{row['name']} 不可用却没列出缺什么"


def test_voice_lookup_accepts_sapi_for_both_directions():
    assert vc.tts_provider("sapi").name == "sapi"
    assert vc.stt_provider("sapi").name == "sapi"
    # 别名也要能用（"本地 SAPI"是常见叫法）。
    assert vc.tts_provider("local_sapi").name == "sapi"

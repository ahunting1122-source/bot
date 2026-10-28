"""MATRIX 4.12 的「本地二进制 TTS / STT」：判据必须落到"真跑一次"。

这份文件守两条线，两条都是上一轮踩过的坑：

1. **`available()` 不许说谎**。piper 是"二进制 + 模型"两件套：只有二进制没有
   `.onnx` 时它一跑就报 "no model"，而 `shutil.which` 却说"可用"。所以这里断言的是
   三段阶梯（找二进制 → 找模型 → **实测一次最小合成**），而不是"装没装"。
2. **能跑的必须真跑**。本机真能跑的那一家是 `flite`（ffmpeg 的 libflite 滤镜）——
   它不装任何东西就能合成，所以这里断言的是**真的字节**：RIFF/WAVE 头、
   `wave` 模块能解析、帧数 > 0。piper 那条路径用"假 piper"（一个 .cmd 透传到
   Python 脚本）走完整链路，证明 `synthesize()` 不是占位实现。

本机实测结论（写在断言里，不是写在注释里）：
`flite` 英文合成 126,798 字节；中文会退化成接近静音，所以对非 ASCII 文本**当场拒绝**。
"""

from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

import pytest

from app import voice as vc
from app import voice_piper as vp


@pytest.fixture(autouse=True)
def _fresh_cache():
    """每条用例前后清一次探测缓存：缓存是性能优化，不该让用例互相影响。"""
    vp.reset_probe_cache()
    yield
    vp.reset_probe_cache()


# ── 1. 纯函数：命令行拼装（离线逐字断言）────────────────────────────────────


def test_piper_argv_is_exact():
    """piper 的参数：文本走 stdin，wav 走 `--output_file`（长选项，官方 README 的写法）。"""
    assert vp.piper_argv("piper", "m.onnx", "out.wav") == [
        "piper",
        "--model",
        "m.onnx",
        "--output_file",
        "out.wav",
    ]


def test_flite_argv_uses_a_text_file_and_relative_name():
    """flite 的文本必须走**文件**且文件名**相对**。

    两个理由都是实测出来的：`text=` 的值里出现逗号会被当成滤镜分隔符截断；
    Windows 绝对路径里的 `C:` 与 `\\` 同样会被滤镜语法吃掉。
    """
    argv = vp.flite_argv("ffmpeg", "text.txt", voice="kal")
    assert argv[:6] == ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi"]
    assert "flite=textfile=text.txt:voice=kal" in argv
    assert argv[-3:] == ["-f", "wav", "-"], "wav 打到 stdout，少一个临时文件"
    # 相对名：不许把绝对路径塞进滤镜参数
    joined = " ".join(argv)
    assert "C:" not in joined and ":\\" not in joined


def test_whisper_argv_writes_a_destination_file():
    """whisper.cpp 走 `-af whisper=...`，产物是 `destination` 里那份文本。"""
    argv = vp.whisper_argv("ffmpeg", "in.wav", model="ggml-base.bin", destination="out.txt")
    assert "whisper=model=ggml-base.bin:language=auto:destination=out.txt" in argv
    assert argv[-3:] == ["-f", "null", "-"], "这个 filter 是 A->A，输出流要丢掉"


def test_is_wave_rejects_a_header_only_file():
    """只有 44 字节头的 wav 是**合法头 + 0 帧**——那正是"命令跑了但没合成出东西"。"""
    header_only = b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + b"\x00" * 32
    assert len(header_only) == 44
    assert vp.is_wave(header_only) is False
    assert vp.is_wave(b"OggS\x00\x02") is False


# ── 2. piper：三段阶梯，缺哪一段都要说清 ────────────────────────────────────


def test_piper_reports_both_gaps_when_nothing_is_installed(monkeypatch):
    """本机实测：piper 不在 PATH、也没有 `.onnx` —— 两段都要报出来。"""
    monkeypatch.setattr(vp.shutil, "which", lambda name: None)
    monkeypatch.delenv(vp.PIPER_BIN_ENV, raising=False)
    monkeypatch.delenv(vp.PIPER_MODEL_ENV, raising=False)
    found = vp.discover_piper(data_dir=None)
    assert found.ok is False
    assert found.binary == "" and found.model == ""
    assert len(found.gaps) == 2
    assert any("不在 PATH" in gap for gap in found.gaps)
    assert any(".onnx" in gap for gap in found.gaps)
    assert found.searched, "必须能回答'你找哪了'"

    text = vp.PIPER_TTS.describe()
    assert "不可用" in text
    for label in ("reason:", "requires:", "install:"):
        assert label in text
    assert "piper-tts" in text, "install 要给真包名"
    assert "huggingface.co/rhasspy/piper-voices" in text, "install 要给模型下载地址"


def test_piper_binary_without_a_model_is_still_unavailable(monkeypatch, tmp_path):
    """**只有二进制没有模型 → 不可用**。这正是上一轮 `shutil.which` 说谎的那个场景。"""
    monkeypatch.setenv(vp.PIPER_BIN_ENV, str(tmp_path / "piper.exe"))
    (tmp_path / "piper.exe").write_bytes(b"fake")
    monkeypatch.setattr(vp, "find_piper_models", lambda *, data_dir=None: ())
    monkeypatch.setenv(vp.PIPER_MODEL_ENV, str(tmp_path / "missing.onnx"))
    found = vp.discover_piper(data_dir=None)
    assert found.binary.endswith("piper.exe")
    assert found.model == ""
    assert vp.PIPER_TTS.available() is False
    assert "没有任何 `.onnx`" in vp.PIPER_TTS.describe()


def _fake_piper(tmp_path: Path) -> Path:
    """造一个**真的能跑**的假 piper：.cmd 把 argv 透传给 Python，脚本写一段合法 wav。

    为什么用 `.cmd`：Windows 的 `CreateProcess` 会通过 cmd.exe 执行批处理，于是
    `subprocess.run([cmd, "--model", …])` 与真 piper 的调用方式完全一致 ——
    测的是"这条链路"，不是"某个 mock 被调用了"。
    """
    script = tmp_path / "fake_piper.py"
    script.write_text(
        "import io, pathlib, sys, wave\n"
        "args = sys.argv[1:]\n"
        "model = args[args.index('--model') + 1]\n"
        "out = args[args.index('--output_file') + 1]\n"
        "text = sys.stdin.buffer.read()\n"
        "buf = io.BytesIO()\n"
        "with wave.open(buf, 'wb') as handle:\n"
        "    handle.setnchannels(1)\n"
        "    handle.setsampwidth(2)\n"
        "    handle.setframerate(16000)\n"
        "    handle.writeframes(b'\\x00\\x00' * max(1, len(text)))\n"
        "pathlib.Path(out).write_bytes(buf.getvalue())\n"
        "print('used-model=' + model)\n",
        encoding="utf-8",
    )
    cmd = tmp_path / "piper.cmd"
    cmd.write_text(
        f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
    )
    return cmd


def test_piper_with_binary_and_model_really_synthesizes(monkeypatch, tmp_path):
    """二进制 + 模型都在时：`available()` 真跑一次探测，`synthesize()` 真出 wav 字节。"""
    cmd = _fake_piper(tmp_path)
    model = tmp_path / "zh_CN-huayan-medium.onnx"
    model.write_bytes(b"onnx-placeholder")
    monkeypatch.setenv(vp.PIPER_BIN_ENV, str(cmd))
    monkeypatch.setenv(vp.PIPER_MODEL_ENV, str(model))

    provider = vp.PiperTts()
    assert provider.discovery().ok is True
    assert provider.available() is True, "假 piper 能出合法 wav，所以必须判可用"
    probe = provider.probe()
    assert probe.ok and probe.detail is not None
    assert probe.detail["probe_wav_bytes"] > 44

    data = provider.synthesize("你好，世界。")
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    with wave.open(io.BytesIO(data), "rb") as handle:
        assert handle.getnframes() > 0
    assert "可用" in provider.describe()


def test_piper_refuses_empty_text_and_unavailable_calls(monkeypatch):
    with pytest.raises(vc.VoiceError, match="文本是空的"):
        vp.PIPER_TTS.synthesize("   ")
    monkeypatch.setattr(vp.shutil, "which", lambda name: None)
    monkeypatch.delenv(vp.PIPER_BIN_ENV, raising=False)
    with pytest.raises(vc.ProviderUnavailable) as excinfo:
        vp.PIPER_TTS.synthesize("hi")
    assert "piper" in str(excinfo.value)


# ── 3. flite：本机**真能跑**的那一家（断言真字节）────────────────────────────


def test_flite_probe_and_synthesis_produce_a_real_wav():
    """本机唯一零安装就能合成的 TTS：拿不到真 wav 就算失败（不是 skip）。"""
    if not vp.discover_ffmpeg():  # pragma: no cover - 本机 ffmpeg 在 PATH 上
        pytest.skip("本机没有 ffmpeg，flite 这条路径不可用")
    if "flite" not in vp.ffmpeg_filters(vp.discover_ffmpeg()):  # pragma: no cover
        pytest.skip("本机 ffmpeg 不带 --enable-libflite")
    assert vp.FLITE_TTS.available() is True
    probe = vp.FLITE_TTS.probe()
    assert probe.ok and probe.detail is not None
    assert probe.detail["probe_wav_bytes"] > 44

    data = vp.FLITE_TTS.synthesize("Hello, orbit: this is a test; 123.")
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    assert len(data) > 10_000, "一句英文必须产出成百上千帧，不是一段静音"
    with wave.open(io.BytesIO(data), "rb") as handle:
        assert handle.getnframes() > 0
        assert handle.getframerate() > 0


def test_flite_refuses_non_ascii_instead_of_returning_silence():
    """中文会合成出接近静音（本机实测 5,998 字节）—— 那比报错坏得多，所以当场拒绝。"""
    if not vp.FLITE_TTS.available():  # pragma: no cover
        pytest.skip("本机 flite 不可用")
    with pytest.raises(vc.VoiceError) as excinfo:
        vp.FLITE_TTS.synthesize("你好，世界。")
    message = str(excinfo.value)
    assert "英文" in message and "sapi" in message, "报错要给出中文的替代方案"
    with pytest.raises(vc.VoiceError, match="文本是空的"):
        vp.FLITE_TTS.synthesize("  ")


def test_flite_describe_admits_english_only():
    text = vp.FLITE_TTS.describe()
    assert "仅英文" in text or "英文" in text
    for label in ("reason:", "requires:", "install:"):
        assert label in text


def test_flite_without_the_filter_is_unavailable(monkeypatch):
    """ffmpeg 在、但没有 flite 滤镜 → 不可用，且说清"缺 --enable-libflite"。"""
    monkeypatch.setattr(vp, "discover_ffmpeg", lambda: r"C:\fake\ffmpeg.exe")
    monkeypatch.setattr(vp, "ffmpeg_filters", lambda exe: frozenset({"sine", "anoisesrc"}))
    assert vp.FLITE_TTS.available() is False
    text = vp.FLITE_TTS.describe()
    assert "没有 flite 滤镜" in text
    assert "--enable-libflite" in text


# ── 4. espeak / festival / say / pico2wave：本机全都没有，如实登记 ─────────────


@pytest.mark.parametrize("name", ["espeak", "espeak-ng", "festival", "say", "pico2wave"])
def test_extra_local_tts_are_registered_with_install_hints(name):
    """本机 `Get-Command` 实测全部 MISS —— 每一条都要有 reason/requires/install。"""
    provider = next(item for item in vp.EXTRA_LOCAL_TTS if item.name == name)
    assert provider.available() is False
    text = provider.describe()
    assert "不可用" in text
    assert "不在 PATH" in text
    for label in ("reason:", "requires:", "install:"):
        assert label in text
    assert len(provider.install) > 10, f"{name} 的 install 太短，用户照着做不了"


def test_extra_local_tts_report_a_missing_binary(monkeypatch):
    """`shutil.which` 找不到时必须是 `False`，且原因点名"不在 PATH"（本机真实状态）。"""
    espeak = next(item for item in vp.EXTRA_LOCAL_TTS if item.name == "espeak")
    monkeypatch.setattr(vp.shutil, "which", lambda name: None)
    assert espeak.available() is False
    assert "不在 PATH" in espeak.describe()


def _wave_bytes(frames: int = 64) -> bytes:
    import io as _io
    import wave as _wave

    buffer = _io.BytesIO()
    with _wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def test_commandline_tts_runs_a_real_command_and_checks_the_wav(monkeypatch):
    """二进制在之后 `available()` **再真跑一次**：出合法 wav 才算可用（不是 which 就算）。"""
    monkeypatch.setattr(vp.shutil, "which", lambda name: sys.executable)
    good = vp.CommandLineTts(
        name="fake-espeak-ok",
        binary="fake-espeak-ok",
        command=f'"{sys.executable}" -c "import sys;sys.stdout.buffer.write(bytes([82,73,70,70,36,0,0,0,87,65,86,69]))"',
    )
    # 头对了但 0 帧 → 不算可用（`is_wave` 要求长过 44 字节头）
    assert good.available() is False

    real = vp.CommandLineTts(
        name="fake-espeak-real",
        binary="fake-espeak-real",
        command=(
            f'"{sys.executable}" -c "import sys,io,wave;'
            "buf=io.BytesIO();"
            "h=wave.open(buf,'wb');h.setnchannels(1);h.setsampwidth(2);h.setframerate(16000);"
            "h.writeframes(b'\\\\x00\\\\x00'*64);h.close();"
            'sys.stdout.buffer.write(buf.getvalue())"'
        ),
    )
    assert real.available() is True
    assert "可用" in real.describe()


def test_commandline_tts_without_a_template_only_reaches_the_first_rung(monkeypatch):
    """没有命令模板时只到第一段阶梯（二进制在 = 可用），但 `synthesize()` 会拒绝并指路。"""
    provider = vp.CommandLineTts(name="fake-pico", binary="fake-pico", command="")
    monkeypatch.setattr(vp.shutil, "which", lambda name: rf"C:\fake\{name}.exe")
    assert provider.available() is True
    with pytest.raises(vc.ProviderUnavailable) as excinfo:
        provider.synthesize("hi")
    assert "tts_command" in str(excinfo.value)


def test_pico2wave_has_no_command_template_and_says_why(monkeypatch):
    """pico2wave 只从 argv 取文本，本层的文件/stdin 通道喂不进去 —— 不猜、不假装。"""
    pico = next(item for item in vp.EXTRA_LOCAL_TTS if item.name == "pico2wave")
    assert pico.command == ""
    monkeypatch.setattr(vp.shutil, "which", lambda name: rf"C:\fake\{name}.exe")
    assert pico.available() is True, "二进制在就算可用（只到第一段阶梯）"
    with pytest.raises(vc.ProviderUnavailable) as excinfo:
        pico.synthesize("hi")
    assert "tts_command" in str(excinfo.value)


# ── 5. whisper.cpp：filter 在、模型不在 → 如实 False ────────────────────────


def test_whisper_stt_reports_the_missing_model(monkeypatch):
    """本机实测：ffmpeg 带 `--enable-whisper`（filter 在），但全盘没有 ggml 模型。"""
    monkeypatch.delenv(vp.WHISPER_MODEL_ENV, raising=False)
    monkeypatch.setattr(vp, "find_whisper_models", lambda *, data_dir=None: ())
    assert vp.WHISPER_STT.available() is False
    text = vp.WHISPER_STT.describe()
    assert "不可用" in text
    assert "ggml" in text
    assert "huggingface.co/ggerganov/whisper.cpp" in text, "install 要给模型下载地址"
    with pytest.raises(vc.ProviderUnavailable):
        vp.WHISPER_STT.transcribe(b"RIFF0000WAVE")


def test_whisper_stt_rejects_non_wave_and_empty():
    with pytest.raises(vc.VoiceError, match="音频是空的"):
        vp.WHISPER_STT.transcribe(b"")
    with pytest.raises(vc.VoiceError, match="ffmpeg"):
        vp.WHISPER_STT.transcribe(b"OggS\x00\x02 not a wav")


def test_whisper_stt_becomes_available_when_the_model_is_there(monkeypatch, tmp_path):
    """模型在 + ffmpeg 带 whisper 滤镜 → 可用（判据是齐备性，不是试转一段）。"""
    model = tmp_path / "ggml-base.bin"
    model.write_bytes(b"ggml")
    monkeypatch.setenv(vp.WHISPER_MODEL_ENV, str(model))
    monkeypatch.setattr(vp, "discover_ffmpeg", lambda: r"C:\fake\ffmpeg.exe")
    monkeypatch.setattr(vp, "ffmpeg_filters", lambda exe: frozenset({"whisper"}))
    assert vp.WHISPER_STT.available() is True
    text = vp.WHISPER_STT.describe()
    assert "可用" in text
    assert "未在本机实测过转写" in text, "没验过的路径必须自陈没验过"


# ── 6. 清单与登记表接线 ─────────────────────────────────────────────────────


def test_local_tts_catalog_has_judgeable_fields():
    rows = vp.local_tts_catalog()
    names = [row["name"] for row in rows]
    assert names[0] == "flite", "flite 打头：它是本机唯一真可用的那一家"
    assert "whisper_cpp" in names
    assert len(rows) == 7  # flite + 5 家常见工具 + whisper
    for row in rows:
        assert isinstance(row["available"], bool)
        assert str(row["describe"]).strip()


def test_describe_all_includes_flite_and_whisper(tmp_path):
    """19 家清单之外，本地二进制通道也必须出现在同一张表里（flite 是本机真可用的）。"""
    rows = vc.describe_all(data_dir=tmp_path)
    by_name = {str(row["name"]): row for row in rows}
    assert by_name["flite"]["kind"] == "local-tts"
    assert by_name["whisper_cpp"]["kind"] == "local-stt-bin"
    assert "espeak" in by_name and "pico2wave" in by_name
    # 11 家 TTS / 8 家 STT 的账不能被搅乱（那是对齐 Hermes 的清单）
    assert len([row for row in rows if row["kind"] == "tts"]) == 11
    assert len([row for row in rows if row["kind"] == "stt"]) == 8


def test_voice_lookup_can_reach_the_binary_providers():
    assert vc.tts_provider("flite").name == "flite"
    assert vc.tts_provider("espeak").name == "espeak"
    assert vc.stt_provider("whisper_cpp").name == "whisper_cpp"
    assert vc.tts_provider("piper") is vp.PIPER_TTS


def test_faster_whisper_stt_is_detected_not_hardcoded(monkeypatch):
    """STT 的 `local` 从恒 False 换成真检测：包在就可用，不在就不可用。"""
    provider = next(item for item in vc.STT_PROVIDERS if item.name == "local")
    monkeypatch.setattr(vc.importlib.util, "find_spec", lambda name: None)
    assert provider.available() is False
    assert "未安装" in provider.describe()
    monkeypatch.setattr(vc.importlib.util, "find_spec", lambda name: object())
    assert provider.available() is True
    assert "可用" in provider.describe()


# ── 7. OneCore（WinRT）：与 SAPI 并列的第四家 ───────────────────────────────


def test_onecore_is_a_separate_provider_not_an_alias_of_sapi():
    """两套音色库 + 两套引擎 → 两家，不是一个东西的两个名字。"""
    from app import voice_local as vl

    assert vl.LOCAL_ONECORE_TTS.name == "onecore"
    assert vl.LOCAL_ONECORE_TTS.name != vl.LOCAL_SAPI_TTS.name
    assert vc.tts_provider("onecore").name == "onecore"
    assert vc.tts_provider("winrt").name == "onecore"


def test_onecore_availability_is_measured_not_guessed():
    """判据与 SAPI 完全一致：可用 → 真产出 wav；不可用 → 带**实测到的报错原文**。"""
    from app import voice_local as vl

    probe = vl.onecore_probe()
    assert vl.LOCAL_ONECORE_TTS.available() is probe.ok
    if probe.ok:
        assert probe.detail is not None
        assert probe.detail["probe_wav_bytes"] > 44
        data = vl.LOCAL_ONECORE_TTS.synthesize("你好")
        assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    else:
        assert probe.reason.strip(), "不可用时必须给出实测原因"
        # 本机（Low 完整性级别）报的是 Internal Speech Error；别的机器可能是
        # powershell 缺失或语音包缺失。三者都必须"说清了"。
        lowered = probe.reason.lower()
        assert (
            "internal speech error" in lowered
            or "powershell" in lowered
            or "音色" in probe.reason
            or "语音包" in probe.reason
        ), f"原因看不出是哪种不可用：{probe.reason}"


def test_onecore_describe_has_the_three_labels():
    from app import voice_local as vl

    text = vl.LOCAL_ONECORE_TTS.describe()
    for label in ("reason:", "requires:", "install:"):
        assert label in text
    if not vl.LOCAL_ONECORE_TTS.available():
        assert "不可用" in text


def test_onecore_script_uses_the_winrt_async_shim():
    """`SynthesizeTextToStreamAsync` 是 WinRT 异步：没有 `AsTask` 反射就拿不到结果。

    这条断言守的是"脚本里那段 `Await` 不是装饰"——去掉它，脚本会以
    `MethodNotFound` 失败（本机实测过）。
    """
    from app import voice_local as vl

    script = vl.ONECORE_TTS_SCRIPT
    assert script.startswith("param("), "param 必须是第一条语句"
    assert "System.Runtime.WindowsRuntime" in script
    assert "IAsyncOperation`1" in script
    assert "MakeGenericMethod" in script
    assert "SynthesizeTextToStreamAsync" in script


def test_onecore_refuses_empty_text():
    from app import voice_local as vl

    with pytest.raises(vc.VoiceError, match="文本是空的"):
        vl.LOCAL_ONECORE_TTS.synthesize("   ")


def test_describe_all_lists_onecore_next_to_sapi(tmp_path):
    rows = vc.describe_all(data_dir=tmp_path)
    local = [row for row in rows if row["kind"] == "local"]
    names = [str(row["name"]) for row in local]
    assert "sapi" in names and "onecore" in names

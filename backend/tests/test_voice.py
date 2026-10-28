"""TTS / STT（MATRIX 4.12）：19 家登记齐全、不可用的给原因、command 通道真能跑。

这台机器**没有网络**，所以 19 家里只有命令通道能被真正执行。这份文件的两条主线：

1. **不假装**：11 家 TTS + 8 家 STT 全部登记在册，不可用的每一家 `available()`
   都是 False，且 `describe()` / 异常消息里必须带**原因 + 安装提示**；
2. **能跑的必须真跑**：`CommandTts` / `CommandStt` 用 `python -c "..."` 当外部命令
   （占位符式与管道式两种写法各测一条），断言字节与文本真的流过去了。

第 2 条是这层的价值所在：它证明"离线也能验证"的通道不是纸面功能。
"""

from __future__ import annotations

import sys

import pytest

from app import voice as vc


def _py(code: str) -> str:
    """把一段 Python 代码包成一条外部命令（代码里**不许出现双引号**）。"""
    return f'"{sys.executable}" -c "{code}"'


# ── 登记表：11 + 8 ─────────────────────────────────────────────────────────

#: 调研 06 §6.4 的 11 个 TTS provider，顺序照抄。
TTS_NAMES = (
    "edge",
    "openai",
    "xai",
    "minimax",
    "mistral",
    "gemini",
    "elevenlabs",
    "neutts",
    "kittentts",
    "piper",
    "deepinfra",
)

#: 调研 06 §6.3 的 8 个 STT provider（顺序 = Hermes 的 auto-detect 优先级）。
STT_NAMES = ("local", "local_command", "groq", "openai", "mistral", "xai", "elevenlabs", "deepinfra")


def test_eleven_tts_providers_are_registered():
    assert tuple(provider.name for provider in vc.TTS_PROVIDERS) == TTS_NAMES
    assert len(vc.TTS_PROVIDERS) == 11


def test_eight_stt_providers_are_registered():
    assert tuple(provider.name for provider in vc.STT_PROVIDERS) == STT_NAMES
    assert len(vc.STT_PROVIDERS) == 8


def test_all_cloud_tts_providers_report_unavailable_with_a_reason():
    """11 家全部不可用，且每家都要说清"缺什么 / 装什么"。"""
    for provider in vc.TTS_PROVIDERS:
        assert provider.available() is False
        text = provider.describe()
        assert provider.name in text
        assert "不可用" in text
        assert "无网络" in text or "未安装" in text or "未下载" in text or "需要" in text


def test_only_the_command_stt_is_available_here():
    """8 家里只有 `local_command` 可能是活的——其余 7 家一律 False。"""
    unavailable = [provider for provider in vc.STT_PROVIDERS if provider.name != "local_command"]
    assert len(unavailable) == 7
    for provider in unavailable:
        assert provider.available() is False
        assert "不可用" in provider.describe()


def test_unavailable_providers_raise_a_specific_error():
    """不可用时**抛错**，不返回空字节 / 空字符串（那会让调用方以为"合成成功但没声音"）。"""
    with pytest.raises(vc.ProviderUnavailable) as excinfo:
        vc.TTS_PROVIDERS[0].synthesize("你好")
    assert "edge" in str(excinfo.value)

    stt = next(provider for provider in vc.STT_PROVIDERS if provider.name == "groq")
    with pytest.raises(vc.ProviderUnavailable) as excinfo:
        stt.transcribe(b"RIFF")
    assert "GROQ_API_KEY" in str(excinfo.value), "原因里要点出缺哪个凭证"


def test_provider_lookup_by_name():
    assert vc.tts_provider("piper").name == "piper"
    assert vc.tts_provider("command").name == "command"
    assert vc.stt_provider("local_command").name == "local_command"
    with pytest.raises(vc.VoiceError, match="未知 TTS provider"):
        vc.tts_provider("not-a-provider")
    with pytest.raises(vc.VoiceError, match="未知 STT provider"):
        vc.stt_provider("not-a-provider")


def test_describe_all_lists_nineteen_plus_the_command_channel(tmp_path):
    rows = vc.describe_all(data_dir=tmp_path)
    tts = [row for row in rows if row["kind"] == "tts"]
    stt = [row for row in rows if row["kind"] == "stt"]
    command = [row for row in rows if row["kind"] == "command"]
    assert len(tts) == 11
    assert len(stt) == 8
    assert len(command) == 1, "命令通道单列，不算第 20 家 provider"
    assert [row["name"] for row in tts] == list(TTS_NAMES)
    assert [row["name"] for row in stt] == list(STT_NAMES)
    # 没有配命令 → 命令通道也不可用（如实说"未配置"，不是"已就绪"）。
    assert command[0]["available"] is False
    assert "未配置" in str(command[0]["describe"])


# ── 配置 ────────────────────────────────────────────────────────────────────


def test_config_round_trip(tmp_path):
    config = vc.VoiceConfig(tts_command="piper -f {audio_file}", stt_command="whisper {audio_file}")
    path = vc.save_config(tmp_path, config)
    assert path.name == "voice.json"
    loaded = vc.VoiceConfig.load(tmp_path)
    assert loaded == config


def test_broken_config_is_treated_as_empty(tmp_path):
    (tmp_path / "voice.json").write_text("{ 这不是 json", encoding="utf-8")
    assert vc.VoiceConfig.load(tmp_path) == vc.VoiceConfig()


def test_missing_config_is_treated_as_empty(tmp_path):
    assert vc.VoiceConfig.load(tmp_path / "nope") == vc.VoiceConfig()


# ── CommandTts：管道式 ─────────────────────────────────────────────────────


def test_command_tts_runs_a_real_subprocess_and_returns_bytes():
    """唯一可离线验证的 TTS：外部命令往 stdout 写什么，就返回什么。"""
    config = vc.VoiceConfig(
        tts_command=_py("import sys; sys.stdout.buffer.write(bytes([82,73,70,70]))")
    )
    provider = vc.CommandTts(config)
    assert provider.available() is True
    assert provider.synthesize("你好") == b"RIFF"
    assert "可用" in provider.describe()


def test_command_tts_feeds_the_text_on_stdin():
    """管道式命令从 stdin 拿到文本——这是"任意 CLI 都能当后端"的关键。"""
    config = vc.VoiceConfig(
        tts_command=_py(
            "import sys; sys.stdout.buffer.write(b'LEN=' + str(len(sys.stdin.buffer.read())).encode())"
        )
    )
    assert vc.CommandTts(config).synthesize("你好") == b"LEN=6"


def test_command_tts_passes_the_voice_through_the_environment():
    config = vc.VoiceConfig(
        tts_command=_py(
            "import os, sys; sys.stdout.buffer.write(os.environ.get('ORBIT_TTS_VOICE', 'none').encode())"
        )
    )
    assert vc.CommandTts(config).synthesize("hi", voice="alloy") == b"alloy"


# ── CommandTts：占位符式 ───────────────────────────────────────────────────


def test_command_tts_supports_placeholder_commands(tmp_path):
    """`{text_file}` / `{out_file}` 这种写法要能跑：真 CLI（piper / whisper）都是这条路。"""
    script = tmp_path / "fake_tts.py"
    script.write_text(
        "import pathlib, sys\n"
        "text = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        "pathlib.Path(sys.argv[2]).write_bytes(b'AUDIO:' + text.encode('utf-8'))\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}" "{{text_file}}" "{{out_file}}"'
    provider = vc.CommandTts(vc.VoiceConfig(tts_command=command))
    assert provider.synthesize("你好") == b"AUDIO:" + "你好".encode()


def test_non_zero_exit_surfaces_stderr():
    """命令失败时把 stderr 尾部带出来：只报"失败"等于让人去猜。"""
    config = vc.VoiceConfig(tts_command=_py("import sys; sys.stderr.write('boom'); sys.exit(3)"))
    with pytest.raises(vc.VoiceError) as excinfo:
        vc.CommandTts(config).synthesize("hi")
    message = str(excinfo.value)
    assert "退出码 3" in message
    assert "boom" in message


def test_command_that_prints_nothing_is_an_error_not_silence():
    config = vc.VoiceConfig(tts_command=_py("pass"))
    with pytest.raises(vc.VoiceError, match="没有输出音频数据"):
        vc.CommandTts(config).synthesize("hi")


def test_missing_command_binary_is_explained():
    config = vc.VoiceConfig(tts_command="definitely-not-a-real-binary-xyz")
    with pytest.raises(vc.VoiceError, match="命令找不到"):
        vc.CommandTts(config).synthesize("hi")


def test_unconfigured_command_tts_says_how_to_configure_it():
    provider = vc.CommandTts(vc.VoiceConfig())
    assert provider.available() is False
    with pytest.raises(vc.CommandNotConfigured, match="voice.json"):
        provider.synthesize("hi")
    assert "未配置" in provider.describe()


# ── CommandStt：真跑 ───────────────────────────────────────────────────────


def test_command_stt_runs_a_real_subprocess_and_returns_text():
    config = vc.VoiceConfig(
        stt_command=_py("import sys; sys.stdout.write('T:' + sys.stdin.buffer.read().decode())")
    )
    provider = vc.CommandStt(config)
    assert provider.available() is True
    assert provider.transcribe(b"RIFFdata") == "T:RIFFdata"


def test_command_stt_rejects_empty_audio():
    provider = vc.CommandStt(vc.VoiceConfig(stt_command=_py("pass")))
    with pytest.raises(vc.VoiceError, match="音频是空的"):
        provider.transcribe(b"")


def test_command_stt_without_output_text_is_an_error():
    provider = vc.CommandStt(vc.VoiceConfig(stt_command=_py("pass")))
    with pytest.raises(vc.VoiceError, match="没有输出文本"):
        provider.transcribe(b"RIFF")


def test_command_stt_reads_the_audio_from_a_placeholder_file(tmp_path):
    script = tmp_path / "fake_stt.py"
    script.write_text(
        "import pathlib, sys\n"
        "data = pathlib.Path(sys.argv[1]).read_bytes()\n"
        "print('BYTES=' + str(len(data)))\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}" "{{audio_file}}"'
    provider = vc.CommandStt(vc.VoiceConfig(stt_command=command))
    assert provider.transcribe(b"RIFF" * 3) == "BYTES=12"


# ── 命令拆词 ────────────────────────────────────────────────────────────────


def test_split_command_keeps_windows_paths_and_strips_quotes():
    argv = vc.split_command(r'"C:\Program Files\Python\python.exe" -c "print(1)"')
    assert argv[0] == r"C:\Program Files\Python\python.exe"
    assert argv[1] == "-c"
    assert argv[2] == "print(1)"


def test_split_command_never_goes_through_a_shell():
    """`voice.json` 是普通配置文件，别让它变成任意代码执行入口。"""
    argv = vc.split_command("echo hi; rm -rf /")
    assert ";" in argv[1], "分号留在参数里，不会被当成命令分隔符执行"


# ── CLI：`orbit model --voice …` ──────────────────────────────────────────


def test_cli_voice_lists_all_providers(tmp_path, capsys):
    from app.cli.commands import model_extra2  # noqa: F401 —— 导入即接线
    from app.cli.main import main

    assert main(["--data-dir", str(tmp_path / "data"), "model", "--voice"]) == 0
    out = capsys.readouterr().out
    for name in TTS_NAMES:
        assert name in out
    for name in STT_NAMES:
        assert name in out
    assert "无网络" in out


def test_cli_voice_set_then_synthesize_round_trip(tmp_path, capsys):
    """配好命令后 `--voice-say` 真的能产出音频文件——端到端证明这条路是通的。"""
    from app.cli.commands import model_extra2  # noqa: F401
    from app.cli.main import main

    data_dir = str(tmp_path / "data")
    command = _py("import sys; sys.stdout.buffer.write(bytes([1,2,3]))")
    assert main(["--data-dir", data_dir, "model", "--voice-set", f"tts_command={command}"]) == 0
    capsys.readouterr()

    assert main(["--data-dir", data_dir, "model", "--voice-say", "你好"]) == 0
    out = capsys.readouterr().out
    assert "已合成 3 字节" in out
    produced = list((tmp_path / "data" / "voice").glob("tts-*.wav"))
    assert produced and produced[0].read_bytes() == bytes([1, 2, 3])


def test_cli_voice_say_without_config_fails_with_a_hint(tmp_path, capsys):
    from app.cli.commands import model_extra2  # noqa: F401
    from app.cli.main import main

    code = main(["--data-dir", str(tmp_path / "data"), "model", "--voice-say", "你好"])
    assert code == 1
    err = capsys.readouterr().err
    assert "tts_command" in err

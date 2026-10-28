"""`app/skin.py`（皮肤 / 颜色 token）的契约测试。

三件事必须被锁住：

1. **token 是固定集合**：每个内置皮肤都要覆盖全部七个 token，值是 `#rrggbb`。
   少一个 token 的皮肤会在渲染端炸 `KeyError`，而"炸"发生在用户切过去的那一刻。
2. **只写 `skin.json`**：`apply` / `set_color` 的落点是数据目录下的一个文件，
   不碰 `settings.json` / `config.py`。测试读回文件内容确认这一点。
3. **无色降级**：`NO_COLOR` 存在或 `--no-color` 时，所有 token 都是**空串**。
   这是无障碍与管道友好的硬要求，不是"顺手支持一下"。
"""

from __future__ import annotations

import json

import pytest

import app.cli.commands.ui  # noqa: F401 —— 导入即接线（serve/console/skin 的 handler 登记）
from app.cli.main import main
from app.config import Settings
from app.skin import (
    DEFAULT_SKIN,
    HEX_COLOR,
    SKINS,
    TOKENS,
    active_tokens,
    apply,
    current,
    describe,
    list_skins,
    no_color_requested,
    reset,
    resolve,
    set_color,
    skin_path,
    tokens,
)


@pytest.fixture(autouse=True)
def _clear_no_color(monkeypatch):
    """清掉环境里的 `NO_COLOR`。

    为什么必须清：本机（CI / 沙箱）常常全局设了 `NO_COLOR=1`，那样每个 token
    都降级成空串，所有颜色断言会莫名其妙全空。真正要测降级的用例显式
    `setenv` 或直接传 ``environ``，不依赖环境里的残留值。
    """
    monkeypatch.delenv("NO_COLOR", raising=False)


@pytest.fixture()
def settings(tmp_path):
    value = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    value.prepare()
    return value


# ── 内置皮肤 ────────────────────────────────────────────────────────────────


def test_has_at_least_three_builtin_skins():
    assert len(SKINS) >= 3, f"只有 {len(SKINS)} 个内置皮肤"
    assert {"default", "mono", "solarized"} <= set(SKINS)
    assert list_skins()[0] == DEFAULT_SKIN


@pytest.mark.parametrize("name", list(SKINS))
def test_every_skin_covers_every_token(name):
    skin = SKINS[name]
    assert skin.description.strip(), f"{name} 没有说明"
    for token in TOKENS:
        value = skin.colors.get(token)
        assert value is not None, f"{name} 缺 token {token}"
        assert HEX_COLOR.match(value), f"{name}.{token} 不是 #rrggbb：{value!r}"


def test_token_set_is_the_documented_one():
    assert TOKENS == ("fg", "bg", "accent", "muted", "error", "success", "warn")


def test_resolve_returns_skin_and_rejects_unknown():
    assert resolve("MONO").name == "mono", "皮肤名应大小写不敏感"
    with pytest.raises(ValueError) as excinfo:
        resolve("nope")
    message = str(excinfo.value)
    assert "default" in message and "solarized" in message, "报错要列出可用皮肤"


# ── 无色降级 ────────────────────────────────────────────────────────────────


def test_tokens_return_real_colors_without_no_color():
    colors = tokens("solarized", environ={})
    assert colors["bg"] == SKINS["solarized"].colors["bg"]
    assert all(value for value in colors.values())


def test_no_color_env_degrades_every_token_to_empty_string():
    """`NO_COLOR` **存在**即降级（按 no-color.org：不看值）。"""
    for environ in ({"NO_COLOR": ""}, {"NO_COLOR": "1"}, {"NO_COLOR": "0"}):
        colors = tokens("default", environ=environ)
        assert set(colors) == set(TOKENS)
        assert all(value == "" for value in colors.values()), f"{environ} 没触发降级"


def test_no_color_flag_degrades_every_token():
    colors = tokens("default", flag=True, environ={})
    assert all(value == "" for value in colors.values())


def test_no_color_requested_sources():
    assert no_color_requested(flag=True, environ={}) is True
    assert no_color_requested(flag=False, environ={"NO_COLOR": "1"}) is True
    assert no_color_requested(flag=False, environ={}) is False


# ── 落盘：apply / set / reset ───────────────────────────────────────────────


def test_apply_writes_skin_json_only(settings):
    path = apply("mono", settings)
    assert path == skin_path(settings)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["skin"] == "mono"
    assert payload["version"] == 2
    assert payload["overrides"] == {}
    assert payload["updated_at"]
    assert current(settings) == "mono"
    # 只写 skin.json：settings.json 不该被创建/改动。
    assert not (settings.data_dir / "settings.json").exists()


def test_apply_rejects_unknown_skin(settings):
    with pytest.raises(ValueError):
        apply("neon", settings)
    assert not skin_path(settings).exists()


def test_set_color_overrides_and_survives_reload(settings):
    apply("default", settings)
    set_color(settings, "accent", "#00FFAA")
    colors = active_tokens(settings)
    assert colors["accent"] == "#00ffaa"
    assert colors["bg"] == SKINS["default"].colors["bg"], "只改一个 token，其他不动"
    pairs = dict(describe(settings))
    assert pairs["accent"] == "#00ffaa"


def test_set_color_validates_token_and_hex(settings):
    apply("default", settings)
    with pytest.raises(ValueError):
        set_color(settings, "shadow", "#00FFAA")
    with pytest.raises(ValueError):
        set_color(settings, "accent", "red")
    with pytest.raises(ValueError):
        set_color(settings, "accent", "#fff")


def test_set_color_on_other_skin_keeps_active_untouched(settings):
    """`--skin X` 只改 X 的配色，**不切换**当前皮肤（否则像"改错东西了"）。"""
    apply("default", settings)
    set_color(settings, "accent", "#123456", skin="mono")
    assert current(settings) == "default"
    assert active_tokens(settings)["accent"] == SKINS["default"].colors["accent"]
    assert dict(describe(settings, "mono"))["accent"] == "#123456"


def test_apply_drops_previous_overrides(settings):
    apply("default", settings)
    set_color(settings, "accent", "#00FFAA")
    apply("solarized", settings)
    assert active_tokens(settings)["accent"] == SKINS["solarized"].colors["accent"]


def test_reset_removes_file_and_falls_back(settings):
    apply("mono", settings)
    assert reset(settings) is True
    assert not skin_path(settings).exists()
    assert current(settings) == DEFAULT_SKIN
    assert reset(settings) is False, "没有文件时 reset 应返回 False 而不是报错"


def test_broken_skin_json_degrades_to_default(settings):
    """坏数据降级，不是报错——一个手改坏的 skin.json 不该让 CLI 起不来。"""
    skin_path(settings).write_text("{不是 json", encoding="utf-8")
    assert current(settings) == DEFAULT_SKIN
    assert active_tokens(settings)["fg"] == SKINS[DEFAULT_SKIN].colors["fg"]


def test_unknown_skin_name_in_file_falls_back(settings):
    skin_path(settings).write_text(json.dumps({"skin": "neon"}), encoding="utf-8")
    assert current(settings) == DEFAULT_SKIN


def test_no_color_applies_to_active_tokens(settings, monkeypatch):
    apply("solarized", settings)
    monkeypatch.setenv("NO_COLOR", "1")
    assert all(value == "" for value in active_tokens(settings).values())


# ── CLI ────────────────────────────────────────────────────────────────────


def test_cli_skin_list_shows_three_skins(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "skin", "list"])
    assert code == 0
    out = capsys.readouterr().out
    for name in ("default", "mono", "solarized"):
        assert name in out


def test_cli_skin_use_then_show(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "skin", "use", "mono"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "skin", "show"]) == 0
    out = capsys.readouterr().out
    assert "mono" in out
    assert SKINS["mono"].colors["fg"] in out


def test_cli_skin_set_writes_override(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "skin", "use", "default"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "skin", "set", "accent", "#00FFAA"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "skin", "show"]) == 0
    assert "#00ffaa" in capsys.readouterr().out


def test_cli_skin_set_rejects_bad_hex(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "skin", "set", "accent", "红色"])
    assert code == 2
    assert "rrggbb" in capsys.readouterr().err


def test_cli_skin_no_color_flag_degrades(tmp_path, capsys):
    """`--no-color` 是全局 flag：皮肤展示同样跟着降级。"""
    code = main(["--data-dir", str(tmp_path / "data"), "--no-color", "skin", "show", "default"])
    assert code == 0
    out = capsys.readouterr().out
    assert SKINS["default"].colors["bg"] not in out
    assert "token" in out


def test_cli_skin_reset(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "skin", "use", "solarized"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "skin", "reset"]) == 0
    assert "默认皮肤" in capsys.readouterr().out
    assert not (tmp_path / "data" / "skin.json").exists()


def test_cli_skin_json_is_machine_readable(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "--json", "skin", "list"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["active"] == DEFAULT_SKIN
    assert {skin["name"] for skin in payload["skins"]} >= {"default", "mono", "solarized"}

"""密钥源与解析器的契约测试。

**绝不碰真实的系统 keyring**：那是用户机器的真实状态，测试往里面写东西属于副作用。
所有涉及 keyring 的用例都注入一个内存后端（`KeyringSource(backend=...)`），
这也正是 `KeyringSource` 留 `backend` 注入点的原因。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.secrets import (
    FILE_WARNING,
    BitwardenSource,
    EnvSource,
    FileSource,
    KeyringSource,
    OnePasswordSource,
    SecretResolver,
    WritableSecretSource,
    service_name,
)


class FakeKeyring:
    """内存版 keyring 后端。接口只需三个方法（get / set / delete_password）。"""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.store.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self.store[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        if (service, key) not in self.store:
            raise KeyError(key)
        del self.store[(service, key)]


class BrokenKeyring:
    """后端不可用（无桌面会话的典型表现）：每次调用都抛。"""

    def get_password(self, service: str, key: str) -> str | None:
        raise RuntimeError("no backend")

    def set_password(self, service: str, key: str, value: str) -> None:
        raise RuntimeError("no backend")

    def delete_password(self, service: str, key: str) -> None:
        raise RuntimeError("no backend")


def _resolver(tmp_path: Path, *, backend: object | None = None, environ: dict | None = None):
    return SecretResolver(
        [
            KeyringSource(tmp_path, backend=backend if backend is not None else FakeKeyring()),
            EnvSource(environ=environ or {}),
            FileSource(tmp_path),
            BitwardenSource(which=lambda name: None),
            OnePasswordSource(which=lambda name: None),
        ]
    )


def test_keyring_service_matches_the_repo_convention(tmp_path):
    """service 必须是 `orbit:<绝对 data_dir>`——换写法会让已有密钥全部失联。"""
    assert service_name(tmp_path) == f"orbit:{tmp_path.resolve()}"
    assert KeyringSource(tmp_path).service == f"orbit:{tmp_path.resolve()}"


def test_priority_keyring_beats_env_beats_file(tmp_path):
    """四源优先级：同名冲突时高优先级胜，其余记进 `shadowed`。"""
    backend = FakeKeyring()
    resolver = _resolver(tmp_path, backend=backend, environ={"ORBIT_MODEL_API_KEY": "from-env"})
    resolver.set("model_api_key", "from-keyring")
    FileSource(tmp_path).set("model_api_key", "from-file")

    assert resolver.resolve("model_api_key") == "from-keyring"
    assert resolver.origin["model_api_key"] == "keyring"
    # 被遮蔽的源要能说出来：否则"我改了 .env 为什么不生效"永远查不出来。
    assert resolver.shadowed["model_api_key"] == ["env", "file"]


def test_env_wins_when_keyring_is_unavailable(tmp_path):
    backend = BrokenKeyring()
    resolver = _resolver(tmp_path, backend=backend, environ={"ORBIT_GITHUB_TOKEN": "from-env"})
    FileSource(tmp_path).set("github_token", "from-file")

    assert resolver.resolve("github_token") == "from-env"
    assert resolver.origin["github_token"] == "env"
    assert resolver.shadowed["github_token"] == ["file"]


def test_file_wins_when_keyring_and_env_have_nothing(tmp_path):
    resolver = _resolver(tmp_path, environ={})
    FileSource(tmp_path).set("db_password", "from-file")
    assert resolver.resolve("db_password") == "from-file"
    assert resolver.origin["db_password"] == "file"
    assert "db_password" not in resolver.shadowed


def test_missing_key_returns_none_and_records_nothing(tmp_path):
    resolver = _resolver(tmp_path)
    assert resolver.resolve("nope") is None
    assert "nope" not in resolver.origin
    assert "nope" not in resolver.shadowed


def test_external_sources_are_last_and_never_win(tmp_path):
    """外部源排在最后；未安装时 `available()=False`，安装后取值抛 NotImplementedError。

    两种情况都不该影响本地源的取值——`_ask` 把"未实现"当"没有"处理。
    """
    resolver = _resolver(tmp_path, environ={"ORBIT_MODEL_API_KEY": "from-env"})
    assert resolver.sources[-2].name == "bitwarden"
    assert resolver.sources[-1].name == "onepassword"
    assert resolver.resolve("model_api_key") == "from-env"
    # 只有 env 有这把键 → 没有"被遮蔽"的源，`shadowed` 里就不该出现这个键。
    assert "model_api_key" not in resolver.shadowed


def test_file_source_describe_warns_about_plaintext(tmp_path):
    """明文必须说出来，并且文件里也要写——拿到文件的人未必读过文档。"""
    source = FileSource(tmp_path)
    source.set("model_api_key", "sk-secret-value")
    description = source.describe()
    assert "明文" in description
    assert "未加密" in description

    payload = json.loads(source.path.read_text(encoding="utf-8"))
    assert payload["_warning"] == FILE_WARNING
    assert "未加密" in payload["_warning"]
    # 它确实是明文（不假装加密）。
    assert payload["model_api_key"] == "sk-secret-value"


def test_file_source_metadata_keys_are_not_treated_as_secrets(tmp_path):
    source = FileSource(tmp_path)
    source.set("a_key", "1")
    assert source.list_keys() == ["a_key"]


def test_external_sources_report_install_hints_when_missing():
    bitwarden = BitwardenSource(which=lambda name: None)
    onepassword = OnePasswordSource(which=lambda name: None)
    for source in (bitwarden, onepassword):
        assert source.available() is False
        description = source.describe()
        assert "未安装" in description
        assert source.install_hint.strip() in description
        assert source.status()["available"] is False
    assert "npm" in bitwarden.describe()
    assert "1password.com" in onepassword.describe()


def test_external_sources_report_status_only_when_installed():
    """装了就只报状态；取值路径必须抛错并说清原因，不许返回空串假装"没配"。"""
    source = OnePasswordSource(which=lambda name: "/usr/local/bin/op" if name == "op" else None)
    assert source.available() is True
    status = source.status()
    assert status["binary"] == "/usr/local/bin/op"
    assert "未实现" in status["read_path"]
    assert "未实现" in source.describe()
    with pytest.raises(NotImplementedError, match="联网"):
        source.get("OPENAI_API_KEY")
    with pytest.raises(NotImplementedError):
        source.list_keys()


def test_env_source_is_read_only():
    source = EnvSource(environ={"ORBIT_MODEL_API_KEY": "x"})
    assert not isinstance(source, WritableSecretSource)
    assert source.get("model_api_key") == "x"
    # 裸大写名也认（各家 SDK 的约定），否则会误报"未配置"。
    assert EnvSource(environ={"OPENAI_API_KEY": "y"}).get("openai_api_key") == "y"


def test_env_source_lists_only_secretish_names():
    source = EnvSource(environ={"ORBIT_MODEL_API_KEY": "a", "ORBIT_DATA_DIR": "b", "HOME": "c"})
    assert source.list_keys() == ["model_api_key"]


def test_set_writes_to_the_highest_priority_writable_source(tmp_path):
    backend = FakeKeyring()
    resolver = _resolver(tmp_path, backend=backend)
    assert resolver.set("model_api_key", "sk-1") == "keyring"
    assert backend.store[(service_name(tmp_path), "model_api_key")] == "sk-1"
    assert not (tmp_path / "secrets.json").exists()


def test_set_falls_back_to_file_when_keyring_is_dead(tmp_path):
    """keyring 不可用时不静默丢失：退回明文文件，并且是**可被发现**的退回。"""
    resolver = _resolver(tmp_path, backend=BrokenKeyring())
    assert resolver.set("model_api_key", "sk-1") == "file"
    assert (tmp_path / "secrets.json").exists()


def test_keyring_index_holds_names_only(tmp_path):
    """keyring 没有可移植的枚举接口，索引文件只存键名——**绝不能存值**。"""
    backend = FakeKeyring()
    resolver = _resolver(tmp_path, backend=backend)
    resolver.set("model_api_key", "sk-super-secret-value")
    index_text = (tmp_path / "secrets.index.json").read_text(encoding="utf-8")
    assert "model_api_key" in index_text
    assert "sk-super-secret-value" not in index_text
    assert resolver.keys()["keyring"] == ["model_api_key"]


def test_remove_clears_every_writable_source(tmp_path):
    backend = FakeKeyring()
    resolver = _resolver(tmp_path, backend=backend)
    resolver.set("model_api_key", "sk-1")
    FileSource(tmp_path).set("model_api_key", "sk-2")
    removed = resolver.remove("model_api_key")
    assert set(removed) == {"keyring", "file"}
    assert resolver.resolve("model_api_key") is None
    assert resolver.keys()["file"] == []


def test_status_never_carries_values(tmp_path):
    """`status()` 是 `orbit secrets status` 的数据源，里面绝不能有值。"""
    backend = FakeKeyring()
    resolver = _resolver(tmp_path, backend=backend, environ={"ORBIT_GITHUB_TOKEN": "ghp_x"})
    resolver.set("model_api_key", "sk-super-secret-value")
    dumped = json.dumps(resolver.status(), ensure_ascii=False)
    assert "sk-super-secret-value" not in dumped
    assert "ghp_x" not in dumped
    assert "model_api_key" in dumped


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_secrets_status_prints_no_secret_values(tmp_path, capsys, monkeypatch):
    """`orbit secrets status` 的输出里**不含任何密钥值**（只有键名与可用性）。"""
    from app.cli.commands import security as security_cli
    from app.cli.main import main

    secret = "sk-cli-secret-value-1234567890"
    backend = FakeKeyring()
    resolver = _resolver(tmp_path / "data", backend=backend)
    resolver.set("model_api_key", secret)
    monkeypatch.setattr(security_cli, "_resolver", lambda context: resolver)

    code = main(["--data-dir", str(tmp_path / "data"), "secrets", "status"])
    captured = capsys.readouterr()
    assert code == 0
    assert "model_api_key" in captured.out
    assert secret not in captured.out
    assert secret not in captured.err
    assert "sk-cli" not in captured.out


def test_cli_secrets_status_json_has_no_values(tmp_path, capsys, monkeypatch):
    from app.cli.commands import security as security_cli
    from app.cli.main import main

    secret = "sk-json-secret-value-1234567890"
    resolver = _resolver(tmp_path / "data", backend=FakeKeyring())
    resolver.set("model_api_key", secret)
    monkeypatch.setattr(security_cli, "_resolver", lambda context: resolver)

    code = main(["--data-dir", str(tmp_path / "data"), "--json", "secrets", "status"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["sources"][0]["source"] == "keyring"
    assert "model_api_key" in payload["keys"]
    assert secret not in json.dumps(payload, ensure_ascii=False)


def test_cli_secrets_set_reads_stdin_and_never_echoes(tmp_path, capsys, monkeypatch):
    """值只从 stdin 来；回显只给末 4 位。"""
    from app.cli.commands import security as security_cli
    from app.cli.main import main

    backend = FakeKeyring()
    resolver = _resolver(tmp_path / "data", backend=backend)
    monkeypatch.setattr(security_cli, "_resolver", lambda context: resolver)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("sk-value-abcdefghijkl\n"))

    code = main(["--data-dir", str(tmp_path / "data"), "secrets", "set", "model_api_key"])
    captured = capsys.readouterr()
    assert code == 0
    assert backend.store[(service_name(tmp_path / "data"), "model_api_key")] == (
        "sk-value-abcdefghijkl"
    )
    assert "sk-value-abcdefghijkl" not in captured.out
    assert "ijkl" in captured.out


def test_cli_secrets_get_is_masked_by_default(tmp_path, capsys, monkeypatch):
    from app.cli.commands import security as security_cli
    from app.cli.main import main

    resolver = _resolver(tmp_path / "data", backend=FakeKeyring())
    resolver.set("model_api_key", "sk-value-abcdefghijkl")
    monkeypatch.setattr(security_cli, "_resolver", lambda context: resolver)

    code = main(["--data-dir", str(tmp_path / "data"), "secrets", "get", "model_api_key"])
    captured = capsys.readouterr()
    assert code == 0
    assert "sk-value-abcdefghijkl" not in captured.out
    assert "…ijkl" in captured.out

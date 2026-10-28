"""`app/plugins.py` 与 `app/cli/commands/plugins.py` 的契约测试。

分六块：manifest 解析、发现与加载、能力注入面（19 种 `register_*`）、
plugin_guard 三级裁决、结构限额、CLI 命令面。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import plugins
from app.cli.commands import plugins as plugins_cli  # noqa: F401 —— 导入即登记命令
from app.cli.main import main
from app.cli.spec import REGISTRY
from app.hooks import HookRegistry

# ── 夹具 ────────────────────────────────────────────────────────────────────

FULL_MANIFEST = """
name: demo-plugin
version: 1.2.3
description: >
  演示插件：覆盖 manifest 的 16 个字段。
author: Orbit
kind: tool
requires_env:
  - DEMO_API_KEY
  - name: DEMO_REGION
    description: "部署区域"
    prompt: "区域（如 cn-hangzhou）"
    password: false
    url: "https://example.com/regions"
    category: setting
optional_env:
  - name: DEMO_ALLOWED_USERS
    description: "允许的用户（逗号分隔）"
    prompt: "允许的用户"
    password: false
label: Demo
hooks:
  - pre_tool_call
  - on_session_end
provides_web_providers:
  - demo-web
pip_dependencies:
  - demo-sdk>=1.0,<2
provides_browser_providers:
  - demo-browser
provides_tools:
  - demo_echo
platforms:
  - windows
  - linux
external_dependencies:
  - name: demo-bin
    install: "winget install demo"
    check: "demo --version"
homepage: "https://example.com/demo"
entry: plugin_entry.py
min_orbit_version: "0.1.0"
enabled_by_default: false
settings:
  region: cn-hangzhou
license: MIT
provides_platform:
  - demo-platform
provides_provider:
  - demo-provider
"""

SAFE_CODE = '''
"""最小插件：注册一个工具与一个 hook。"""


def register(ctx):
    ctx.register_tool(
        "demo_echo",
        "回显参数",
        {"type": "object", "properties": {}},
        lambda args: {"echo": args},
    )
    ctx.register_hook("pre_tool_call", lambda payload: {"seen": payload.get("tool")})
'''


def write_plugin(
    root: Path,
    name: str,
    manifest: str,
    code: str | None = SAFE_CODE,
    *,
    entry: str = "__init__.py",
) -> Path:
    """在 `root/<name>/` 落一个插件（plugin.yaml + 入口模块）。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(manifest, encoding="utf-8")
    if code is not None:
        (directory / entry).write_text(code, encoding="utf-8")
    return directory


def settings_for(data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=data_dir)


# ── manifest：16 字段 / 未知字段 / kind / env ───────────────────────────────


def test_manifest_fields_table_has_exactly_sixteen():
    """调研 05 §1.2 的 16 个顶层字段，一个不多一个不少。"""
    assert len(plugins.MANIFEST_FIELDS) == 16
    assert set(plugins.MANIFEST_FIELDS) == {
        "name",
        "version",
        "description",
        "author",
        "kind",
        "requires_env",
        "label",
        "optional_env",
        "hooks",
        "provides_web_providers",
        "pip_dependencies",
        "provides_browser_providers",
        "provides_tools",
        "platforms",
        "external_dependencies",
        "homepage",
    }
    for field_name, meaning in plugins.MANIFEST_FIELDS.items():
        assert meaning.strip(), f"{field_name} 没写含义"


def test_manifest_parses_all_sixteen_fields(tmp_path):
    path = write_plugin(tmp_path, "demo-plugin", FULL_MANIFEST, SAFE_CODE, entry="plugin_entry.py")
    manifest = plugins.load_manifest(path / "plugin.yaml")
    assert manifest.name == "demo-plugin"
    assert manifest.version == "1.2.3"
    assert manifest.description.startswith("演示插件")
    assert manifest.author == "Orbit"
    assert manifest.kind == "tool"
    assert [item.name for item in manifest.requires_env] == ["DEMO_API_KEY", "DEMO_REGION"]
    assert manifest.label == "Demo"
    assert [item.name for item in manifest.optional_env] == ["DEMO_ALLOWED_USERS"]
    assert manifest.hooks == ("pre_tool_call", "on_session_end")
    assert manifest.provides_web_providers == ("demo-web",)
    assert manifest.pip_dependencies == ("demo-sdk>=1.0,<2",)
    assert manifest.provides_browser_providers == ("demo-browser",)
    assert manifest.provides_tools == ("demo_echo",)
    assert manifest.platforms == ("windows", "linux")
    assert manifest.external_dependencies[0]["name"] == "demo-bin"
    assert manifest.homepage == "https://example.com/demo"
    # Orbit 扩展字段同样要读出来（它们不在 16 个里，但不许丢）
    assert manifest.entry == "plugin_entry.py"
    assert manifest.min_orbit_version == "0.1.0"
    assert manifest.enabled_by_default is False
    assert manifest.settings == {"region": "cn-hangzhou"}
    assert manifest.license == "MIT"
    assert manifest.provides_platform == ("demo-platform",)
    assert manifest.provides_provider == ("demo-provider",)


def test_unknown_fields_are_recorded_not_fatal(tmp_path):
    """未知字段不报错，但要能查出来；扩展字段**不能**被算成未知。"""
    path = write_plugin(
        tmp_path,
        "demo-plugin",
        "name: demo-plugin\nversion: 1.0.0\nfrobnicate: true\nentry: __init__.py\n",
    )
    manifest = plugins.load_manifest(path / "plugin.yaml")
    assert manifest.unknown_fields == ("frobnicate",)
    assert "entry" not in manifest.unknown_fields
    assert "settings" not in manifest.unknown_fields


def test_manifest_rejects_illegal_kind(tmp_path):
    path = write_plugin(tmp_path, "demo-plugin", "name: demo-plugin\nkind: widget\n")
    with pytest.raises(plugins.ManifestError) as excinfo:
        plugins.load_manifest(path / "plugin.yaml")
    assert "kind" in str(excinfo.value)
    assert "tool" in str(excinfo.value)


def test_manifest_normalizes_hermes_kind_aliases(tmp_path):
    """Hermes 的真实 kind 取值要能加载，否则对真实生态零兼容。"""
    for raw, expected in (("backend", "tool"), ("standalone", "tool"), ("model-provider", "provider")):
        directory = write_plugin(tmp_path, f"p-{raw}", f"name: p-{raw}\nkind: {raw}\n")
        manifest = plugins.load_manifest(directory / "plugin.yaml")
        assert manifest.kind == expected, raw
    directory = write_plugin(tmp_path, "p-plain", "name: p-plain\n")
    assert plugins.load_manifest(directory / "plugin.yaml").kind == "tool"


def test_manifest_rejects_unsafe_name(tmp_path):
    for bad in ("../evil", "a/b", "", "-leading"):
        path = tmp_path / f"bad-{abs(hash(bad))}" / "plugin.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"name: '{bad}'\n", encoding="utf-8")
        with pytest.raises(plugins.ManifestError):
            plugins.load_manifest(path)


def test_both_env_forms_parse_and_required_flags(tmp_path):
    path = write_plugin(tmp_path, "demo-plugin", FULL_MANIFEST, SAFE_CODE, entry="plugin_entry.py")
    manifest = plugins.load_manifest(path / "plugin.yaml")
    bare = manifest.requires_env[0]
    rich = manifest.requires_env[1]
    optional = manifest.optional_env[0]
    assert bare.name == "DEMO_API_KEY" and bare.required is True
    assert bare.description == "" and bare.password is False
    assert rich.required is True
    assert rich.description == "部署区域"
    assert rich.prompt == "区域（如 cn-hangzhou）"
    assert rich.url == "https://example.com/regions"
    assert rich.category == "setting"
    assert optional.required is False


def test_env_prompts_shape_for_setup_wizard(tmp_path):
    """`env_prompts` 输出可被向导直接渲染的 6 子字段 + required 标记。"""
    path = write_plugin(tmp_path, "demo-plugin", FULL_MANIFEST, SAFE_CODE, entry="plugin_entry.py")
    manifest = plugins.load_manifest(path / "plugin.yaml")
    prompts = plugins.env_prompts(manifest)
    assert [item["name"] for item in prompts] == [
        "DEMO_API_KEY",
        "DEMO_REGION",
        "DEMO_ALLOWED_USERS",
    ]
    assert set(prompts[0]) == {
        "name",
        "description",
        "prompt",
        "password",
        "url",
        "category",
        "required",
    }
    assert prompts[0]["required"] is True
    assert prompts[2]["required"] is False
    # 裸字符串没有 prompt 文案，退回到变量名，保证向导不会渲染出空白提问
    assert prompts[0]["prompt"] == "DEMO_API_KEY"


def test_missing_env_only_looks_at_required(tmp_path):
    path = write_plugin(tmp_path, "demo-plugin", FULL_MANIFEST, SAFE_CODE, entry="plugin_entry.py")
    manifest = plugins.load_manifest(path / "plugin.yaml")
    assert manifest.missing_env({}) == ("DEMO_API_KEY", "DEMO_REGION")
    assert manifest.missing_env({"DEMO_API_KEY": "x"}) == ("DEMO_REGION",)
    assert manifest.missing_env({"DEMO_API_KEY": "x", "DEMO_REGION": "y"}) == ()


def test_import_phase_is_lazy_for_tool_providers(tmp_path):
    """声明 ``provides_tools`` 的插件是 **lazy**（MATRIX 13.3 的时机规则）。

    反直觉但有意：贡献工具的插件最重，而工具清单只在真要拼提示词时才需要；
    不贡献工具的插件必须 eager，否则错过启动窗口就再没人调用它。
    """
    lazy = write_plugin(
        tmp_path, "p-lazy-tools", "name: p-lazy-tools\nkind: platform\nprovides_tools:\n  - t\n"
    )
    eager = write_plugin(tmp_path, "p-eager", "name: p-eager\nkind: platform\n")
    lazy_manifest = plugins.load_manifest(lazy / "plugin.yaml")
    eager_manifest = plugins.load_manifest(eager / "plugin.yaml")
    assert lazy_manifest.import_phase == "lazy"
    assert eager_manifest.import_phase == "eager"
    # `eager` 只是 import_phase 的派生值，两者不许互相矛盾
    assert lazy_manifest.eager is False
    assert eager_manifest.eager is True


# ── 发现 ────────────────────────────────────────────────────────────────────


def test_discover_scans_both_roots(tmp_path):
    user_root = tmp_path / "data" / "plugins"
    bundled_root = tmp_path / "repo" / "plugins"
    write_plugin(user_root, "p-user", "name: p-user\nversion: 1.0.0\n")
    write_plugin(bundled_root, "group/p-bundled", "name: p-bundled\nversion: 2.0.0\n")

    result = plugins.discover([user_root, bundled_root])
    assert [item.name for item in result] == ["p-user", "p-bundled"]
    assert len(result) == 2
    assert result.by_name("p-bundled") is not None
    assert result.roots == [user_root, bundled_root]


def test_plugin_roots_puts_user_root_first(tmp_path):
    roots = plugins.plugin_roots(settings_for(tmp_path / "data"))
    assert roots[0] == tmp_path / "data" / "plugins"
    assert roots[1].name == "plugins" and roots[1] != roots[0]


def test_discover_keeps_good_plugins_when_one_manifest_is_broken(tmp_path):
    root = tmp_path / "plugins"
    write_plugin(root, "p-good", "name: p-good\n")
    write_plugin(root, "p-bad", "name: p-bad\nkind: nonsense\n")
    result = plugins.discover([root])
    assert [item.name for item in result] == ["p-good"]
    assert len(result.errors) == 1
    assert "kind" in result.errors[0].message


def test_discover_reports_duplicate_names(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    write_plugin(first, "p-dup", "name: p-dup\n")
    write_plugin(second, "p-dup", "name: p-dup\n")
    result = plugins.discover([first, second])
    assert [item.name for item in result] == ["p-dup"]
    assert any("重复" in error.message for error in result.errors)


# ── 加载 ────────────────────────────────────────────────────────────────────


def test_load_calls_register_and_wires_capabilities(tmp_path):
    directory = write_plugin(tmp_path, "demo-plugin", "name: demo-plugin\n", SAFE_CODE)
    manifest = plugins.load_manifest(directory / "plugin.yaml")
    registry = HookRegistry()
    loaded = plugins.load(manifest, hooks=registry)
    assert loaded.ok, loaded.error
    assert loaded.context is not None
    assert "demo_echo" in loaded.context.tools
    assert registry.hooks("pre_tool_call"), "hook 应写进宿主传进来的同一个注册表"
    assert loaded.context.capabilities()["landed"] == ["register_hook", "register_tool"]


def test_load_isolates_import_error(tmp_path):
    """坏插件（import 期抛错）只记错误，不影响其它插件。"""
    good = write_plugin(tmp_path, "p-good", "name: p-good\n", SAFE_CODE)
    bad = write_plugin(
        tmp_path,
        "p-bad",
        "name: p-bad\n",
        "raise RuntimeError('这个插件在 import 期就炸了')\n",
    )
    manifests = [
        plugins.load_manifest(good / "plugin.yaml"),
        plugins.load_manifest(bad / "plugin.yaml"),
    ]
    loaded = plugins.load_all(manifests, hooks=HookRegistry())
    assert loaded[0].ok and loaded[0].context is not None
    assert "demo_echo" in loaded[0].context.tools
    assert not loaded[1].ok
    assert "这个插件在 import 期就炸了" in loaded[1].error


def test_load_isolates_register_error(tmp_path):
    directory = write_plugin(
        tmp_path, "p-boom", "name: p-boom\n", "def register(ctx):\n    raise ValueError('register 炸了')\n"
    )
    loaded = plugins.load(plugins.load_manifest(directory / "plugin.yaml"))
    assert not loaded.ok
    assert "register 炸了" in loaded.error


def test_load_rejects_entry_outside_plugin_directory(tmp_path):
    directory = write_plugin(
        tmp_path, "p-escape", "name: p-escape\nentry: ../outside.py\n"
    )
    (tmp_path / "outside.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    loaded = plugins.load(plugins.load_manifest(directory / "plugin.yaml"))
    assert not loaded.ok
    assert "entry" in loaded.error


# ── PluginContext：19 种 register_* ─────────────────────────────────────────


def test_context_exposes_all_nineteen_register_methods():
    manifest = plugins.PluginManifest(name="p", kind="tool")
    context = plugins.PluginContext(manifest, hooks=HookRegistry())
    assert len(plugins.PLUGIN_CAPABILITIES) == 19
    for capability in plugins.PLUGIN_CAPABILITIES:
        assert hasattr(context, capability), capability
        assert callable(getattr(context, capability)), capability


def test_capability_split_is_exact_and_disjoint():
    """19 = 9 有落点 + 10 显式不支持；两边不许重叠、不许漏。"""
    landed = set(plugins.LANDED_CAPABILITIES)
    unsupported = set(plugins.UNSUPPORTED_CAPABILITIES)
    assert landed | unsupported == set(plugins.PLUGIN_CAPABILITIES)
    assert not landed & unsupported
    assert len(landed) == 9
    assert len(unsupported) == 10


def test_register_tool_records_and_pushes_to_sink(tmp_path):
    pushed: list[tuple] = []

    class Sink:
        def register(self, tools, executor, source=None, override=False):
            pushed.append((tools, source, override))

    manifest = plugins.PluginManifest(name="p", kind="tool")
    context = plugins.PluginContext(manifest, tool_sink=Sink())
    tool = context.register_tool(
        "demo_echo", "回显", {"type": "object", "properties": {}}, lambda args: args
    )
    assert tool.name == "demo_echo"
    assert context.tools["demo_echo"] is tool
    assert pushed and pushed[0][1] == "plugin:p"
    assert pushed[0][0][0].name == "demo_echo"


def test_register_tools_batch_accepts_dicts():
    context = plugins.PluginContext(plugins.PluginManifest(name="p"), hooks=HookRegistry())
    registered = context.register_tools(
        [{"name": "a", "description": "甲"}, plugins.PluginTool(name="b", source="plugin:p")]
    )
    assert [item.name for item in registered] == ["a", "b"]
    assert set(context.tools) == {"a", "b"}


def test_register_hook_goes_through_the_registry():
    registry = HookRegistry()
    context = plugins.PluginContext(plugins.PluginManifest(name="p"), hooks=registry)
    event = context.register_hook("pre_tool_call", lambda payload: None)
    assert event == "pre_tool_call"
    assert registry.hooks("pre_tool_call")


def test_register_model_provider_lands_in_providers_table():
    """有落点：`app/providers.py::PROVIDERS` 是模块级可变表。"""
    from app.providers import PROVIDERS, ProviderProfile

    manifest = plugins.PluginManifest(name="p")
    context = plugins.PluginContext(manifest, hooks=HookRegistry())
    key = context.register_model_provider(
        {
            "id": "plugin-demo",
            "name": "Plugin Demo",
            "transport": "chat_completions",
            "default_base_url": "https://example.com/v1",
        }
    )
    try:
        assert key == "plugin-demo"
        assert isinstance(PROVIDERS["plugin-demo"], ProviderProfile)
        assert context.model_providers == ["plugin-demo"]
    finally:
        PROVIDERS.pop("plugin-demo", None)


def test_register_skill_lands_in_skills_root(tmp_path):
    """有落点：复制进 `<data_dir>/skills/<name>/SKILL.md`，`app/skills.py` 直接读得到。"""
    from app import skills

    source = tmp_path / "carried" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: demo\ntriggers:\n  - 演示\n---\n正文\n", encoding="utf-8")
    settings = settings_for(tmp_path / "data")
    context = plugins.PluginContext(plugins.PluginManifest(name="p"), settings=settings)
    target = context.register_skill("demo", source.parent)
    assert (target / "SKILL.md").is_file()
    loaded = skills.load_skill(plugins.skills_root(settings), "demo")
    assert "正文" in loaded["body"]
    assert context.skills == ["demo"]


def test_register_command_lands_in_cli_registry():
    """有落点：登记进 `app/cli/spec.py::REGISTRY`，`orbit <name>` 立刻可用。"""
    name = "plugin-demo-cmd"
    context = plugins.PluginContext(plugins.PluginManifest(name="p"), hooks=HookRegistry())
    try:
        assert context.register_command(name, lambda ctx, args: 0, "演示命令") == name
        assert REGISTRY.get(name) is not None
        assert context.commands == [name]
    finally:
        REGISTRY._commands.pop(name, None)  # noqa: SLF001 —— Registry 没给删除 API


def test_register_command_refuses_to_shadow_builtin():
    context = plugins.PluginContext(plugins.PluginManifest(name="p"), hooks=HookRegistry())
    with pytest.raises(plugins.PluginError) as excinfo:
        context.register_command("status", lambda ctx, args: 0)
    assert "冲突" in str(excinfo.value)


@pytest.mark.parametrize("capability", sorted(plugins.UNSUPPORTED_CAPABILITIES))
def test_unsupported_capabilities_raise_with_reason(capability):
    """没有落点的一律抛 `UnsupportedCapability`，且**消息里必须带原因**。"""
    context = plugins.PluginContext(plugins.PluginManifest(name="p"), hooks=HookRegistry())
    with pytest.raises(plugins.UnsupportedCapability) as excinfo:
        getattr(context, capability)()
    error = excinfo.value
    assert error.capability == capability
    assert error.reason.strip(), f"{capability} 没写原因"
    assert capability in str(error)
    assert "没有落点" in str(error)
    assert capability in context.used


# ── plugin_guard：三级裁决 ──────────────────────────────────────────────────


def scan_of(tmp_path: Path, code: str, *, manifest: str = "name: p\n", data_dir: Path | None = None):
    directory = write_plugin(tmp_path, "p", manifest, code)
    return plugins.scan_plugin(directory, source="test", data_dir=data_dir)


def test_guard_safe_plugin(tmp_path):
    result = scan_of(tmp_path, SAFE_CODE)
    assert result.verdict == "safe"
    assert plugins.should_allow_plugin_install(result) == (True, "允许（扫描干净）")
    assert result.provenance["scanner_version"] == plugins.SCANNER_VERSION


def test_guard_caution_on_reading_own_credentials(tmp_path):
    """读自己的 API key 是插件的**合法**做法，所以只到 caution，不到 dangerous。"""
    result = scan_of(
        tmp_path,
        "import os\n\nTOKEN = os.environ.get('DEMO_API_KEY')\n",
    )
    assert result.verdict == "caution"
    assert {item.pattern_id for item in result.findings} == {"env_secret_read"}
    allowed, reason = plugins.should_allow_plugin_install(result)
    assert allowed is None and "确认" in reason
    assert plugins.should_allow_plugin_install(result, force=True)[0] is True


def test_guard_caution_on_writing_outside_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    result = scan_of(
        tmp_path,
        "def dump():\n    open('D:/tmp/outside.txt', 'w').write('x')\n",
        data_dir=data_dir,
    )
    assert result.verdict == "caution"
    assert {item.pattern_id for item in result.findings} == {"write_outside_data_dir"}


def test_guard_write_inside_data_dir_is_not_flagged(tmp_path):
    """写自己的数据目录是**该做的事**，不该被记发现。"""
    data_dir = tmp_path / "data"
    target = str(data_dir / "plugin-data" / "p" / "state.json").replace("\\", "/")
    result = scan_of(
        tmp_path,
        f"def save():\n    open('{target}', 'w').write('x')\n",
        data_dir=data_dir,
    )
    assert result.verdict == "safe"


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess\nsubprocess.run(['whoami'])\n",
        "eval('1 + 1')\n",
        "import os\nos.system('whoami')\n",
        "import urllib.request\nurllib.request.urlopen('https://example.com')\n",
        "open('C:/Windows/Temp/x.txt', 'w').write('x')\n",
    ],
)
def test_guard_dangerous_verdicts(tmp_path, code):
    result = scan_of(tmp_path, code)
    assert result.verdict == "dangerous", result.findings
    # 至少一条 critical（同一行可能既命中"写系统目录"又命中"写 data_dir 之外"，
    # 后者按设计只是 caution 级别，不该把裁决拉回去）
    assert any(item.severity == "critical" for item in result.findings)


def test_force_never_overrides_dangerous(tmp_path):
    """硬规矩：`--force` 不覆盖 dangerous（这是安全底线，不是可配置策略）。"""
    result = scan_of(tmp_path, "import subprocess\nsubprocess.run(['whoami'])\n")
    allowed, reason = plugins.should_allow_plugin_install(result, force=True)
    assert allowed is False
    assert "--force" in reason and "dangerous" in reason


def test_install_local_refuses_dangerous_even_with_force(tmp_path):
    source = write_plugin(tmp_path / "src", "p-evil", "name: p-evil\n", "import subprocess\nsubprocess.run(['whoami'])\n")
    data_dir = tmp_path / "data"
    with pytest.raises(plugins.PluginGuardError) as excinfo:
        plugins.install_local(source, settings_for(data_dir), force=True)
    assert "dangerous" in str(excinfo.value)
    assert not (data_dir / "plugins" / "p-evil").exists(), "被拒时不许留下目录"


# ── 结构限额 ────────────────────────────────────────────────────────────────


def test_structure_limit_too_many_files(tmp_path):
    directory = write_plugin(tmp_path, "p-many", "name: p-many\n", SAFE_CODE)
    for index in range(plugins.MAX_PLUGIN_FILE_COUNT):
        (directory / f"f{index}.txt").write_text("x", encoding="utf-8")
    result = plugins.scan_plugin(directory)
    assert "too_many_files" in {item.pattern_id for item in result.findings}
    assert result.verdict == "dangerous"
    assert plugins.should_allow_plugin_install(result, force=True)[0] is False


def test_structure_limit_total_size(tmp_path):
    directory = write_plugin(tmp_path, "p-fat", "name: p-fat\n", SAFE_CODE)
    chunk = b"a" * (plugins.MAX_PLUGIN_SINGLE_FILE_KB * 1024)
    for index in range(11):  # 11MB > 10MB，但每个文件都不超单文件限额
        (directory / f"blob{index}.bin").write_bytes(chunk)
    result = plugins.scan_plugin(directory)
    ids = {item.pattern_id for item in result.findings}
    assert "oversized_bundle" in ids
    assert "oversized_file" not in ids
    assert result.verdict == "dangerous"


def test_structure_limit_single_file(tmp_path):
    directory = write_plugin(tmp_path, "p-big", "name: p-big\n", SAFE_CODE)
    (directory / "huge.txt").write_bytes(b"b" * (plugins.MAX_PLUGIN_SINGLE_FILE_KB * 1024 + 1))
    result = plugins.scan_plugin(directory)
    assert "oversized_file" in {item.pattern_id for item in result.findings}
    assert result.verdict == "dangerous"


def make_escape_link(link: Path, target: Path) -> bool:
    """造一条"指到目录之外"的链接。符号链接不行就退回 junction。

    本机实测：普通用户建不了符号链接（``WinError 1314``），但 ``mklink /J`` 建的目录
    junction **不需要**管理员权限，而且它同样能逃出目录——只是 ``Path.is_symlink()``
    对它返回 False。所以扫描器的判据必须落在"解析后的真实路径"上，这条测试正是
    为了钉住这一点。
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name != "nt":
        return False
    import subprocess

    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def test_structure_limit_symlink_escape(tmp_path):
    """链接指向插件目录之外必须被拒（critical → dangerous，--force 也不放行）。"""
    directory = write_plugin(tmp_path, "p-link", "name: p-link\n", SAFE_CODE)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("机密", encoding="utf-8")
    if not make_escape_link(directory / "escape", outside):
        pytest.skip("本机既不允许符号链接也不允许 junction，无法构造逃逸用例")
    result = plugins.scan_plugin(directory)
    assert "symlink_escape" in {item.pattern_id for item in result.findings}
    assert result.verdict == "dangerous"
    assert plugins.should_allow_plugin_install(result, force=True)[0] is False


# ── 安装 / 移除 / 状态 ──────────────────────────────────────────────────────


def test_install_local_roundtrip(tmp_path):
    source = write_plugin(tmp_path / "src", "p-ok", "name: p-ok\nversion: 1.0.0\n")
    settings = settings_for(tmp_path / "data")
    result = plugins.install_local(source, settings)
    assert result.enabled is True
    assert result.verdict == "safe"
    target = tmp_path / "data" / "plugins" / "p-ok"
    assert (target / "plugin.yaml").is_file()
    found = plugins.discover(plugins.plugin_roots(settings))
    assert [item.name for item in found] == ["p-ok"]
    assert plugins.load_state(settings).is_enabled("p-ok")
    plugins.remove_local("p-ok", settings)
    assert not target.exists()
    # 移除后它不该再出现在启用名单里（is_enabled 的 default 由 manifest 决定，
    # 插件已经不存在，所以这里断言的是"状态文件里没有残留条目"）
    assert "p-ok" not in plugins.load_state(settings).enabled


def test_install_local_honours_no_enable_and_enabled_by_default(tmp_path):
    source = write_plugin(tmp_path / "src", "p-off", "name: p-off\nenabled_by_default: false\n")
    settings = settings_for(tmp_path / "data")
    result = plugins.install_local(source, settings)
    assert result.enabled is False
    assert plugins.load_state(settings).is_enabled("p-off") is False
    # 显式 --enable 可以覆盖 manifest 的默认
    plugins.remove_local("p-off", settings)
    result = plugins.install_local(source, settings, force=True, enable=True)
    assert result.enabled is True


def test_install_local_needs_force_to_overwrite(tmp_path):
    source = write_plugin(tmp_path / "src", "p-dup", "name: p-dup\n")
    settings = settings_for(tmp_path / "data")
    plugins.install_local(source, settings)
    with pytest.raises(plugins.PluginGuardError):
        plugins.install_local(source, settings)
    assert plugins.install_local(source, settings, force=True).name == "p-dup"


def test_state_enable_disable_and_override(tmp_path):
    settings = settings_for(tmp_path / "data")
    state = plugins.load_state(settings)
    state.disable("p")
    assert plugins.load_state(settings).is_enabled("p") is False
    state.enable("p", allow_tool_override=True)
    reloaded = plugins.load_state(settings)
    assert reloaded.is_enabled("p") is True
    assert reloaded.allow_tool_override("p") is True


def test_remove_refuses_path_traversal(tmp_path):
    settings = settings_for(tmp_path / "data")
    with pytest.raises(plugins.PluginError):
        plugins.remove_local("../etc", settings)


def test_plugin_data_dir_is_outside_install_tree(tmp_path):
    settings = settings_for(tmp_path / "data")
    data = plugins.plugin_data_dir("p", settings)
    assert data == tmp_path / "data" / "plugin-data" / "p"
    assert data.is_dir()
    with pytest.raises(plugins.PluginError):
        plugins.plugin_data_dir("../evil", settings)


# ── doctor ──────────────────────────────────────────────────────────────────


def test_doctor_clean_plugin_has_no_errors(tmp_path):
    directory = write_plugin(tmp_path, "p-ok", "name: p-ok\nversion: 1.0.0\n", SAFE_CODE)
    checks = plugins.doctor(plugins.load_manifest(directory / "plugin.yaml"))
    assert not [item for item in checks if item.level == "error"]
    names = {item.name for item in checks}
    assert {"name", "kind", "entry", "register", "scan"} <= names


def test_doctor_reports_unknown_hook_and_missing_register(tmp_path):
    directory = write_plugin(
        tmp_path,
        "p-bad",
        "name: p-bad\nversion: 1.0.0\nhooks:\n  - pre_whatever\n",
        "VALUE = 1\n",
    )
    checks = plugins.doctor(plugins.load_manifest(directory / "plugin.yaml"))
    errors = {item.name: item.detail for item in checks if item.level == "error"}
    assert "hooks" in errors and "pre_whatever" in errors["hooks"]
    assert "register" in errors


def test_doctor_warns_on_missing_env(tmp_path):
    directory = write_plugin(
        tmp_path,
        "p-env",
        "name: p-env\nrequires_env:\n  - DEMO_KEY\n",
        SAFE_CODE,
    )
    checks = plugins.doctor(plugins.load_manifest(directory / "plugin.yaml"))
    warns = {item.name: item.detail for item in checks if item.level == "warn"}
    assert "DEMO_KEY" in warns["requires_env"]


# ── CLI 命令面 ──────────────────────────────────────────────────────────────


def test_cli_plugins_list(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    source = write_plugin(tmp_path / "src", "p-cli", "name: p-cli\nversion: 9.9.9\n")
    assert main(["--data-dir", data_dir, "plugins", "install", str(source)]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "plugins", "list"]) == 0
    out = capsys.readouterr().out
    assert "p-cli" in out and "9.9.9" in out


def test_cli_plugins_list_json(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    source = write_plugin(tmp_path / "src", "p-json", "name: p-json\n")
    assert main(["--data-dir", data_dir, "plugins", "install", str(source)]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "--json", "plugins", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert [item["name"] for item in payload["plugins"]] == ["p-json"]


def test_cli_plugins_show(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    source = write_plugin(
        tmp_path / "src", "p-show", "name: p-show\nversion: 1.0.0\nrequires_env:\n  - P_SHOW_KEY\n"
    )
    assert main(["--data-dir", data_dir, "plugins", "install", str(source)]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "plugins", "show", "p-show"]) == 0
    out = capsys.readouterr().out
    assert "p-show" in out and "P_SHOW_KEY" in out and "entry" in out


def test_cli_plugins_enable_disable(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    source = write_plugin(tmp_path / "src", "p-toggle", "name: p-toggle\n")
    assert main(["--data-dir", data_dir, "plugins", "install", str(source)]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "plugins", "disable", "p-toggle"]) == 0
    assert "已禁用" in capsys.readouterr().out
    assert main(["--data-dir", data_dir, "plugins", "enable", "p-toggle"]) == 0
    assert "已启用" in capsys.readouterr().out


def test_cli_plugins_capabilities_lists_all_nineteen(tmp_path, capsys):
    """19 种 `ctx.register_*` 都要列出来，并给出三态计数。

    原断言检查的是旧实现里的「17 个实证」/「推断」字样。那套口径已经被
    `app/plugin_capabilities.py` 取代（wired / exposed / unsupported 三态，
    且 exposed 与 unsupported 必须写清缺哪一层）。**数量会变，三态自洽不会**——
    所以改成断言后者。
    """
    from app import plugin_capabilities

    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "plugins", "capabilities"]) == 0
    out = capsys.readouterr().out
    summary = plugin_capabilities.summary()
    assert summary["total"] == 19
    assert summary["wired"] + summary["exposed"] + summary["unsupported"] == 19
    assert "19 种 register_*" in out
    assert f"wired {summary['wired']}" in out
    assert f"exposed {summary['exposed']}" in out
    assert f"unsupported {summary['unsupported']}" in out
    # 每一条都要有落点或缺失原因——只说「不支持」等于没说
    for item in plugin_capabilities.CAPABILITIES:
        assert item.name in out, item.name
        assert item.orbit_landing.strip(), f"{item.name} 没写落点/缺哪一层"


def test_cli_plugins_capabilities_for_one_plugin(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    source = write_plugin(
        tmp_path / "src",
        "p-caps",
        "name: p-caps\nprovides_tools:\n  - t\nhooks:\n  - pre_tool_call\n",
    )
    assert main(["--data-dir", data_dir, "plugins", "install", str(source)]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "plugins", "capabilities", "p-caps"]) == 0
    out = capsys.readouterr().out
    assert "p-caps" in out and "register_tool" in out


def test_cli_plugins_doctor_and_ci_exit_code(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    good = write_plugin(tmp_path / "good", "p-good", "name: p-good\nversion: 1.0.0\n", SAFE_CODE)
    assert main(["--data-dir", data_dir, "plugins", "doctor", str(good)]) == 0
    capsys.readouterr()

    bad = write_plugin(
        tmp_path / "bad", "p-bad", "name: p-bad\nversion: 1.0.0\nhooks:\n  - pre_whatever\n", "X = 1\n"
    )
    assert main(["--data-dir", data_dir, "plugins", "doctor", str(bad)]) == 0
    assert "错误" in capsys.readouterr().out
    assert main(["--data-dir", data_dir, "plugins", "doctor", "--ci", str(bad)]) == 1


def test_cli_plugins_install_remote_reports_unimplemented(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    code = main(["--data-dir", data_dir, "plugins", "install", "https://github.com/owner/repo"])
    assert code == 1
    err = capsys.readouterr().err
    assert "未实现" in err and "网络" in err


def test_cli_plugins_install_owner_repo_shorthand_is_remote(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "plugins", "install", "owner/repo"]) == 1
    assert "未实现" in capsys.readouterr().err


def test_cli_plugins_install_missing_local_dir(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "plugins", "install", str(tmp_path / "nope")]) == 1
    assert "不存在" in capsys.readouterr().err


def test_cli_plugins_remove_and_remove_bundled_refused(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    source = write_plugin(tmp_path / "src", "p-rm", "name: p-rm\n")
    assert main(["--data-dir", data_dir, "plugins", "install", str(source)]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "plugins", "remove", "p-rm"]) == 0
    assert "已移除" in capsys.readouterr().out
    assert not (tmp_path / "data" / "plugins" / "p-rm").exists()
    # 已移除后再 remove → 找不到
    assert main(["--data-dir", data_dir, "plugins", "remove", "p-rm"]) == 1


def test_cli_plugins_search_local_only(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    source = write_plugin(
        tmp_path / "src", "p-search", "name: p-search\ndescription: 演示搜索用\n"
    )
    assert main(["--data-dir", data_dir, "plugins", "install", str(source)]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "plugins", "search", "搜索"]) == 0
    out = capsys.readouterr().out
    assert "p-search" in out
    # 社区索引需要网络 → --refresh 必须诚实报错
    assert main(["--data-dir", data_dir, "plugins", "search", "--refresh"]) == 1
    assert "未实现" in capsys.readouterr().err


def test_cli_plugins_pack_export_and_show(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    source = write_plugin(tmp_path / "src", "p-pack", "name: p-pack\nversion: 3.2.1\n")
    assert main(["--data-dir", data_dir, "plugins", "install", str(source)]) == 0
    capsys.readouterr()
    pack_file = tmp_path / "orbit-pack.yaml"
    assert main(["--data-dir", data_dir, "plugins", "pack", "export", "--name", str(pack_file)]) == 0
    assert pack_file.is_file()
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "plugins", "pack", "show", str(pack_file)]) == 0
    out = capsys.readouterr().out
    assert "p-pack" in out and "3.2.1" in out


def test_cli_plugins_pack_install_reports_unimplemented(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "plugins", "pack", "install", "pack.yaml"]) == 1
    assert "未实现" in capsys.readouterr().err


def test_cli_plugins_update_local_rescans(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    source = write_plugin(tmp_path / "src", "p-upd", "name: p-upd\n")
    assert main(["--data-dir", data_dir, "plugins", "install", str(source)]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "plugins", "update", "p-upd"]) == 0
    out = capsys.readouterr().out
    assert "未实现" in out and "safe" in out


def test_plugins_command_is_registered_with_all_subcommands():
    command = REGISTRY.get("plugins")
    assert command is not None
    names = {sub.name for sub in command.subcommands}
    assert names == {
        "list",
        "show",
        "enable",
        "disable",
        "install",
        "remove",
        "search",
        "doctor",
        "capabilities",
        "pack",
        "update",
        # 本轮新增（`plugins_extra.py`）：能力面收尾
        "info",
        "validate",
        "reload",
        "deps",
        "override",
        "pages",
    }
    assert command.state == "partial", "install/search/pack/update 有未实现部分，不能标 done"
    for sub in command.subcommands:
        assert sub.help.strip()

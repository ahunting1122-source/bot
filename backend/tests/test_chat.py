"""`orbit chat` 与 oneshot 模式的契约测试。

不测"agent 回答得好不好"（那要真模型），只测**命令面与驱动逻辑**：
- 未实现的 flag 要明确报错并指向 MATRIX 条目，而不是静默忽略
- oneshot 只把最终回答打到 stdout（脚本靠这个）
- 全局 `-z` 等价于 `chat -z`
- 任务进入 WAITING_USER 时能接着问、能续跑

agent 栈用一个替身 runner 顶掉：真跑要拉浏览器、加载模型、连 MCP，
在单元测试里既慢又不稳定。
"""

from __future__ import annotations

import json

import pytest

import app.cli.commands.chat as chat_cli
from app.cli.main import main
from app.config import Settings
from app.main import migrate
from app.storage import Message, Store, Task


class StubRunner:
    """替身：入队即写一条回答并把任务标成功。"""

    def __init__(self, store, answer: str = "替身回答") -> None:
        self.store = store
        self.answer = answer
        self.enqueued: list[str] = []

    async def enqueue(self, task_id, resume=None, *, platform="cli"):
        # 签名要与真实 `AgentRunner.enqueue` 一致（MATRIX 6.4 之后 `_submit()`
        # 会传 `platform=`）。假件签名不准会让「产品真的传了参数」变成测试报错。
        self.platform_seen = platform
        from app.storage import Message

        self.enqueued.append(task_id)
        task = await self.store.get(Task, task_id)
        assert task is not None
        await self.store.save(
            Message(
                id=f"answer-{task_id}",
                conversation_id=task.conversation_id,
                role="assistant",
                content=self.answer,
            )
        )
        await self.store.transition(task_id, "SUCCEEDED")

    async def close(self):
        return None


def _stub_stack(tmp_path, monkeypatch, *, answer: str = "替身回答"):
    """把 `_build_stack` 换成"只给一个临时库 + 替身 runner"。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    runner = StubRunner(store, answer)

    def fake_build(context):
        return runner, store, None

    monkeypatch.setattr(chat_cli, "_build_stack", fake_build)
    return store, runner


# ── 非法 flag 值必须报错（合法值不再报"未实现"）──────────────────────────


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        # 合法值（low/medium/high）现在真的生效，所以拿非法值来验校验
        (["--reasoning", "超强"], "未知的推理力度"),
        (["--toolsets", "不存在的工具集"], "未知工具集"),
        (["--provider", "不存在的供应商"], "未知供应商"),
        (["--max-turns", "0"], "正整数"),
        (["--run-budget", "-5"], "正整数"),
    ],
)
def test_illegal_flag_values_fail_loudly(tmp_path, capsys, args, needle):
    code = main(["--data-dir", str(tmp_path / "data"), "chat", *args])
    assert code == 1
    assert needle in capsys.readouterr().err


def test_reasoning_accepts_the_three_real_levels(tmp_path, monkeypatch):
    """`low` / `medium` / `high` 是**合法值**，不该再报"未实现"。

    这条存在的理由：上一版把 `--reasoning` 整个当成未实现，于是"能不能传"这件事
    从来没被验证过。现在它真的会改 `settings.reasoning_level`，所以要有一条
    正向用例——否则下次有人把它改回去也不会红。
    """
    from app import reasoning
    from app.cli.context import CliError

    captured: dict = {}

    def _boom(context):
        # 记下改过的 settings，然后**主动抛一个具体异常**——不要用裸 Exception，
        # 那会把「任何失败」都当成预期结果（ruff B017 拦得对）。
        captured["settings"] = context.settings
        raise CliError("测试用：栈不真的搭起来")

    monkeypatch.setattr("app.cli.commands.chat._build_stack", _boom)
    # `main()` 会把 CliError 转成退出码 1（打印消息），不会往外抛——所以断言退出码，
    # 不断言异常。`_boom` 已经被调用过，说明 flag 处理走到了搭栈这一步。
    code = main(["--data-dir", str(tmp_path / "data"), "chat", "--reasoning", "high", "-z", "hi"])
    assert code == 1
    assert captured.get("settings") is not None
    assert captured["settings"].reasoning_level in reasoning.LEVELS


def test_missing_in_dir_is_refused(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "chat", "--in", str(tmp_path / "nope")])
    assert code == 1
    assert "不存在" in capsys.readouterr().err


# ── oneshot ────────────────────────────────────────────────────────────────


def test_oneshot_prints_only_the_final_answer(tmp_path, monkeypatch, capsys):
    _stub_stack(tmp_path, monkeypatch, answer="这是最终回答")
    code = main(["--data-dir", str(tmp_path / "data"), "chat", "-z", "你好"])
    assert code == 0
    out = capsys.readouterr().out
    assert out.strip() == "这是最终回答"


def test_global_oneshot_flag_is_equivalent(tmp_path, monkeypatch, capsys):
    """`orbit -z "…"` 等价于 `orbit chat -z "…"`——脚本里不必记得先写命令名。"""
    store, runner = _stub_stack(tmp_path, monkeypatch, answer="全局 oneshot")
    code = main(["--data-dir", str(tmp_path / "data"), "-z", "你好"])
    assert code == 0
    assert capsys.readouterr().out.strip() == "全局 oneshot"
    assert runner.enqueued, "任务没有被入队"


def test_oneshot_json_payload(tmp_path, monkeypatch, capsys):
    _stub_stack(tmp_path, monkeypatch, answer="结构化")
    code = main(["--data-dir", str(tmp_path / "data"), "--json", "chat", "-z", "你好"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["answer"] == "结构化"
    assert payload["conversation_id"]


def test_oneshot_writes_the_user_message(tmp_path, monkeypatch):
    _stub_stack(tmp_path, monkeypatch)
    main(["--data-dir", str(tmp_path / "data"), "chat", "-z", "记一下这句话"])

    # 用一个新的 Context 读库：`main()` 已经把它的 Store 关掉了。
    from sqlalchemy import select

    from app.cli.context import Context

    context = Context(data_dir=tmp_path / "data")
    try:
        rows = context.run(context.store.rows(select(Message)))
        assert any(row.content == "记一下这句话" and row.role == "user" for row in rows)
    finally:
        context.close()


def test_waiting_user_is_resumed(tmp_path, monkeypatch, capsys):
    """任务停在 WAITING_USER 时，chat 要能问一句并把回答喂回去。"""

    class AskingRunner(StubRunner):
        def __init__(self, store):
            super().__init__(store)
            self.rounds = 0

        async def enqueue(self, task_id, resume=None, *, platform="cli"):
            # 签名要与真实 `AgentRunner.enqueue` 一致（MATRIX 6.4 之后 `_submit()`
            # 会传 `platform=`）。假件签名不准会让「产品真的传了参数」变成测试报错。
            self.platform_seen = platform
            from app.storage import Message

            self.enqueued.append(task_id)
            task = await self.store.get(Task, task_id)
            assert task is not None
            self.rounds += 1
            if self.rounds == 1:
                await self.store.transition(
                    task_id, "WAITING_USER", waiting={"question": "你要几月的？"}
                )
                return
            await self.store.save(
                Message(
                    id=f"answer-{task_id}",
                    conversation_id=task.conversation_id,
                    role="assistant",
                    content=f"好，按「{resume.get('answer')}」来",
                )
            )
            await self.store.transition(task_id, "SUCCEEDED")

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    runner = AskingRunner(store)
    monkeypatch.setattr(chat_cli, "_build_stack", lambda context: (runner, store, None))
    monkeypatch.setattr("builtins.input", lambda *a, **k: "八月")

    code = main(["--data-dir", str(tmp_path / "data"), "chat", "-z", "帮我查数据"])
    assert code == 0
    assert "八月" in capsys.readouterr().out
    assert runner.rounds == 2

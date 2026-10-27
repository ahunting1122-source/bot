"""日志配置的契约测试（`app/logging_setup.py`）。

这套测试存在的理由：Orbit 之前**一条日志都没有落过盘**——所有模块都写了
`logging.getLogger("orbit.xxx")`，但从来没有配置 handler，消息全靠 lastResort
打到 stderr。所以这里重点验证"真的写进文件了"，而不是"函数被调用了"。
"""

from __future__ import annotations

import logging

import pytest

from app import logging_setup


@pytest.fixture(autouse=True)
def clean_logging():
    """每个用例前后都摘干净：否则 handler 会挂到别的用例的临时目录上。"""
    logging_setup.reset()
    yield
    logging_setup.reset()


def test_configure_creates_both_log_files(tmp_path):
    directory = logging_setup.configure(tmp_path)
    assert directory == tmp_path / "logs"
    logging.getLogger("orbit.test").info("hello")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert (directory / "orbit.log").exists()
    assert (directory / "errors.log").exists()
    assert "hello" in (directory / "orbit.log").read_text(encoding="utf-8")


def test_errors_log_only_gets_warning_and_above(tmp_path):
    directory = logging_setup.configure(tmp_path)
    logger = logging.getLogger("orbit.test")
    logger.info("这是普通信息")
    logger.warning("这是警告")
    for handler in logging.getLogger().handlers:
        handler.flush()
    errors = (directory / "errors.log").read_text(encoding="utf-8")
    main = (directory / "orbit.log").read_text(encoding="utf-8")
    assert "这是警告" in errors
    assert "这是普通信息" not in errors
    # 主日志两份都收。
    assert "这是普通信息" in main and "这是警告" in main


def test_task_context_is_stamped_into_every_line(tmp_path):
    directory = logging_setup.configure(tmp_path)
    logger = logging.getLogger("orbit.test")
    with logging_setup.task_context("task-abc"):
        logger.info("带上下文")
    logger.info("不带上下文")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (directory / "orbit.log").read_text(encoding="utf-8")
    assert "[task=task-abc]" in text
    assert "[task=-]" in text


def test_task_context_restores_previous_value(tmp_path):
    logging_setup.configure(tmp_path)
    with logging_setup.task_context("outer"):
        assert logging_setup.current_task_id() == "outer"
        with logging_setup.task_context("inner"):
            assert logging_setup.current_task_id() == "inner"
        assert logging_setup.current_task_id() == "outer"
    assert logging_setup.current_task_id() == "-"


def test_configure_is_idempotent(tmp_path):
    """重复配置不许把 handler 越堆越多——测试里每个用例配一次，堆起来会拖慢并写坏文件。"""
    for _ in range(5):
        logging_setup.configure(tmp_path)
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_orbit_logging", False)]
    assert len(ours) == 2  # 主日志 + 错误日志


def test_reconfigure_moves_output_to_the_new_directory(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    logging_setup.configure(first)
    logging.getLogger("orbit.test").info("写在第一个目录")
    logging_setup.configure(second)
    logging.getLogger("orbit.test").info("写在第二个目录")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "写在第一个目录" in (first / "logs" / "orbit.log").read_text(encoding="utf-8")
    assert "写在第二个目录" in (second / "logs" / "orbit.log").read_text(encoding="utf-8")
    # 重配之后不该再往旧目录写。
    assert "写在第二个目录" not in (first / "logs" / "orbit.log").read_text(encoding="utf-8")


def test_reset_detaches_everything(tmp_path):
    logging_setup.configure(tmp_path)
    logging_setup.reset()
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_orbit_logging", False)]
    assert ours == []

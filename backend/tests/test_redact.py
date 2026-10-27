"""密钥脱敏的契约测试。

两类断言刻意分开写：

1. **每类模式都要真命中一次**。少写一类，`PATTERNS` 里那条就可能是死代码——
   死掉的正则不会报错，只会静默漏掉一种密钥。
2. **误脱敏也要防**。脱敏层一旦把 UUID / 时间戳 / 正常 URL 也遮掉，日志就不可读了，
   然后有人会把它关掉——那时才是真正的泄漏开始。所以假阳性用例和命中用例同等重要。
"""

from __future__ import annotations

import re

import pytest

from app.redact import MASK, PATTERNS, is_secret_name, redact, redact_obj

#: 每类模式一条样本：`(期望 kind, 文本, 原文里那段"绝不能出现在输出里"的秘密)`。
#:
#: 样本都刻意写成"真实形态"而不是"最短能命中"：最短能命中会把模式调宽，而调宽的
#: 代价全在假阳性一侧。
CASES: tuple[tuple[str, str, str], ...] = (
    (
        "anthropic_key",
        "export ANTHROPIC_API_KEY=sk-ant-api03-QWERTYuiop1234567890asdf",
        "sk-ant-api03-QWERTYuiop1234567890asdf",
    ),
    (
        "private_key_block",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEA",
    ),
    (
        "openai_key",
        "key: sk-QWERTYuiop1234567890asdfghjk",
        "sk-QWERTYuiop1234567890asdfghjk",
    ),
    ("aws_access_key", "AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    (
        "github_token",
        "token=ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
        "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
    ),
    (
        "github_pat",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
    ),
    ("gitlab_token", "glpat-aBcDeFgHiJkLmNoPqRsT", "glpat-aBcDeFgHiJkLmNoPqRsT"),
    (
        "slack_token",
        "xoxb-123456789012-aBcDeFgHiJkLmNoPqRsT",
        "xoxb-123456789012-aBcDeFgHiJkLmNoPqRsT",
    ),
    (
        "google_api_key",
        "AIzaSyA1234567890aBcDeFgHiJkLmNoPqRsTuV",
        "AIzaSyA1234567890aBcDeFgHiJkLmNoPqRsTuV",
    ),
    ("stripe_key", "sk_live_aBcDeFgHiJkLmNoPqRsT", "sk_live_aBcDeFgHiJkLmNoPqRsT"),
    (
        "jwt",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.aBcDeFgHiJkLmNoPqRsT",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.aBcDeFgHiJkLmNoPqRsT",
    ),
    (
        "bearer_token",
        "Authorization: Bearer aBcDeFgHiJkLmNoPqRsTuVwXyZ012345",
        "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345",
    ),
    (
        "url_userinfo",
        "postgres://alice:s3cretPassword@db.example.com:5432/app",
        "s3cretPassword",
    ),
    (
        "conn_string_password",
        "Server=db;Password=hunter2hunter2;Database=app",
        "hunter2hunter2",
    ),
    ("dotenv_assignment", "DB_PASSWORD=hunter2hunter2", "hunter2hunter2"),
    ("long_hex", "d41d8cd98f00b204e9800998ecf8427e", "d41d8cd98f00b204e9800998ecf8427e"),
)


def test_pattern_table_covers_at_least_fifteen_kinds():
    """至少 15 类。这条是"别把模式表悄悄删小"的门禁。"""
    kinds = {pattern.kind for pattern in PATTERNS}
    assert len(kinds) >= 15, f"只有 {len(kinds)} 类：{sorted(kinds)}"
    # 每个 kind 必须带中文说明——`hits` 里只有 kind，人要能查回它是什么。
    for pattern in PATTERNS:
        assert pattern.label.strip(), pattern.kind
        assert re.search(r"[\u4e00-\u9fff]", pattern.label), f"{pattern.kind} 说明不是中文"


@pytest.mark.parametrize(("kind", "text", "secret"), CASES)
def test_each_pattern_kind_hits(kind: str, text: str, secret: str):
    """每类模式各命中一次，且原文不再出现。"""
    result = redact(text)
    assert kind in result.kinds, f"{kind} 没命中：{result.hits} / {result.text!r}"
    assert secret not in result.text


def test_hits_never_contain_the_secret():
    """`hits` 只记类别与次数——它会被写进日志与 JSON，绝不能夹带原文。"""
    secret = "sk-QWERTYuiop1234567890asdfghjk"
    result = redact(f"key={secret}")
    assert result.hits == [{"kind": "openai_key", "count": 1}]
    dumped = repr(result.hits)
    assert secret not in dumped
    assert "QWERTY" not in dumped


def test_fingerprint_keeps_first_and_last_four():
    """保留首 4 + 末 4：够认出是哪把钥匙，不足以复原。"""
    result = redact("sk-aaaaaaaaaaaaaaaaaaaaaaaa1234")
    assert result.text == f"sk-a{MASK}1234"
    assert len(result.text) < 12


def test_short_secret_keeps_less():
    """短值不套用"首 4 末 4"——12 位以下留 8 位就泄露了大半。"""
    result = redact("DB_PASSWORD=abc12345")
    assert "abc12345" not in result.text
    # 8~11 位：只留首 2 末 2
    assert result.text.endswith("45")


@pytest.mark.parametrize(
    "text",
    [
        "普通文本，没有任何密钥，只是中文说明。",
        "550e8400-e29b-41d4-a716-446655440000",
        "2026-10-27T23:55:00Z",
        "https://example.com/path?q=1&lang=zh",
        "ORBIT_DATA_DIR=D:\\bbbooottt\\data",
        "MAX_TOKENS=4096",
        "KEY_PATH=C:\\keys\\model.pem",
        "def run(command): return subprocess.run(command)",
    ],
)
def test_no_false_positives_on_ordinary_text(text: str):
    """这些都不该被脱敏。误脱敏会让日志不可读，最终导致有人把整层关掉。"""
    result = redact(text)
    assert result.hits == [], f"误命中：{result.hits}"
    assert result.text == text


def test_long_hex_is_masked_even_when_it_might_be_a_digest():
    """已知取舍：摘要（SHA-256）会被一起遮掉。

    形态上无法区分"32 位 hex 是密钥还是摘要"，而漏掉真密钥的代价远大于多遮一个哈希。
    这条用例是把这个取舍**钉住**，免得以后有人"顺手放宽"而不自知。
    """
    digest = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    assert "long_hex" in redact(digest).kinds


def test_empty_and_secretless_text_is_returned_as_is():
    assert redact("").text == ""
    assert redact("").hits == []
    plain = "nothing to see here"
    assert redact(plain).text == plain
    assert not redact(plain)


def test_redact_obj_walks_nested_structures():
    """工具结果是嵌套 JSON，密钥可能在第 4 层的 headers 里。"""
    payload = {
        "ok": True,
        "attempts": 3,
        "nothing": None,
        "headers": {"Authorization": "Bearer aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"},
        "items": [
            {"url": "https://alice:s3cretPassword@db.example.com/app"},
            ["sk-QWERTYuiop1234567890asdfghjk", 42],
        ],
        "tuple": ("AKIAIOSFODNN7EXAMPLE",),
    }
    cleaned = redact_obj(payload)
    assert "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345" not in str(cleaned)
    assert "s3cretPassword" not in str(cleaned)
    assert "sk-QWERTYuiop1234567890asdfghjk" not in str(cleaned)
    assert "AKIAIOSFODNN7EXAMPLE" not in str(cleaned)
    # 结构与非字符串值必须原样保留，否则下游解析会炸。
    assert cleaned["ok"] is True
    assert cleaned["attempts"] == 3
    assert cleaned["nothing"] is None
    assert cleaned["items"][1][1] == 42
    assert isinstance(cleaned["tuple"], tuple)
    assert set(cleaned) == set(payload)


def test_redact_obj_keeps_keys_intact():
    """只脱敏值：改键会让 `result["path"]` 直接 KeyError。"""
    assert redact_obj({"path": "C:\\a", "model_api_key": "sk-x"}) == {
        "path": "C:\\a",
        "model_api_key": "sk-x",
    }


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("model_api_key", True),
        ("GITHUB_TOKEN", True),
        ("OPENAI_API_KEY", True),
        ("db_password", True),
        ("client_secret", True),
        ("api-key", True),
        ("access_key_id", True),
        # 不是密钥
        ("monkey", False),
        ("KEYBOARD", False),
        ("max_tokens", False),
        ("password_file", False),
        ("token_path", False),
        ("data_dir", False),
        ("", False),
    ],
)
def test_is_secret_name(name: str, expected: bool):
    assert is_secret_name(name) is expected


def test_redact_is_stable_when_applied_twice():
    """脱敏要幂等：同一条结果既进上下文又进库，可能被处理两次。

    不幂等的话第二次会把指纹再脱敏一次（`hits` 冒出第二类，指纹也被吃掉）。
    """
    once = redact("key=sk-QWERTYuiop1234567890asdfghjk")
    twice = redact(once.text)
    assert twice.text == once.text
    assert twice.hits == []

"""密钥脱敏：在"模型看到之前"和"落库之前"把密钥换成**可辨认但不可复原**的指纹。

## 为什么需要这一层

密钥进入模型的上下文有两条路，两条都是"泄漏即持久化"：

1. **工具结果 → 上下文**。`run_command` 的输出、`local_read_document` 读到的 `.env`、
   HTTP 响应体、`git config` 的回显……里面出现明文密钥是常态，不是异常。一旦进了
   上下文，它就被写进 `messages` 表、被摘要、被索引进 RAG（`policies.py` 的三条落库
   路径），于是"某次读了一个配置文件"会变成"密钥永久留在会话历史与向量库里"。
2. **工具结果 → 落库**。同一条结果还要进 `tool_calls.result`，那是审计与导出的来源
   （`orbit sessions export` / `orbit dump`）。导出文件会被拷走、被贴进 issue。

所以脱敏必须发生在**两个 choke point**，而不是散落在每个工具里：散着写必然漏。

## 接线点（本轮**不改**这两个文件，由集成方接线）

1. `app/tools.py::as_tool_text` —— 模型看到之前：

       def as_tool_text(result: dict) -> str:
           from app.redact import redact_obj
           return json.dumps(redact_obj(result), ensure_ascii=False, default=str)

2. `app/policies.py` 的 `record` —— 落库之前（`DefaultToolResultPolicy.record` 里
   把 `text` 写进 `tool_calls.result` 之前）：

       from app.redact import redact
       text = redact(text).text

**为什么是"指纹"而不是"删除"**：`sk-a…1234` 足够回答"这是哪把钥匙 / 是不是同一把"，
不足以复原。直接抹成 `***` 会让"密钥无效"与"密钥被脱敏"这两件事在日志里长得一样，
排查时无法区分。

**为什么保留首 4 末 4**：真实密钥的前缀（`sk-` / `AKIA` / `ghp_`）本身就是"哪家供应商"
的信息，末 4 位是运维对账时唯一记得住的部分。中间整段丢弃。

## 刻意的取舍（误脱敏 > 漏脱敏）

长十六进制（≥32 位）一律脱敏，代价是 git SHA-1（40 位）、SHA-256 摘要、MD5 也会被
一起遮掉——从**形态上无法区分**"32 位 hex 是密钥还是摘要"，而漏掉一个真密钥的代价
远大于多遮一个哈希。同理 `.env` 风格赋值只对"键名像密钥"的行生效（见
:func:`is_secret_name`），否则 `ORBIT_DATA_DIR=D:\\data` 这种正常配置也会被遮，
那会让脱敏层本身变成噪声源、进而被人关掉。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: 指纹里用的省略号。刻意用单字符 `…` 而不是 `***`：它不可能是密钥的合法字符，
#: 因此"这段被脱敏过"在文本里是自证的。
MASK = "…"

#: 指纹保留的首/末位数。
_HEAD = 4
_TAIL = 4

#: 短于这个长度的密钥不保留任何字符——`head4 + tail4` 会把整个值暴露出来。
_MIN_FOR_FULL_FINGERPRINT = 12

#: 键名里出现这些"词"就认为它是密钥类（见 :func:`is_secret_name`）。
_SECRET_NAME_TOKENS: frozenset[str] = frozenset(
    {
        "KEY",
        "TOKEN",
        "SECRET",
        "SECRETS",
        "PASSWORD",
        "PASSWD",
        "PWD",
        "CREDENTIAL",
        "CREDENTIALS",
        "APIKEY",
        "AUTH",
        "PRIVATEKEY",
        "ACCESSKEY",
    }
)

#: 键名的最后一个词若是这些，说明它是**指向**密钥的元数据（路径 / 开关 / 名字），
#: 不是密钥本身。`KEY_PATH`、`TOKEN_FILE`、`PASSWORD_ENV` 都属于这一类。
#:
#: 刻意**不含** `ID`：`AWS_ACCESS_KEY_ID` 就是凭据本身（虽然比 secret key 弱一档），
#: 把它当元数据会漏掉一整类真实泄漏。
_NAME_TAIL_NOISE: frozenset[str] = frozenset(
    {"PATH", "DIR", "FILE", "URL", "URI", "NAME", "TYPE", "ENV", "COUNT", "LIMIT", "TTL"}
)


def _fingerprint(value: str) -> str:
    """把一段密钥压成 `首 4…末 4` 的指纹。

    三档而不是一刀切，是因为"保留首 4 末 4"在短值上等于没脱敏：
    12 位以下留 8 位就泄露了大半，8 位以下干脆一位都不留。
    """
    if len(value) >= _MIN_FOR_FULL_FINGERPRINT:
        return f"{value[:_HEAD]}{MASK}{value[-_TAIL:]}"
    if len(value) >= 8:
        return f"{value[:2]}{MASK}{value[-2:]}"
    return MASK


@dataclass(frozen=True)
class SecretPattern:
    """一类密钥的形态。

    ``group`` 指向**要脱敏的那一段**：`user:pass@host` 里只有 `pass` 是密钥，
    把整个 URL 换掉会让日志失去"请求打到哪"这个信息。
    """

    #: 稳定的机器可读标识（写进 `hits`，不写原文）。
    kind: str
    #: 中文说明，给人看。
    label: str
    regex: re.Pattern[str]
    #: 取哪一组做替换。0 = 整个匹配。
    group: str | int = 0
    #: 固定替换文本。整块结构（PEM）用它，指纹在那里没有意义。
    replacement: str | None = None
    #: 二次判据：返回 False 则这次匹配不算命中（用于 `.env` 的键名白名单）。
    guard: Callable[[re.Match[str]], bool] | None = None


def is_secret_name(name: str) -> bool:
    """判断一个**配置键名**是不是密钥类（`*_KEY` / `*_TOKEN` / `PASSWORD` …）。

    实现按"分词后看有没有密钥词"而不是"看后缀"：`monkey` 以 `key` 结尾但不是密钥，
    `KEYBOARD` 同理。分词还顺手解决了 `MODEL_API_KEY`（三个词，命中 `KEY`）与
    `MAX_TOKENS`（`TOKENS` ≠ `TOKEN`，不命中）的区别。

    最后一步排除"指向密钥的元数据"：`KEY_PATH` / `TOKEN_FILE` / `PASSWORD_ENV`
    是**指向**密钥的路径或变量名，本身不是秘密，遮掉它们只会让配置不可读。
    """
    if not name:
        return False
    # 非字母数字一律当分隔符：`model.api-key` / `MODEL-API-KEY` / `model_api_key` 同解。
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", name.upper()) if token]
    if not tokens:
        return False
    if tokens[-1] in _NAME_TAIL_NOISE:
        return False
    return any(token in _SECRET_NAME_TOKENS for token in tokens)


def _dotenv_guard(match: re.Match[str]) -> bool:
    """`.env` 赋值行只在**键名像密钥**时才算命中。"""
    return is_secret_name(match.group("name"))


#: 全部模式。**顺序有意义**：先具体、后笼统。
#:
#: 例：`OPENAI_API_KEY=sk-…` 应当记成 `openai_key`（"这是 OpenAI 的钥匙"比
#: "这是一行 .env"有用得多），所以 `sk-` 排在 `.env` 赋值之前；长十六进制排在最后，
#: 否则 `sk-<32 位 hex>` 会先被它吃掉。
PATTERNS: tuple[SecretPattern, ...] = (
    # ── 各家供应商的固定前缀 token ────────────────────────────────────────────
    SecretPattern(
        "anthropic_key",
        "Anthropic API key（sk-ant- 前缀）",
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    ),
    SecretPattern(
        "private_key_block",
        "PEM 私钥块（-----BEGIN … PRIVATE KEY-----）",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?"
            r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
        ),
        # 整块结构没有"首末"可言，指纹会变成 `----…----` 这种废话；给固定标记。
        replacement="-----BEGIN PRIVATE KEY-----…（已脱敏）-----END PRIVATE KEY-----",
    ),
    SecretPattern(
        "openai_key",
        "OpenAI / DeepSeek 风格 API key（sk- 前缀）",
        # `(?!ant-)` 让 Anthropic 的钥匙不被这里抢走（虽然它排在前面，但两条都可能
        # 命中同一段文本时，早的那条已经改写了文本；留着是为了模式自身可独立理解）。
        re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_\-]{20,}"),
    ),
    SecretPattern(
        "aws_access_key",
        "AWS 访问密钥 ID（AKIA / ASIA 前缀）",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    SecretPattern(
        "github_token",
        "GitHub token（ghp_/gho_/ghu_/ghs_/ghr_ 前缀）",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    ),
    SecretPattern(
        "github_pat",
        "GitHub 细粒度 token（github_pat_ 前缀）",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    SecretPattern(
        "gitlab_token",
        "GitLab token（glpat- 前缀）",
        re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    ),
    SecretPattern(
        "slack_token",
        "Slack token（xoxb/xoxa/xoxp/xoxr/xoxs 前缀）",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    ),
    SecretPattern(
        "google_api_key",
        "Google API key（AIza 前缀）",
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ),
    SecretPattern(
        "stripe_key",
        "Stripe 密钥（sk_live_ / rk_live_ 前缀）",
        re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}\b"),
    ),
    SecretPattern(
        "jwt",
        "JWT（三段 base64url，以 eyJ 开头）",
        re.compile(
            r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"
        ),
    ),
    # ── 结构化容器里的密钥 ────────────────────────────────────────────────────
    SecretPattern(
        "bearer_token",
        "HTTP Authorization: Bearer 头",
        re.compile(r"(?i)\bBearer\s+(?P<secret>[A-Za-z0-9._\-+/=]{16,})"),
        group="secret",
    ),
    SecretPattern(
        "url_userinfo",
        "URL 里内嵌的 user:password@",
        # 只换密码段：协议、用户名、主机名是排查要用的信息，不是秘密。
        re.compile(
            r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^:/@\s]{0,64}):"
            r"(?P<secret>[^:/@\s]{4,})@"
        ),
        group="secret",
    ),
    SecretPattern(
        "conn_string_password",
        "连接串密码（Password=… / pwd=…）",
        # 前面的 `\b` 让 `DB_PASSWORD=` 不被这里吃掉（`_` 是词字符，没有边界），
        # 那种形态交给下一条 `.env` 赋值模式。
        re.compile(r"(?i)\b(?:password|passwd|pwd|pass)\s*=\s*(?P<secret>[^\s;'\",]{4,})"),
        group="secret",
    ),
    SecretPattern(
        "dotenv_assignment",
        ".env 风格赋值（SECRET_NAME=value）",
        re.compile(
            r"(?m)^[ \t]*(?:export[ \t]+)?(?P<name>[A-Za-z][A-Za-z0-9_]{2,})"
            r"[ \t]*=[ \t]*(?P<secret>[^\s#]{4,})"
        ),
        group="secret",
        guard=_dotenv_guard,
    ),
    # ── 兜底：形态上像密钥的长十六进制 ────────────────────────────────────────
    SecretPattern(
        "long_hex",
        "长十六进制串（≥32 位，可能是密钥或摘要）",
        re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    ),
)


@dataclass
class RedactResult:
    """脱敏结果。**`hits` 只记类别与次数，绝不记原文。**"""

    text: str
    hits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def kinds(self) -> list[str]:
        return [str(hit["kind"]) for hit in self.hits]

    @property
    def count(self) -> int:
        return sum(int(hit["count"]) for hit in self.hits)

    def __bool__(self) -> bool:
        """有命中即真——调用方写 `if redact(text):` 就是"这段里有密钥"。"""
        return bool(self.hits)


def redact(text: str) -> RedactResult:
    """把文本里所有命中的密钥换成指纹。

    逐类 `re.sub`（而不是一次扫描求并集）是为了让**先具体后笼统**的顺序真正生效，
    也让 `hits` 能按"第一次出现的顺序"排列——读起来就是文本里从上到下的顺序。
    """
    if not text:
        return RedactResult(text or "", [])

    counts: dict[str, int] = {}
    order: list[str] = []
    out = text

    for pattern in PATTERNS:
        def _replace(match: re.Match[str], pattern: SecretPattern = pattern) -> str:
            if pattern.guard is not None and not pattern.guard(match):
                return match.group(0)
            if pattern.group == 0:
                value = match.group(0)
            else:
                value = match.group(pattern.group)
            if not value or MASK in value:
                # 已经是指纹（或空值）：跳过。否则后面的笼统模式会把前一条打好的
                # 指纹再脱敏一次，`hits` 里就会出现两类、指纹本身也被吃掉。
                return match.group(0)
            counts[pattern.kind] = counts.get(pattern.kind, 0) + 1
            if pattern.kind not in order:
                order.append(pattern.kind)
            masked = pattern.replacement if pattern.replacement is not None else _fingerprint(value)
            if pattern.group == 0:
                return masked
            # 只替换捕获组那一段，前后的上下文原样保留。
            whole = match.group(0)
            start, end = match.span(pattern.group)
            offset = match.start()
            return whole[: start - offset] + masked + whole[end - offset :]

        out = pattern.regex.sub(_replace, out)

    return RedactResult(out, [{"kind": kind, "count": counts[kind]} for kind in order])


def redact_obj(obj: Any) -> Any:
    """递归脱敏 `dict` / `list` / `tuple` / `str`。

    工具结果是嵌套 JSON，密钥可能埋在第 4 层的 `headers` 里，只处理顶层等于没做。

    **只脱敏值、不脱敏键**：键名是结构，改键会让下游 `result["path"]` 这类取值
    直接 KeyError——脱敏层不该有这种副作用。（键名里塞密钥是极罕见的形态，且
    `mcp.json` 那种"键是变量名、值是密钥"的布局恰好是值侧覆盖的。）
    """
    if isinstance(obj, str):
        return redact(obj).text
    if isinstance(obj, dict):
        return {key: redact_obj(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(item) for item in obj)
    return obj

# Hermes → Orbit 功能对标 · 导航

**任务**：照着 Hermes 完成 Orbit，包括 CLI 工具与 Hermes 的所有层。
**完成标志**：Hermes 有的功能 Orbit 都有，且每项有可复核证据。

## 先读这三份（按顺序）

| 文件 | 作用 | 什么时候读 |
| --- | --- | --- |
| `ROADMAP.md` | 15 个波次的目标、验收判据、已知边界 | 接手时先读，决定这一轮做什么 |
| `MATRIX.md` | **主清单**：180+ 条，逐条给 Hermes 依据 / 波次 / 状态 / 证据 | 每轮开始与结束各看一次 |
| `PROGRESS.md` | 监视子agent 的抽查报告（整份重写，含"文档声称完成但查无实据"） | 每轮结束前核对 |

## 调研产物 `research/`

| 文件 | 内容 | 关键数字 |
| --- | --- | --- |
| `01-cli-surface.md` | Hermes 全部命令面 | 74 顶层 / 295 二级 / 36 三级 = **408 节点** / 23 全局 flag |
| `02-tool-layer.md` | 工具层与注册表机制 | **94** 内置工具 / 19 功能域 / 32 toolset |
| `03-persistence-layer.md` | 持久化与会话 | 6 库 **62 表**（state.db 26 业务表）/ 14,595 条消息 |
| `04-skill-layer.md` | Skill 与 curator | **21 类目 / 91 技能** / frontmatter 16 字段 |
| `05-plugin-gateway-layer.md` | 插件与网关 | 18 组 / **103 provider** / **22 平台** / 19 种 register_* |
| `06-model-layer.md` | 模型适配 | 39 目录 → **48 profile** / 5 transport / config 43 键 |
| `07-memory-layer.md` | 记忆层 | MEMORY.md 2,200 字符 / USER.md 1,375 / 8 provider |
| `08-orbit-current-state.md` | **调研时的 Orbit 快照**（**不是现状**） | 16 层成熟度 / 28 工具 / 24 表 / 15 条「声明了没接线」—— **这是第 1 轮的样子；现状看 `MATRIX.md`** |
| `09-core-loop-layer.md` | 核心循环与上下文 | 三层 prompt / 预算与心跳 / 压缩谱系 / UI 入口 |

`research/_raw/` 是**原始证据**：408 次 `--help` 的逐字输出（`cmd-*.txt`、`sub/`、`sub3/`、
`sub-digest.txt`、`sub3-digest.txt`）。抄命令面时以它为准，不要凭记忆。
`research/_tools/` 是各库的 schema 转储与 CLI help 快照。

## 实现现状

```
backend/app/cli/            # W1 建成的 CLI 层
├── spec.py                 #   命令注册表（唯一真相源）
├── shorthand.py            #   选项短字符串 → Option（"‑‑name NAME" / "{a,b}" / "[x]"）
├── context.py              #   运行时上下文：配置水合、异步桥、表格/JSON 输出
├── main.py                 #   argparse 编译 + 分发 + 统一错误出口
└── commands/
    ├── core.py             #   status doctor dump logs prompt-size completion config{9 子命令}
    ├── _table.py           #   声明式命令表 → Command（未实现命令的登记器）
    └── hermes_parity.py    #   全量命令面转录（W2）
scripts/orbit.cmd|ps1       # 启动器（本机无网络装不了包，直接跑源码，等价于装好的 orbit）
scripts/parity_report.py    # 从注册表一键生成 CLI 对标矩阵
backend/tests/test_cli.py   # 命令面契约测试
```

**怎么跑**：`E:\bbbooottt\scripts\orbit.cmd status`（或把 `scripts` 加进 PATH 后 `orbit status`）。
开发时：`cd backend; .\.venv\Scripts\python.exe -m app.cli <命令>`。

**怎么重算对标矩阵**：`cd backend; .\.venv\Scripts\python.exe ..\scripts\parity_report.py --out ..\handoff\hermes-parity\cli-matrix.md`

## 协作规矩（这几条是踩过坑之后定的）

1. **`state` 不许虚报**。第一轮把 7 个命令全标 `done`，被监视子agent 逐条对照 Hermes
   `--help` 抓出 6 个是子集（缺选项/缺子命令）。现在 `done` 的含义是**选项面逐条对齐**，
   不是"能跑"。
2. **命令面以 `research/_raw/` 为准**，不凭记忆、不信前作转述。本轮已修正三处转述错误：
   技能类目 26→**21**、平台 20→**22**、`_DO_NOT_CAPTURE_BLOCK` 根本**不存在**。
3. **不假装支持**。做不到的能力（如 OAuth 登录、bedrock transport、MoA 运行时聚合）
   必须抛清晰异常并在 MATRIX 里如实标 ⬜/🟡，不许留空壳。
4. **只读 Hermes**。安装目录与 `hermes-home` 零写入；exe 只跑 `--help`；sqlite 只
   `mode=ro&immutable=1`。research 文件里的结论因此有一部分标【推断】。
5. **每波结束必须**：`pytest` 绿（`test_connectors.py::test_browser_append_and_reload`
   是既有失败，忽略）、`ruff` / `mypy` clean、MATRIX 状态更新、提交 git。
6. **本机无网络、无 setuptools**：不能 `uv pip install`、不能加新依赖。所有实现必须落在
   已装依赖（`httpx` / `pydantic` / `langchain-openai` / `keyring` / `sqlalchemy` / `playwright`）与标准库上。

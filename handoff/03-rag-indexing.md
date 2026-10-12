# 方向 3 · RAG 写入侧：chunks 表与 FTS5 索引

**先读 `handoff/00-CONTEXT.md`。**

## 目标

让"网页内容 + 工具结果"可被检索。建 `chunks` 表 + SQLite **FTS5(trigram)** 索引，在 `digest` 之后异步写入。**本阶段不上向量、不装 embedding**（那是方向 6）。

## 已定决策

- `chunks(id, source_type, source_id, host, ordinal, text, tokens, hash, origin, title_path, created_at)`
- `source_type`：`web_page`（网页观察正文）/ `tool_result`（超长工具结果）/ `local_file` / `skill`（后期）。**`origin` 是硬要求**（URL/文件路径/消息 id），无出处不入索引
- 切块规则（结构优先、定长兜底，三层）：①结构切（markdown 标题、HTML p/li/tr、空行）②合并到 300–500 中文字，超 512 token 在句子边界再切 ③overlap 50–100 字
- 特殊：表格**一条数据行 + 表头独立成一块**（表头必带；只有表头的表整表一块）——原「整表 ≤500 字一块 / 大表按行组切」于 2026-10-09 废除，实测行级切块 A/B 双优（A 0.625→0.688、B 0.917→1.000，四档粒度对照见 `scripts/diag_table_split.py`）；块首拼标题路径（`H1 > H2`）；hash 去重；相邻块相似度 >0.9 合并
- 索引写入**异步**（`asyncio.to_thread` 或队列），不阻塞工具返回；失败只记日志（尤其 MCP resources 来源，方向 7 会接）
- 索引器做成 `Indexer` 协议（FTS5 实现 + 未来向量实现并列），不写死

## 涉及文件

`backend/app/indexing.py`（新：切块器 + FTS5 写入/查询）、`backend/app/graph/nodes.py`（digest 后挂写入）、`backend/migrations/versions/0004_chunks.py`（新）、`backend/tests/test_indexing.py`（新）

## FTS5 注意

- 建表用 `CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='chunks', content_rowid='rowid', tokenize='trigram')`
- trigram 分词对中文子串检索可用；查 SQL 时 `MATCH` 配 `rank`（BM25）排序
- WAL 模式已开（`Store.tune()`），读写并发没问题

## 验收

1. 真实跑一次 B 站任务后，`chunks` 表能查到搜索结果页内容，`origin` 指向正确 URL
2. 用 `select ... from chunks_fts where chunks_fts match '机械键盘' order by rank` 能命中
3. 同一页面重复观察不产生重复块（hash 判重）
4. 全量测试与评估集不回归

## 禁区

不做向量、不做 rerank、不做查询改写（后续方向）；不索引截图与密码；不索引被拒绝的操作。

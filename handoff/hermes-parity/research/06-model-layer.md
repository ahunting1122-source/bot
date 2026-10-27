# 06 · Hermes 模型适配层 / Provider 层完整清单

> 目标：把 Hermes Agent CN（runtime `0.21.0-cn.18`）的**模型适配层**拆到可实现粒度，作为 Orbit 模型层的 parity 基线。
> 全部结论均来自本机磁盘文件与 `--help` 输出，**未做任何写入**，**未读取任何 api_key 明文**（一律 `sk-***`）。

---

## 0. 结论摘要

| 指标 | 数值 |
|---|---|
| Provider **插件目录**数 | **39**（`_internal/plugins/model-providers/`） |
| 注册的 **ProviderProfile** 数 | **48**（6 个目录各注册多个 profile） |
| `api_mode` / transport 类型数 | **5**：`chat_completions` / `anthropic_messages` / `codex_responses` / `bedrock_converse` / `copilot_acp` |
| 鉴权类型数 | **7**：`api_key` / `oauth_external` / `oauth_device_code` / `copilot` / `aws_sdk` / `external_process` / `keyless` |
| `config.yaml` **顶层键**数 | **43**（41 个 `config get` 实测 + `fallback_model` + `_config_version`） |
| 用户 `config.yaml` 实际出现的顶层键 | **9** 活动 + **2** 注释示例 = **11** |
| `models.dev` 能力元数据缓存 | 232 provider / **8947** 模型 |
| 辅助模型 slot 数 | **18**（`auxiliary.*`）+ 2 个 MoA slot 族 |
| TTS provider / STT provider | **11** / **8**（内置）+ 无限 command-provider |

**一句话**：Hermes 的模型层不是"一个客户端"，而是**三张正交的表**——
① `plugins/model-providers/*` 声明 **provider 身份**（base_url / env / auth_type / api_mode / 模型清单来源）；
② `api_mode` 决定**线协议**（chat_completions、anthropic_messages、codex_responses、bedrock_converse、ACP 子进程）；
③ `config.yaml` 的 `model.* / providers.* / auxiliary.* / moa.* / fallback_model` 决定**运行时选谁、用哪个便宜模型兜底、失败后谁接班**。

---

## 1. 证据与方法（可复现）

### 1.1 读取的路径

| 用途 | 路径 |
|---|---|
| Provider 插件（源码，可读） | `E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18\_internal\plugins\model-providers\<id>\{plugin.yaml,__init__.py}` |
| 工具层源码 | `...\_internal\tools\`（`tts_tool.py` / `tts_streaming.py` / `vision_tools.py` / `transcription_tools.py` / `image_source.py` / `openrouter_client.py` / `xai_http.py`） |
| 模型能力元数据快照 | `...\_internal\agent\models_dev_snapshot.json`（3.27 MB） |
| 运行时 exe | `...\0.21.0-cn.18\hermes-agent-cn-runtime-win32-x64.exe`（75 MB，PyInstaller onedir） |
| 用户配置（只读） | `E:\Hermes Agent CN Desktop\data\hermes-home\{config.yaml, models_dev_cache.json, provider_models_cache.json, auth.json, .env}` |

### 1.2 关键限制（影响证据强度）

- **`providers` / `hermes_cli` / `agent` 的 Python 源码被打包在 exe 的 PYZ 里**（zlib 压缩），磁盘上不可读：`_internal/` 下只有 `agent/models_dev_snapshot.json` 和 `hermes_cli/web_dist/`（前端产物），`providers/` 包不存在于文件系统。
  → `ProviderProfile` 基类的**字段清单**是从 48 个 profile 的**调用点**反推的（见 §2.1），字段语义标注为"由调用点推定"。
- 因此 `ProviderProfile` 的**默认值**（如 `api_mode` 缺省 = `chat_completions`）无法从源码直读，本文以"未设置该 kwarg 的 profile 一律走 OpenAI 兼容 chat completions"这一**跨 profile 一致行为**为据。
- exe 仅允许 `--help`。为补齐 config schema，额外使用了 `config get <key>`（**纯只读**，返回内置默认树或 `Config key not set`）——这是唯一能拿到完整默认配置的手段，无写入。
- 模型数取自 `models_dev_cache.json`（models.dev 目录，2026 年快照），是**目录模型数**，不等于各家 `/v1/models` 实时数；标 `live` 者表示该 provider 无目录条目、只能实时拉取。

---

## 2. 分层架构

```
┌─ plugins/model-providers/<dir>/plugin.yaml      ← 清单：name / kind: model-provider / version / description / author
│
├─ plugins/model-providers/<dir>/__init__.py      ← 构造 ProviderProfile(...) 并 register_provider(profile)
│                                                   （一个文件可注册多个 profile，如 alibaba 注册 4 个）
│
├─ ProviderProfile 字段（由调用点反推）
│   name, aliases, display_name, description, signup_url,
│   env_vars, base_url, models_url, auth_type, api_mode,
│   default_headers, default_aux_model, default_max_tokens,
│   fallback_models, fixed_temperature, supports_vision,
│   supports_vision_tool_messages, supports_health_check
│
├─ ProviderProfile 钩子（子类 override，按需）
│   fetch_models()                 实时拉模型清单（各家 /models 形态不同）
│   build_extra_body()             extra_body 组装（Gemini thinking_config / Qwen vl_high_resolution / Nous tags）
│   build_api_kwargs_extras()      → (extra_body, top_level) 二元组；**推理参数落点差异全在这里**
│   prepare_messages()             消息规范化（NVIDIA 去 tool.name / Qwen content→list + cache_control）
│   resolve_aux_model()            辅助模型解析（Nous 走 Portal 推荐接口）
│   default_vision_model()         vision 默认模型（DeepInfra 走目录 tag）
│   supported_reasoning_efforts()  catalog 声明的 effort 词表（Router）
│   get_max_tokens()               按模型覆盖 max_tokens（OpenCode Go: mimo-v2.5-pro→131072）
│
└─ 运行时：config.yaml(model.*, providers.*) → auth.py / credential_pool → runtime_provider.py
           → 按 api_mode 选 transport → 模型能力查询走 model_metadata.py ← models_dev_cache.json
```

### 2.1 `api_mode` → 线协议映射（核心 parity 表）

| `api_mode` | 实际协议 | 使用它的 profile | 关键差异 |
|---|---|---|---|
| `chat_completions` | OpenAI `/v1/chat/completions`（缺省值） | 绝大多数（含 gemini，但 gemini 用**自建 native client** 绕过标准 transport） | 推理参数各家自己拼 `extra_body` |
| `anthropic_messages` | Anthropic Messages `/v1/messages` | `anthropic`、`minimax`、`minimax-cn`、`minimax-oauth`、`commandcode-anthropic` | 原生用 `x-api-key`；CommandCode 的 `/anthropic` 用 `Authorization: Bearer`（`agent/anthropic_adapter.py` 按 hostname `api.commandcode.ai` 特判） |
| `codex_responses` | OpenAI **Responses** `/v1/responses` | `xai`、`openai-codex`、`actual`、`meta-ai`、`router` | 有 per-model reasoning-effort 校验、reasoning summary、prompt caching、`reasoning.encrypted_content` 回放 |
| `bedrock_converse` | AWS Bedrock Converse（boto3 SDK） | `bedrock` | 无 REST `/v1/models`，模型枚举必须走 SDK |
| `copilot_acp` | 外部 ACP 子进程（`acp://copilot`） | `copilot-acp` | 不是 HTTP；`run_agent.py` 单独处理 |

> ⚠️ **重要**：`copilot`、`opencode-zen`、`opencode-go`、`commandcode` 这几个是**按模型动态路由 api_mode**的（Claude→anthropic_messages，GPT-5/Codex/Grok→codex_responses，其余→chat_completions）。profile 里写的 `api_mode` 只是"兜底子集"。这层 per-model 路由在 `hermes_cli/providers.py`（`host_mandated_api_mode`）与 `hermes_cli/models.py` 里，源码不可读。

---

## 3. Provider 清单表（39 目录 / 48 profile）

### 3.1 主表

列说明：
- **transport** = profile 的 `api_mode`（未设 → `chat_completions`）。
- **base_url 来源** = `固定`（profile 内硬编码）/ `固定+env覆盖`（另有 `*_BASE_URL` 环境变量）/ `用户填`（setup 时手填）/ `env 函数`（模块级 `_base_url()` 读 env）。
- **模型数** = `models.dev` 目录模型数；`live` = 无目录条目，只能实时 `/v1/models`；`—` = 不适用。
- **tools/reasoning/vision** = 该 provider 在目录中具备该能力的模型数（`n/N`），来自 `models_dev_cache.json` 聚合；profile 级 `supports_vision` 另行标注。

| # | id | 显示名 | transport (api_mode) | base_url | base_url 来源 | 鉴权 | 需要的 env 变量 | 模型数（目录 / 回退） | 代表模型 | tools | reasoning | vision |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `actual` | Actual Computer | `codex_responses` | `https://api.actual.inc/v1` | 固定+env覆盖（`ACTUAL_BASE_URL`，本地模式→`http://127.0.0.1:8080/v1`） | `api_key` | `ACTUAL_API_KEY`, `ACTUAL_BASE_URL` | live / 0 | 由 `/v1/models` 决定 | live | live | live |
| 2 | `ai-gateway` | Vercel AI Gateway（未设 display_name，显示 `ai-gateway`） | `chat_completions` | `https://ai-gateway.vercel.sh/v1` | 固定 | `api_key` | `AI_GATEWAY_API_KEY` | **418** / 0 | `google/gemini-3.5-flash`、`google/gemini-4-argon`；aux 默认 `google/gemini-3-flash` | 253/418 | 225/418 | 214/418 |
| 3 | `alibaba` | Alibaba DashScope（国际） | `chat_completions` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | 固定 | `api_key` | `DASHSCOPE_API_KEY` | **53** / 0 | `qwen-max-latest`、`qwen3-max-2025-09-23` | 47/53 | 34/53 | 29/53 |
| 4 | `alibaba-cn` | Alibaba Cloud DashScope (China) | `chat_completions` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 固定+env覆盖（`DASHSCOPE_CN_BASE_URL`） | `api_key` | `DASHSCOPE_API_KEY`, `DASHSCOPE_CN_BASE_URL` | **80** / 0 | `qwen3-235b-a22b-instruct-2507`、`qwen-flash` | 71/80 | 52/80 | 27/80 |
| 5 | `alibaba-token-plan` | Alibaba Cloud (Token Plan) | `chat_completions` | `https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` | 固定+env覆盖 | `api_key` | `ALIBABA_TOKEN_PLAN_API_KEY`, `ALIBABA_TOKEN_PLAN_BASE_URL` | **29** / 0 | 同 DashScope 模型族 | 22/29 | 22/29 | 13/29 |
| 6 | `alibaba-token-plan-cn` | Alibaba Cloud (Token Plan, China) | `chat_completions` | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | 固定+env覆盖 | `api_key` | `ALIBABA_TOKEN_PLAN_API_KEY`, `ALIBABA_TOKEN_PLAN_CN_BASE_URL` | **29** / 0 | 同上 | 22/29 | 22/29 | 13/29 |
| 7 | `alibaba-coding-plan` | Alibaba Cloud (Coding Plan) | `chat_completions` | `https://coding-intl.dashscope.aliyuncs.com/v1` | 固定+env覆盖 | `api_key` | `ALIBABA_CODING_PLAN_API_KEY`, `DASHSCOPE_API_KEY`, `ALIBABA_CODING_PLAN_BASE_URL` | **10** / 0 | qwen-coder 族 | 10/10 | 7/10 | 4/10 |
| 8 | `alibaba-coding-plan-cn` | Alibaba Cloud (Coding Plan, China) | `chat_completions` | `https://coding.dashscope.aliyuncs.com/v1` | 固定+env覆盖 | `api_key` | `ALIBABA_CODING_PLAN_API_KEY`, `DASHSCOPE_API_KEY`, `ALIBABA_CODING_PLAN_CN_BASE_URL` | **10** / 0 | 同上 | 10/10 | 7/10 | 4/10 |
| 9 | `anthropic` | Anthropic (Claude) | `anthropic_messages` | `https://api.anthropic.com` | 固定 | `api_key`（**`x-api-key` + `anthropic-version: 2023-06-01`**，非 Bearer） | `ANTHROPIC_API_KEY`, `ANTHROPIC_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` | **17** / 0 | `claude-opus-5`、`claude-sonnet-5`；aux `claude-haiku-4-5-20251001` | 17/17 | 17/17 | 17/17 |
| 10 | `arcee` | Arcee AI | `chat_completions` | `https://api.arcee.ai/api/v1` | 固定 | `api_key` | `ARCEEAI_API_KEY`, `ARCEE_BASE_URL` | **11** / 0 | Arcee AFM 族 | 11/11 | 11/11 | 3/11 |
| 11 | `azure-foundry` | Azure Foundry | `chat_completions` | **空**（per-resource，setup 时用户填） | 用户填 | `api_key` | `AZURE_FOUNDRY_API_KEY`, `AZURE_FOUNDRY_BASE_URL` | **96** / 0 | Azure 部署名 | 77/96 | 62/96 | 72/96 |
| 12 | `bedrock` | AWS Bedrock | `bedrock_converse` | `https://bedrock-runtime.us-east-1.amazonaws.com` | 固定 | **`aws_sdk`**（IAM / boto3，**无 env var**） | `AWS_REGION`, `AWS_PROFILE`（SDK 侧） | **200** / 0 | Anthropic/Titan/Nova on Bedrock | 195/200 | 155/200 | 153/200 |
| 13 | `commandcode` | CommandCode | `chat_completions` | `https://api.commandcode.ai/provider/v1` | 固定+env覆盖（`COMMANDCODE_BASE_URL`） | `api_key` | `COMMANDCODE_API_KEY`, `COMMANDCODE_BASE_URL` | **72** / **11** | `deepseek/deepseek-v4-pro`、`Qwen/Qwen3.7-Max`、`moonshotai/Kimi-K2.6`、`zai-org/GLM-5.1`；aux `deepseek/deepseek-v4-flash` | 72/72 | 72/72 | 59/72 |
| 14 | `commandcode-anthropic` | CommandCode (Anthropic) | `anthropic_messages` | `https://api.commandcode.ai/provider/v1` | 固定+env覆盖（`COMMANDCODE_ANTHROPIC_BASE_URL`） | `api_key`（**Bearer**，非 `x-api-key`） | `COMMANDCODE_API_KEY`, `COMMANDCODE_ANTHROPIC_BASE_URL` | live（过滤 `claude-*`）/ **3** | `claude-sonnet-4-6`、`claude-opus-4-7`、`claude-haiku-4-5-20251001` | live | live | live |
| 15 | `copilot` | GitHub Copilot / GitHub Models | `chat_completions`（**per-model 路由**：GPT-5+/Codex→codex_responses，Claude→anthropic_messages） | `https://api.githubcopilot.com` | 固定 | **`copilot`**（GitHub token 换 Copilot token） | `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` | **36** / 0 | `gpt-5.5`、`gpt-5.4`（含 `xhigh` effort） | 36/36 | 36/36 | 36/36 |
| 16 | `copilot-acp` | GitHub Copilot (ACP) | `copilot_acp`（profile 里写 `chat_completions`，实际由 `run_agent.py` 特判） | `acp://copilot`（内部 scheme） | — | **`external_process`** | 无（子进程托管） | — / 0 | 由 ACP 子进程决定 | — | — | — |
| 17 | `custom` | Custom / Ollama / 本地 OpenAI 兼容 | `chat_completions` | **空**（用户填） | 用户填 | 无固定 key（`env_vars=()`） | 无（用户自定义） | live / 0 | Ollama / vLLM / llama.cpp / LM Studio / GLM on ARK | live | live | live |
| 18 | `deepinfra` | DeepInfra | `chat_completions` | `https://api.deepinfra.com/v1/openai` | 固定+env覆盖 | `api_key` | `DEEPINFRA_API_KEY`, `DEEPINFRA_BASE_URL` | **77** / 0（**故意留空**） | `deepseek-ai/DeepSeek-V4-Flash`（aux）；vision 默认从目录 `chat`+`vision` tag 动态选 | 74/77 | 58/77 | 42/77 |
| 19 | `deepseek` | DeepSeek | `chat_completions` | `https://api.deepseek.com/v1` | 固定+env覆盖 | `api_key` | `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` | **4** / **2** | `deepseek-v4-pro`、`deepseek-v4-flash`（aux 同 flash） | 4/4 | 4/4 | 3/4 |
| 20 | `fireworks` | Fireworks AI | `chat_completions` | `https://api.fireworks.ai/inference/v1` | 固定 | `api_key` | `FIREWORKS_API_KEY` | **29** / **3** | `accounts/fireworks/models/kimi-k3`、`.../glm-5p2`；aux `accounts/fireworks/models/glm-5p2` | 29/29 | 29/29 | 18/29 |
| 21 | `gemini` | Google Gemini（AI Studio） | `chat_completions`（**但走自建 `GeminiNativeClient`**，绕过标准 transport） | `https://generativelanguage.googleapis.com/v1beta` | 固定+env覆盖 | `api_key` | `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `GEMINI_BASE_URL` | **38** / 0 | `gemini-4-argon`、`gemini-3.5-flash`；aux `gemini-3.6-flash` | 24/38 | 30/38 | 33/38 |
| 22 | `gmi` | GMI Cloud | `chat_completions` | `https://api.gmi-serving.com/v1` | 固定+env覆盖 | `api_key` | `GMI_API_KEY`, `GMI_BASE_URL` | **17** / **7** | `zai-org/GLM-5.1-FP8`、`moonshotai/Kimi-K2.5`、`openai/gpt-5.4`；aux `google/gemini-3.1-flash-lite-preview` | 17/17 | 17/17 | 8/17 |
| 23 | `huggingface` | HuggingFace | `chat_completions` | `https://router.huggingface.co/v1` | 固定+env覆盖 | `api_key`（HF token） | `HF_TOKEN`, `HF_BASE_URL` | **82** / **2** | `Qwen/Qwen3.5-72B-Instruct`、`deepseek-ai/DeepSeek-V3.2` | 78/82 | 61/82 | 33/82 |
| 24 | `kilocode` | Kilo Code | `chat_completions` | `https://api.kilo.ai/api/gateway` | 固定 | `api_key` | `KILOCODE_API_KEY` | **396** / 0 | 聚合网关；aux `google/gemini-3.6-flash` | 330/396 | 283/396 | 244/396 |
| 25 | `kimi-coding` | Kimi / Moonshot（国际） | `chat_completions` | `https://api.moonshot.ai/v1`（`sk-kimi-*` key 另有 `https://api.kimi.com/coding` 的 Anthropic 面） | 固定+env覆盖 | `api_key` | `KIMI_API_KEY`, `KIMI_CODING_API_KEY`, `KIMI_BASE_URL` | **6** / 0 | `kimi-k2-turbo-preview`（aux）、`kimi-k3`、`kimi-k2.5` | 6/6 | 6/6 | 6/6 |
| 26 | `kimi-coding-cn` | Kimi / Moonshot（中国） | `chat_completions` | `https://api.moonshot.cn/v1` | 固定+env覆盖 | `api_key` | `KIMI_CN_API_KEY` | **6** / 0 | 同上 | 6/6 | 6/6 | 6/6 |
| 27 | `meta-ai` | Meta Model API（Muse Spark） | `codex_responses` | `https://api.meta.ai/v1` | env 函数（`META_BASE_URL`） | `api_key` | `MODEL_API_KEY`, `META_API_KEY`, `META_MODEL_API_KEY`, `META_BASE_URL` | **6** / **2** | `muse-spark-1.2`、`muse-spark-1.2-contributor`（aux） | 6/6 | 6/6 | 6/6（`supports_vision=True`） |
| 28 | `minimax` | MiniMax（国际） | `anthropic_messages` | `https://api.minimax.io/anthropic` | 固定+env覆盖 | `api_key` | `MINIMAX_API_KEY`, `MINIMAX_BASE_URL` | **8** / 0 | `MiniMax-M3`（aux）、`MiniMax-M2.7` | 8/8 | 8/8 | 2/8 |
| 29 | `minimax-cn` | MiniMax（中国） | `anthropic_messages` | `https://api.minimaxi.com/anthropic` | 固定+env覆盖 | `api_key` | `MINIMAX_CN_API_KEY`, `MINIMAX_CN_BASE_URL` | **8** / 0 | 同上 | 8/8 | 8/8 | 2/8 |
| 30 | `minimax-oauth` | MiniMax (OAuth) | `anthropic_messages` | `https://api.minimax.io/anthropic` | 固定 | **`oauth_external`**（token 在 `auth.json`，**无 env**） | 无（`env_vars=()`） | live / 0 | `MiniMax-M2.7`（aux） | live | live | live |
| 31 | `nebius-token-factory` | Nebius Token Factory | `chat_completions` | `https://api.tokenfactory.nebius.com/v1` | 固定+env覆盖 | `api_key` | `NEBIUS_API_KEY`, `NEBIUS_TOKEN_FACTORY_API_KEY`, `NEBIUS_BASE_URL` | **26** / **9** | `NousResearch/Hermes-4-70B`、`deepseek-ai/DeepSeek-V4-Pro`、`Qwen/Qwen3.5-397B-A17B-fast`；aux `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | 25/26 | 22/26 | 9/26 |
| 32 | `nous` | Nous Research（Portal） | `chat_completions` | `https://inference-api.nousresearch.com/v1` | 固定+env覆盖（`NOUS_BASE_URL`） | **`oauth_device_code`** | `NOUS_API_KEY`, `NOUS_BASE_URL` | live / **2** | `hermes-3-405b`、`hermes-3-70b`；aux **走 Portal `/api/nous/recommended-models` 动态推荐** | live | live | live |
| 33 | `novita` | NovitaAI | `chat_completions` | `https://api.novita.ai/openai/v1` | 固定+env覆盖 | `api_key` | `NOVITA_API_KEY`, `NOVITA_BASE_URL` | **89** / **6** | `moonshotai/kimi-k2.5`、`zai-org/glm-5`、`deepseek/deepseek-r1-0528`；aux `deepseek/deepseek-v3-0324` | 70/89 | 66/89 | 39/89 |
| 34 | `nvidia` | NVIDIA NIM | `chat_completions` | `https://integrate.api.nvidia.com/v1` | 固定+env覆盖 | `api_key` | `NVIDIA_API_KEY`, `NVIDIA_BASE_URL` | **52** / **2** | `nvidia/llama-3.1-nemotron-70b-instruct`、`nvidia/llama-3.3-70b-instruct` | 25/52 | 19/52 | 26/52 |
| 35 | `ollama-cloud` | Ollama Cloud | `chat_completions` | `https://ollama.com/v1` | 固定+env覆盖 | `api_key` | `OLLAMA_API_KEY`, `OLLAMA_BASE_URL` | **23** / 0 | aux `nemotron-3-nano:30b` | 23/23 | 22/23 | 12/23 |
| 36 | `openai-codex` | OpenAI Codex | `codex_responses` | `https://chatgpt.com/backend-api/codex` | 固定 | **`oauth_external`**（**无 env**） | 无（`env_vars=()`） | live / 0 | `gpt-5.5`、`gpt-5.4`（MoA 默认 reference slot 用它） | live | live | live |
| 37 | `opencode-free` | OpenCode Free | `chat_completions` | `https://opencode.ai/zen/v1` | 固定 | **keyless**（`Authorization: ""` 空头，拒绝任何 bearer） | 无（`env_vars=()`） | live / 0 | `laguna-s-2.1-free`（aux）、`big-pickle`（对非 opencode UA 返 429） | live | live | live |
| 38 | `opencode-zen` | OpenCode Zen | `chat_completions`（**per-model 路由**：Claude→anthropic_messages，GPT-5/Codex/Grok/Muse→codex_responses） | `https://opencode.ai/zen/v1` | 固定+env覆盖 | `api_key` | `OPENCODE_ZEN_API_KEY`, `OPENCODE_ZEN_BASE_URL` | **130** / 0 | `claude-opus-4-1`、`gpt-5.5-pro`、`gemini-4-argon`、`kimi-k3`；aux `gemini-3-flash` | 130/130 | 125/130 | 87/130 |
| 39 | `opencode-go` | OpenCode Go | `chat_completions`（**per-model 路由**：GPT/Grok/Muse→codex_responses，MiniMax/Qwen→anthropic_messages） | `https://opencode.ai/zen/go/v1` | 固定+env覆盖 | `api_key` | `OPENCODE_GO_API_KEY`, `OPENCODE_GO_BASE_URL` | **37** / 0 | `glm-5`（aux）；`mimo-v2.5-pro` 有 131072 max_tokens 硬顶 | 37/37 | 37/37 | 26/37 |
| 40 | `openrouter` | OpenRouter | `chat_completions` | `https://openrouter.ai/api/v1` | 固定+env覆盖 | `api_key` | `OPENROUTER_API_KEY` | **389** / **5** | `anthropic/claude-sonnet-4.6`、`openai/gpt-5.4`、`google/gemini-3.7-flash`、`qwen/qwen3-plus`、`deepseek/deepseek-chat` | 322/389 | 275/389 | 239/389 |
| 41 | `qwen-oauth` | Qwen Portal | `chat_completions` | `https://portal.qwen.ai/v1` | 固定 | **`oauth_external`** | `QWEN_API_KEY`（OAuth 面） | live / 0 | Qwen 族（Portal 侧） | live | live | live |
| 42 | `router` | Ramp Router (router.com) | `codex_responses` | `https://api.router.com/v1` | env 函数（`RAMP_ROUTER_BASE_URL`） | `api_key` | `RAMP_ROUTER_API_KEY`, `ROUTER_API_KEY`, `RAMP_ROUTER_BASE_URL` | live（**account-scoped，故意不写回退**）/ 0 | aux `gpt-5.4-mini`；`supports_vision=True` | live | live（catalog 逐模型声明 effort 词表） | live |
| 43 | `stepfun` | StepFun Step Plan | `chat_completions` | `https://api.stepfun.ai/step_plan/v1` | 固定+env覆盖 | `api_key` | `STEPFUN_API_KEY`, `STEPFUN_BASE_URL` | **7** / 0 | `step-3.5-flash`（aux）、`step-5-preview`、`step-3.7-flash` | 4/7 | 4/7 | 2/7 |
| 44 | `upstage` | Upstage Solar | `chat_completions` | `https://api.upstage.ai/v1` | 固定+env覆盖 | `api_key` | `UPSTAGE_API_KEY`, `UPSTAGE_BASE_URL` | **5** / **1** | `solar-pro3`（setup 默认）、`solar-pro4` | 5/5 | 4/5 | **0/5** |
| 45 | `vertex` | Google Vertex AI | `chat_completions`（Gemini via OpenAI-compatible 端点） | `https://aiplatform.googleapis.com/...`（Vertex 端点） | 固定+env覆盖 | **OAuth2**（`VERTEX_CREDENTIALS_PATH`） | `VERTEX_CREDENTIALS_PATH` | **61** / 0 | Gemini 族 on Vertex | 54/61 | 48/61 | 47/61 |
| 46 | `xai` | xAI Grok | `codex_responses` | `https://api.x.ai/v1` | 固定+env覆盖 | `api_key` | `XAI_API_KEY`, `XAI_BASE_URL` | **13** / 0 | `grok-4.7`、`grok-4.6`、`grok-4.5`、`grok-imagine-*` | 7/13 | 7/13 | 13/13 |
| 47 | `xiaomi` | Xiaomi MiMo | `chat_completions` | `https://api.xiaomimimo.com/v1` | 固定+env覆盖 | `api_key` | `XIAOMI_API_KEY`, `XIAOMI_BASE_URL` | **6** / 0 | `mimo-v2.6-pro`、`mimo-v2.5-pro` | 6/6 | 6/6 | 4/6（`supports_vision=True`，但 `supports_vision_tool_messages=False`） |
| 48 | `zai` | Z.AI (GLM) | `chat_completions` | `https://api.z.ai/api/paas/v4` | 固定+env覆盖 | `api_key` | `GLM_API_KEY`, `ZAI_API_KEY`, `Z_AI_API_KEY`, `GLM_BASE_URL` | **18** / **3** | `glm-5.2`、`glm-5`、`glm-4-9b`；aux `glm-4.5-flash` | 18/18 | 18/18 | 6/18 |

### 3.2 一目录多 profile 的 6 个目录

| 目录 | 注册的 profile | 拆分理由 |
|---|---|---|
| `alibaba` | `alibaba`, `alibaba-cn`, `alibaba-token-plan`, `alibaba-token-plan-cn` | 同一 key 类型、不同区域/计费端点的 4 个面；profile 名与 models.dev catalog key **精确对齐**，保证 `model.provider: alibaba-cn` 能解析到模型元数据 |
| `alibaba-coding-plan` | `alibaba-coding-plan`, `alibaba-coding-plan-cn` | 专用 coding 端点 + 专用 key tier |
| `commandcode` | `commandcode`, `commandcode-anthropic` | 同一 base_url，两种线协议（OpenAI chat vs Anthropic Messages）；后者靠 Bearer 而非 `x-api-key` |
| `kimi-coding` | `kimi-coding`, `kimi-coding-cn` | `.ai` / `.cn` 双端点 |
| `minimax` | `minimax`, `minimax-cn`, `minimax-oauth` | 国际 / 中国 / OAuth 三态 |
| `opencode-zen` | `opencode-zen`, `opencode-go` | Zen 免费+付费池 vs Go 订阅池，同 `_ATTRIBUTION_HEADERS` |

### 3.3 鉴权方式汇总（7 类）

| auth_type | profile 数 | 代表 | 凭证落点 |
|---|---|---|---|
| `api_key`（含未声明 = 默认） | 37 | 绝大多数 | `.env` / `providers.<id>.api_key` / `credential_pool` |
| `oauth_external` | 3 | `openai-codex`、`qwen-oauth`、`minimax-oauth` | `hermes-home/auth.json`，**无 env** |
| `oauth_device_code` | 1 | `nous` | `auth.json`（`hermes auth add nous --type oauth` / `hermes portal login`） |
| `copilot` | 1 | `copilot` | GitHub token → Copilot token（`COPILOT_GITHUB_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN`） |
| `aws_sdk` | 1 | `bedrock` | IAM（boto3 凭据链），**`env_vars=()`** |
| `external_process` | 1 | `copilot-acp` | ACP 子进程自管 |
| `keyless`（无 auth_type，靠空 Authorization 头） | 1 | `opencode-free` | 无凭证；`default_headers["Authorization"] = ""` 主动压制 SDK 的 `Bearer <placeholder>` |

### 3.4 凭证池（`credential_pool`）数据结构

`hermes-home/auth.json` 实测结构：

```yaml
version: <int>
providers: {}                 # 空（当前只用 pool）
credential_pool:
  <provider_id>:
    - id: <hex6>              # 短 id，`hermes auth remove <p> <index|id|label>` 用
      label: <string>         # 默认取来源 env 变量名，如 DEEPSEEK_API_KEY
      auth_type: api_key
      priority: 0
      source: "env:DEEPSEEK_API_KEY"
      last_status: ""         # 耗尽状态；`hermes auth reset <p>` 清零
      last_status_at: ""
      last_error_code: ""
      last_error_reason: ""
      last_error_message: ""
      last_error_reset_at: ""
      base_url: https://api.deepseek.com/v1
      request_count: 0
      secret_fingerprint: sk-***
updated_at: <iso8601>
```

> 这是"凭证池"的核心：**同一 provider 可挂多把 key**，按 `priority` 轮转；某把被 429/配额耗尽 → `last_status` 标记 → 切下一把；`hermes auth reset` 清标记。Orbit 若要 parity，这是必须实现的**熔断层**。

### 3.5 推理（reasoning）线格式差异 —— parity 最易踩坑处

`build_api_kwargs_extras()` 返回 `(extra_body, top_level)` 二元组，各家落点完全不同：

| provider | 关闭推理 | 开启 + effort | 备注 |
|---|---|---|---|
| `deepseek` | `extra_body.thinking = {"type":"disabled"}` | `thinking={"type":"enabled"}` + **top-level** `reasoning_effort` ∈ low/medium/high/max | V4+ 才生效（`deepseek-v3*` 不动）；不回显 `reasoning_content` 会 400 |
| `zai` | `extra_body.thinking={"type":"disabled"}` | `thinking={"type":"enabled"}`；GLM-5.2 另加 top-level `reasoning_effort` ∈ **high/max**；GLM-5.3 ∈ low/medium/high/max | GLM<4.5 完全不发 |
| `kimi-coding` | `thinking={"type":"disabled"}` | **二选一**：有 effort → 只发 top-level `reasoning_effort`（low/high/max）；无 → 只发 `thinking={"type":"enabled"}` | **同时发两者 → 400 "cannot specify both"** |
| `custom`（Ollama/vLLM/ARK） | top-level `reasoning_effort="none"`；仅 Ollama URL 追加 `extra_body.think=False` | top-level `reasoning_effort`（clamp 到 max） | `think` 是 Ollama 专属，Mistral/Groq 会 422 |
| `meta-ai` | **`minimal`**（Meta 拒绝 `none`） | top-level `reasoning_effort` ∈ minimal..xhigh | 忽略 core 的 `supports_reasoning` 门（第三方插件无法改 core 白名单） |
| `ollama-cloud` | **必须** top-level `reasoning_effort="none"`（忽略 `extra_body.thinking`） | top-level，`xhigh→max` | `minimal` 会 400 → clamp 到 low |
| `upstage` | 省略字段（Solar 默认 minimal=off） | top-level，仅 low/medium/high；**未设时默认 medium（默认开）** | deny-list：`solar-mini`/`syn-pro` 不发 |
| `nebius-token-factory` | 返回 `{}` | top-level，按模型 allowlist（deepseek-r1/v4、gpt-oss、glm-5、kimi-k2、minimax-m2、qwen3） | — |
| `openrouter` | 见下 | `extra_body.reasoning = reasoning_config`（按 catalog `supported_efforts` clamp） | **Claude 4.6+ 强制推理**：不发 `reasoning`，改把 effort 落到 top-level **`verbosity`**；Grok 模型另加 `x-grok-conv-id` 头 |
| `nous` | `extra_body.reasoning={"enabled":false}`（**唯一真正认 disable 的线格式**） | `reasoning = reasoning_config` | catalog 说 mandatory 时静默不发 |
| `ai-gateway` | `reasoning={"enabled":true,"effort":"medium"}` 兜底 | `extra_body.reasoning = reasoning_config` | 全量透传 |
| `copilot` | — | `extra_body.reasoning={"effort":...}`，按 live catalog 的 `github_model_reasoning_efforts()` clamp | `gpt-5.5/5.4` 支持 `xhigh` |
| `opencode-zen/go/free` | 模型分派：GLM-5.2 → top-level `reasoning_effort`；Kimi-K2 → thinking/effort 二选一；DeepSeek → 同 deepseek；Ox Alpha(`x-preview-f-free`) → 仅 low/high/max | 同左 | Go 的 `mimo-v2.5-pro` 另有 131072 `max_tokens` 硬顶 |
| `gemini` | `thinking_config`（native）或 `extra_body.extra_body.google.thinking_config`（`/openai` 兼容子路径，snake_case） | 同左 | 走自建 client |
| `qwen-oauth` | — | 不发 reasoning；发 `vl_high_resolution_images=true` + top-level `metadata` | system 消息末段注入 `cache_control: ephemeral` |
| `router` | catalog `reasoning.supported=false` → 整个 reasoning 字段省略 | 按 `supported_reasoning_efforts()`（cache-only，磁盘镜像 24h TTL + 后台预热） | 发错 level → 400 `invalid-argument` |
| `xai` / `actual` / `openai-codex` | 交给 codex_responses transport 通用处理 | 同左 | — |

---

## 4. `config.yaml` Schema 全集

### 4.1 顶层键总表（43 个内置键）

> 「默认值」来自 `hermes config get <key>`（内置默认树）；「用户实际」指 `hermes-home/config.yaml` 是否出现。

| # | 键 | 类型 | 默认值（摘要） | 含义 | 用户实际 |
|---|---|---|---|---|---|
| 1 | `model` | map | `{}`（无内置默认） | **主模型选择**：`provider` / `default` / `base_url` / `api_mode` / `api_key` / `max_tokens` / `temperature` | ✅ `provider: deepseek`, `default: deepseek-v4-flash`, `base_url: https://api.deepseek.com`, `api_mode: chat_completions`, `api_key: sk-***` |
| 2 | `providers` | map<id, map> | `{}` | **按 provider 覆盖**：`name` / `base_url` / `api_mode` / `transport` / `model` / `models.<m>.{supports_tools,supports_reasoning}` / `api_key` | ✅ `deepseek`, `custom:api-kkrich-ltd` |
| 3 | `auxiliary` | map | 18 个 slot（见 §4.3） | **辅助模型路由**：压缩 / 标题 / vision / 审批 / 审查 / MoA 等 | ✅ 仅 `vision` |
| 4 | `stt` | map | `enabled:true`, `language:en`, 8 家 provider 块 | **语音转文字** | ✅ `provider: local_command`, `local.model: small` |
| 5 | `tts` | map | `provider: edge`, 11 家 provider 块 | **文字转语音** | — |
| 6 | `voice` | map | `record_key: ctrl+b`, `auto_tts:false`, `barge_in:true` 等 13 键 | 语音模式（录音/打断/静音阈值） | — |
| 7 | `wake_word` | map | `enabled:false`, `provider: openwakeword`, `phrase: "hey hermes"`, `sensitivity:0.6` | 唤醒词（openwakeword / sherpa / porcupine） | — |
| 8 | `approvals` | map | `mode: smart`, `timeout:300`, `cron_mode: deny` | 危险命令审批策略 | ✅ `mode: 'off'` |
| 9 | `onboarding` | map | `seen: {}`, `profile_build: ask` | 引导流程已读标记 | ✅ `seen.busy_input_prompt: true` |
| 10 | `desktop` | map | `repo_scan_enabled:true`, `auto_continue.{enabled,freshness_minutes,max_attempts}` | 桌面 App 行为 | ✅ `models.provider_order`（40 项有序列表） |
| 11 | `plugins` | map | `hook_callback_timeout: 30` | 插件开关与条目 | ✅ `enabled: [superpowers/.hermes-plugin]`, `disabled: []`, `entries.<path>.allow_tool_override` |
| 12 | `_config_version` | int | 代码侧当前 **39** | **schema 版本号**，`hermes config migrate` 递增 | ✅ **33**（落后 6 版） |
| 13 | `security` | map | `redact_secrets:true`, `tirith_enabled:true`, `tirith_timeout:5`, `tirith_fail_open:true`, `allow_private_urls:false`, … | 安全（脱敏 / tirith 预执行扫描 / 私网 URL / 网站黑名单） | 注释示例 |
| 14 | `fallback_model` | map | 无（**不启用**） | **主模型失败时的 failover 目标** | 注释示例 |
| 15 | `moa` | map | `default_preset: default`, `presets.default.{reference_models[2], aggregator, max_tokens:4096, enabled:true}` | **Mixture of Agents** | — |
| 16 | `agent` | map | `max_turns:null`, `api_max_retries:3`, `reasoning_overrides:{}`, `reasoning_echo:false`, `service_tier:''`, `tool_use_enforcement:auto`, … | Agent 循环行为（含推理覆盖） | — |
| 17 | `display` | map | `personality:''`, `interface: cli`, `show_reasoning:true`, `streaming:false`, `skin: default`, `language: en`, `platforms.{telegram,discord,slack,wecom}.streaming` | 终端/TUI 显示 | — |
| 18 | `terminal` | map | `backend: local`, `timeout:180`, `cwd: '.'`, `env_passthrough:[]`, `docker_image`, `modal_image`, `daytona_image`, … | 终端/沙箱后端 | — |
| 19 | `timezone` | str | 空（server-local） | 时区 | — |
| 20 | `compression` | map | `enabled:true`, `threshold:0.5`, `target_ratio:0.2`, `protect_last_n:20`, `protect_first_n:3`, **`in_place:true`**, `model_thresholds:{}` | **上下文压缩**（含 model 级阈值） | — |
| 21 | `context` | map | `engine: compressor`, `memory_trim.{enabled,cooldown_seconds,log_every_n,info_log_min_delta_mb}` | 上下文引擎 | — |
| 22 | `memory` | map | `memory_enabled:true`, `user_profile_enabled:true`, `memory_char_limit:2200`, `user_char_limit:1375`, `nudge_interval:10`, `provider:''` | 记忆 | — |
| 23 | `tools` | map | `tool_search.{enabled:auto, threshold_pct:5, search_default_limit:5, max_search_limit:25, listing:auto, listing_max_tokens:4000}` | 工具搜索/列表预算 | — |
| 24 | `mcp` | map | `auto_reload_on_config_change: true` | MCP | — |
| 25 | `gateway` | map | `platform_connect_timeout:30`, `loop_watchdog:true`, `delivery_ledger:true`, `api_server.max_concurrent_runs:10`, … | 消息网关 | — |
| 26 | `hooks` | map | `{}` | shell hooks | — |
| 27 | `skills` | map | `external_dirs:[]`, `project_discovery:true`, `inline_shell:false`, `ledger:true`, `tier1_advisory:true` | 技能 | — |
| 28 | `web` | map | `backend:''`, `extract_char_limit:15000`, `keyless_fallback:true`, `cache_ttl_minutes:20`, `provider_tier:{}` | Web 搜索/抓取 | — |
| 29 | `sessions` | map | `retention_days:90`, `auto_archive_days:3`, `max_resume_messages:20000`, `cjk_fts:true` | 会话历史 | — |
| 30 | `cron` | map | `model:''`, `model_provider:''`, `preflight:true`, `model_drift_guard:true`, `max_parallel_jobs:null` | 定时任务（**独立 model/provider**） | — |
| 31 | `kanban` | map | `dispatch_in_gateway:true`, `auto_decompose:true`, `max_in_progress:null` | 协作看板 | — |
| 32 | `logging` | map | `level: INFO`, `max_size_mb:5`, `backup_count:3` | 日志 | — |
| 33 | `telemetry` | map | `shared_metrics.enabled: false` | 遥测 | — |
| 34 | `secrets` | map | `bitwarden.{enabled:false, access_token_env:BWS_ACCESS_TOKEN, project_id:'', cache_ttl_seconds:300, …}`, `onepassword.{enabled:false, env:{}, account:'', service_account_token_env:OP_SERVICE_ACCOUNT_TOKEN, …}` | **外部密钥源** | — |
| 35 | `proxy` | map | `enabled:false`, `tunnel_port:9090`, `credential_source: env`, `enforce_on_docker:true`, `allow_env_fallback:false` | **iron-proxy egress 防火墙** | — |
| 36 | `curator` | map | `enabled:true`, `interval_hours:168`, `stale_after_days:30`, `archive_after_days:90`, `backup.{enabled,keep}` | 技能后台维护 | — |
| 37 | `lsp` | map | `enabled:true`, `wait_mode: document`, `wait_timeout:5.0`, `install_strategy:auto`, `idle_timeout:600.0`, `servers:{}` | LSP | — |
| 38 | `computer_use` | map | `cua_telemetry:false`, `max_image_dimension:1456`, `capture_after_mode: som`, `permission_mode: standard` | 计算机操作后端 | — |
| 39 | `browser` | map | `backend:''`, `inactivity_timeout:120`, `snapshot_threshold:15000`, `engine:auto`, `headed:false`, `dialog_policy: must_respond`, `camofox.*`, `extension_control.*` | 浏览器 | — |
| 40 | `dashboard` | map | `theme: default`, `turn_isolation:false`, `ws_ping_interval:20.0`, `oauth.{client_id,portal_url}`, `basic_auth.*` | Web 控制台 | — |
| 41 | `privacy` | map | `redact_pii: false` | PII 脱敏 | — |
| 42 | `checkpoints` | map | `enabled:false`, `max_snapshots:20`, `max_total_size_mb:500`, `retention_days:7`, `auto_prune:true`, `min_interval_hours:24` | 文件快照回滚 | — |
| 43 | `doctor` | map | `live_probe_timeout: 10` | `hermes doctor` 活探针超时 | — |

> **顶层键合计 43 个**（`hermes config get <key>` 实测 41 个 + 用户侧 `fallback_model` + `_config_version`；其中 `model`/`timezone` 内置值为空、`hooks`/`providers` 内置为空 map）。
> 另探测了约 90 个候选键名，以下均**不存在**：`fallback`(单数)、`profiles`、`image`/`image_gen`、`video`、`audio`、`embeddings`、`project`、`notifications`、`sandbox`、`permissions`、`egress`、`reasoning`、`acp`、`ui`/`theme`/`skin`、`transcription`、`vision`、`budget`/`cost`/`usage`、`insights`、`pets`、`bundles`、`worktree`、`git`、`shell`、`paths`、`env`、`flags`、`experimental`、`features`、`tirith`、`instructions`、`rules`、`learning`、`sync`、`peer`、`webhook`、`pairing`、`backup`、`update`、`logs`、`debug`、`dump`、`verify`、`status`。
>
> **`profiles` 不是 config.yaml 的键**：多实例隔离由 `hermes profile` 命令 + 独立 `$HERMES_HOME` 实现（`config get profiles` → `Config key not set`）。
>
> **`stt.provider` 也不在内置默认树里**（`config get stt.provider` → not set），它是纯用户侧键——默认 provider 由 `transcription_tools.py` 的 auto-detect 顺序决定。

### 4.2 `model.*` 与 `providers.*`（模型层核心）

`model.*`（用户 config.yaml 实测 + 各 provider 注释推定）：

| 键 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `model.provider` | str | 无（必填） | provider id，须与 `ProviderProfile.name` 精确匹配（含 `alibaba-cn` 这类 catalog 对齐名） |
| `model.default` | str | 无 | 默认模型 id |
| `model.base_url` | str | 无（取 profile 的 `base_url`） | 覆盖 provider 默认端点 |
| `model.api_mode` | str | `chat_completions` | 覆盖线协议 |
| `model.api_key` | str | 无 | 明文 key（`sk-***`）；优先于 env |
| `model.max_tokens` | int | 取 profile `default_max_tokens` | 输出上限（`custom` 默认 65536，避免 Ollama `num_predict=128` 截断） |
| `model.temperature` | float | 取 profile `fixed_temperature`（Kimi = OMIT） | 温度 |

`providers.<id>.*`：

| 键 | 类型 | 示例 | 含义 |
|---|---|---|---|
| `name` | str | `DeepSeek` | 显示名覆盖 |
| `base_url` | str | `https://api.deepseek.com` | 该 provider 的端点 |
| `api_mode` | str | `chat_completions` | 线协议 |
| `transport` | str | `openai_chat` | **config 层 transport 别名**（实测唯一观测值 `openai_chat`；语义等同 `api_mode: chat_completions`） |
| `model` | str | `deepseek-v4-flash` | 该 provider 下选中的模型 |
| `models.<m>.supports_tools` | bool | `true` | **手工能力覆盖**（覆盖 catalog 推断） |
| `models.<m>.supports_reasoning` | bool | `true` | 同上 |
| `api_key` | str | `sk-***` | 该 provider 专用 key |

> **`providers.<id>` 的 key 可以是"合成 id"**：用户 config 里出现 `custom:api-kkrich-ltd`，说明 `<provider>:<label>` 形式可注册多个同 provider 的不同端点。`provider_models_cache.json` 的缓存键也印证：`"custom:https://api.deepseek.com"`。

### 4.3 `auxiliary.*` 全 18 个 slot

公共字段（除特别标注）：`provider`（默认 `auto`）、`model`（`''`）、`base_url`（`''`）、`api_key`（`sk-***`）、`timeout`（秒）、`extra_body`（`{}`）、`reasoning_effort`（`''`）。

| slot | timeout 默认 | 独有字段 | 用途 |
|---|---|---|---|
| `auxiliary.vision` | 120 | `download_timeout: 30` | **图片分析**（`vision_analyze` 工具） |
| `auxiliary.compression` | 120 | `max_output_tokens: 0` | 上下文压缩摘要 |
| `auxiliary.skills_hub` | 30 | — | 技能 hub 检索 |
| `auxiliary.approval` | 30 | — | 审批判定 |
| `auxiliary.review` | — | `api_mode: ''` | 代码审查 |
| `auxiliary.mcp` | 30 | — | MCP 工具描述处理 |
| `auxiliary.title_generation` | 30 | `enabled:true`, `prefer_fast_model:false`, `language:''` | 会话标题 |
| `auxiliary.memory_query_rewrite` | 8 | — | 记忆检索 query 改写 |
| `auxiliary.tts_audio_tags` | 30 | — | TTS 音频标签生成 |
| `auxiliary.triage_specifier` | 120 | — | 任务分诊 |
| `auxiliary.kanban_decomposer` | 180 | — | 看板任务拆解 |
| `auxiliary.profile_describer` | 60 | — | profile 描述生成 |
| `auxiliary.goal_judge` | 60 | — | 目标达成判定 |
| `auxiliary.curator` | 600 | — | 技能策展 |
| `auxiliary.monitor` | 60 | — | 监控 |
| `auxiliary.background_review` | 120 | `enabled:true`, `max_input_tokens: 600000` | 后台审查 |
| `auxiliary.moa_reference` | **900** | — | MoA reference slot 默认模型 |
| `auxiliary.moa_aggregator` | **900** | — | MoA aggregator slot 默认模型 |

顶层 `auxiliary.*` 非 slot 键：

| 键 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `auxiliary.transient_retries` | int | `2` | 辅助调用瞬时失败重试 |
| `auxiliary.free_only` | bool | `false` | 只用免费模型 |
| `auxiliary.openrouter_model` | str | `''` | OpenRouter 专用辅助模型 |
| `auxiliary.stream_only_base_urls` | list | `[]` | 仅流式的端点白名单 |

### 4.4 `stt.*` / `tts.*`

**`stt.*`**（内置默认全量）：

| 键 | 默认 | 含义 |
|---|---|---|
| `stt.enabled` | `true` | 总开关 |
| `stt.echo_transcripts` | `true` | 回显转写 |
| `stt.language` | `en` | 语言 |
| `stt.provider` | **（无内置默认，用户设 `local_command`）** | 选择 provider |
| `stt.local.model` | `base` | faster-whisper 尺寸（`tiny`/`base`/`small`/`medium`/`large-v3`；云模型名会被降级回默认） |
| `stt.local.language` / `initial_prompt` | `''` | 本地解码参数 |
| `stt.local.vad` | `true` | VAD |
| `stt.local.vad_min_silence_ms` | `500` | — |
| `stt.local.no_speech_prob_threshold` | `0.6` | — |
| `stt.local.logprob_threshold` | `-1.0` | — |
| `stt.local.unload_after_idle_seconds` | `0` | 空闲卸载 |
| `stt.cloud_trim_silence` / `cloud_trim_threshold_db` / `cloud_trim_keep_ms` | `true` / `-40` / `300` | 云端前处理 |
| `stt.groq.model` | `whisper-large-v3-turbo` | — |
| `stt.openai.model` | `whisper-1` | — |
| `stt.mistral.model` | `voxtral-mini-latest` | — |
| `stt.xai.language` | `''` | — |
| `stt.elevenlabs.model_id` | `scribe_v2`（+`language_code`, `tag_audio_events`, `diarize`） | — |
| `stt.deepinfra.model` | `''` | — |

**`tts.*`**：`provider: edge`；每家一个块（`edge.voice: en-US-AriaNeural`、`elevenlabs.{voice_id,model_id}`、`openai.{model:gpt-4o-mini-tts,voice:alloy}`、`gemini.{model,voice,audio_tags,persona_prompt_file}`、`xai.{voice_id,language,speed,auto_speech_tags,optimize_streaming_latency,sample_rate,bit_rate}`、`mistral.{model,voice_id}`、`minimax.{model,voice_id}`、`kittentts.{model,voice}`、`neutts.{ref_audio,ref_text,model,device}`、`piper.voice`、`deepinfra.{model,voice}`）。另支持 `tts.<provider>.max_text_length` 覆盖每 provider 的字符上限。

### 4.5 `moa.*`（Mixture of Agents）

**是什么**：`/moa <prompt>` 让**多个"reference 模型"并行作答**，再由一个 **aggregator 模型**汇总成最终答案。用于用便宜模型堆出接近旗舰的质量。

**几个 slot**：每个 preset 有 **N 个 reference slot + 1 个 aggregator slot**。内置 `default` preset = **2 reference + 1 aggregator = 3 个模型槽**。

| 键 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `moa.default_preset` | str | `default` | 默认 preset 名 |
| `moa.active_preset` | str | `''` | 当前激活（空 = 用 default） |
| `moa.save_traces` | bool | `false` | 保存各 slot 的中间输出 |
| `moa.trace_dir` | str | `''` | trace 落盘目录 |
| `moa.privacy_filter` | str | `''` | 隐私过滤 |
| `moa.presets.<name>.reference_models[]` | list<{provider, model}> | `[{openai-codex, gpt-5.5}, {openrouter, deepseek/deepseek-v4-pro}]` | reference 槽 |
| `moa.presets.<name>.aggregator.{provider,model}` | map | `{openrouter, anthropic/claude-opus-4.8}` | 汇总槽 |
| `moa.presets.<name>.max_tokens` | int | `4096` | 每槽输出上限 |
| `moa.presets.<name>.enabled` | bool | `true` | 该 preset 开关 |

### 4.6 `security.*` / `fallback_model` / `desktop.*` / `plugins.*`

**`security.*`**（内置默认）：

| 键 | 默认 | 含义 |
|---|---|---|
| `security.redact_secrets` | `true` | **API key / token / 密码在工具输出、日志、聊天响应中打码**（模型与用户都看不到） |
| `security.tirith_enabled` | `true` | tirith 预执行扫描（二进制可用时） |
| `security.tirith_path` | `tirith` | 二进制路径 |
| `security.tirith_timeout` | `5` | 扫描超时（秒） |
| `security.tirith_fail_open` | `true` | 扫描器失败时放行 |
| `security.allow_private_urls` | `false` | 允许访问私网 URL（SSRF 防护） |
| `security.allow_data_training_tiers_noninteractive` | `false` | 非交互下允许"会用于训练"的免费 tier |
| `security.approval.transport` / `.transport_fallback` | `builtin` / `deny` | 审批传输 |
| `security.protected_instruction_files` / `.protected_instruction_extra_patterns` | `true` / `[]` | 保护 AGENTS.md 等指令文件 |
| `security.website_blocklist.{enabled,domains,shared_files}` | `false` / `[]` / `[]` | 网站黑名单 |
| `security.acked_advisories` | `[]` | 已确认的安全通告 |
| `security.allow_lazy_installs` | `true` | 允许按需安装 |

**`fallback_model.*`**（注释示例，未启用）：

| 键 | 类型 | 含义 |
|---|---|---|
| `fallback_model.provider` | str | failover provider id |
| `fallback_model.model` | str | failover 模型 |
| `fallback_model.base_url` | str | 自定义 OpenAI 兼容端点的端点（可选） |
| `fallback_model.key_env` | str | 自定义端点的 key 环境变量名（可选） |

**支持 failover 的 provider 白名单**（来自 config.yaml 注释，权威）：`openrouter`(OPENROUTER_API_KEY)、`openai-codex`(OAuth)、`nous`(OAuth)、`zai`(ZAI_API_KEY)、`kimi-coding`(KIMI_API_KEY)、`kimi-coding-cn`(KIMI_CN_API_KEY)、`minimax`(MINIMAX_API_KEY)、`minimax-cn`(MINIMAX_CN_API_KEY)、`bedrock`(AWS IAM / boto3)。

**`desktop.*`**：`repo_scan_enabled:true`、`repo_scan_roots:[]`、`repo_scan_exclude_paths:[]`、`electron_flags:[]`、`ozone_platform_hint:auto`、`disable_gpu:auto`、`password_store:auto`、`macos_signing_identity:''`、`auto_continue.{enabled:true, freshness_minutes:15, max_attempts:2}`、**`desktop.models.provider_order`**（用户侧 40 项有序列表，决定模型选择器排序）。

**`plugins.*`**：`hook_callback_timeout:30`、`enabled:[<path>]`、`disabled:[]`、`entries.<path>.allow_tool_override: bool`。

**`_config_version`**：int。代码当前 **39**；`hermes-home/config.yaml` 为 **33** → 落后 6 版，`hermes config migrate` 可补齐（会保留旧键）。

---

## 5. 模型选择与容错：命令全集

### 5.1 `hermes config`（9 个子命令）

```
hermes config {show,edit,get,set,unset,path,env-path,check,migrate}
```
| 子命令 | 选项 | 说明 |
|---|---|---|
| `show` | 无 | 人类可读摘要（Paths / API Keys 有无 / Model / Display / Terminal / Timezone / Context Compression / 平台） |
| `edit` | 无 | `$EDITOR` 打开 config.yaml |
| `get <key>` | `--json` | 打印**解析后的**值（含内置默认）；未知键 → `Config key not set: <key>`（exit 1） |
| `set <key> <value>` | `--force` | 写入；未知键会打印提示（值仍写入），`--force` 静默 |
| `unset <key>` | 无 | 删除键 |
| `path` | 无 | config.yaml 路径 |
| `env-path` | 无 | `.env` 路径 |
| `check` | 无 | 检查缺失/过时项；打印 `Config version: N ✓` 与全部可选 env 变量及其关联工具 |
| `migrate` | 无 | 用新选项更新 config（不改动已有值） |

### 5.2 `hermes model`（交互式选模型）

```
hermes model [--refresh] [--portal-url URL] [--inference-url URL] [--client-id ID]
             [--scope SCOPE] [--no-browser] [--timeout N] [--ca-bundle PEM] [--insecure]
```
- `--refresh`：**清空模型选择器磁盘缓存**，重新拉所有 provider 的 live `/v1/models`。
- 其余选项只作用于 Nous Portal 登录流程。
- 选择器缓存 = `hermes-home/provider_models_cache.json`（实测结构：`{<provider 或 provider:base_url>: {fp: <指纹>, at: <ts>, models: [...]}}`；`opencode-free` 的 fp 是字面量 `keyless:opencode-free`）。

### 5.3 `hermes moa`（Mixture of Agents）

```
hermes moa {list|ls, configure|config [name], delete|rm <name>}
```
- `list`：显示当前 MoA model slots。
- `configure [name]`：交互式选 reference / aggregator 模型，写入 `moa.presets.<name>`。
- `delete <name>`：删 preset。
- 运行时入口是 `/moa <prompt>` 斜杠命令。详见 §4.5。

### 5.4 `hermes fallback`（failover 链）

```
hermes fallback {list|ls, add, remove|rm, clear}
```
- **触发条件**：限流 **429**、过载 **529**、服务错误 **503**、**连接失败**（`fallback --help` 原文："rate-limit, overload, or connection errors"）。
- `add` 打开与 `hermes model` **同一个 picker**，选中后 append 到链尾。
- `remove` 从链中挑一条删除；`clear` 清空。
- 链存于 `config.yaml` 的 `fallback_model`（单条）与 fallback 链结构（多条有序）。
- 文档：`https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers`

### 5.5 `hermes proxy`（本地 OpenAI 兼容代理 → OAuth provider）

```
hermes proxy {start, status, providers}
hermes proxy start [--provider nous|xai] [--host HOST] [--port PORT]
```
- 用途：起一个本地 HTTP server，把 **OpenAI 兼容请求**转发到**已 OAuth 认证的 provider**（Nous Portal / xAI）。外部 App 用**任意 bearer** 指向代理，代理替换成真实凭证。
- 默认 `--host 127.0.0.1`（`0.0.0.0` 可暴露到 LAN）、`--port 8645`、`--provider nous`。
- `status`：显示哪些 upstream 已就绪；`providers`：列出可用 upstream。

### 5.6 `hermes auth`（凭证池）

```
hermes auth {add, list, remove, reset, status, logout, spotify}
hermes auth add <provider> [--type oauth|api-key|api_key] [--label LABEL] [--api-key KEY]
                [--portal-url URL] [--inference-url URL] [--client-id ID] [--scope SCOPE]
                [--no-browser] [--timeout N] [--insecure] [--ca-bundle PEM]
hermes auth list [provider]
hermes auth remove <provider> <target>     # target = index | id | label
hermes auth reset <provider>               # 清除该 provider 全部凭证的 exhaustion 状态
hermes auth status <provider>
hermes auth logout <provider>
hermes auth spotify [--client-id ID]       # PKCE 认证 Spotify
```
- `--api-key` 不传则**安全提示输入**。
- 数据结构见 §3.4。`reset` 是 429 熔断后的恢复手段。

### 5.7 `hermes login` / `hermes logout`

```
hermes login  [--provider P] [--portal-url] [--inference-url] [--client-id] [--scope]
              [--no-browser] [--timeout] [--ca-bundle] [--insecure]     # 已废弃
hermes logout [--provider {nous,openai-codex,xai-oauth,spotify}]
```
- **`login` 已标注 Deprecated**：改用 `hermes auth` / `hermes model` / `hermes setup`。`--provider` 被忽略。
- `logout` 只有 **4 个 provider** 可登出：`nous`、`openai-codex`、`xai-oauth`、`spotify`（即所有 OAuth 类）。默认作用于"当前活跃 provider"。

### 5.8 `hermes portal`（Nous Portal）

```
hermes portal {login, info, status, open, tools}      # 无子命令 = login
```
- `login`：登录 + 一键配置（选模型、设 Nous 为 provider、提供 Tool Gateway）＝ `hermes auth add nous --type oauth` ＝ `hermes setup --portal`。
- `info`：Portal 认证 + Tool Gateway 路由摘要。
- `status`；`open`：浏览器打开订阅页；`tools`：列出 Tool Gateway 工具及哪些经 Nous 路由。

### 5.9 `hermes secrets`（外部密钥源）

```
hermes secrets {bitwarden|bw, onepassword|op|1password}
hermes secrets bitwarden {setup,status,token,sync,disable,install}
hermes secrets onepassword {setup,status,token,set,remove,sync,disable}
```
- 作用：**进程启动时**从外部密钥管理器拉 API key，而不是存 `~/.hermes/.env`。
- Bitwarden Secrets Manager：`install` 下载并校验 pinned `bws` **v2.0.0**；`setup` 装 bws + 存 access token + 选 project；`token` 轮换；`sync` 拉取并报告变更；`disable`。
- 1Password（`op://` 引用）：`setup` 校验 `op` CLI + 设 account/token env var + 启用；`set` 把 env var 映射到 `op://` 引用；`remove`；`sync`；`token` 轮换 service-account token。
- 配置落点：`secrets.bitwarden.*` / `secrets.onepassword.*`（见 §4.1 #34）。
- 文档：`https://hermes-agent.nousresearch.com/docs/user-guide/secrets/`

### 5.10 `hermes egress`（iron-proxy 凭证注入防火墙）

```
hermes egress {install,setup,start,stop,restart,reload,status,disable,config}
hermes egress install [--force]                                  # 下载 iron-proxy v0.39.0
hermes egress setup [--tunnel-port N] [--from-bitwarden|--no-bitwarden]
                    [--rotate-tokens|--no-restart|--restart]
hermes egress status [--show-tokens]
hermes egress reload                                             # 热重载 proxy.yaml（管理 API，不断连）
```
- 作用：**TLS 拦截式出站防火墙**，在请求离开沙箱前把 **proxy token 换成真实 API 凭证**。**默认关闭**。
- `setup` 交互式向导：install + CA + mint tokens + 写 config。
- `--from-bitwarden`：从 `secrets.bitwarden` 配置发现 provider key（而非当前 env），BW 不可达时**硬失败**不静默回退。
- `--rotate-tokens`：为每个 provider 重铸 token（默认保留已有 token，避免打断正在跑的沙箱）。
- `status --show-tokens`：默认只显示脱敏前缀。
- 配置落点：`proxy.*`（`tunnel_port: 9090`、`credential_source: env|bitwarden`、`enforce_on_docker:true`、`allow_env_fallback:false`、`upstream_deny_cidrs`、`extra_allowed_hosts`）。
- 文档：`https://hermes-agent.nousresearch.com/docs/user-guide/egress/iron-proxy`

### 5.11 `hermes migrate`（退役模型迁移）

```
hermes migrate {xai}
hermes migrate xai [--apply] [--no-backup]
```
- 扫描 `config.yaml` 中对**2026-05-15 退役的 xAI 模型**的引用，按 xAI 迁移指南改写为官方替代。
- **默认 dry-run**（不写）；`--apply` 原地改写；`--no-backup` 跳过时间戳备份。
- 另有 `hermes config migrate`（升级 schema 版本，不动值）。

---

## 6. 辅助模型与多模态

### 6.1 `auxiliary.vision` 的用途

- **驱动 `vision_analyze` 工具**：`tools/vision_tools.py` 的 `vision_analyze_tool(image_url, user_prompt, model, task_id, region)`。
- 走**集中式 auxiliary vision router**，可自动选择：**OpenRouter / Nous / Codex / 原生 Anthropic / 自定义 OpenAI 兼容端点**（`provider: auto` 时由 `agent/auxiliary_client.py` 按可用凭证挑）。
- profile 可覆盖默认 vision 模型：`ProviderProfile.default_vision_model()`（DeepInfra 用目录 `chat`+`vision` tag 动态选；`NousProfile.resolve_aux_model(vision=True)` 走 Portal 推荐接口）。
- 关键预算/行为键：`auxiliary.vision.timeout`（LLM 调用，120s）、`auxiliary.vision.download_timeout`（下载，30s，可被 `HERMES_VISION_DOWNLOAD_TIMEOUT` 覆盖）、`auxiliary.vision.extra_body`、`auxiliary.vision.reasoning_effort`。

### 6.2 `vision_tools.py` 支持的输入形态

统一由 **`tools/image_source.py::resolve_image_source()`** 单一解析器收敛（"data:/http(s)/file/local/container" 五类）：

| 形态 | 说明 |
|---|---|
| `http://` / `https://` URL | 先下载（`_download_image`，3 次重试，受 `check_website_access` + `is_safe_url` 约束，**防 SSRF**：挡 `169.254.169.254` 等） |
| `data:image/...;base64,...`（RFC 2397） | 直接解码 |
| 本地绝对/相对路径 | 直接用（**不删除**）；HTTP 下载的临时文件才清理 |
| 容器内路径（Docker/远程 backend） | 通过 exec-read **在沙箱内**读字节（防止 `vision_analyze('/etc/passwd')` 逃逸宿主；媒体缓存路径例外，走 host-read） |
| 视频（`permitted=("video",)` 显式开启） | 扩展名表 + mp4 magic sniff；ffmpeg 抽帧后有并发上限（`_vision_cpu_executor`） |

处理链：**magic-byte 类型检测 → 不支持格式（SVG/BMP/TIFF…）转 PNG → `region=[x1,y1,x2,y2]` 裁剪 → 超 5MB 自动缩放/JPEG 重编码（`_RESIZE_TARGET_BYTES`）→ base64 data URL**。
硬上限：ingest **50 MB**（`_MAX_INGEST_BYTES`），provider payload **20 MB**（`_MAX_BASE64_BYTES`）。
返回值：JSON 字符串 `{"success": bool, "analysis": str}`（裁剪/缩放时会附带 `scale_info` / `crop_offset` 说明）。

### 6.3 `stt`（语音转文字）

**内置 8 个 provider**（`BUILTIN_STT_PROVIDERS`，顺序即 auto-detect 优先级）：

| provider | 模型 | 备注 |
|---|---|---|
| `local` | faster-whisper（`tiny`/`base`/`small`/`medium`/`large-v3`） | 首选；不可用时 lazy-install |
| `local_command` | 由 `HERMES_LOCAL_STT_COMMAND` 或 `stt.providers.<name>: type: command` 定义 | **用户 config 选的就是它**，`stt.local.model: small` |
| `groq` | `whisper-large-v3-turbo` | 需 `GROQ_API_KEY` + openai SDK |
| `openai` | `whisper-1` | 需 OpenAI audio 凭证 |
| `mistral` | `voxtral-mini-latest` | `mistralai` 包 + `MISTRAL_API_KEY`（PyPI 隔离期会跳过） |
| `xai` | — | 走 `tools/xai_http.py::resolve_xai_http_credentials()` |
| `elevenlabs` | `scribe_v2` | `ELEVENLABS_API_KEY` |
| `deepinfra` | `stt.deepinfra.model` | **auto-detect 里排最后**，避免误抢 xAI/ElevenLabs |

- 解析顺序：**内置 → `stt.providers.<name>: type: command`（command-provider registry）→ 插件注册的 `TranscriptionProvider` → "No STT provider available"**。
- `local` 与 `local_command` 互为回退（一个不可用就切另一个）。
- `local_command` 的 `model` 会被 `_normalize_local_model()` 归一：云模型名（`whisper-1`、groq 模型名）会被降级回 `DEFAULT_LOCAL_MODEL` 并 warn。

### 6.4 TTS provider 清单

**`tools/tts_tool.py`（11 个 provider，含每 provider 字符上限）**：

| provider | `max_text_length` | 备注 |
|---|---|---|
| `edge` | 5000 | 默认 provider（`edge-tts`，免费） |
| `openai` | 4096 | `gpt-4o-mini-tts` |
| `xai` | 15000 | — |
| `minimax` | 10000 | `speech-02-hd` |
| `mistral` | 4000 | `voxtral-mini-tts-2603` |
| `gemini` | 32000 | 32k token 上下文 |
| `elevenlabs` | 10000（**按 model_id 变化**：`eleven_v3` 5000 / `multilingual_v2` 10000 / `flash_v2` 30000 / `flash_v2_5` 40000） | — |
| `neutts` | 2000 | 本地 `neutts-air-q4-gguf` |
| `kittentts` | 2000 | 本地 25MB 模型 |
| `piper` | 5000 | 本地 VITS |
| `deepinfra` | — | — |

均可由 `tts.<provider>.max_text_length` 覆盖。另有 **TTS command-provider registry**（`tts.providers.<name>: type: command`）让任意 CLI 成为后端。

**`tools/tts_streaming.py`（真流式，int16 mono PCM 分块）**：
- 注册制 `@register("name")` + `StreamingTTSProvider` 抽象基类。
- 已实现 **4 个真 streamer**：`elevenlabs`、`openai`、`gemini`、`xai`。
- `auto` 优先序：`["elevenlabs", "gemini", "openai", "xai"]`。
- 无分块 API 的 provider 退化为**按句切分**的伪流式（由 dispatcher 处理，不换 provider、不换音色）。
- `tts.streaming.provider` 可钉死某个 streamer；设为不可用者则返回 `None`（不静默替换）。

---

## 7. 上下文 / 提示预算工具

### 7.1 `hermes prompt-size`

```
hermes prompt-size [--platform PLATFORM] [--json]
```
- **输出**：一个"全新会话的**固定提示预算**"字节分解——**system prompt 总计、skills index、memory、user profile、tool-schema JSON**。
- `--platform`：模拟的平台（`cli`、`telegram`、`discord`、…），默认 `cli`（**同一 agent 在不同平台注入的系统提示不同**，所以必须指定）。
- `--json`：输出机器可读的分解。
- **完全离线，不发任何 API 请求**（`--help` 原文："Runs offline (no API call)"）。这是评估"系统提示瘦身"的基线工具。

### 7.2 `models_dev_snapshot.json` 的用途

- 位置：`_internal/agent/models_dev_snapshot.json`（**3.27 MB**），随发行版**打包**。
- 内容：models.dev 目录的**离线快照**，按 `<provider_id>.models.<model_id>` 组织。
- 单模型字段（实测 `gpt-5.4-pro` / `kimi-k2-thinking-turbo` / `grok-4-1-fast-reasoning` 等）：

```json
{
  "id": "gpt-5.4-mini-2026-03-17",
  "name": "...", "description": "...", "family": "gpt-mini",
  "attachment": true,
  "reasoning": true,
  "reasoning_options": [{"type": "effort", "values": ["none","low","medium","high","xhigh"]}],
  "tool_call": true,
  "structured_output": true,
  "temperature": false,
  "open_weights": false,
  "modalities": {"input": ["text","image"], "output": ["text"]},
  "limit": {"context": 400000, "input": 272000, "output": 128000},
  "cost": {"input": 0.75, "output": 4.5, "cache_read": ..., "cache_write": ..., "context_over_200k": {...}, "tiers": [...]},
  "knowledge": "2025-08-31", "release_date": "2026-03-19", "last_updated": "2026-03-19"
}
```

- **用途**：`agent/model_metadata.py` 的**离线兜底能力表**——上下文窗口、输出上限、是否 tool_call、是否 reasoning 及**合法 effort 词表**、模态（vision/audio）、价格（含 200k 以上分档）。网络不可用或 catalog 拉取失败时，靠它决定"这个模型能不能开 tools / 能不能发 `reasoning_effort: xhigh` / 上下文窗口多大 / 成本估算"。
- 与之配套的**运行时缓存**：`hermes-home/models_dev_cache.json`（**5.96 MB**，232 provider / **8947** 模型，带 `.etag` 做增量更新）——是 snapshot 的**热更新版**，agent 运行时优先读它。
- 两者关系：**snapshot = 出厂内置下限保证；cache = 联网后的最新真相**。

---

## 8. Orbit 模型层必须补齐的 top 5

> 判据：Hermes 有、Orbit 目前没有、且**不补就会在真实多 provider 场景下直接坏掉**的能力。按"坏了多严重 × 补起来多贵"排序。

### #1 Provider 注册表 + `ProviderProfile` 钩子机制（而不是硬编码 if/else）
Hermes 把 48 个 provider 收敛成**声明式 profile**（`name/aliases/env_vars/base_url/auth_type/api_mode/default_headers/default_aux_model/fallback_models`）+ **7 个可覆盖钩子**（`fetch_models`、`build_extra_body`、`build_api_kwargs_extras`、`prepare_messages`、`resolve_aux_model`、`default_vision_model`、`supported_reasoning_efforts`）。
**不补的后果**：每接一家就要改核心代码，第 10 家开始不可维护；且 39 个目录里 **6 个是一目录多 profile**（区域/协议/计费拆分），没有注册表就表达不了。
**成本**：低。`plugin.yaml + __init__.py` 的目录扫描 + `register_provider()` last-writer-wins，用户目录可覆盖内置同名 profile。

### #2 五态 `api_mode` 传输层 + **per-model 路由**
5 种线协议（`chat_completions` / `anthropic_messages` / `codex_responses` / `bedrock_converse` / `copilot_acp`），且 `copilot`、`opencode-zen`、`opencode-go`、`commandcode` 是**按模型名动态选协议**的（Claude→Messages，GPT-5/Codex/Grok→Responses，其余→chat）。
**不补的后果**：接 OpenAI Responses 系（xAI/Codex/Router/Meta）和 Anthropic 系（MiniMax/CommandCode）会全部走错协议直接 4xx；而且 `codex_responses` 才带 prompt caching、reasoning summary、`encrypted_content` 回放——走错协议等于白付钱。
**成本**：中。4 个 transport 实现 + 一张 `host → api_mode` 强制表。

### #3 推理参数的**逐 provider 线格式翻译表**（§3.5 那 16 行）
这是 Hermes 最厚的一块脏活：`extra_body.thinking` / top-level `reasoning_effort` / `reasoning` 对象 / `thinking_config` / `verbosity` 五种落点，还带互斥规则（Kimi/OpenCode "不能同时发 thinking 和 reasoning_effort"）、拒绝规则（Meta 拒 `none`、Ollama Cloud 拒 `minimal`、GLM-5.2 最低 `high`）、强制推理（Claude 4.6+ 不能关）。
**不补的后果**：每个 provider 都会随机 400；用户的"关掉思考"开关静默失效（Hermes 明确修过这个：Z.AI GLM-4.5+ 默认思考开，不发 `thinking:disabled` 就等于没关）。
**成本**：中高，但**只能靠这张表**——没有通用解，必须逐家写。

### #4 失败容错三件套：`fallback` 链 + `credential_pool` + 429 熔断
- `fallback_model` / `hermes fallback` 链：**429 / 529 / 503 / 连接失败**四类触发，按序尝试，支持自定义 OpenAI 兼容端点（`base_url` + `key_env`）。
- `credential_pool`：同一 provider 挂多把 key，`priority` 轮转 + `last_status` 耗尽标记 + `hermes auth reset` 恢复。
- `proxy`（本地 OpenAI 兼容代理，把任意 bearer 换成真凭证）+ `egress`（iron-proxy 出站凭证注入，沙箱内不落真 key）。
**不补的后果**：单 key 撞 429 就整轮失败；沙箱里必须放明文 key（安全事故）。
**成本**：低-中，收益最高。

### #5 模型能力元数据层（`models_dev` snapshot + cache + 手工覆盖三级）
**三级解析**：① 手工覆盖（`providers.<id>.models.<m>.supports_tools/supports_reasoning`）→ ② 运行时 cache（`models_dev_cache.json`，232 provider / 8947 模型 / ETag 增量）→ ③ 出厂 snapshot（`agent/models_dev_snapshot.json`，3.27 MB 离线兜底）。
再叠加 **`prompt-size`** 这个"系统提示 + tool schema 字节分解"离线工具。
**不补的后果**：不知道模型的 context 窗口 → 上下文溢出；不知道 `tool_call: false` → 对着不支持的模型发 tools 直接 400；不知道 `reasoning_options[].values` → 发出模型不认的 effort；不知道价格 → 无法做成本护栏（Hermes 还有 `agent.empty_response_guard.cost_threshold_usd: 0.25`）。
**成本**：中。但可以**直接复用 models.dev 的公开 JSON schema**，不必自建。

---

## 9. 未覆盖 / 未验证（诚实标注）

| 项 | 状态 | 原因 |
|---|---|---|
| `ProviderProfile` 基类的**字段默认值与校验逻辑** | 由 48 个调用点反推，非源码直读 | `providers/` 包在 exe 的 PYZ 内（zlib 压缩），磁盘不可读；`findstr` 探针 `reasoning_overrides`/`fallback_model`/`config_version` 均 not found |
| `hermes_cli/providers.py` 的 **`host_mandated_api_mode` 主机强制表**、`hermes_cli/models.py` 的 **per-model 路由与 reasoning caps** | 未读源码 | 同上；仅从 profile 注释与 docstring 得知其存在与行为 |
| `agent/reasoning_effort.py` 的 **`EFFORT_LADDER` 与各家 `*_EFFORTS` 词表** | 未读源码 | 同上；本文只记录了各 profile 引用到的常量名（`DEEPSEEK_V4_EFFORTS`、`GLM52_EFFORTS`、`KIMI_K3_EFFORTS`、`OLLAMA_CLOUD_EFFORTS`、`SOLAR_EFFORTS`、`META_AI_EFFORTS`、`NEBIUS_EFFORTS`、`OX_ALPHA_EFFORTS`、`OPENAI_COMPAT_WIRE_EFFORTS`） |
| `transport` 配置键的**完整合法取值集** | 仅观测到 `openai_chat` 一个值 | 无 schema 可查；`tools/*.py` 中无 `openai_chat` 字样 |
| 各 provider 的 **live `/v1/models` 实时模型数** | 未拉取 | 需网络 + 有效凭证；表中数字是 models.dev 目录数，已明确标注 |
| `models_dev_cache.json` 中 **`router`（Ramp Router）无条目** | 已确认缺失 | Router 是 account-scoped catalog，profile 明确"故意不写 fallback_models" |
| **`prompt-size` 的实际字节输出** | 未运行 | 遵守"exe 只允许 `--help`"约束；已从 `--help` 说明其输出内容 |
| `hermes config show` 读到的 home 与 hermes-home 不同 | 已发现 | 无 `HERMES_HOME` 时读 `C:\Users\Administrator\AppData\Local\hermes\`（该目录**无 config.yaml**，故 `show` 显示的是内置默认）；`hermes-home/config.yaml` 需由桌面 App 设 `HERMES_HOME` 才会被读到 |

---

*文档生成：只读取证，零写入 Hermes 目录。所有 api_key 已打码为 `sk-***`。*

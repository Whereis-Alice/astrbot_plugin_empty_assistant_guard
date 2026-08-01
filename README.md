# astrbot_plugin_empty_assistant_guard

用于定位并可选修复这类报错：

```text
Assistant messages must contain text, reasoning content, or tool_calls.
```

插件会检查同一轮请求的几个阶段：

- `agent_begin` / `agent_done`: Agent 的 `run_context.messages`
- `request_early` / `request_late`: `ProviderRequest.contexts`
- `provider_prepare`: OpenAI 兼容 Provider 真正发给上游前的 `payloads["messages"]`
- `provider_http_serialized_payload`: OpenAI SDK 已将请求转换为 HTTP JSON 后、HTTPX 发送前的消息摘要
- `tool_start` / `tool_result`: 最近执行过的 LLM 工具、工具所属插件和工具结果摘要

## 命令

- `empty_assistant_guard_status`
  查看当前会话中符合模型筛选的最近一次诊断摘要。默认只看 Kimi/Moonshot。
- `empty_assistant_guard_dump`
  查看符合模型筛选的最近一次请求 dump 目录。

AstrBot 的一条用户消息可能连续产生多次 LLM 请求，例如主模型调用工具、工具执行后再次请求主模型，以及表情包插件单独调用 DeepSeek。插件会为每个会话保留最近多次请求，后续辅助模型不会再覆盖 Kimi 的状态。

对于没有经过标准 `ProviderRequest` hook 的 Agent Runner 请求，插件会直接从 Runner 的 Provider ID、模型名和当前事件创建独立记录，适用于 TokenRouter 这类路径。

如果上游仍返回空 assistant 400，即使当前 payload 已经被 AstrBot 内置清理过，插件也会记录 `provider_error`、原始上下文摘要和 dump 路径；状态中的 `last_provider_error` 表示这条请求确实失败过，不会再显示成普通的 `bad_messages=0` 请求。

状态命令会优先显示最近一条发生过错误、修复或拦截的 Kimi 请求，避免后续成功请求覆盖故障记录。API 错误 dump 还会列出所有 assistant 消息的索引、content、reasoning 和工具调用字段。

dump 文件保存在：

```text
data/plugins/astrbot_plugin_empty_assistant_guard/requests/<umo-hash>-<request-id>/events.jsonl
```

## 处理模式

默认 `provider_action=repair`，发现不合法的空 assistant 会在请求发给上游前自动修复。

推荐配置：

```text
provider_action = repair
patch_agent_runner = true
patch_openai_provider = true
repair_empty_assistant_with_tool_calls = true
empty_assistant_tool_calls_model_keywords = kimi,moonshot
capture_serialized_http_payload = true
repair_serialized_http_payload = false
serialized_payload_repair_mode = space
serialized_payload_repair_model_keywords = kimi,moonshot
repair_strategy = drop
drop_orphan_tool_messages = true
fallback_repair_on_unmatched_api_error = true
fallback_repair_when_wire_payload_clean = false
fallback_repair_max_attempts = 3
status_model_keywords = kimi,moonshot
status_only_errors = true
capture_hook_diffs = true
recent_request_limit = 20
```

`repair` 会在请求发给上游前删除无文本、无 `reasoning_content`、无调用的 assistant 消息。开启 `repair_empty_assistant_with_tool_calls` 后，匹配 Kimi/Moonshot 的“正文为空但带 `tool_calls`/`function_call`”也会提前删除；若 assistant 后面紧跟 `tool` 消息，插件默认会一起删除这些孤立 tool 消息。清理只修改当前请求副本，不会直接删除已保存的会话历史。

这是针对 Kimi/TokenRouter 400 的快速兼容模式。它优先在 Agent Runner 和 Provider payload 阶段处理，避免等上游返回错误后再盲目重试。`empty_assistant_tool_calls_model_keywords` 默认只匹配 `kimi,moonshot`，其他模型不会受这项规则影响。

`status_model_keywords` 只控制 `status` 和 `dump` 显示哪一个模型的最近记录，不会缩小守卫的检测与修复范围。留空可恢复为显示所有模型。

如果一次 Agent 先请求 Kimi、报错后再回退 Gemini，状态中的 `provider_error_serialized_payload` 是发生错误的原始模型快照，不会被后续回退请求覆盖；`serialized_http_payload` 则表示当前记录最后一次实际发送的请求。

`status_only_errors` 默认开启，状态和 dump 指令只显示发生过 Provider 错误的请求；普通成功请求或只在发送前修复的请求不会覆盖错误记录。关闭后可恢复查看所有发生过异常、修复或拦截的请求。

`capture_hook_diffs` 默认开启，会逐个记录 `OnLLMRequestEvent` handler 前后的 `ProviderRequest.contexts` 差异。`capture_serialized_http_payload` 默认开启，会在 OpenAI SDK 完成 JSON 序列化、HTTPX 发送前记录消息摘要、请求字段、请求体大小和 SHA-256 短哈希，不记录完整请求体或 API Key。若 `source_hint` 显示某个 handler 首次引入空 assistant，优先检查该插件；若 `provider_http_serialized_payload` 仍干净但 Kimi 继续报错，问题发生在 HTTP 请求之后，更可能是 TokenRouter 或上游转换层。

序列化观测在 `0.2.6` 起放到后台线程与 HTTP 请求并行执行，避免几百个工具的请求被诊断日志拖慢。

`repair_serialized_http_payload` 默认关闭。开启后只修改匹配模型的最终 HTTP JSON。对于匹配 Kimi/Moonshot 的空正文调用 assistant，即使配置为 `space`，也会跳过补空格并执行删除；其他模型仍按 `space` 或 `drop` 配置处理。它不会清空 AstrBot 会话历史，修改只存在于本次发送的请求副本中。通常应优先让请求前清理生效，只有排查序列化链路时才开启这个选项。

如果 AstrBot 报告 `OpenAI completion has no usable output`，这是上游返回了空模型结果，不等同于请求中的空 assistant。状态命令会额外显示 `empty_output_count`、`last_empty_output` 和 `empty_output_response`，其中包含响应 ID、结束原因和 token 用量摘要。此类问题优先检查 TokenRouter/Kimi 转换层和上下文长度，不要继续增加 `fallback_repair_max_attempts`。

当日志显示 `bad_messages=0` 但仍匹配到空 assistant 400 时，0.2.2 默认不会再删除最近的正常 assistant 并反复重试。`fallback_repair_when_wire_payload_clean` 仅用于针对 TokenRouter 序列化问题做实验，普通使用应保持关闭。

如果上游已经把异常消息清理掉，但仍返回明确的 `Assistant messages must contain text, reasoning content, or tool_calls.`，`fallback_repair_on_unmatched_api_error` 会保留 0.1.8 起的有效兜底：删除请求副本中的最近一条 `assistant` 及其后紧邻的孤立 `tool`，再有限次重试。请求前清理会优先执行；Kimi/Moonshot 即使本地 HTTP payload 已确认干净，也会保留这条兜底，最多 3 次，以应对 TokenRouter 的上游转换。普通模型仍受 `fallback_repair_when_wire_payload_clean` 控制。关闭 `fallback_repair_on_unmatched_api_error` 可完全停用兜底。

升级插件不会覆盖 AstrBot 已保存的旧配置。如果状态中仍显示 `provider_action: report_only`，请在插件配置中手动改成 `repair`，否则插件只记录问题，不会自动修复。

## 看来源

状态里重点看：

- `source_hint`
  粗略判断是历史里原本就有、某个 request hook 引入、Provider 序列化阶段出现，还是与最近工具有关。
- `mutations`
  哪个文件/插件在 `on_llm_request` 阶段改过 `request.contexts` 或 `extra_user_content_parts`。
- `recent_tools`
  最近工具名、工具所属插件、工具结果是否为空。
- `finding`
  空 assistant 前后的角色。如果后面紧跟 `tool`，通常是 assistant 的 `tool_calls` 丢了；如果前面是 `tool`，通常是工具后模型返回了空最终回复。

## 兼容性说明

插件使用轻量 monkey patch 观察 `ProviderOpenAIOfficial._prepare_chat_payload`、OpenAI `AsyncCompletions.create()` 参数，以及 `AsyncAPIClient._send_request` 发送前已经序列化的 HTTP JSON。卸载或停用插件时会尝试恢复 patch。若同时启用了其他 provider 诊断插件，建议先用 `report_only` 跑一轮确认行为。

0.2.3 起，OpenAI SDK 包装器会保留原始 `create()` 函数签名，并自动修复旧版本 patch 期间 Provider 缓存的参数元数据。若曾出现 `Missing required arguments; Expected either ('messages' and 'model')`，请至少升级到 0.2.3。

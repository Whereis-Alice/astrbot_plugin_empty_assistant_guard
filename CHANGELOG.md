# 更新日志

## 0.2.2

- 当最终 OpenAI 客户端 payload 已确认没有空 assistant 时，默认跳过删除历史 assistant 的盲目兜底，避免重复重试和上下文损坏。
- 增加 `fallback_repair_when_wire_payload_clean` 配置项，只有明确需要测试 TokenRouter 序列化兼容性时才建议开启。
- 状态和 dump 增加跳过兜底的原因说明。

## 0.2.1

- 单独记录上游返回的空模型结果（`content=None`、无 reasoning、无 tool_calls），不再把它误判为请求中的空 assistant。
- 状态和 dump 增加空模型结果的次数、错误摘要、响应 ID、结束原因和 token 用量摘要。
- 对非流式 OpenAI completion 保存精简响应摘要，帮助区分 Kimi/TokenRouter 返回异常与本地请求上下文问题。

## 0.2.0

- 增加 request hook 级别的上下文前后快照，记录具体插件 handler 修改了哪些消息。
- 当某个 handler 首次引入空 assistant 时，在来源提示和 dump 中标出该 handler。
- 增加真正调用 OpenAI 客户端前的最终消息摘要，区分本地插件链、AstrBot/OpenAI 序列化和 TokenRouter 上游转换问题。
- 增加 `capture_hook_diffs` 配置项，默认开启，只记录摘要和预览。
- 状态增加最终客户端 payload 的请求次数、异常消息数和消息数。

## 0.1.9

- 增加 `status_only_errors` 配置项，默认让状态和 dump 指令只显示发生过 Provider 错误的请求。
- 没有匹配的错误请求时，状态指令会明确提示当前没有错误记录。
- 状态输出增加 `status_only_errors` 当前值，便于确认筛选模式。

## 0.1.8

- 将未定位空 assistant 时的兜底从单次尝试改为有限次数的最近 assistant 轮换。
- 每次重试只移除一条最近 assistant 及其紧邻的孤立 tool，默认最多尝试 3 次。
- 增加 `fallback_repair_max_attempts` 配置项，范围为 1 到 10。
- 达到兜底次数上限后停止继续删除，避免过度修改请求上下文。
- 在兜底日志中显示当前尝试次数和最大次数。

## 0.1.7

- 增加无法定位具体空 assistant 时的单次兜底重试。
- 仅在匹配明确的空 assistant 400 且 `provider_action=repair` 时启用兜底。
- 从本次请求副本中移除最后一条 assistant，并清理其后紧邻的孤立 tool 消息。
- 同步修复后的 `context_query`，避免 Provider 重试时重新使用旧消息列表。
- 每个请求最多执行一次兜底，不修改已保存的正常会话历史。
- 增加 `fallback_repair_on_unmatched_api_error` 配置项和对应诊断日志。
- 将本文件全部改为中文。

## 0.1.6

- 状态命令优先显示最近一条发生过错误、修复或拦截的匹配 Kimi 请求，避免被后续成功请求覆盖。
- API 错误 dump 增加 assistant 消息字段摘要，包括消息索引、content、reasoning、tool_calls 和 function_call。
- 规范化类似枚举的消息角色，并实际检查工具调用字段内容，减少误判。

## 0.1.5

- 即使最终 payload 中已经看不到可识别的空 assistant，也会记录匹配到的空 assistant API 错误。
- 在重试阶段检查并修复 `context_query`，避免 AstrBot 替换 `payloads["messages"]` 后仍使用未清理的消息。
- 在 AstrBot 内置 assistant 清理逻辑前后都执行插件检查。
- 状态命令增加 `provider_error_count` 和 `last_provider_error`。
- 当 request hook 修改了上下文但没有检测到空 assistant 时，补充更明确的来源提示。

## 0.1.4

- 当 TokenRouter 或其他 Provider 路径没有暴露可用的 `ProviderRequest` 绑定时，增加 Agent Runner 兜底状态。
- 增加从 Runner Provider 配置、`get_model()` 和请求模型字段中提取模型名的逻辑。
- 避免把过期的 Gemini 状态误用为当前 Kimi 请求。
- 在诊断状态中显示 patch 是否实际安装，避免把 `runner=config:true` 误认为 patch 已生效。

## 0.1.3

- 为每个会话保留有限数量的最近 LLM 请求，不再只显示最后一次辅助模型请求。
- 增加 `status_model_keywords`，默认使用 `kimi,moonshot`，让状态和 dump 命令跟随最近的 Kimi/Moonshot 请求。
- 增加 `recent_request_limit`，用于控制每个会话保留的请求数量。
- 将 Runner 和后置 request 诊断绑定到准确的 `ProviderRequest` 状态，避免跨请求归因。
- 明确 Agent Runner 守卫只移除非法的空 assistant，不会清空正常会话上下文。
- 当旧配置仍使用 `provider_action=report_only` 时，在插件启动时给出警告。

## 0.1.2

- 增加针对 AstrBot v4.26.8 的 Agent Runner 级保护。
- patch `ToolLoopAgentRunner._sanitize_contexts_for_provider`，在所有 Provider 请求和重试路径之前移除空 assistant。
- patch `ToolLoopAgentRunner._complete_with_assistant_response`，阻止 `Message(role="assistant", content=[])` 被保存回 `run_context.messages`。
- 增加默认开启的 `patch_agent_runner` 配置项。
- 在 `empty_assistant_guard_status` 中增加插件版本和 patch 状态。

## 0.1.1

- 将 `provider_action` 默认值从 `report_only` 改为 `repair`。
- 增强 OpenAI 兼容 Provider 的 `_sanitize_assistant_messages` patch。
- 增加针对 `Assistant messages must contain text, reasoning content, or tool_calls.` 的重试修复。
- 改进 Provider payload 修复，在重试前处理不安全消息。

## 0.1.0

- 首次发布。
- 增加 request、Provider payload、Agent 上下文和工具事件审计。
- 增加 `empty_assistant_guard_status` 和 `empty_assistant_guard_dump` 命令。
- 增加 `report_only`、`repair` 和 `block` 三种可选 Provider payload 处理模式。
- 增加删除空 assistant 及相关孤立 tool 消息、替换占位文本两种修复策略。

# astrbot_plugin_empty_assistant_guard

用于定位并可选修复这类报错：

```text
Assistant messages must contain text, reasoning content, or tool_calls.
```

插件会检查同一轮请求的几个阶段：

- `agent_begin` / `agent_done`: Agent 的 `run_context.messages`
- `request_early` / `request_late`: `ProviderRequest.contexts`
- `provider_prepare`: OpenAI 兼容 Provider 真正发给上游前的 `payloads["messages"]`
- `tool_start` / `tool_result`: 最近执行过的 LLM 工具、工具所属插件和工具结果摘要

## 命令

- `empty_assistant_guard_status`
  查看当前会话中符合模型筛选的最近一次诊断摘要。默认只看 Kimi/Moonshot。
- `empty_assistant_guard_dump`
  查看符合模型筛选的最近一次请求 dump 目录。

AstrBot 的一条用户消息可能连续产生多次 LLM 请求，例如主模型调用工具、工具执行后再次请求主模型，以及表情包插件单独调用 DeepSeek。插件会为每个会话保留最近多次请求，后续辅助模型不会再覆盖 Kimi 的状态。

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
repair_strategy = drop
drop_orphan_tool_messages = true
status_model_keywords = kimi,moonshot
recent_request_limit = 20
```

`repair` 会在请求发给上游前删除无文本、无 `reasoning_content`、无 `tool_calls` 的 assistant 消息。若空 assistant 后面紧跟 `tool` 消息，插件默认会一起删除这些孤立 tool 消息，因为它们通常是丢失了前置 `tool_calls` 的残片。

`status_model_keywords` 只控制 `status` 和 `dump` 显示哪一个模型的最近记录，不会缩小守卫的检测与修复范围。留空可恢复为显示所有模型。

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

插件使用轻量 monkey patch 观察 `ProviderOpenAIOfficial._prepare_chat_payload` 的最终 payload。卸载或停用插件时会尝试恢复 patch。若同时启用了其他 provider 诊断插件，建议先用 `report_only` 跑一轮确认行为。

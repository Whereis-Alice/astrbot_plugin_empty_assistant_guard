# Changelog

## 0.1.2

- Added Agent Runner level protection for AstrBot v4.26.8.
- Patched `ToolLoopAgentRunner._sanitize_contexts_for_provider` so empty assistant messages are removed before any provider request, including TokenRouter and fallback/re-query paths.
- Patched `ToolLoopAgentRunner._complete_with_assistant_response` to prevent `Message(role="assistant", content=[])` from being saved back into `run_context.messages`.
- Added the `patch_agent_runner` configuration option, enabled by default.
- Added plugin version and patch status to `empty_assistant_guard_status`.

## 0.1.1

- Changed the default `provider_action` from `report_only` to `repair`.
- Added a stronger OpenAI-compatible Provider patch for `_sanitize_assistant_messages`.
- Added retry-time repair for the upstream error: `Assistant messages must contain text, reasoning content, or tool_calls.`
- Improved provider payload repair so unsafe messages can be fixed before retry.

## 0.1.0

- Initial release.
- Added request, provider payload, Agent context, and tool-event auditing for empty assistant messages.
- Added `empty_assistant_guard_status` and `empty_assistant_guard_dump` commands.
- Added optional provider payload actions: `report_only`, `repair`, and `block`.
- Added repair strategies for dropping empty assistant messages and related orphan tool messages.

# Changelog

## 0.1.4

- Added a Runner fallback state when TokenRouter or another provider path does not expose a usable `ProviderRequest` binding.
- Added provider/model extraction from Runner provider config, `get_model()`, and request model fields.
- Prevented a stale Gemini state from being reused as the current Kimi request.
- Added installed-patch status to the diagnostic output so `runner=config:true` is no longer confused with a successfully installed patch.

## 0.1.3

- Kept a bounded history of recent LLM requests for each conversation instead of exposing only the last auxiliary-model request.
- Added `status_model_keywords`, defaulting to `kimi,moonshot`, so `empty_assistant_guard_status` and `empty_assistant_guard_dump` follow the most recent Kimi/Moonshot request.
- Added `recent_request_limit` for multi-request conversations.
- Bound runner and late request diagnostics to the exact `ProviderRequest` state when available, avoiding cross-request attribution.
- Clarified that the Agent Runner guard removes only invalid empty assistant messages and never clears normal conversation context.
- Added a startup warning when an existing installation still uses `provider_action=report_only`.

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

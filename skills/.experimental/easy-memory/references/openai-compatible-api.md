# OpenAI-Compatible API Contract

This document defines the canonical runtime contract for the future memory-management agent integration used by `easy-memory`.

## Scope

This contract is for optional preprocessing during:
- `scripts/read_today_log.py`
- `scripts/search_memory.py`

Default behavior remains unchanged when the memory-management agent is not enabled.

## Runtime Boundary

`easy-memory` has two different storage scopes:
- Shared skill implementation:
  - installed skill files such as `scripts/`, `references/`, `assets/`, and `agents/`
  - these may be reused across many projects from the skill installation directory
- Project-local memory data:
  - `./easy-memory` under the current working directory
  - each project keeps its own log set

Any future agent integration must preserve this split. The agent may read project-local memory content gathered by the scripts, but it must not reinterpret the installation directory as the log storage location.

## Enablement

The memory-management agent is optional.

Required script interface:
- `--task-context` is always required for `read_today_log.py` and `search_memory.py`
- when the memory-management agent is disabled, scripts must validate that `--task-context` is non-empty and then ignore it
- when the memory-management agent is enabled, scripts may pass `--task-context` and gathered memory data to the agent as a preprocessing step

## Configuration Placement

Canonical tracked files:
- `references/memory-agent-system-prompt.md`
- `references/openai-compatible-api.md`
- `references/response-schema.md`

Provider-specific compatibility notes may also be stored under `references/` as informational snapshots.
Those notes must not replace or redefine the canonical protocol described in this document.

Local runtime configuration only:
- local config file path
- API key when the provider requires authentication
- base URL
- model ID
- enablement toggle
- timeout and retry policy
- any installer-generated host adapter file

Secrets must not be stored in tracked repository files.

## Recommended Local Configuration Keys

Future implementations should support these local configuration keys:
- `EASY_MEMORY_AGENT_CONFIG_FILE`
- `EASY_MEMORY_AGENT_ENABLED`
- `EASY_MEMORY_AGENT_BASE_URL`
- `EASY_MEMORY_AGENT_API_KEY`
- `EASY_MEMORY_AGENT_MODEL`
- `EASY_MEMORY_AGENT_API_STYLE`
- `EASY_MEMORY_AGENT_DISABLE_THINKING`
- `EASY_MEMORY_AGENT_TIMEOUT_SECONDS`
- `EASY_MEMORY_AGENT_SYSTEM_PROMPT_FILE`

`EASY_MEMORY_AGENT_SYSTEM_PROMPT_FILE` is an optional local override. If unset, the canonical prompt source should be the installed `references/memory-agent-system-prompt.md`.

## Local Config File

The default local config file path should be:
- `./easy-memory/agent-config.json`

If `EASY_MEMORY_AGENT_CONFIG_FILE` is set, it should override the default config file location.

Recommended precedence:
1. environment variables
2. local config file
3. built-in defaults

Recommended JSON keys in the local config file:
- `enabled`
- `api_style`
- `base_url`
- `api_key`
- `model`
- `disable_thinking`
- `timeout_seconds`
- `system_prompt_file`

Example:

```json
{
  "enabled": true,
  "api_style": "openai_chat_completions",
  "base_url": "https://example.com/v1",
  "model": "gpt-4.1-mini",
  "disable_thinking": false,
  "timeout_seconds": 20,
  "system_prompt_file": "./easy-memory/custom-memory-agent-prompt.md"
}
```

This file is local runtime state. It must not be treated as canonical skill source and should not be committed with secrets.
If the provider does not require authentication, `api_key` may be omitted or set to an empty string.
`api_style` must describe the transport contract used by the runtime implementation.
Current supported values are:
- `openai_chat_completions`
- `ollama_native_chat`

`disable_thinking` is a provider-specific runtime toggle.
It is intended primarily for local Ollama deployments that expose native thinking-capable models.
When the selected transport does not support an explicit thinking toggle, the implementation may ignore this field.

The canonical example fixture for this local config file should live at:
- `assets/examples/agent-config.example.json`

## Minimum API Compatibility

The minimum required compatibility target is an OpenAI-compatible Chat Completions interface:
- method: `POST`
- path: `/chat/completions`
- authorization: optional `Bearer <api_key>` when the provider requires it
- content type: `application/json`

`base_url` should point to the API root, typically ending in `/v1`.
Some local OpenAI-compatible runtimes may also accept the full `/v1/chat/completions` URL directly and may not require an API key.

Future implementations may add support for Responses-style APIs, but Chat Completions is the canonical minimum contract for broad compatibility.

## Optional Ollama Native Extension

Runtime implementations may additionally support Ollama native chat as a non-canonical provider extension.

If enabled, the transport contract is:
- method: `POST`
- path: `/api/chat`
- authorization: optional `Bearer <api_key>` when a reverse proxy requires it
- content type: `application/json`

Recommended local config for this mode:

```json
{
  "enabled": true,
  "api_style": "ollama_native_chat",
  "base_url": "http://127.0.0.1:11434",
  "model": "qwen3.5:9b",
  "disable_thinking": true,
  "timeout_seconds": 20
}
```

When `api_style` is `ollama_native_chat` and `disable_thinking` is `true`, the runtime should send Ollama native `think: false`.
Structured-output requests should prefer Ollama native JSON-schema `format` constraints when available, while keeping the same downstream schema validation rules.

## Request Construction

The request should contain:
- one system message derived from `references/memory-agent-system-prompt.md` or a local override
- one user message carrying the preprocessing payload as JSON text
- a `response_format` constraint when the provider supports OpenAI-compatible structured JSON output

The user payload should contain:
- `schema_version`
- `mode`
- `task_context`
- `cwd`
- `log_dir`
- `entries`

For `search_memory.py`, the payload should also contain:
- `keywords`
- `max_results`

The canonical request schema version for agent calls should be:
- `easy_memory_agent_request_v1`

Runtime implementations should prefer an OpenAI-compatible `json_schema` response-format constraint for this response contract. If a provider rejects `response_format`, the implementation may retry without it, but schema validation of the returned JSON must remain strict.

Each entry object should include:
- `entry_id`
- `ref_level`
- `factual`
- `content`
- `timestamp`
- `paths`

Each `paths` item should include one related resource object with:
- `path_id`
- `resource_type`
- `path`
- `directory`

Compatibility naming note:
- `paths` and `path_id` remain the canonical field names in request and response payloads for backward compatibility.
- Despite those historical names, each item now represents a related resource that may be either a local path or a URL/document address.
- See `references/compatibility-notes.md` for the consolidated naming-compatibility summary.

`resource_type` should be:
- `local_path` for absolute local filesystem paths
- `url` for URLs or document addresses

For `local_path`, `directory` is the absolute local parent directory (or the directory itself when the stored target is a directory).
For `url`, `directory` is the derived parent/container URL.

Older memories without related-resource metadata must use an empty `paths` array.
Legacy related-resource metadata without `resource_type` remains valid; runtimes should infer the type when needed.

## Failure Behavior

If agent configuration is missing, invalid, or the API call fails:
- the scripts must remain usable in non-agent mode,
- raw log reading and raw search behavior must still be available,
- the failure must not silently rewrite or delete memory content.

This fallback rule also applies to:
- network errors,
- request timeouts,
- provider protocol mismatches,
- schema-validation failures,
- and unexpected runtime exceptions in the agent-processing path.

When fallback happens because of an agent-side failure or invalid agent response, the implementation should append a diagnostic record containing the full available response content to a runtime-generated error log under the installed skill directory, not under the project-local `./easy-memory` directory.

## Successful Script Output Block

When the memory-management agent is enabled and the agent returns a valid response, `read_today_log.py` and `search_memory.py` may emit a machine-readable success block instead of the raw output.

The canonical source of truth for that final script output contract is:
- `references/script-output-schema.md`

The block should use these exact markers:
- begin: `EASY_MEMORY_AGENT_RESULT_BEGIN`
- end: `EASY_MEMORY_AGENT_RESULT_END`

The content between the markers should be a single JSON object with this script-output schema version:
- `easy_memory_agent_script_output_v1`

The JSON object should contain:
- `schema_version`
- `mode`
- `status`
- `summary`
- `suggested_keywords`
- `warnings`
- `entries`
- `important_notice` when applicable

Each `entries` item should contain:
- `entry_id`
- `raw_line`
- `ref_level`
- `factual`
- `content`
- `timestamp`
- `score`
- `reason`
- `paths`

For search mode, each `entries` item should also include:
- `log_file`

Each `paths` item should contain one related resource object with:
- `path_id`
- `resource_type`
- `path`
- `directory`

If the scripts do not produce this block, callers should treat the output as fallback raw mode output.

The canonical example file for this final script output should live at:
- `assets/examples/script-output.example.json`

## Source Of Truth Rule

The raw memory logs remain the source of truth.
The future agent is only a preprocessing layer and must not become the only way to access stored memory.

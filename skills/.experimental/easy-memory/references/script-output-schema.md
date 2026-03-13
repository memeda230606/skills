# Script Output Schema

This document defines the canonical final output contract for `easy-memory` scripts when the optional memory-management agent succeeds.

## Scope

This contract applies to:
- `scripts/read_today_log.py`
- `scripts/search_memory.py`

It does not replace the raw fallback output. If the scripts do not emit the canonical success block described here, callers must treat the result as raw fallback output.

## Success Block Markers

When the memory-management agent is enabled and a valid agent response is accepted, the scripts should emit a machine-readable block delimited by these exact markers:
- begin: `EASY_MEMORY_AGENT_RESULT_BEGIN`
- end: `EASY_MEMORY_AGENT_RESULT_END`

The content between these markers must be a single JSON object.

## Script Output Schema Version

The canonical script output schema version is:
- `easy_memory_agent_script_output_v1`

## Canonical JSON Shape

```json
{
  "schema_version": "easy_memory_agent_script_output_v1",
  "mode": "search_memory",
  "status": "ok",
  "summary": "Short summary of task-relevant memory.",
  "suggested_keywords": ["keyword-one", "keyword-two"],
  "warnings": [],
  "entries": [
    {
      "entry_id": "entry-001",
      "log_file": "2026-03-13.log",
      "raw_line": "[ID:entry-001] ... [TIME:2026-03-13:16:00]",
      "ref_level": "high",
      "factual": true,
      "content": "Relevant memory content.",
      "timestamp": "2026-03-13:16:00",
      "score": 0.95,
      "reason": "Why this entry remained relevant after filtering.",
      "paths": [
        {
          "path_id": "path-001",
          "resource_type": "local_path",
          "path": "/abs/path/to/file.py",
          "directory": "/abs/path/to"
        }
      ]
    }
  ],
  "important_notice": "IMPORTANT NOTICE: ..."
}
```

## Top-Level Field Rules

- `schema_version`
  - required
  - must equal `"easy_memory_agent_script_output_v1"`
- `mode`
  - required
  - allowed values: `"read_today_log"`, `"search_memory"`
- `status`
  - required
  - allowed values: `"ok"`, `"no_relevant_memory"`
  - `needs_raw_fallback` must not appear in this success block; that case should fall back to raw output instead
- `summary`
  - required
  - short plain-text summary of the filtered task-relevant result
- `suggested_keywords`
  - required
  - array of strings, may be empty
- `warnings`
  - required
  - array of strings, may be empty
- `entries`
  - required
  - array, may be empty
- `important_notice`
  - optional for `read_today_log`
  - recommended for `search_memory`

## Entry Rules

Each `entries` item must contain:
- `entry_id`
  - required
  - string
- `raw_line`
  - required
  - the original raw memory line from the log
- `ref_level`
  - required
  - string
- `factual`
  - required
  - boolean
- `content`
  - required
  - string
- `timestamp`
  - required
  - string in the original memory timestamp format
- `score`
  - required
  - number in the range `0.0` to `1.0`
- `reason`
  - required
  - short plain-text explanation of why the entry remained relevant after unrelated content was filtered out
- `paths`
  - required
  - array, may be empty

For `search_memory`, each `entries` item should also include:
- `log_file`
  - recommended
  - source log file name

## Related Resource Rules

Compatibility naming note:
- `paths` and `path_id` remain the canonical JSON field names in script output for backward compatibility.
- Each item should nevertheless be interpreted as a related resource object, not as a local filesystem path only.

Each `paths` item must contain:
- `path_id`
  - required
  - string
- `resource_type`
  - required
  - string
  - allowed values: `"local_path"`, `"url"`
- `path`
  - required
  - absolute local path string, URL/document address string, or empty string if previously cleared and still relevant to the output contract
- `directory`
  - required
  - absolute local parent directory, derived parent/container URL, or empty string if previously cleared and still relevant to the output contract

## Fallback Rule

If any of the following is true, callers must not assume the canonical success block is present:
- the memory-management agent is disabled,
- the local config is missing or invalid,
- the API request fails,
- the agent response fails protocol or schema validation,
- the agent asks for raw fallback.

In those cases, callers must parse the script output as raw fallback output instead.

## Canonical Example

The canonical example file for this schema should live at:
- `assets/examples/script-output.example.json`

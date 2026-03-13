# Memory-Agent Response Schema

This document defines the canonical response format for the future memory-management preprocessing agent.

## Goals

The response must:
- be easy for scripts to parse deterministically,
- preserve compatibility with entries that do not have stored related resources,
- preserve path IDs when they were provided in the request,
- exclude content unrelated to the current task before producing output,
- stay advisory rather than replacing the raw memory logs as source of truth.

## Required Output Type

The agent must return a single JSON object and no surrounding prose.

## Canonical Schema

```json
{
  "schema_version": "easy_memory_agent_response_v1",
  "mode": "read_today_log",
  "status": "ok",
  "summary": "Short preprocessing summary.",
  "relevant_entries": [
    {
      "entry_id": "entry-id",
      "score": 0.92,
      "reason": "Why this memory is relevant.",
      "path_ids": ["path-id-1", "path-id-2"]
    }
  ],
  "suggested_keywords": ["keyword-one", "keyword-two"],
  "warnings": []
}
```

## Field Rules

- `schema_version`
  - required
  - must equal `"easy_memory_agent_response_v1"`
- `mode`
  - required
  - must echo the request mode
  - allowed values: `"read_today_log"`, `"search_memory"`
- `status`
  - required
  - allowed values: `"ok"`, `"no_relevant_memory"`, `"needs_raw_fallback"`
- `summary`
  - required
  - short plain-text summary
  - must summarize only the task-relevant content that remains after unrelated memory content has been removed
- `relevant_entries`
  - required
  - array, may be empty
  - must contain only entries that remain relevant after unrelated memory content has been filtered out
- `suggested_keywords`
  - required
  - array, may be empty
  - useful mainly for search mode
- `warnings`
  - required
  - array of plain-text warnings, may be empty

## relevant_entries Item Rules

Compatibility naming note:
- `path_ids` remains the canonical field name in agent responses for backward compatibility.
- Each `path_ids` value should be interpreted as a related resource ID, regardless of whether the referenced target is a local path or a URL/document address.

Each `relevant_entries` item must contain:
- `entry_id`
  - required
  - must reference an input memory entry ID
- `score`
  - required
  - numeric value in the range `0.0` to `1.0`
- `reason`
  - required
  - short plain-text explanation
  - must explain relevance only in relation to the filtered current task
- `path_ids`
  - required
  - array, may be empty
  - each value must match a `path_id` present in the corresponding input entry
  - each value must come from the same input entry referenced by `entry_id`; cross-entry path ID reuse is invalid
  - must include only path IDs that remain relevant after unrelated content has been excluded
  - if the model is uncertain about path ID ownership or path relevance, it must return an empty array rather than guessing
  - if the input memory had no stored related resources, this array must be empty

## Validation Rules

Scripts should reject or fall back from responses that:
- are not valid JSON objects,
- omit required fields,
- reference unknown `entry_id` values,
- reference unknown `path_id` values,
- return prose outside the JSON object.

Implementations may strip a single surrounding JSON code fence before validation, but they must still reject surrounding prose and must not relax field-level schema validation.

## Fallback Rule

If parsing fails or validation fails, the script should ignore the agent result and continue with raw read/search behavior.

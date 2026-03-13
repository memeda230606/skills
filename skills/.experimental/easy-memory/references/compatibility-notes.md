# Compatibility Notes

This document consolidates the naming-compatibility rules for `easy-memory`
related-resource metadata.

## Purpose

`easy-memory` originally stored only local filesystem paths.
Later revisions expanded the same metadata channel so it can also store:
- absolute local filesystem paths
- URLs
- document addresses

For backward compatibility, the historical field names and CLI option names were
kept stable even though their meaning is now broader.

## Historical Names And Current Meaning

- `PATHS`
  - Historical on-disk log field name
  - Current meaning: array of related resource objects
- `path_id`
  - Historical object field name
  - Current meaning: unique related resource ID
- `path_ids`
  - Historical agent-response field name
  - Current meaning: list of related resource IDs
- `--related-path`
  - Historical CLI option name
  - Current meaning: local absolute path or URL/document address
- `--path-update`
  - Historical CLI option name
  - Current meaning: replace one related resource by ID
- `--path-clear`
  - Historical CLI option name
  - Current meaning: clear one related resource by ID while preserving the ID

## Stable Compatibility Rules

- Existing logs that use `PATHS` remain valid.
- Existing payloads that use `path_id` and `path_ids` remain valid.
- Existing automation or tooling that calls `--related-path`, `--path-update`, or
  `--path-clear` does not need renaming.
- New implementations should interpret these historical names using the broader
  related-resource meaning.

## Resource Interpretation

Each related resource object should be interpreted using `resource_type`:
- `local_path`
  - `path` is an absolute local filesystem path
  - `directory` is the absolute parent directory, or the directory itself if the
    stored target is already a directory
- `url`
  - `path` is the URL or document address
  - `directory` is the derived parent or container URL

If older metadata does not include `resource_type`, the runtime should infer it.

## Reading Strategy

When reading `easy-memory` metadata:
- do not assume `path` means a local filesystem path
- treat `path_id` as a stable related resource identifier
- use `resource_type` to decide whether the target is local or remote

## Source Of Truth

This note explains naming compatibility only.
The canonical structural contracts remain:
- `SKILL.md`
- `references/openai-compatible-api.md`
- `references/response-schema.md`
- `references/script-output-schema.md`

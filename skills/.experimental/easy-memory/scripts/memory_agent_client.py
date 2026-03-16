from __future__ import annotations

import json
import re
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib import error, parse, request

from memory_agent_config import (
    DEFAULT_CODEX_MODEL,
    MemoryAgentConfig,
    load_system_prompt_text,
)

SCHEMA_VERSION = "easy_memory_agent_response_v1"
SCRIPT_OUTPUT_SCHEMA_VERSION = "easy_memory_agent_script_output_v1"
SCRIPT_OUTPUT_BEGIN = "EASY_MEMORY_AGENT_RESULT_BEGIN"
SCRIPT_OUTPUT_END = "EASY_MEMORY_AGENT_RESULT_END"
ALLOWED_MODES = {"read_today_log", "search_memory"}
ALLOWED_STATUS_VALUES = {"ok", "no_relevant_memory", "needs_raw_fallback"}
_FENCED_JSON_RE = re.compile(
    r"^```(?:json)?\s*\n(?P<body>.*)\n```$",
    re.DOTALL,
)
_UNSET = object()


class MemoryAgentClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        raw_api_response: Mapping[str, Any] | None = None,
        content_text: str | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_api_response = (
            dict(raw_api_response) if raw_api_response is not None else None
        )
        self.content_text = content_text
        self.response_body = response_body

    def attach_context(
        self,
        *,
        raw_api_response: Mapping[str, Any] | object = _UNSET,
        content_text: str | object = _UNSET,
        response_body: str | object = _UNSET,
    ) -> "MemoryAgentClientError":
        if raw_api_response is not _UNSET and self.raw_api_response is None:
            self.raw_api_response = (
                dict(raw_api_response)
                if isinstance(raw_api_response, Mapping)
                else None
            )
        if content_text is not _UNSET and self.content_text is None:
            self.content_text = (
                content_text if isinstance(content_text, str) else None
            )
        if response_body is not _UNSET and self.response_body is None:
            self.response_body = (
                response_body if isinstance(response_body, str) else None
            )
        return self


class MemoryAgentTransportError(MemoryAgentClientError):
    pass


class MemoryAgentProtocolError(MemoryAgentClientError):
    pass


class MemoryAgentSchemaError(MemoryAgentClientError):
    pass


@dataclass(frozen=True)
class MemoryAgentResponse:
    raw_api_response: dict[str, Any]
    content_text: str
    parsed_payload: dict[str, Any]


def format_script_output_block(
    mode: str,
    response_payload: Mapping[str, Any],
    entries: list[Mapping[str, Any]],
    important_notice: str | None = None,
) -> str:
    output_payload = {
        "schema_version": SCRIPT_OUTPUT_SCHEMA_VERSION,
        "mode": mode,
        "status": response_payload["status"],
        "summary": response_payload["summary"],
        "suggested_keywords": response_payload["suggested_keywords"],
        "warnings": response_payload["warnings"],
        "entries": entries,
    }
    if important_notice is not None:
        output_payload["important_notice"] = important_notice

    rendered_json = json.dumps(
        output_payload,
        ensure_ascii=False,
        indent=2,
    )
    return "\n".join(
        [
            SCRIPT_OUTPUT_BEGIN,
            rendered_json,
            SCRIPT_OUTPUT_END,
        ]
    )


def call_memory_agent(
    config: MemoryAgentConfig,
    request_payload: Mapping[str, Any],
) -> MemoryAgentResponse:
    config.require_runtime_ready()

    request_mode = _require_request_mode(request_payload)
    system_prompt = build_runtime_system_prompt(
        canonical_prompt=load_system_prompt_text(config),
        request_mode=request_mode,
        request_payload=request_payload,
    )
    response_schema = build_response_json_schema(request_mode)

    if config.api_style == "codex_exec":
        response_json, content_text = _run_codex_exec(
            config=config,
            system_prompt=system_prompt,
            request_payload=request_payload,
            response_schema=response_schema,
        )
    elif config.api_style == "ollama_native_chat":
        api_payload = build_ollama_chat_payload(
            model=config.model or "",
            system_prompt=system_prompt,
            request_payload=request_payload,
            response_schema=response_schema,
            disable_thinking=config.disable_thinking,
        )
        response_json = _post_ollama_chat_with_fallback(
            base_url=config.base_url or "",
            api_key=config.api_key,
            timeout_seconds=config.timeout_seconds,
            payload=api_payload,
            fallback_payload=build_ollama_chat_payload(
                model=config.model or "",
                system_prompt=system_prompt,
                request_payload=request_payload,
                disable_thinking=config.disable_thinking,
            ),
        )
        try:
            content_text = _extract_ollama_message_text(response_json)
        except MemoryAgentClientError as exc:
            raise exc.attach_context(raw_api_response=response_json)
    else:
        api_payload = build_chat_completions_payload(
            model=config.model or "",
            system_prompt=system_prompt,
            request_payload=request_payload,
            response_format=build_response_format_schema(response_schema),
        )

        response_json = _post_chat_completions_with_fallback(
            base_url=config.base_url or "",
            api_key=config.api_key,
            timeout_seconds=config.timeout_seconds,
            payload=api_payload,
            fallback_payload=build_chat_completions_payload(
                model=config.model or "",
                system_prompt=system_prompt,
                request_payload=request_payload,
            ),
        )
        try:
            content_text = _extract_message_text(response_json)
        except MemoryAgentClientError as exc:
            raise exc.attach_context(raw_api_response=response_json)

    try:
        parsed_payload = parse_agent_response_content(
            content_text=content_text,
            request_mode=request_mode,
            request_payload=request_payload,
        )
    except MemoryAgentClientError as exc:
        raise exc.attach_context(
            raw_api_response=response_json,
            content_text=content_text,
        )

    return MemoryAgentResponse(
        raw_api_response=response_json,
        content_text=content_text,
        parsed_payload=parsed_payload,
    )


def build_codex_exec_prompt(
    *,
    system_prompt: str,
    request_payload: Mapping[str, Any],
) -> str:
    return "\n\n".join(
        [
            "You are running inside Codex CLI exec as a pure JSON preprocessing step for easy-memory.",
            (
                "Do not use shell commands, do not inspect the workspace, "
                "do not call MCP tools, and do not modify any files. "
                "Use only the provided request payload."
            ),
            "Return exactly one JSON object that matches the supplied output schema.",
            "Canonical prompt:",
            system_prompt,
            "Input payload JSON:",
            json.dumps(
                dict(request_payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ]
    )


def _run_codex_exec(
    *,
    config: MemoryAgentConfig,
    system_prompt: str,
    request_payload: Mapping[str, Any],
    response_schema: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    prompt_text = build_codex_exec_prompt(
        system_prompt=system_prompt,
        request_payload=request_payload,
    )
    with tempfile.TemporaryDirectory(prefix="easy-memory-codex-exec-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        schema_path = tmp_path / "response-schema.json"
        output_path = tmp_path / "response.json"
        schema_path.write_text(
            json.dumps(dict(response_schema), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        command = build_codex_exec_command(
            config=config,
            schema_path=schema_path,
            output_path=output_path,
            prompt_text=prompt_text,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise MemoryAgentTransportError(
                f"Codex CLI executable not found: {config.codex_binary}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise MemoryAgentTransportError(
                "Codex exec request timed out.",
                raw_api_response={
                    "command": command,
                    "stdout": exc.stdout,
                    "stderr": exc.stderr,
                },
                response_body=_format_codex_exec_output(
                    stdout_text=exc.stdout,
                    stderr_text=exc.stderr,
                ),
            ) from exc

        output_text = (
            output_path.read_text(encoding="utf-8").strip()
            if output_path.exists()
            else ""
        )
        raw_response = {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "output_file": output_text,
        }
        if completed.returncode != 0:
            raise MemoryAgentTransportError(
                f"Codex exec failed with exit code {completed.returncode}.",
                raw_api_response=raw_response,
                response_body=_format_codex_exec_output(
                    stdout_text=completed.stdout,
                    stderr_text=completed.stderr,
                ),
            )

        content_text = output_text or _extract_codex_exec_content(completed.stdout)
        if not content_text:
            raise MemoryAgentProtocolError(
                "Codex exec did not produce a structured response.",
                raw_api_response=raw_response,
                response_body=_format_codex_exec_output(
                    stdout_text=completed.stdout,
                    stderr_text=completed.stderr,
                ),
            )
        return raw_response, content_text


def build_codex_exec_command(
    *,
    config: MemoryAgentConfig,
    schema_path: Path,
    output_path: Path,
    prompt_text: str,
) -> list[str]:
    command = [
        config.codex_binary,
        "exec",
        "--ephemeral",
        "--color",
        "never",
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "-m",
        config.model or DEFAULT_CODEX_MODEL,
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
    ]
    if config.codex_profile:
        command.extend(["-p", config.codex_profile])
    if config.codex_service_tier:
        command.extend(
            [
                "-c",
                f"service_tier={_toml_string_literal(config.codex_service_tier)}",
            ]
        )
    if config.codex_reasoning_effort:
        command.extend(
            [
                "-c",
                "model_reasoning_effort="
                f"{_toml_string_literal(config.codex_reasoning_effort)}",
            ]
        )
    command.append(prompt_text)
    return command


def _toml_string_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _extract_codex_exec_content(stdout_text: str) -> str:
    stripped = stdout_text.strip()
    if not stripped:
        return ""
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            return line
    return stripped


def _format_codex_exec_output(
    *,
    stdout_text: str | None,
    stderr_text: str | None,
) -> str:
    sections = []
    if stdout_text:
        sections.append(f"stdout:\n{stdout_text}")
    if stderr_text:
        sections.append(f"stderr:\n{stderr_text}")
    return "\n\n".join(sections) if sections else ""


def build_chat_completions_payload(
    model: str,
    system_prompt: str,
    request_payload: Mapping[str, Any],
    response_format: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    dict(request_payload),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "temperature": 0,
    }
    if response_format is not None:
        payload["response_format"] = dict(response_format)
    return payload


def build_ollama_chat_payload(
    model: str,
    system_prompt: str,
    request_payload: Mapping[str, Any],
    response_schema: Mapping[str, Any] | None = None,
    disable_thinking: bool = False,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    dict(request_payload),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "stream": False,
        "options": {
            "temperature": 0,
        },
    }
    if response_schema is not None:
        payload["format"] = dict(response_schema)
    if disable_thinking:
        payload["think"] = False
    return payload


def build_runtime_system_prompt(
    canonical_prompt: str,
    request_mode: str,
    request_payload: Mapping[str, Any],
) -> str:
    required_template = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "mode": request_mode,
            "status": "ok",
            "summary": "required non-empty string",
            "relevant_entries": [
                {
                    "entry_id": "entry-id",
                    "score": 0.95,
                    "reason": "short factual relevance reason",
                    "path_ids": [],
                }
            ],
            "suggested_keywords": ["keyword-one"],
            "warnings": [],
        },
        ensure_ascii=False,
        indent=2,
    )
    protocol_lines = [
        "Runtime protocol requirements:",
        f'1. schema_version must be "{SCHEMA_VERSION}".',
        f'2. mode must be exactly "{request_mode}".',
        '3. status must be one of "ok", "no_relevant_memory", or "needs_raw_fallback".',
        "4. summary is mandatory and must always be a non-empty string.",
        "5. relevant_entries must be an array of objects with entry_id, score, reason, and path_ids.",
        "6. suggested_keywords and warnings must be arrays of strings.",
        "7. Do not wrap the JSON object in Markdown fences.",
        "8. Do not add any prose before or after the JSON object.",
        "9. For every relevant entry, path_ids must be a subset of that same entry's own path IDs only.",
        "10. Never copy a path_id from one entry to another entry.",
        "11. If you are uncertain about any path_id, return an empty path_ids array for that entry.",
        "12. If an input entry has no paths, path_ids must be [].",
        "Required JSON skeleton:",
        required_template,
        "Entry/path ownership summary:",
        build_entry_path_ownership_summary(request_payload),
    ]
    return "\n\n".join([canonical_prompt, "\n".join(protocol_lines)])


def build_entry_path_ownership_summary(
    request_payload: Mapping[str, Any],
) -> str:
    entries = request_payload.get("entries")
    if not isinstance(entries, list) or not entries:
        return "No entries available."
    lines = []
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        entry_id = item.get("entry_id")
        if not isinstance(entry_id, str):
            continue
        path_items = item.get("paths")
        if not isinstance(path_items, list) or not path_items:
            lines.append(f"- {entry_id}: []")
            continue
        path_ids = []
        for path_item in path_items:
            if not isinstance(path_item, Mapping):
                continue
            path_id = path_item.get("path_id")
            if isinstance(path_id, str):
                path_ids.append(path_id)
        if path_ids:
            lines.append(
                f"- {entry_id}: [{', '.join(path_ids)}]"
            )
        else:
            lines.append(f"- {entry_id}: []")
    return "\n".join(lines) if lines else "No entries available."


def build_response_json_schema(request_mode: str) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "mode",
            "status",
            "summary",
            "relevant_entries",
            "suggested_keywords",
            "warnings",
        ],
        "properties": {
            "schema_version": {
                "type": "string",
                "enum": [SCHEMA_VERSION],
            },
            "mode": {
                "type": "string",
                "enum": [request_mode],
            },
            "status": {
                "type": "string",
                "enum": sorted(ALLOWED_STATUS_VALUES),
            },
            "summary": {
                "type": "string",
            },
            "relevant_entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "entry_id",
                        "score",
                        "reason",
                        "path_ids",
                    ],
                    "properties": {
                        "entry_id": {"type": "string"},
                        "score": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "reason": {"type": "string"},
                        "path_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "suggested_keywords": {
                "type": "array",
                "items": {"type": "string"},
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }


def build_response_format_schema(response_schema: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "easy_memory_agent_response",
            "strict": True,
            "schema": dict(response_schema),
        },
    }


def parse_agent_response_content(
    content_text: str,
    request_mode: str,
    request_payload: Mapping[str, Any],
) -> dict[str, Any]:
    stripped = extract_json_object_text(content_text)
    if not stripped:
        raise MemoryAgentProtocolError("Agent returned empty content.")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise MemoryAgentProtocolError(
            "Agent output is not valid JSON."
        ) from exc
    if not isinstance(parsed, dict):
        raise MemoryAgentSchemaError(
            "Agent output must be a single JSON object."
        )
    validate_agent_response_schema(
        response_payload=parsed,
        request_mode=request_mode,
        request_payload=request_payload,
    )
    return parsed


def extract_json_object_text(content_text: str) -> str:
    stripped = content_text.strip()
    if not stripped:
        return ""
    fenced_match = _FENCED_JSON_RE.fullmatch(stripped)
    if fenced_match:
        return fenced_match.group("body").strip()
    return stripped


def validate_agent_response_schema(
    response_payload: Mapping[str, Any],
    request_mode: str,
    request_payload: Mapping[str, Any],
) -> None:
    required_keys = {
        "schema_version",
        "mode",
        "status",
        "summary",
        "relevant_entries",
        "suggested_keywords",
        "warnings",
    }
    missing_keys = sorted(required_keys - set(response_payload.keys()))
    if missing_keys:
        missing_text = ", ".join(missing_keys)
        raise MemoryAgentSchemaError(
            f"Agent response is missing required keys: {missing_text}"
        )

    schema_version = response_payload["schema_version"]
    if schema_version != SCHEMA_VERSION:
        raise MemoryAgentSchemaError(
            f"Unexpected schema_version: {schema_version}"
        )

    mode = response_payload["mode"]
    if mode not in ALLOWED_MODES:
        raise MemoryAgentSchemaError(f"Invalid mode: {mode}")
    if mode != request_mode:
        raise MemoryAgentSchemaError(
            f"Agent response mode does not match request mode: {mode}"
        )

    status = response_payload["status"]
    if status not in ALLOWED_STATUS_VALUES:
        raise MemoryAgentSchemaError(f"Invalid status: {status}")

    summary = response_payload["summary"]
    if not isinstance(summary, str):
        raise MemoryAgentSchemaError("summary must be a string.")

    suggested_keywords = response_payload["suggested_keywords"]
    _require_string_list(suggested_keywords, "suggested_keywords")

    warnings = response_payload["warnings"]
    _require_string_list(warnings, "warnings")

    relevant_entries = response_payload["relevant_entries"]
    if not isinstance(relevant_entries, list):
        raise MemoryAgentSchemaError("relevant_entries must be a list.")

    known_entry_ids, known_path_ids = _collect_known_ids(request_payload)
    for index, item in enumerate(relevant_entries):
        if not isinstance(item, dict):
            raise MemoryAgentSchemaError(
                f"relevant_entries[{index}] must be an object."
            )
        _validate_relevant_entry_item(
            item=item,
            index=index,
            known_entry_ids=known_entry_ids,
            known_path_ids=known_path_ids,
        )


def _validate_relevant_entry_item(
    item: Mapping[str, Any],
    index: int,
    known_entry_ids: set[str],
    known_path_ids: dict[str, set[str]],
) -> None:
    required_keys = {"entry_id", "score", "reason", "path_ids"}
    missing_keys = sorted(required_keys - set(item.keys()))
    if missing_keys:
        missing_text = ", ".join(missing_keys)
        raise MemoryAgentSchemaError(
            f"relevant_entries[{index}] is missing required keys: {missing_text}"
        )

    entry_id = item["entry_id"]
    if not isinstance(entry_id, str):
        raise MemoryAgentSchemaError(
            f"relevant_entries[{index}].entry_id must be a string."
        )
    if entry_id not in known_entry_ids:
        raise MemoryAgentSchemaError(
            f"relevant_entries[{index}] references unknown entry_id: {entry_id}"
        )

    score = item["score"]
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise MemoryAgentSchemaError(
            f"relevant_entries[{index}].score must be a number."
        )
    if score < 0 or score > 1:
        raise MemoryAgentSchemaError(
            f"relevant_entries[{index}].score must be in the range 0.0 to 1.0."
        )

    reason = item["reason"]
    if not isinstance(reason, str):
        raise MemoryAgentSchemaError(
            f"relevant_entries[{index}].reason must be a string."
        )

    path_ids = item["path_ids"]
    _require_string_list(path_ids, f"relevant_entries[{index}].path_ids")
    known_ids_for_entry = known_path_ids[entry_id]
    for path_id in path_ids:
        if path_id not in known_ids_for_entry:
            raise MemoryAgentSchemaError(
                f"relevant_entries[{index}] references unknown path_id for entry {entry_id}: {path_id}"
            )


def _collect_known_ids(
    request_payload: Mapping[str, Any],
) -> tuple[set[str], dict[str, set[str]]]:
    entries = request_payload.get("entries")
    if not isinstance(entries, list):
        raise MemoryAgentSchemaError(
            "Request payload must include an entries list for schema validation."
        )

    known_entry_ids: set[str] = set()
    known_path_ids: dict[str, set[str]] = {}
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise MemoryAgentSchemaError(
                f"Request entries[{index}] must be an object."
            )
        entry_id = item.get("entry_id")
        if not isinstance(entry_id, str):
            raise MemoryAgentSchemaError(
                f"Request entries[{index}].entry_id must be a string."
            )
        if entry_id in known_entry_ids:
            raise MemoryAgentSchemaError(
                f"Request entries contain duplicate entry_id: {entry_id}"
            )
        known_entry_ids.add(entry_id)
        path_items = item.get("paths", [])
        if not isinstance(path_items, list):
            raise MemoryAgentSchemaError(
                f"Request entries[{index}].paths must be a list."
            )
        path_ids_for_entry: set[str] = set()
        for path_index, path_item in enumerate(path_items):
            if not isinstance(path_item, dict):
                raise MemoryAgentSchemaError(
                    f"Request entries[{index}].paths[{path_index}] must be an object."
                )
            path_id = path_item.get("path_id")
            if not isinstance(path_id, str):
                raise MemoryAgentSchemaError(
                    f"Request entries[{index}].paths[{path_index}].path_id must be a string."
                )
            path_ids_for_entry.add(path_id)
        known_path_ids[entry_id] = path_ids_for_entry
    return known_entry_ids, known_path_ids


def _require_request_mode(request_payload: Mapping[str, Any]) -> str:
    mode = request_payload.get("mode")
    if not isinstance(mode, str):
        raise MemoryAgentSchemaError("Request payload must include a string mode.")
    if mode not in ALLOWED_MODES:
        raise MemoryAgentSchemaError(f"Invalid request mode: {mode}")
    return mode


def _require_string_list(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise MemoryAgentSchemaError(f"{label} must be a list.")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise MemoryAgentSchemaError(
                f"{label}[{index}] must be a string."
            )


def _post_chat_completions(
    base_url: str,
    api_key: str | None,
    timeout_seconds: float,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    url = _build_chat_completions_url(base_url)
    headers = {
        "Content-Type": "application/json",
    }
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    return _post_json_request(
        url=url,
        headers=headers,
        timeout_seconds=timeout_seconds,
        payload=payload,
    )


def _post_ollama_chat(
    base_url: str,
    api_key: str | None,
    timeout_seconds: float,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    url = _build_ollama_chat_url(base_url)
    headers = {
        "Content-Type": "application/json",
    }
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    return _post_json_request(
        url=url,
        headers=headers,
        timeout_seconds=timeout_seconds,
        payload=payload,
    )


def _post_chat_completions_with_fallback(
    base_url: str,
    api_key: str | None,
    timeout_seconds: float,
    payload: Mapping[str, Any],
    fallback_payload: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        return _post_chat_completions(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            payload=payload,
        )
    except MemoryAgentTransportError as exc:
        if not _should_retry_without_structured_output(exc):
            raise
    return _post_chat_completions(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        payload=fallback_payload,
    )


def _post_ollama_chat_with_fallback(
    base_url: str,
    api_key: str | None,
    timeout_seconds: float,
    payload: Mapping[str, Any],
    fallback_payload: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        return _post_ollama_chat(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            payload=payload,
        )
    except MemoryAgentTransportError as exc:
        if not _should_retry_without_structured_output(exc):
            raise
    return _post_ollama_chat(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        payload=fallback_payload,
    )


def _should_retry_without_structured_output(
    exc: MemoryAgentTransportError,
) -> bool:
    message = str(exc).lower()
    if (
        "response_format" not in message
        and "json_schema" not in message
        and "format" not in message
        and "schema" not in message
    ):
        return False
    markers = (
        "unsupported",
        "not supported",
        "unknown",
        "invalid",
        "unexpected",
        "not allowed",
    )
    return any(marker in message for marker in markers)


def _post_json_request(
    *,
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    http_request = request.Request(
        url,
        data=raw_body,
        headers=dict(headers),
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MemoryAgentTransportError(
            f"Memory agent HTTP error {exc.code}: {body}",
            response_body=body,
        ) from exc
    except socket.timeout as exc:
        raise MemoryAgentTransportError(
            "Memory agent request timed out."
        ) from exc
    except TimeoutError as exc:
        raise MemoryAgentTransportError(
            "Memory agent request timed out."
        ) from exc
    except error.URLError as exc:
        raise MemoryAgentTransportError(
            f"Memory agent connection failed: {exc.reason}"
        ) from exc

    try:
        parsed_body = json.loads(body)
    except json.JSONDecodeError as exc:
        raise MemoryAgentProtocolError(
            "Memory agent API did not return valid JSON.",
            response_body=body,
        ) from exc
    if not isinstance(parsed_body, dict):
        raise MemoryAgentProtocolError(
            "Memory agent API response must be a JSON object.",
            response_body=body,
        )
    return parsed_body


def _build_chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if not normalized:
        raise MemoryAgentTransportError("base_url must not be empty.")
    parsed_base = parse.urlsplit(normalized)
    if not parsed_base.scheme or not parsed_base.netloc:
        raise MemoryAgentTransportError(
            f"Invalid base_url for memory agent: {base_url}"
        )
    if parsed_base.path.endswith("/chat/completions"):
        return normalized
    if parsed_base.path.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/chat/completions"


def _build_ollama_chat_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if not normalized:
        raise MemoryAgentTransportError("base_url must not be empty.")
    parsed_base = parse.urlsplit(normalized)
    if not parsed_base.scheme or not parsed_base.netloc:
        raise MemoryAgentTransportError(
            f"Invalid base_url for memory agent: {base_url}"
        )
    base_without_suffix = normalized
    suffixes = (
        "/api/chat",
        "/v1/chat/completions",
        "/chat/completions",
        "/v1",
    )
    for suffix in suffixes:
        if base_without_suffix.endswith(suffix):
            base_without_suffix = base_without_suffix[: -len(suffix)]
            break
    if base_without_suffix.endswith("/api"):
        return f"{base_without_suffix}/chat"
    return f"{base_without_suffix}/api/chat"


def _extract_message_text(api_response: Mapping[str, Any]) -> str:
    choices = api_response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise MemoryAgentProtocolError(
            "Memory agent API response is missing choices."
        )
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise MemoryAgentProtocolError(
            "Memory agent API response choice must be an object."
        )
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise MemoryAgentProtocolError(
            "Memory agent API response is missing message."
        )
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for index, item in enumerate(content):
            if not isinstance(item, dict):
                raise MemoryAgentProtocolError(
                    f"message.content[{index}] must be an object."
                )
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        joined = "".join(text_parts).strip()
        if joined:
            return joined
    raise MemoryAgentProtocolError(
        "Memory agent API response does not contain supported message content."
    )


def _extract_ollama_message_text(api_response: Mapping[str, Any]) -> str:
    message = api_response.get("message")
    if not isinstance(message, dict):
        raise MemoryAgentProtocolError(
            "Ollama native response is missing message."
        )
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    raise MemoryAgentProtocolError(
        "Ollama native response does not contain supported message content."
    )

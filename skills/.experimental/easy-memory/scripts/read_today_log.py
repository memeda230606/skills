#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date

from memory_agent_client import (
    MemoryAgentClientError,
    call_memory_agent,
    format_script_output_block,
)
from memory_agent_config import MemoryAgentConfigError, load_memory_agent_config
from memory_agent_failure_log import append_agent_failure_log
from memory_utils import (
    format_related_path_lines,
    log_base_dir,
    log_path_for_date,
    normalize_task_context,
    parse_entry_line,
    require_initialized,
)

EMPTY_LOG_MESSAGE = (
    "No log entries for today. Created an empty log file; "
    "please continue with the remaining task steps."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read today's easy-memory log."
    )
    parser.add_argument(
        "--task-context",
        required=True,
        help=(
            "Required user question/problem context. Reserved for future "
            "memory-agent preprocessing and ignored unless that agent is enabled."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    task_context = normalize_task_context(args.task_context)

    base_dir = log_base_dir(create=True)
    require_initialized(base_dir)

    log_path = log_path_for_date(date.today(), base_dir)
    if not log_path.exists():
        log_path.touch()
        print(EMPTY_LOG_MESSAGE)
        return 0

    content = log_path.read_text(encoding="utf-8")
    if not content.strip():
        print(EMPTY_LOG_MESSAGE)
        return 0

    parsed_items = []
    for line in content.splitlines():
        entry = parse_entry_line(line)
        if entry:
            parsed_items.append(
                {
                    "line": line,
                    "entry": entry,
                }
            )

    agent_output = maybe_render_agent_output(
        task_context=task_context,
        base_dir=base_dir,
        parsed_items=parsed_items,
    )
    if agent_output is not None:
        print(agent_output)
        return 0

    rendered_lines = render_raw_output(content.splitlines())
    output = "\n".join(rendered_lines)
    if output:
        print(output)
    return 0


def maybe_render_agent_output(
    task_context: str,
    base_dir,
    parsed_items: list[dict],
) -> str | None:
    if not parsed_items:
        return None

    try:
        config = load_memory_agent_config()
    except MemoryAgentConfigError as exc:
        print(f"Memory-agent fallback: {exc}", file=sys.stderr)
        return None

    if not config.enabled:
        return None

    request_payload = {
        "schema_version": "easy_memory_agent_request_v1",
        "mode": "read_today_log",
        "task_context": task_context,
        "cwd": str(base_dir.parent.resolve()),
        "log_dir": str(base_dir.resolve()),
        "entries": [
            build_request_entry(item)
            for item in parsed_items
        ],
    }

    try:
        response = call_memory_agent(config, request_payload)
        parsed_payload = response.parsed_payload
        if parsed_payload["status"] == "needs_raw_fallback":
            append_agent_failure_log(
                config=config,
                request_payload=request_payload,
                fallback_reason="agent requested raw fallback",
                response=response,
            )
            print(
                "Memory-agent fallback: agent requested raw fallback.",
                file=sys.stderr,
            )
            return None
        return render_agent_output(parsed_payload, parsed_items)
    except (MemoryAgentConfigError, MemoryAgentClientError) as exc:
        append_agent_failure_log(
            config=config,
            request_payload=request_payload,
            fallback_reason="agent error fallback",
            error=exc,
        )
        print(f"Memory-agent fallback: {exc}", file=sys.stderr)
        return None
    except Exception as exc:
        append_agent_failure_log(
            config=config,
            request_payload=request_payload,
            fallback_reason="unexpected agent error fallback",
            error=exc,
        )
        print(
            f"Memory-agent fallback: unexpected agent error ({exc.__class__.__name__}): {exc}",
            file=sys.stderr,
        )
        return None


def build_request_entry(item: dict) -> dict:
    entry = item["entry"]
    return {
        "entry_id": entry["id"],
        "ref_level": entry["ref"],
        "factual": entry["factual"],
        "content": entry["content"],
        "timestamp": entry["timestamp"],
        "paths": [
            {
                "path_id": path_item["id"],
                "path": path_item["path"],
                "directory": path_item["directory"],
                "resource_type": path_item["resource_type"],
            }
            for path_item in entry["path_entries"]
        ],
    }


def render_raw_output(lines: list[str]) -> list[str]:
    rendered_lines: list[str] = []
    for line in lines:
        rendered_lines.append(line)
        entry = parse_entry_line(line)
        if entry:
            for related_line in format_related_path_lines(entry["path_entries"]):
                rendered_lines.append(f"  {related_line}")
    return rendered_lines


def render_agent_output(
    response_payload: dict,
    parsed_items: list[dict],
) -> str:
    relevant_entries = response_payload["relevant_entries"]
    entry_lookup = {
        item["entry"]["id"]: item
        for item in parsed_items
    }
    structured_entries: list[dict] = []
    for relevant_item in relevant_entries:
        item = entry_lookup[relevant_item["entry_id"]]
        selected_paths = [
            path_item
            for path_item in item["entry"]["path_entries"]
            if path_item["id"] in set(relevant_item["path_ids"])
        ]
        structured_entries.append(
            {
                "entry_id": item["entry"]["id"],
                "raw_line": item["line"],
                "ref_level": item["entry"]["ref"],
                "factual": item["entry"]["factual"],
                "content": item["entry"]["content"],
                "timestamp": item["entry"]["timestamp"],
                "score": relevant_item["score"],
                "reason": relevant_item["reason"],
                "paths": [
                    {
                        "path_id": path_item["id"],
                        "path": path_item["path"],
                        "directory": path_item["directory"],
                        "resource_type": path_item["resource_type"],
                    }
                    for path_item in selected_paths
                ],
            }
        )
    return format_script_output_block(
        mode="read_today_log",
        response_payload=response_payload,
        entries=structured_entries,
    )


if __name__ == "__main__":
    raise SystemExit(main())

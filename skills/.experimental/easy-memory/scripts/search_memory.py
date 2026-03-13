#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

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
    list_log_files,
    normalize_task_context,
    parse_entry_line,
    require_initialized,
)

_REF_LEVEL_SCORES = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

_TIME_FORMAT = "%Y-%m-%d:%H:%M"

EMPTY_LOG_MESSAGE = (
    "No log entries for today. Created an empty log file; "
    "please continue with the remaining task steps."
)
NO_MATCH_MESSAGE = "No matching entries found for the provided keywords."
IMPORTANT_REMINDER = (
    "IMPORTANT NOTICE: The foregoing search history may be used as material reference "
    "for this task; however, should any subsequent work disclose new information "
    "inconsistent with, superseding, or rendering any entry outdated, you are hereby "
    "required, prior to writing new logs or submitting this task, to correct or update "
    "the relevant entries using the appropriate tool scripts, or to delete them."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search memory logs in the easy-memory directory."
    )
    parser.add_argument(
        "keywords",
        nargs="+",
        help="Keywords (English preferred; space-separated).",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=5,
        help="Maximum number of entries to return (default: 5).",
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


def ref_level_score(value: str) -> int:
    normalized = value.strip().lower()
    if normalized in _REF_LEVEL_SCORES:
        return _REF_LEVEL_SCORES[normalized]
    try:
        return int(normalized)
    except ValueError:
        return 0


def parse_timestamp(value: str) -> datetime:
    try:
        return datetime.strptime(value, _TIME_FORMAT)
    except ValueError:
        return datetime.min


def main() -> int:
    args = parse_args()
    base_dir = log_base_dir(create=True)
    require_initialized(base_dir)

    keywords = [k.lower() for k in args.keywords]
    task_context = normalize_task_context(args.task_context)
    max_results = args.max_results
    if max_results <= 0:
        raise SystemExit("max-results must be a positive integer.")

    log_paths = list_log_files(base_dir)
    if not log_paths:
        log_path_for_date(date.today(), base_dir).touch()
        print(EMPTY_LOG_MESSAGE)
        return 0

    matches = []
    order = 0
    has_any_entries = False
    for log_path in log_paths:
        lines = log_path.read_text(encoding="utf-8").splitlines()
        if lines:
            has_any_entries = True
        for line in lines:
            entry = parse_entry_line(line)
            haystack = line
            if entry:
                related_text = " ".join(
                    f"{item['path']} {item['directory']} {item.get('resource_type', '')}"
                    for item in entry["path_entries"]
                )
                haystack = f"{entry['content']} {related_text}".strip()
            if any(k in haystack.lower() for k in keywords):
                factual_score = 0
                ref_score = 0
                timestamp = datetime.min
                if entry:
                    factual_score = 1 if entry["factual"] else 0
                    ref_score = ref_level_score(entry["ref"])
                    timestamp = parse_timestamp(entry["timestamp"])
                matches.append(
                    {
                        "log": log_path.name,
                        "line": line,
                        "entry": entry,
                        "path_entries": entry["path_entries"] if entry else [],
                        "factual": factual_score,
                        "ref": ref_score,
                        "timestamp": timestamp,
                        "order": order,
                    }
                )
                order += 1

    if not has_any_entries:
        log_path = log_path_for_date(date.today(), base_dir)
        if not log_path.exists():
            log_path.touch()
        print(EMPTY_LOG_MESSAGE)
        return 0

    if not matches:
        print(NO_MATCH_MESSAGE)
        return 0

    matches.sort(
        key=lambda item: (item["factual"], item["ref"], item["timestamp"]),
        reverse=True,
    )
    selected_matches = matches[:max_results]

    agent_output = maybe_render_agent_output(
        task_context=task_context,
        base_dir=base_dir,
        keywords=args.keywords,
        max_results=max_results,
        selected_matches=selected_matches,
    )
    if agent_output is not None:
        print(agent_output)
        return 0

    results: list[str] = []
    for item in selected_matches:
        results.append(f"{item['log']}: {item['line']}")
        for related_line in format_related_path_lines(item["path_entries"]):
            results.append(f"  {related_line}")
    results.append(IMPORTANT_REMINDER)
    print("\n".join(results))
    return 0


def maybe_render_agent_output(
    task_context: str,
    base_dir,
    keywords: list[str],
    max_results: int,
    selected_matches: list[dict],
) -> str | None:
    if not selected_matches:
        return None
    if any(item["entry"] is None for item in selected_matches):
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
        "mode": "search_memory",
        "task_context": task_context,
        "cwd": str(base_dir.parent.resolve()),
        "log_dir": str(base_dir.resolve()),
        "keywords": keywords,
        "max_results": max_results,
        "entries": [
            build_request_entry(item)
            for item in selected_matches
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
        return render_agent_output(parsed_payload, selected_matches)
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
    return {
        "entry_id": item["entry"]["id"],
        "log_file": item["log"],
        "ref_level": item["entry"]["ref"],
        "factual": item["entry"]["factual"],
        "content": item["entry"]["content"],
        "timestamp": item["entry"]["timestamp"],
        "paths": [
            {
                "path_id": path_item["id"],
                "path": path_item["path"],
                "directory": path_item["directory"],
                "resource_type": path_item["resource_type"],
            }
            for path_item in item["path_entries"]
        ],
    }


def render_agent_output(
    response_payload: dict,
    selected_matches: list[dict],
) -> str:
    relevant_entries = response_payload["relevant_entries"]
    entry_lookup = {
        item["entry"]["id"]: item
        for item in selected_matches
    }
    structured_entries: list[dict] = []
    for relevant_item in relevant_entries:
        item = entry_lookup[relevant_item["entry_id"]]
        selected_paths = [
            path_item
            for path_item in item["path_entries"]
            if path_item["id"] in set(relevant_item["path_ids"])
        ]
        structured_entries.append(
            {
                "entry_id": item["entry"]["id"],
                "log_file": item["log"],
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
        mode="search_memory",
        response_payload=response_payload,
        entries=structured_entries,
        important_notice=IMPORTANT_REMINDER,
    )


if __name__ == "__main__":
    raise SystemExit(main())

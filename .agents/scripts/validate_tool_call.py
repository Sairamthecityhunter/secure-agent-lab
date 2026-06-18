#!/usr/bin/env python3
"""validate_tool_call.py — PreToolUse hook for run_command interception.

Invoked by the agent runtime (via hooks.json) before every run_command
execution.  The agent runtime passes the pending tool call as JSON on stdin.
This script reads that payload, applies the allowlist policy defined in
CONTEXT.md, and signals approval or rejection via exit code:

    Exit 0  — command is approved; agent proceeds with execution.
    Exit 1  — command is BLOCKED; agent receives an error and must not execute.

The JSON payload on stdin follows the Antigravity hooks schema:
{
  "tool": "run_command",
  "input": {
    "command": "<the shell command string>",
    "cwd": "<optional working directory>"
  }
}
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Policy: explicitly approved command prefixes
# Extend this list only after deliberate security review.
# ---------------------------------------------------------------------------
APPROVED_PREFIXES: list[str] = [
    # Package / dependency management
    "uv ",
    "uv sync",
    "uv run",
    "pip ",
    # agents-cli operations
    "agents-cli ",
    # Git read-only operations
    "git status",
    "git log",
    "git diff",
    "git show",
    # Code quality
    "ruff ",
    "semgrep ",
    # Safe diagnostics
    "python3 -c",
    "python -c",
    "echo ",
    "cat ",
    "ls ",
    "pwd",
    "which ",
    "head ",
    "tail ",
    # pre-commit (approved per CONTEXT.md §3)
    "pre-commit ",
]

# ---------------------------------------------------------------------------
# Policy: patterns that are ALWAYS blocked regardless of prefix
# ---------------------------------------------------------------------------
BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf\b"),  # destructive recursive delete
    re.compile(r"\bsudo\b"),  # privilege escalation
    re.compile(r"\bcurl\b.*\|\s*(ba)?sh"),  # remote code execution via pipe
    re.compile(r"\bwget\b.*\|\s*(ba)?sh"),
    re.compile(r">\s*/dev/sd[a-z]"),  # raw disk writes
    re.compile(r"\bdd\s+if="),  # disk dump
    re.compile(r"\bchmod\s+777\b"),  # world-writable permission grants
    re.compile(r"\beval\s+"),  # dynamic code evaluation
    re.compile(r"\bexec\s+"),  # process replacement
    re.compile(r"AIzaSy[A-Za-z0-9_\-]+"),  # hardcoded Google API key in command
]


def load_payload() -> dict[str, Any]:
    """Read and parse the JSON tool-call payload from stdin."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            # No payload — fail open with a warning (shouldn't happen in practice).
            print(
                "[hooks] WARNING: empty stdin payload; approving by default.",
                file=sys.stderr,
            )
            sys.exit(0)
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[hooks] ERROR: could not parse hook payload: {exc}", file=sys.stderr)
        sys.exit(1)


def extract_command(payload: dict[str, Any]) -> str:
    """Pull the command string out of the tool-call payload."""
    try:
        return str(payload["input"]["command"]).strip()
    except KeyError:
        print("[hooks] ERROR: payload missing 'input.command' field.", file=sys.stderr)
        sys.exit(1)


def is_blocked(command: str) -> tuple[bool, str]:
    """Return (blocked, reason) for the given command string."""
    # Hard-block patterns take priority
    for pattern in BLOCKED_PATTERNS:
        if pattern.search(command):
            return True, f"matches blocked pattern: {pattern.pattern!r}"

    # Must match at least one approved prefix
    normalized = command.lstrip()
    for prefix in APPROVED_PREFIXES:
        if normalized.startswith(prefix) or normalized == prefix.rstrip():
            return False, "approved prefix match"

    return True, "no approved prefix matched — add to APPROVED_PREFIXES if intentional"


def main() -> None:
    payload = load_payload()
    command = extract_command(payload)

    blocked, reason = is_blocked(command)

    if blocked:
        print(
            f"[hooks] BLOCKED run_command: {command!r}\n"
            f"        Reason: {reason}\n"
            f"        Per CONTEXT.md §2: shell execution requires explicit approval in hooks.json.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[hooks] APPROVED run_command: {command!r}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()

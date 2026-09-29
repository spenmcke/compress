"""cmprs prompt and conservative output protocol.

Adapted from the upstream prompt and output parser (MIT).
See THIRD_PARTY_NOTICES.md for attribution.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re


FOCUS_RE = re.compile(
    r"^\s*#\s*(?:compress|compress-compress|cmprs[-_ ]focus|context[-_ ]focus(?:[-_ ]question)?)\s*:\s*(.*?)\s*$",
    re.IGNORECASE,
)
DEFAULT_FOCUS = (
    "Which exact results, errors, identifiers, paths, diagnostics, and code lines "
    "from this command output are needed for the next action?"
)
RAW_FOCUSES = frozenset({"raw", "verbatim", "uncompressed", "full output"})
RANGE_RE = re.compile(r"^(?P<start>[1-9]\d*)(?:-(?P<end>[1-9]\d*))?$")
TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


@dataclass(frozen=True)
class Resolution:
    effective_output: str
    kind: str
    valid: bool
    keep_original: bool
    reason: str


def estimate_tokens(text: str) -> int:
    """Cheap, deterministic token estimate used only for gating and telemetry."""
    lexical = len(TOKEN_RE.findall(text))
    byte_estimate = math.ceil(len(text.encode("utf-8", errors="replace")) / 4)
    return max(lexical, byte_estimate)


def extract_focus(command: str) -> tuple[str | None, str]:
    """Extract one strict standalone focus comment and remove it from prompt text."""
    matches: list[tuple[int, str]] = []
    lines = command.splitlines()
    for index, line in enumerate(lines):
        match = FOCUS_RE.match(line)
        if match:
            matches.append((index, match.group(1).strip()))
    first_nonblank = next((index for index, line in enumerate(lines) if line.strip()), None)
    if (
        len(matches) != 1
        or not matches[0][1]
        or matches[0][0] != first_nonblank
        or len(matches[0][1]) > 1000
    ):
        return None, command
    index, focus = matches[0]
    prompt_command = "\n".join(line for i, line in enumerate(lines) if i != index).strip()
    return focus, prompt_command


def resolve_focus(command: str) -> tuple[str | None, str]:
    """Use an explicit legacy focus or derive a conservative CLI focus."""
    focus, prompt_command = extract_focus(command)
    if focus is not None:
        return focus, prompt_command
    if not command.strip() or any(FOCUS_RE.match(line) for line in command.splitlines()):
        return None, command
    return DEFAULT_FOCUS, command


def number_lines(text: str) -> str:
    return "\n".join(f"{index}> {line}" for index, line in enumerate(text.split("\n"), 1))


def render_prompt(goal: str, focus: str, command: str, output: str) -> str:
    """Render the released cmprs model's expected inference prompt."""
    return f"""You are a context compression assistant. Compress the tool output while preserving the evidence the agent needs for the correct next tool call.

## Global Task Goal
{goal or "(not provided)"}

## Context Focus Question
{focus}

## Tool Call Executed
$ {command or "(none)"}

## Original Tool Output
{number_lines(output) if output else "(empty)"}

## Instructions
Return EXACTLY one JSON object with fields in this order:
1. `"type"`
2. `"content"`

Global rules:
- The compressed output must be less than or equal to the original length.
- Return JSON only. Do NOT add commentary, headings, markdown fences, or wrapper text.
- Prioritize information preservation over aggressive compression. When in doubt, keep the content.

Choose exactly one case:

Case 1: No compression needed
- Use when the output is already concise or when compression may remove needed evidence.
- Output Example: `{{"type":"unchanged","content":null}}`
- `"content"` must be `null`.

Case 2: Plain-text compression
- Use for summaries of non-code outputs such as listings, passing test summaries, stats, and logs where exact lines are not needed.
- Output Example: `{{"type":"plain","content":"All selected tests passed; no failure traceback was present."}}`
- `"content"` must contain only the compressed tool output content.
- Do not include line numbers like `1>` or markdown code fences.

Case 3: Code-snippet compression
- Use for source code, tests, diffs/patches, grep/rg context, stack traces, or any output where line-level structure, syntax, or adjacency may matter.
- Output Example: `{{"type":"code","content":["1-5:license header and imports","40-58:unrelated helper function"]}}`
- `"content"` must be a non-empty JSON array of sorted, non-overlapping omit ranges within Original Tool Output.
- Entry format: `"N:summary"` or `"N-M:summary"`. Lines not covered by omit ranges are kept verbatim for the agent.
- Omit only lines clearly irrelevant to the CFQ, do not omit all nonblank original lines. The agent must see verbatim evidence, not only `(compressed N lines: ...)` markers.
- If the CFQ asks to show/examine a class/function/method including X, do not omit that class/function/method body.
- For broad reads like "read the file", "understand the implementation", or "understand expected behavior", compress conservatively and keep potentially relevant code/test bodies.
- Keep enclosing function, class, and control-flow structure needed to understand any kept line.
- If relevance is unclear, use `"unchanged"`.

## Compressed Output
"""


def render_all_content_retry_prompt(
    goal: str, focus: str, command: str, output: str
) -> str:
    """Render one corrective retry after the model omitted every exact content line."""
    return render_prompt(goal, focus, command, output) + """

## Correction After Rejected Attempt
Your previous response omitted every nonblank original line. That is unsafe for this
integration. Return `unchanged`, or return code omit ranges that leave the exact lines
answering the Context Focus Question verbatim. Do not return a summary-only result.
"""


def resolve_response(raw: str, original: str) -> Resolution:
    """Validate model output and fail back to the byte-identical original."""
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return Resolution(original, "fallback", False, True, "invalid_json")
    if not isinstance(parsed, dict) or set(parsed) != {"type", "content"}:
        return Resolution(original, "fallback", False, True, "invalid_schema")

    kind = parsed.get("type")
    content = parsed.get("content")
    if kind == "unchanged" and content is None:
        return Resolution(original, "unchanged", True, True, "model_unchanged")

    if kind == "plain" and isinstance(content, str) and content.strip():
        effective = content.strip()
        if estimate_tokens(effective) >= estimate_tokens(original) or len(effective) >= len(original):
            return Resolution(original, "fallback", False, True, "not_smaller")
        return Resolution(effective, "plain", True, False, "compressed")

    if kind != "code" or not isinstance(content, list) or not content:
        return Resolution(original, "fallback", False, True, "invalid_schema")

    lines = original.split("\n")
    ranges: list[tuple[int, int, str]] = []
    previous_end = 0
    for entry in content:
        if not isinstance(entry, str) or not entry.strip():
            return Resolution(original, "fallback", False, True, "invalid_range")
        reference, separator, summary = entry.strip().partition(":")
        match = RANGE_RE.fullmatch(reference.strip())
        if not match:
            return Resolution(original, "fallback", False, True, "invalid_range")
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if start > end or start <= previous_end or end > len(lines):
            return Resolution(original, "fallback", False, True, "invalid_range")
        ranges.append((start, end, summary.strip() if separator else ""))
        previous_end = end

    omitted = {line_no for start, end, _ in ranges for line_no in range(start, end + 1)}
    nonblank = {index for index, line in enumerate(lines, 1) if line.strip()}
    if nonblank and nonblank.issubset(omitted):
        return Resolution(original, "fallback", False, True, "omits_all_content")

    result: list[str] = []
    cursor = 1
    for start, end, summary in ranges:
        result.extend(lines[cursor - 1 : start - 1])
        count = end - start + 1
        suffix = f": {summary}" if summary else ""
        result.append(f"(compressed {count} lines{suffix})")
        cursor = end + 1
    result.extend(lines[cursor - 1 :])
    effective = "\n".join(result)
    if estimate_tokens(effective) >= estimate_tokens(original) or len(effective) >= len(original):
        return Resolution(original, "fallback", False, True, "not_smaller")
    return Resolution(effective, "code", True, False, "compressed")

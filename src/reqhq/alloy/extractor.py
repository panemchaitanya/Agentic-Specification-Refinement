"""Extract fenced Alloy modules and @trace line-to-REQ-ID mappings from literate Spec.md."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re

from reqhq.alloy.models import ExtractedAlloyBlock

_FENCE_OPEN_RE = re.compile(
    r"^\s*```alloy\s+module\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*$",
    re.IGNORECASE,
)
_FENCE_CLOSE_RE = re.compile(r"^\s*```\s*$")
_TRACE_MARKER_RE = re.compile(r"//\s*@trace\s+([^\r\n]+)")
_REQ_ID_TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*-[A-Za-z0-9_-]+)\b")
_DECL_SYMBOL_RE = re.compile(
    r"^\s*(?:(?:abstract|one|lone|some|var)\s+)*"
    r"(?:sig|fact|pred|fun|assert)\s+([A-Za-z_][A-Za-z0-9_']*)"
)


def _parse_trace_ids(line: str) -> list[str]:
    """Extract ordered, deduplicated REQ-ID tokens from any `// @trace ...` marker on a line."""
    match = _TRACE_MARKER_RE.search(line)
    if not match:
        return []
    raw = match.group(1).strip()
    tokens = _REQ_ID_TOKEN_RE.findall(raw)
    if not tokens:
        # Fallback for non-hyphenated custom IDs separated by commas/spaces.
        tokens = [
            tok.strip(" ,;")
            for tok in re.split(r"[\s,;]+", raw)
            if tok.strip(" ,;") and re.match(r"^[A-Za-z][A-Za-z0-9_.-]*$", tok.strip(" ,;"))
        ]
    seen: set[str] = set()
    ordered: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.add(tok)
            ordered.append(tok)
    return ordered


def _build_line_to_req_map(code: str) -> dict[int, list[str]]:
    """Map 1-based Alloy code lines to REQ-IDs from inline and block-scoped `// @trace` comments."""
    lines = code.splitlines()
    line_map: dict[int, list[str]] = {}

    pending_trace_ids: list[str] = []
    block_trace_ids: list[str] = []
    brace_depth = 0
    in_traced_block = False

    def _add(line_no: int, ids: list[str]) -> None:
        bucket = line_map.setdefault(line_no, [])
        for req_id in ids:
            if req_id not in bucket:
                bucket.append(req_id)

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        trace_ids = _parse_trace_ids(line)

        if trace_ids:
            _add(idx, trace_ids)
            is_comment_only = stripped.startswith("//")
            if is_comment_only and not in_traced_block:
                for rid in trace_ids:
                    if rid not in pending_trace_ids:
                        pending_trace_ids.append(rid)
                continue
            elif is_comment_only and in_traced_block:
                # Inline trace comment inside an active traced block applies to the block too.
                for rid in trace_ids:
                    if rid not in block_trace_ids:
                        block_trace_ids.append(rid)
                continue

        if not stripped:
            if not in_traced_block:
                pending_trace_ids = []
            continue

        # Preserve pending @trace IDs across non-@trace comment lines (e.g. description comments).
        if stripped.startswith("//") or stripped.startswith("--"):
            if in_traced_block:
                _add(idx, block_trace_ids)
            elif pending_trace_ids:
                _add(idx, pending_trace_ids)
            continue

        # Strip single-line comment before counting braces.
        code_part = line.split("//", 1)[0].split("--", 1)[0]
        opens = code_part.count("{")
        closes = code_part.count("}")

        if pending_trace_ids and not in_traced_block:
            _add(idx, pending_trace_ids)
            if opens > 0:
                in_traced_block = True
                block_trace_ids = list(pending_trace_ids)
                pending_trace_ids = []
                brace_depth = opens - closes
                if brace_depth <= 0:
                    in_traced_block = False
                    block_trace_ids = []
                    brace_depth = 0
                continue
            else:
                # Single-line declaration/check following a @trace comment.
                pending_trace_ids = []
                continue

        if trace_ids and not in_traced_block and opens > 0:
            in_traced_block = True
            block_trace_ids = list(trace_ids)
            brace_depth = opens - closes
            if brace_depth <= 0:
                in_traced_block = False
                block_trace_ids = []
                brace_depth = 0
            continue

        if in_traced_block:
            _add(idx, block_trace_ids)
            brace_depth += opens - closes
            if brace_depth <= 0:
                in_traced_block = False
                block_trace_ids = []
                brace_depth = 0

    return line_map


def extract_alloy_blocks(spec_md: str) -> dict[str, ExtractedAlloyBlock]:
    """Extract all ```alloy module=<name> blocks from literate Spec.md.

    `start_line` and `end_line` are 1-based line numbers in `spec_md`.
    `line_to_req_ids` maps 1-based line numbers within each extracted `.als` block to REQ-IDs.
    """
    blocks: dict[str, ExtractedAlloyBlock] = {}
    lines = spec_md.splitlines()

    current_module: str | None = None
    current_start: int = 0
    current_lines: list[str] = []

    for line_no, line in enumerate(lines, start=1):
        if current_module is None:
            open_match = _FENCE_OPEN_RE.match(line)
            if open_match:
                module_name = open_match.group(1)
                if module_name in blocks:
                    raise ValueError(f"Duplicate Alloy module block in Spec.md: {module_name!r}")
                current_module = module_name
                current_start = line_no + 1
                current_lines = []
        else:
            if _FENCE_CLOSE_RE.match(line):
                code = "\n".join(current_lines)
                end_line = max(current_start, line_no - 1)
                blocks[current_module] = ExtractedAlloyBlock(
                    module_name=current_module,
                    code=code,
                    start_line=current_start,
                    end_line=end_line,
                    line_to_req_ids=_build_line_to_req_map(code),
                )
                current_module = None
                current_lines = []
            else:
                current_lines.append(line)

    if current_module is not None:
        raise ValueError(f"Unclosed Alloy fenced block for module {current_module!r}")

    return blocks


def write_alloy_files(spec_md: str, out_dir: Path) -> dict[str, Path]:
    """Extract Alloy modules from Spec.md and write `<module_name>.als` files into `out_dir`."""
    blocks = extract_alloy_blocks(spec_md)
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for module_name, block in blocks.items():
        als_path = target_dir / f"{module_name}.als"
        content = block.code if block.code.endswith("\n") else block.code + "\n"
        als_path.write_text(content, encoding="utf-8")
        written[module_name] = als_path
    return written


def replace_alloy_block(spec_md: str, module_name: str, new_code: str) -> str:
    """Replace the contents of a named ```alloy module=<name> block inside Spec.md."""
    lines = spec_md.splitlines()
    out: list[str] = []
    in_target = False
    replaced = False

    for line in lines:
        if not in_target:
            open_match = _FENCE_OPEN_RE.match(line)
            if open_match and open_match.group(1) == module_name:
                in_target = True
                replaced = True
                out.append(line)
                out.extend(new_code.rstrip("\r\n").splitlines())
                continue
            out.append(line)
        else:
            if _FENCE_CLOSE_RE.match(line):
                in_target = False
                out.append(line)

    if not replaced:
        raise ValueError(f"Alloy module block {module_name!r} not found in Spec.md")

    trailing_newline = "\n" if spec_md.endswith("\n") else ""
    return "\n".join(out) + trailing_newline


def build_req_to_alloy_refs(
    blocks: Mapping[str, ExtractedAlloyBlock],
) -> dict[str, list[str]]:
    """Build deterministic `REQ-ID -> [module.als::symbol (L#)]` traceability references."""
    refs: dict[str, list[str]] = {}
    for module_name in sorted(blocks):
        block = blocks[module_name]
        code_lines = block.code.splitlines()
        req_first_symbol: dict[str, str] = {}

        for line_no in sorted(block.line_to_req_ids):
            line_text = code_lines[line_no - 1] if 1 <= line_no <= len(code_lines) else ""
            decl_match = _DECL_SYMBOL_RE.match(line_text)
            symbol = decl_match.group(1) if decl_match else None
            for req_id in block.line_to_req_ids[line_no]:
                if symbol is not None and req_id not in req_first_symbol:
                    req_first_symbol[req_id] = f"{module_name}.als::{symbol}"
                elif req_id not in req_first_symbol:
                    req_first_symbol[req_id] = f"{module_name}.als:L{line_no}"
                elif "::" not in req_first_symbol[req_id] and symbol is not None:
                    req_first_symbol[req_id] = f"{module_name}.als::{symbol}"

        for req_id, ref in sorted(req_first_symbol.items()):
            bucket = refs.setdefault(req_id, [])
            if ref not in bucket:
                bucket.append(ref)

    return dict(sorted(refs.items()))


__all__ = [
    "build_req_to_alloy_refs",
    "extract_alloy_blocks",
    "replace_alloy_block",
    "write_alloy_files",
]

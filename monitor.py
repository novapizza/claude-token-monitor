#!/usr/bin/env python3
"""Claude Code token usage monitor.

Reads session JSONL logs from ~/.claude/projects/ and reports token
usage, estimated costs, and trends. Commands: summary, daily, projects,
sessions, export, live.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator

# Windows consoles often default to cp1252; force UTF-8 so rich's
# ellipsis and box-drawing characters render correctly.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

try:
    from rich import box
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    RICH = True
except ImportError:  # graceful fallback
    RICH = False


# Pricing per 1M tokens (USD). Source: platform.claude.com/docs/en/docs/about-claude/pricing
# Order matters: most-specific prefix first (first substring match wins in model_price).
# cw_5m = 5-minute cache write (1.25x input), cw_1h = 1-hour cache write (2x input).
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7":   {"in":  5.0, "out": 25.0, "cr": 0.50, "cw_5m":  6.25, "cw_1h": 10.0},
    "claude-opus-4-6":   {"in":  5.0, "out": 25.0, "cr": 0.50, "cw_5m":  6.25, "cw_1h": 10.0},
    "claude-opus-4-5":   {"in":  5.0, "out": 25.0, "cr": 0.50, "cw_5m":  6.25, "cw_1h": 10.0},
    "claude-opus-4-1":   {"in": 15.0, "out": 75.0, "cr": 1.50, "cw_5m": 18.75, "cw_1h": 30.0},
    "claude-opus-4":     {"in": 15.0, "out": 75.0, "cr": 1.50, "cw_5m": 18.75, "cw_1h": 30.0},
    "claude-sonnet-4":   {"in":  3.0, "out": 15.0, "cr": 0.30, "cw_5m":  3.75, "cw_1h":  6.0},
    "claude-haiku-4":    {"in":  1.0, "out":  5.0, "cr": 0.10, "cw_5m":  1.25, "cw_1h":  2.0},
    "claude-3-5-sonnet": {"in":  3.0, "out": 15.0, "cr": 0.30, "cw_5m":  3.75, "cw_1h":  6.0},
    "claude-3-5-haiku":  {"in":  0.8, "out":  4.0, "cr": 0.08, "cw_5m":  1.00, "cw_1h":  1.6},
    "claude-3-opus":     {"in": 15.0, "out": 75.0, "cr": 1.50, "cw_5m": 18.75, "cw_1h": 30.0},
    "claude-3-haiku":    {"in": 0.25, "out": 1.25, "cr": 0.03, "cw_5m":  0.30, "cw_1h":  0.48},
}
DEFAULT_PRICE = {"in": 3.0, "out": 15.0, "cr": 0.30, "cw_5m": 3.75, "cw_1h": 6.0}


def model_price(model: str) -> dict[str, float]:
    m = (model or "").lower()
    for prefix, price in PRICING.items():
        if prefix in m:
            return price
    return DEFAULT_PRICE


def calc_cost(usage: dict, model: str) -> float:
    p = model_price(model)
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    # Cache writes come in two TTLs at different rates. Newer payloads split
    # them under `cache_creation`; legacy payloads only carry the combined
    # `cache_creation_input_tokens`, which we treat as 5-minute (the default).
    creation = usage.get("cache_creation") or {}
    cw_5m = int(creation.get("ephemeral_5m_input_tokens") or 0)
    cw_1h = int(creation.get("ephemeral_1h_input_tokens") or 0)
    if not creation:
        cw_5m = int(usage.get("cache_creation_input_tokens") or 0)
    return (
        inp   * p["in"]    / 1_000_000
        + out * p["out"]   / 1_000_000
        + cr  * p["cr"]    / 1_000_000
        + cw_5m * p["cw_5m"] / 1_000_000
        + cw_1h * p["cw_1h"] / 1_000_000
    )


# Context window cap per model family (in tokens). Used by the live monitor
# and rule 11 (large-context) to scale "approaching the cap" thresholds —
# a 750K call on Opus 4.7 (1M cap) is at 75%, the same risk profile as a
# 150K call on a 200K-cap model. Sonnet 4.x 1M-tier is plan-conditional and
# not detectable from the model ID alone, so we keep it at the default.
CONTEXT_CAP: dict[str, int] = {
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-7": 1_000_000,
}
DEFAULT_CONTEXT_CAP = 200_000


def context_cap(model: str) -> int:
    m = (model or "").lower()
    for prefix, cap in CONTEXT_CAP.items():
        if prefix in m:
            return cap
    return DEFAULT_CONTEXT_CAP


def projects_root() -> Path:
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
    return home / ".claude" / "projects"


def decode_project(folder: str) -> str:
    # Claude Code encodes "C:\Users\foo\bar" as "c--Users-foo-bar".
    if len(folder) >= 3 and folder[1:3] == "--":
        return folder[0].upper() + ":/" + folder[3:].replace("-", "/")
    return folder.replace("-", "/")


def shorten_path(path: str) -> str:
    """Replace the user home prefix with ~ for display."""
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    if not home:
        return path
    norm_home = home.replace("\\", "/").rstrip("/")
    if path.lower().startswith(norm_home.lower()):
        return "~" + path[len(norm_home):]
    return path


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------- Time-window filter ---------------------------- #


def _parse_duration(s: str) -> timedelta:
    """Parse '7d', '24h', '30m', '2w' shorthand into a timedelta."""
    s = (s or "").strip().lower()
    if not s:
        raise ValueError("empty duration")
    unit = s[-1]
    try:
        n = float(s[:-1])
    except ValueError as exc:
        raise ValueError(
            f"invalid duration: {s!r} (expected '7d', '24h', '30m', '2w')"
        ) from exc
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    if unit == "w":
        return timedelta(weeks=n)
    raise ValueError(f"unknown duration unit {unit!r} (use m/h/d/w)")


def _parse_local_bound(s: str, *, end_of_day: bool = False) -> datetime:
    """Parse YYYY-MM-DD (local-date) or full ISO timestamp into an aware datetime.

    end_of_day=True bumps a date-only input to the start of the next day, so
    that `--until 2026-04-30` includes everything up through April 30 23:59.
    """
    s = s.strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        d = date.fromisoformat(s)
        if end_of_day:
            d = d + timedelta(days=1)
        return datetime.combine(d, datetime.min.time()).astimezone()
    return datetime.fromisoformat(s).astimezone()


def parse_window(args) -> tuple[datetime | None, datetime | None]:
    """Resolve --since / --until / --last on `args` into (since, until).

    --last conflicts with --since. Returns (None, None) when no flags set.
    Both bounds are timezone-aware (local zone).
    """
    since = until = None
    last = getattr(args, "last", None)
    s = getattr(args, "since", None)
    u = getattr(args, "until", None)
    if last:
        if s:
            raise SystemExit("error: --last conflicts with --since")
        since = datetime.now().astimezone() - _parse_duration(last)
    elif s:
        since = _parse_local_bound(s)
    if u:
        until = _parse_local_bound(u, end_of_day=True)
    return since, until


def filter_records(records: list, args) -> list:
    """Apply --since/--until/--last bounds from args, if present."""
    since, until = parse_window(args)
    if since is None and until is None:
        return records
    out = []
    for r in records:
        ts = parse_ts(r.timestamp)
        if ts is None:
            continue
        if since and ts < since:
            continue
        if until and ts >= until:
            continue
        out.append(r)
    return out


@dataclass
class Record:
    project: str
    session_id: str
    timestamp: str
    model: str
    usage: dict
    cost: float
    cwd: str
    msg_id: str
    tools: list = None  # list[str] of tool names used in this assistant turn
    read_paths: list = None  # list[str] of file_path args passed to the Read tool

    def __post_init__(self):
        if self.tools is None:
            self.tools = []
        if self.read_paths is None:
            self.read_paths = []


def _extract_tool_info(msg: dict) -> tuple[list[str], list[str]]:
    """Return (tool_names, read_paths) from an assistant message."""
    content = msg.get("content")
    if not isinstance(content, list):
        return [], []
    tools: list[str] = []
    paths: list[str] = []
    for c in content:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        name = c.get("name") or ""
        if name:
            tools.append(name)
        if name == "Read":
            inp = c.get("input")
            if isinstance(inp, dict):
                fp = inp.get("file_path")
                if fp:
                    paths.append(fp)
    return tools, paths


def iter_records(root: Path) -> Iterator[Record]:
    """Yield one Record per distinct assistant API call.

    Multiple JSONL entries share the same message.id when an assistant
    turn has several content blocks. Their `usage` block is the full
    per-call total and is duplicated — so we dedupe by (session, msg_id)
    and accumulate tool_use blocks across entries with the same id.
    """
    if not root.exists():
        return

    # key -> {"tools": [...], "read_paths": [...], "base": Record-kwargs or None}
    partial: dict[tuple[str, str], dict] = {}
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            try:
                f = jsonl.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with f:
                for line in f:
                    line = line.strip()
                    if not line or line[0] != "{":
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("type") != "assistant":
                        continue
                    msg = e.get("message") or {}
                    msg_id = msg.get("id") or ""
                    session_id = e.get("sessionId") or ""
                    key = (session_id, msg_id)

                    info = partial.get(key)
                    if info is None:
                        info = {"tools": [], "read_paths": [], "base": None}
                        partial[key] = info

                    t, p = _extract_tool_info(msg)
                    info["tools"].extend(t)
                    info["read_paths"].extend(p)

                    if info["base"] is None:
                        usage = msg.get("usage")
                        if usage:
                            model = msg.get("model") or "unknown"
                            info["base"] = {
                                "project": project_dir.name,
                                "session_id": session_id,
                                "timestamp": e.get("timestamp") or "",
                                "model": model,
                                "usage": usage,
                                "cost": calc_cost(usage, model),
                                "cwd": e.get("cwd") or "",
                                "msg_id": msg_id,
                            }

    for info in partial.values():
        base = info["base"]
        if base is None:
            continue
        yield Record(**base, tools=info["tools"], read_paths=info["read_paths"])


def empty_agg() -> dict:
    return {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0, "calls": 0, "last": None}


def aggregate(records: Iterable[Record], key_fn: Callable[[Record], str | None]) -> dict[str, dict]:
    agg: dict[str, dict] = defaultdict(empty_agg)
    for r in records:
        k = key_fn(r)
        if k is None:
            continue
        a = agg[k]
        u = r.usage
        a["in"]  += int(u.get("input_tokens") or 0)
        a["out"] += int(u.get("output_tokens") or 0)
        a["cr"]  += int(u.get("cache_read_input_tokens") or 0)
        a["cw"]  += int(u.get("cache_creation_input_tokens") or 0)
        a["cost"] += r.cost
        a["calls"] += 1
        ts = parse_ts(r.timestamp)
        if ts and (a["last"] is None or ts > a["last"]):
            a["last"] = ts
    return agg


def fmt_num(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(int(n))


def fmt_cost(c: float) -> str:
    if c >= 1:
        return f"${c:,.2f}"
    if c == 0:
        return "$0"
    return f"${c:.4f}"


def load_all() -> list[Record]:
    return list(iter_records(projects_root()))


def load_records(args) -> list[Record]:
    """load_all() with --since/--until/--last from args applied."""
    return filter_records(load_all(), args)


# ----------------------- Tier-2 routing analytics ----------------------- #


def _subagent_first_prompt(subagent_file: Path) -> str:
    """Return the first user-message content of a subagent session (the prompt
    the delegator sent). Empty string on any error."""
    try:
        with subagent_file.open("r", encoding="utf-8", errors="replace") as f:
            first = f.readline()
    except OSError:
        return ""
    try:
        d = json.loads(first)
    except json.JSONDecodeError:
        return ""
    msg = d.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts) if parts else ""
    return ""


def _sum_subagent_usage(subagent_file: Path) -> dict:
    """Sum token usage across all assistant messages in a subagent transcript.
    Returns dict with in/out/cr/cw token totals, actual cost (using the
    subagent's recorded model pricing) and hypothetical cost if the same
    tokens had run on Opus."""
    agg = {"in": 0, "out": 0, "cr": 0, "cw": 0}
    model = ""
    try:
        f = subagent_file.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return {**agg, "cost_actual": 0.0, "cost_opus": 0.0, "model": model}
    with f:
        for line in f:
            line = line.strip()
            if not line or line[0] != "{":
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            usage = msg.get("usage")
            if not usage:
                continue
            if not model:
                model = msg.get("model") or ""
            agg["in"]  += int(usage.get("input_tokens") or 0)
            agg["out"] += int(usage.get("output_tokens") or 0)
            agg["cr"]  += int(usage.get("cache_read_input_tokens") or 0)
            agg["cw"]  += int(usage.get("cache_creation_input_tokens") or 0)
    def _cost_with(price: dict) -> float:
        # Aggregated cw is the combined 5m+1h token count (the split is only
        # recoverable from raw per-call usage). Price at the 5m rate, which is
        # the default TTL — slight under-estimate when 1h caching is in play,
        # but consistent across both sides of the actual-vs-Opus comparison.
        return (
            agg["in"]  * price["in"]    / 1_000_000
            + agg["out"] * price["out"] / 1_000_000
            + agg["cr"]  * price["cr"]  / 1_000_000
            + agg["cw"]  * price["cw_5m"] / 1_000_000
        )
    cost_actual = _cost_with(model_price(model))
    # Hypothetical baseline: the parent would have done this work on its own
    # model. Today that's Opus 4.6/4.7 at $5/$25 — not legacy Opus 4 at $15/$75.
    cost_opus = _cost_with(PRICING["claude-opus-4-7"])
    return {**agg, "cost_actual": cost_actual, "cost_opus": cost_opus, "model": model}


def collect_routing_stats(root: Path, project_filter: str | None = None) -> list[dict]:
    """Scan parent sessions for Agent→routine-worker invocations and link each
    to its subagent transcript (matched by prompt fingerprint) to compute
    per-delegation savings (Opus hypothetical cost − Sonnet actual cost).

    Returns a list of delegation dicts, one per invocation, with keys:
      project, session_id, timestamp, description, prompt,
      in/out/cr/cw, cost_actual, cost_opus, saved, model, matched.
    """
    delegations: list[dict] = []
    if not root.exists():
        return delegations

    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        if project_filter:
            q = project_filter.lower()
            if (q not in decode_project(project_dir.name).lower()
                    and q not in project_dir.name.lower()):
                continue
        for parent_file in project_dir.glob("*.jsonl"):
            session_id = parent_file.stem
            subagent_dir = project_dir / session_id / "subagents"

            parent_calls: list[dict] = []
            try:
                pf = parent_file.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with pf:
                for line in pf:
                    line = line.strip()
                    if not line or line[0] != "{":
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("type") != "assistant":
                        continue
                    msg = e.get("message") or {}
                    for b in (msg.get("content") or []):
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") != "tool_use" or b.get("name") != "Agent":
                            continue
                        inp = b.get("input") or {}
                        if inp.get("subagent_type") != "routine-worker":
                            continue
                        parent_calls.append({
                            "description": (inp.get("description") or "").strip(),
                            "prompt": (inp.get("prompt") or "").strip(),
                            "timestamp": e.get("timestamp") or "",
                        })

            if not parent_calls:
                continue

            # Fingerprint each subagent file by the first 500 chars of its
            # first user message (= the prompt the delegator sent).
            subagent_by_fp: dict[str, Path] = {}
            if subagent_dir.exists():
                for sf in subagent_dir.glob("agent-*.jsonl"):
                    fp = _subagent_first_prompt(sf)[:500].strip()
                    if fp:
                        subagent_by_fp.setdefault(fp, sf)

            for pc in parent_calls:
                fp = pc["prompt"][:500].strip()
                sf = subagent_by_fp.get(fp)
                base = {
                    "project": project_dir.name,
                    "session_id": session_id,
                    "timestamp": pc["timestamp"],
                    "description": pc["description"],
                    "prompt": pc["prompt"],
                    "in": 0, "out": 0, "cr": 0, "cw": 0,
                    "cost_actual": 0.0, "cost_opus": 0.0,
                    "saved": 0.0, "model": "", "matched": False,
                }
                if sf is not None:
                    u = _sum_subagent_usage(sf)
                    base.update({
                        "in": u["in"], "out": u["out"],
                        "cr": u["cr"], "cw": u["cw"],
                        "cost_actual": u["cost_actual"],
                        "cost_opus": u["cost_opus"],
                        "saved": u["cost_opus"] - u["cost_actual"],
                        "model": u["model"],
                        "matched": bool(u["in"] or u["out"] or u["cr"] or u["cw"]),
                    })
                delegations.append(base)
    return delegations


# ------------------------------- Commands ------------------------------- #


def cmd_summary(args) -> None:
    records = load_records(args)
    if not records:
        print("No usage records found in", projects_root())
        return

    total = empty_agg()
    for r in records:
        u = r.usage
        total["in"]   += int(u.get("input_tokens") or 0)
        total["out"]  += int(u.get("output_tokens") or 0)
        total["cr"]   += int(u.get("cache_read_input_tokens") or 0)
        total["cw"]   += int(u.get("cache_creation_input_tokens") or 0)
        total["cost"] += r.cost
        total["calls"] += 1

    by_model = aggregate(records, lambda r: r.model)
    sessions = {r.session_id for r in records if r.session_id}

    if not RICH:
        print(f"Sessions: {len(sessions)}   API calls: {total['calls']}")
        print(f"Input:  {fmt_num(total['in'])}   Output: {fmt_num(total['out'])}")
        print(f"Cache R:{fmt_num(total['cr'])}   Cache W:{fmt_num(total['cw'])}")
        print(f"Cost:   {fmt_cost(total['cost'])}")
        return

    console = Console()
    console.rule("[bold]Claude Code Token Usage[/bold]")
    console.print(f"Sessions: [cyan]{len(sessions)}[/cyan]   "
                  f"API calls: [cyan]{total['calls']}[/cyan]   "
                  f"Est. cost: [green]{fmt_cost(total['cost'])}[/green]")

    t = Table(title="Totals", box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Metric"); t.add_column("Tokens", justify="right"); t.add_column("% of tokens", justify="right")
    grand = total["in"] + total["out"] + total["cr"] + total["cw"]
    def pct(n: int) -> str:
        return f"{(n / grand * 100):.1f}%" if grand else "0%"
    t.add_row("Input",       fmt_num(total["in"]),  pct(total["in"]))
    t.add_row("Output",      fmt_num(total["out"]), pct(total["out"]))
    t.add_row("Cache read",  fmt_num(total["cr"]),  pct(total["cr"]))
    t.add_row("Cache write", fmt_num(total["cw"]),  pct(total["cw"]))
    console.print(t)

    t = Table(title="By Model", box=box.SIMPLE_HEAVY)
    t.add_column("Model")
    t.add_column("Calls", justify="right")
    t.add_column("Input", justify="right")
    t.add_column("Output", justify="right")
    t.add_column("Cache R", justify="right")
    t.add_column("Cache W", justify="right")
    t.add_column("Cost", justify="right")
    for model, a in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"]):
        t.add_row(
            model, str(a["calls"]),
            fmt_num(a["in"]), fmt_num(a["out"]),
            fmt_num(a["cr"]), fmt_num(a["cw"]),
            fmt_cost(a["cost"]),
        )
    console.print(t)

    suggestions = analyze_suggestions(records)
    _render_alert_banners(console, suggestions)
    if suggestions:
        _render_suggestions(console, suggestions, top=5)


def cmd_daily(args) -> None:
    records = load_records(args)
    by_day = aggregate(
        records,
        lambda r: parse_ts(r.timestamp).date().isoformat() if parse_ts(r.timestamp) else None,
    )
    days = sorted(by_day.keys(), reverse=True)[: args.days]
    if not days:
        print("No dated records found.")
        return

    if not RICH:
        for d in days:
            a = by_day[d]
            print(f"{d}  calls={a['calls']:4d}  in={fmt_num(a['in']):>8s}  "
                  f"out={fmt_num(a['out']):>8s}  cost={fmt_cost(a['cost'])}")
        return

    console = Console()
    t = Table(title=f"Daily Usage (last {len(days)})", box=box.SIMPLE_HEAVY)
    t.add_column("Date"); t.add_column("Calls", justify="right")
    t.add_column("Input", justify="right"); t.add_column("Output", justify="right")
    t.add_column("Cache R", justify="right"); t.add_column("Cache W", justify="right")
    t.add_column("Cost", justify="right")
    total_cost = 0.0
    for d in days:
        a = by_day[d]
        total_cost += a["cost"]
        t.add_row(d, str(a["calls"]),
                  fmt_num(a["in"]), fmt_num(a["out"]),
                  fmt_num(a["cr"]), fmt_num(a["cw"]),
                  fmt_cost(a["cost"]))
    console.print(t)
    console.print(f"[bold]Total for window:[/bold] [green]{fmt_cost(total_cost)}[/green]")

    suggestions = analyze_suggestions(records)
    if suggestions:
        _render_suggestions(console, suggestions, top=3)


def cmd_projects(args) -> None:
    records = load_records(args)
    by_project = aggregate(records, lambda r: r.project)
    if not by_project:
        print("No records.")
        return

    if not RICH:
        for project, a in sorted(by_project.items(), key=lambda kv: -kv[1]["cost"])[: args.top]:
            print(f"{shorten_path(decode_project(project)):60s}  calls={a['calls']:4d}  cost={fmt_cost(a['cost'])}")
        return

    console = Console()
    t = Table(title=f"Top {args.top} Projects by Cost", box=box.SIMPLE_HEAVY)
    t.add_column("Project"); t.add_column("Calls", justify="right")
    t.add_column("Input", justify="right"); t.add_column("Output", justify="right")
    t.add_column("Cost", justify="right"); t.add_column("Last active", justify="right")
    for project, a in sorted(by_project.items(), key=lambda kv: -kv[1]["cost"])[: args.top]:
        short = shorten_path(decode_project(project))
        if len(short) > 55:
            short = "..." + short[-52:]
        last = a["last"].strftime("%Y-%m-%d") if a["last"] else "-"
        t.add_row(short, str(a["calls"]),
                  fmt_num(a["in"]), fmt_num(a["out"]),
                  fmt_cost(a["cost"]), last)
    console.print(t)

    suggestions = analyze_suggestions(records)
    if suggestions:
        _render_suggestions(console, suggestions, top=3)


def cmd_sessions(args) -> None:
    records = load_records(args)
    by_session = aggregate(records, lambda r: r.session_id)
    if not by_session:
        print("No records.")
        return

    console = Console() if RICH else None
    rows = sorted(by_session.items(), key=lambda kv: -kv[1]["cost"])[: args.top]
    if not RICH:
        for sess, a in rows:
            print(f"{sess[:8]}...  calls={a['calls']:4d}  cost={fmt_cost(a['cost'])}")
        return

    t = Table(title=f"Top {args.top} Sessions by Cost", box=box.SIMPLE_HEAVY)
    t.add_column("Session"); t.add_column("Calls", justify="right")
    t.add_column("Input", justify="right"); t.add_column("Output", justify="right")
    t.add_column("Cache R", justify="right"); t.add_column("Cache W", justify="right")
    t.add_column("Cost", justify="right"); t.add_column("Last active", justify="right")
    for sess, a in rows:
        last = a["last"].strftime("%Y-%m-%d %H:%M") if a["last"] else "-"
        t.add_row(sess[:8] + "...", str(a["calls"]),
                  fmt_num(a["in"]), fmt_num(a["out"]),
                  fmt_num(a["cr"]), fmt_num(a["cw"]),
                  fmt_cost(a["cost"]), last)
    console.print(t)


def cmd_export(args) -> None:
    records = load_records(args)
    if args.output == "-":
        out = sys.stdout
        close = False
    else:
        out = open(args.output, "w", encoding="utf-8", newline="")
        close = True
    try:
        if args.format == "csv":
            w = csv.writer(out)
            w.writerow([
                "timestamp", "project", "session_id", "model",
                "input_tokens", "output_tokens",
                "cache_read_tokens", "cache_write_tokens",
                "cost_usd",
            ])
            for r in records:
                u = r.usage
                w.writerow([
                    r.timestamp, decode_project(r.project), r.session_id, r.model,
                    int(u.get("input_tokens") or 0),
                    int(u.get("output_tokens") or 0),
                    int(u.get("cache_read_input_tokens") or 0),
                    int(u.get("cache_creation_input_tokens") or 0),
                    f"{r.cost:.6f}",
                ])
        else:
            json.dump([
                {
                    "timestamp": r.timestamp,
                    "project": decode_project(r.project),
                    "session_id": r.session_id,
                    "model": r.model,
                    "usage": r.usage,
                    "cost_usd": round(r.cost, 6),
                }
                for r in records
            ], out, indent=2)
    finally:
        if close:
            out.close()
    if close:
        print(f"Wrote {len(records)} records -> {args.output}", file=sys.stderr)


def cmd_live(args) -> None:
    if not RICH:
        print("Live mode requires `pip install rich`.", file=sys.stderr)
        sys.exit(1)
    from rich.console import Group
    from rich.panel import Panel
    console = Console()

    # CLI overrides (None = derive thresholds from the active session's model cap).
    warn_override  = getattr(args, "context_warn",  None)
    alert_override = getattr(args, "context_alert", None)
    budget_daily = getattr(args, "budget_daily", None)

    def _ctx_tokens(r: Record) -> int:
        u = r.usage
        return (int(u.get("input_tokens") or 0)
                + int(u.get("cache_read_input_tokens") or 0)
                + int(u.get("cache_creation_input_tokens") or 0))

    def render():
        records = load_all()
        now = datetime.now().astimezone()
        today_iso = date.today().isoformat()
        yday_iso = (date.today() - timedelta(days=1)).isoformat()
        by_day = aggregate(
            records,
            lambda r: parse_ts(r.timestamp).date().isoformat() if parse_ts(r.timestamp) else None,
        )
        today = by_day.get(today_iso, empty_agg())
        yday = by_day.get(yday_iso, empty_agg())
        total = empty_agg()
        for a in by_day.values():
            for k in ("in", "out", "cr", "cw", "calls"):
                total[k] += a[k]
            total["cost"] += a["cost"]

        t = Table(
            title=f"Claude Code Live Monitor  —  {now.strftime('%Y-%m-%d %H:%M:%S')}",
            box=box.ROUNDED, show_header=True, header_style="bold",
        )
        t.add_column("Scope"); t.add_column("Calls", justify="right")
        t.add_column("Input", justify="right"); t.add_column("Output", justify="right")
        t.add_column("Cache R", justify="right"); t.add_column("Cache W", justify="right")
        t.add_column("Cost", justify="right")
        for label, a, style in [
            ("Today", today, "cyan"),
            ("Yesterday", yday, "dim"),
            ("All-time", total, "bold green"),
        ]:
            t.add_row(
                f"[{style}]{label}[/{style}]", str(a["calls"]),
                fmt_num(a["in"]), fmt_num(a["out"]),
                fmt_num(a["cr"]), fmt_num(a["cw"]),
                f"[{style}]{fmt_cost(a['cost'])}[/{style}]",
            )

        # ---------- burn rate over the last 30 minutes ----------
        cutoff_30m = now - timedelta(minutes=30)
        recent_30m = []
        for r in records:
            ts = parse_ts(r.timestamp)
            if ts and ts >= cutoff_30m:
                recent_30m.append(r)
        cost_30m = sum(r.cost for r in recent_30m)
        burn_per_hr = cost_30m * 2.0  # 30 min → hourly extrapolation
        burn_line = (
            f"[bold]Burn (30m):[/bold] [cyan]{fmt_cost(cost_30m)}[/cyan]  "
            f"[bold]Rate:[/bold] [cyan]{fmt_cost(burn_per_hr)}/hr[/cyan]  "
            f"[bold]Calls (30m):[/bold] {len(recent_30m)}"
        )
        # Optional daily-budget projection: at this rate, ETA to limit.
        if budget_daily and budget_daily > 0:
            remaining = max(0.0, budget_daily - today["cost"])
            if burn_per_hr > 0 and remaining > 0:
                eta_hours = remaining / burn_per_hr
                eta_at = (now + timedelta(hours=eta_hours)).strftime("%H:%M")
                pct_used = today["cost"] / budget_daily * 100
                color = "red" if pct_used >= 100 else "yellow" if pct_used >= 80 else "green"
                burn_line += (
                    f"\n[bold]Daily budget:[/bold] "
                    f"[{color}]{fmt_cost(today['cost'])} / {fmt_cost(budget_daily)} "
                    f"({pct_used:.0f}%)[/{color}]  "
                    f"[bold]ETA to limit at this rate:[/bold] [cyan]~{eta_at}[/cyan] "
                    f"({eta_hours:.1f}h)"
                )
            elif today["cost"] >= budget_daily:
                burn_line += (
                    f"\n[bold red]Daily budget exceeded:[/bold red] "
                    f"{fmt_cost(today['cost'])} / {fmt_cost(budget_daily)}"
                )
            else:
                burn_line += (
                    f"\n[bold]Daily budget:[/bold] "
                    f"{fmt_cost(today['cost'])} / {fmt_cost(budget_daily)} "
                    f"(idle — no spend in last 30m)"
                )

        # ---------- active session panel (records in last 10 min) ----------
        cutoff_10m = now - timedelta(minutes=10)
        active = [
            r for r in records
            if parse_ts(r.timestamp) and parse_ts(r.timestamp) >= cutoff_10m
        ]
        if active:
            latest = max(active, key=lambda r: parse_ts(r.timestamp))
            cur_session = latest.session_id
            cur_recs = [r for r in records if r.session_id == cur_session]
            ctx_values = [_ctx_tokens(r) for r in cur_recs]
            peak_ctx = max(ctx_values) if ctx_values else 0
            last_ctx = _ctx_tokens(latest)
            cur_cost = sum(r.cost for r in cur_recs)
            cur_calls = len(cur_recs)
            cur_proj = shorten_path(decode_project(latest.project))
            if len(cur_proj) > 60:
                cur_proj = "…" + cur_proj[-57:]

            cap = context_cap(latest.model)
            warn_tok  = warn_override  if warn_override  else int(cap * _CTX_WARN_RATIO)
            alert_tok = alert_override if alert_override else int(cap * _CTX_ALERT_RATIO)
            if peak_ctx >= alert_tok:
                ctx_color, border = "red", "red"
                ctx_warn = (
                    f"  [bold red on white] ⚠ NEAR/OVER {fmt_num(cap)} CAP — /clear NOW [/bold red on white]"
                )
            elif peak_ctx >= warn_tok:
                ctx_color, border = "yellow", "yellow"
                ctx_warn = "  [bold yellow]⚠ Large context — consider /clear[/bold yellow]"
            else:
                ctx_color, border = "green", "cyan"
                ctx_warn = ""

            ts_latest = parse_ts(latest.timestamp).astimezone().strftime("%H:%M:%S")
            session_body = (
                f"[bold]Session:[/bold] {cur_session[:8]}…   "
                f"[bold]Project:[/bold] {cur_proj}\n"
                f"[bold]Model:[/bold] {latest.model}   "
                f"[bold]Calls:[/bold] {cur_calls}   "
                f"[bold]Cost:[/bold] {fmt_cost(cur_cost)}   "
                f"[bold]Last call:[/bold] {ts_latest}\n"
                f"[bold]Context (last call):[/bold] [{ctx_color}]{fmt_num(last_ctx)} tok[/{ctx_color}]   "
                f"[bold]Peak this session:[/bold] [{ctx_color}]{fmt_num(peak_ctx)} tok[/{ctx_color}]"
                f"{ctx_warn}"
            )
            session_panel = Panel(
                session_body, title="Active Session (last 10 min)",
                border_style=border,
            )
            return Group(t, burn_line, session_panel)

        return Group(
            t, burn_line,
            "[dim]No activity in the last 10 minutes — waiting for a Claude Code session…[/dim]",
        )

    with Live(render(), refresh_per_second=2, console=console, screen=False) as live:
        try:
            while True:
                time.sleep(max(1.0, args.interval))
                live.update(render())
        except KeyboardInterrupt:
            pass


# -------------------------- Heatmap / trend / budget ------------------------- #


_HEAT_STEPS = [
    # (threshold_fraction, color, glyph)
    (0.00, "grey23",     "·"),
    (0.02, "grey50",     "░"),
    (0.10, "cyan",       "▒"),
    (0.25, "blue",       "▓"),
    (0.45, "green",      "▓"),
    (0.65, "yellow",     "█"),
    (0.80, "orange3",    "█"),
    (0.92, "red",        "█"),
]


def _heat_cell(frac: float) -> str:
    step = _HEAT_STEPS[0]
    for s in _HEAT_STEPS:
        if frac >= s[0]:
            step = s
    color, glyph = step[1], step[2]
    return f"[{color}]{glyph}[/]"


def cmd_heatmap(args) -> None:
    """Day-of-week × hour-of-day heatmap of usage (local time)."""
    if not RICH:
        print("Heatmap requires `pip install rich`.", file=sys.stderr)
        sys.exit(1)
    records = load_records(args)
    metric = args.metric  # "cost" | "calls" | "tokens"

    # grid[dow 0=Mon..6=Sun][hour 0..23] = float
    grid: list[list[float]] = [[0.0] * 24 for _ in range(7)]
    totals_by_dow = [0.0] * 7
    totals_by_hour = [0.0] * 24
    counted = 0
    for r in records:
        ts = parse_ts(r.timestamp)
        if ts is None:
            continue
        local = ts.astimezone()  # convert UTC -> local
        dow = local.weekday()
        hour = local.hour
        if metric == "cost":
            val = r.cost
        elif metric == "calls":
            val = 1.0
        else:  # tokens: sum of all usage token fields
            u = r.usage
            val = float(
                int(u.get("input_tokens") or 0)
                + int(u.get("output_tokens") or 0)
                + int(u.get("cache_read_input_tokens") or 0)
                + int(u.get("cache_creation_input_tokens") or 0)
            )
        grid[dow][hour] += val
        totals_by_dow[dow] += val
        totals_by_hour[hour] += val
        counted += 1

    if counted == 0:
        print("No dated records to display.")
        return

    peak = max(max(row) for row in grid) or 1.0
    console = Console()
    dow_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def fmt_total(v: float) -> str:
        if metric == "cost":
            return fmt_cost(v)
        if metric == "calls":
            return str(int(v))
        return fmt_num(int(v))

    # Axis header: two rows of digits so we don't eat horizontal space
    hour_tens = "".join(f"{h//10 if h % 5 == 0 else ' '}" for h in range(24))
    hour_ones = "".join(str(h % 10) for h in range(24))

    console.print(
        f"[bold]Usage Heatmap[/bold] ({metric}, local time)  "
        f"peak cell: [cyan]{fmt_total(peak)}[/cyan]   "
        f"grand total: [green]{fmt_total(sum(totals_by_dow))}[/green]"
    )
    console.print(f"       {hour_tens}   Total")
    console.print(f"       {hour_ones}")
    for dow in range(7):
        cells = "".join(_heat_cell(grid[dow][h] / peak) for h in range(24))
        console.print(f" [bold]{dow_labels[dow]}[/bold]   {cells}   {fmt_total(totals_by_dow[dow]):>8s}")
    # column totals line
    col_totals = "".join(
        _heat_cell(totals_by_hour[h] / (max(totals_by_hour) or 1.0)) for h in range(24)
    )
    console.print(f" [dim]Σh[/dim]   {col_totals}")
    # legend
    legend = "  ".join(
        f"{_heat_cell(step[0] + 0.01)} {int(step[0]*100)}%+"
        for step in _HEAT_STEPS
    )
    console.print(f"\n Legend: {legend}")


def cmd_trend(args) -> None:
    """Daily cost/token trend for a single project (substring match)."""
    records = load_records(args)
    q = args.project.lower()
    matches: list[Record] = [
        r for r in records
        if q in decode_project(r.project).lower() or q in (r.cwd or "").lower()
    ]
    if not matches:
        print(f"No records match project query: {args.project!r}")
        print("Try `monitor projects` to see available projects.")
        sys.exit(1)

    # identify the matched project (most common decoded path)
    label_counts: dict[str, int] = defaultdict(int)
    for r in matches:
        label_counts[decode_project(r.project)] += 1
    primary = max(label_counts.items(), key=lambda kv: kv[1])[0]

    by_day = aggregate(
        matches,
        lambda r: parse_ts(r.timestamp).date().isoformat() if parse_ts(r.timestamp) else None,
    )
    days = sorted(by_day.keys())
    if args.days:
        days = days[-args.days:]

    if not days:
        print("No dated records for project.")
        return

    # sparkline of cost
    costs = [by_day[d]["cost"] for d in days]
    peak = max(costs) or 1.0
    bars = " ▁▂▃▄▅▆▇█"
    spark = "".join(bars[min(len(bars) - 1, int(c / peak * (len(bars) - 1)))] for c in costs)

    if not RICH:
        print(f"Project: {shorten_path(primary)}")
        print(f"Spark:   {spark}")
        for d in days:
            a = by_day[d]
            print(f"{d}  calls={a['calls']:4d}  cost={fmt_cost(a['cost'])}")
        return

    console = Console()
    console.print(f"[bold]Project:[/bold] {shorten_path(primary)}")
    console.print(f"[bold]Sessions:[/bold] {len({r.session_id for r in matches if r.session_id})}   "
                  f"[bold]Total spend:[/bold] [green]{fmt_cost(sum(costs))}[/green]   "
                  f"[bold]Spark:[/bold] [cyan]{spark}[/cyan]")

    t = Table(title=f"Daily Trend (last {len(days)} active days)", box=box.SIMPLE_HEAVY)
    t.add_column("Date", no_wrap=True); t.add_column("Calls", justify="right")
    t.add_column("Input", justify="right"); t.add_column("Output", justify="right")
    t.add_column("Cache R", justify="right"); t.add_column("Cache W", justify="right")
    t.add_column("Cost", justify="right", no_wrap=True)
    t.add_column("", justify="left", no_wrap=True)
    bar_width = 12
    for d in days:
        a = by_day[d]
        bar_len = int(a["cost"] / peak * bar_width)
        bar = "█" * bar_len
        t.add_row(
            d, str(a["calls"]),
            fmt_num(a["in"]), fmt_num(a["out"]),
            fmt_num(a["cr"]), fmt_num(a["cw"]),
            fmt_cost(a["cost"]),
            f"[cyan]{bar}[/cyan]",
        )
    console.print(t)


def _iso_week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def cmd_weekly(args) -> None:
    """Per-ISO-week usage (Mon-Sun)."""
    records = load_records(args)

    by_week: dict[str, dict] = defaultdict(empty_agg)
    week_sessions: dict[str, set] = defaultdict(set)
    week_projects: dict[str, set] = defaultdict(set)
    for r in records:
        ts = parse_ts(r.timestamp)
        if ts is None:
            continue
        ws = _iso_week_start(ts.astimezone().date()).isoformat()
        a = by_week[ws]
        u = r.usage
        a["in"]  += int(u.get("input_tokens") or 0)
        a["out"] += int(u.get("output_tokens") or 0)
        a["cr"]  += int(u.get("cache_read_input_tokens") or 0)
        a["cw"]  += int(u.get("cache_creation_input_tokens") or 0)
        a["cost"] += r.cost
        a["calls"] += 1
        if r.session_id:
            week_sessions[ws].add(r.session_id)
        if r.project:
            week_projects[ws].add(r.project)

    weeks = sorted(by_week.keys())
    if args.weeks:
        weeks = weeks[-args.weeks:]
    if not weeks:
        print("No dated records.")
        return

    if not RICH:
        for w in weeks:
            a = by_week[w]
            print(f"{w}  calls={a['calls']:5d}  cost={fmt_cost(a['cost'])}  "
                  f"sessions={len(week_sessions[w]):2d}  projects={len(week_projects[w]):2d}")
        return

    console = Console()
    costs = [by_week[w]["cost"] for w in weeks]
    peak = max(costs) or 1.0
    bar_width = 14

    t = Table(title=f"Weekly Usage (last {len(weeks)} weeks)", box=box.SIMPLE_HEAVY)
    t.add_column("Week of", no_wrap=True)
    t.add_column("Sess", justify="right")
    t.add_column("Proj", justify="right")
    t.add_column("Calls", justify="right")
    t.add_column("Input", justify="right")
    t.add_column("Output", justify="right")
    t.add_column("Cost", justify="right", no_wrap=True)
    t.add_column("", justify="left", no_wrap=True)
    for w in weeks:
        a = by_week[w]
        bar = "█" * int(a["cost"] / peak * bar_width)
        t.add_row(
            w, str(len(week_sessions[w])), str(len(week_projects[w])),
            str(a["calls"]),
            fmt_num(a["in"]), fmt_num(a["out"]),
            fmt_cost(a["cost"]),
            f"[cyan]{bar}[/cyan]",
        )
    console.print(t)
    console.print(f"\n[bold]Total for window:[/bold] [green]{fmt_cost(sum(costs))}[/green]  "
                  f"across {len({s for w in weeks for s in week_sessions[w]})} sessions")


def cmd_calendar(args) -> None:
    """GitHub-style yearly activity grid (7 rows × 53 cols)."""
    if not RICH:
        print("Calendar requires `pip install rich`.", file=sys.stderr)
        sys.exit(1)
    records = load_records(args)
    year = args.year or date.today().year

    day_cost: dict[date, float] = defaultdict(float)
    day_calls: dict[date, int] = defaultdict(int)
    for r in records:
        ts = parse_ts(r.timestamp)
        if ts is None:
            continue
        d = ts.astimezone().date()
        if d.year != year:
            continue
        day_cost[d] += r.cost
        day_calls[d] += 1

    if not day_cost:
        print(f"No records for year {year}.")
        return

    metric_map = day_cost if args.metric == "cost" else day_calls
    peak = max(metric_map.values()) or 1.0

    # Build a 7×53 grid. Each column is a week starting on Monday.
    jan1 = date(year, 1, 1)
    # shift so column 0 aligns with the Monday of jan1's week
    grid_start = jan1 - timedelta(days=jan1.weekday())
    console = Console()
    dow_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Month label row: show month letter at the first column of each month
    month_row = [" "] * 53
    for m in range(1, 13):
        first = date(year, m, 1)
        col = (first - grid_start).days // 7
        if 0 <= col < 53:
            month_row[col] = first.strftime("%b")[0]
    # spread 3-letter month initials every few columns? keep single letters.

    console.print(f"[bold]Activity Calendar {year}[/bold]  "
                  f"(metric: {args.metric}, peak day: "
                  f"{fmt_cost(peak) if args.metric == 'cost' else int(peak)})")
    # Month axis — align with cell columns (day-label block is 7 chars wide)
    month_axis = " " * 7 + "".join(month_row)
    console.print(f"[dim]{month_axis}[/dim]")

    for dow in range(7):
        cells = []
        for col in range(53):
            d = grid_start + timedelta(weeks=col, days=dow)
            if d.year != year or d > date.today():
                cells.append("[grey15] [/]")
                continue
            v = metric_map.get(d, 0)
            cells.append(_heat_cell(v / peak))
        console.print(f"  [dim]{dow_labels[dow]}[/dim]  {''.join(cells)}")

    # Legend + totals
    legend = "  ".join(
        f"{_heat_cell(step[0] + 0.01)} {int(step[0]*100)}%+"
        for step in _HEAT_STEPS
    )
    total_cost = sum(day_cost.values())
    console.print(f"\n  [bold]{len(day_cost)}[/bold] active days  "
                  f"[bold]{sum(day_calls.values())}[/bold] calls  "
                  f"[bold green]{fmt_cost(total_cost)}[/bold green] spent")
    console.print(f"  Legend: {legend}")


def cmd_cache(args) -> None:
    """Cache efficiency analysis: hit rate and estimated savings."""
    records = load_records(args)
    if not records:
        print("No records.")
        return

    def analyze(recs: list[Record]) -> dict:
        inp = out = cr = cw = 0
        cost = 0.0
        uncached_cost = 0.0
        for r in recs:
            u = r.usage
            i = int(u.get("input_tokens") or 0)
            o = int(u.get("output_tokens") or 0)
            cri = int(u.get("cache_read_input_tokens") or 0)
            cwi = int(u.get("cache_creation_input_tokens") or 0)
            inp += i; out += o; cr += cri; cw += cwi
            cost += r.cost
            # "what would it have cost without caching?" — all cache reads
            # and cache creations would have been plain input tokens.
            p = model_price(r.model)
            uncached_cost += (
                (i + cri + cwi) * p["in"] / 1_000_000
                + o * p["out"] / 1_000_000
            )
        total_input_like = inp + cr + cw
        hit_rate = (cr / total_input_like) if total_input_like else 0.0
        savings = uncached_cost - cost
        return {
            "calls": len(recs),
            "input": inp, "output": out, "cache_read": cr, "cache_write": cw,
            "cost": cost, "uncached_cost": uncached_cost,
            "savings": savings,
            "hit_rate": hit_rate,
        }

    overall = analyze(records)

    # per-project breakdown
    by_project: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        by_project[r.project].append(r)
    project_stats = {
        p: analyze(recs) for p, recs in by_project.items()
    }

    if not RICH:
        print(f"Overall cache hit rate: {overall['hit_rate']*100:.1f}%")
        print(f"Spent: {fmt_cost(overall['cost'])}  "
              f"Would have cost without caching: {fmt_cost(overall['uncached_cost'])}")
        print(f"Estimated savings: {fmt_cost(overall['savings'])}")
        return

    console = Console()
    console.print(f"[bold]Cache Efficiency[/bold]")
    console.print(f"  Hit rate:          [cyan]{overall['hit_rate']*100:.1f}%[/cyan] "
                  f"of input-side tokens came from cache")
    console.print(f"  Actual spend:      [green]{fmt_cost(overall['cost'])}[/green]")
    console.print(f"  Without caching:   [red]{fmt_cost(overall['uncached_cost'])}[/red]")
    console.print(f"  Estimated savings: [bold green]{fmt_cost(overall['savings'])}[/bold green] "
                  f"([bold]{(overall['savings']/overall['uncached_cost']*100 if overall['uncached_cost'] else 0):.0f}%[/bold])")

    t = Table(title=f"Top {args.top} Projects by Spend (cache view)", box=box.SIMPLE_HEAVY)
    t.add_column("Project", overflow="ellipsis")
    t.add_column("Calls", justify="right")
    t.add_column("Hit %", justify="right")
    t.add_column("Spent", justify="right", no_wrap=True)
    t.add_column("No-cache", justify="right", no_wrap=True)
    t.add_column("Saved", justify="right", no_wrap=True)
    for project, stats in sorted(project_stats.items(), key=lambda kv: -kv[1]["cost"])[: args.top]:
        short = shorten_path(decode_project(project))
        hr = stats["hit_rate"] * 100
        hr_color = "green" if hr >= 80 else "yellow" if hr >= 50 else "red"
        t.add_row(
            short, str(stats["calls"]),
            f"[{hr_color}]{hr:.0f}%[/{hr_color}]",
            fmt_cost(stats["cost"]),
            fmt_cost(stats["uncached_cost"]),
            f"[green]{fmt_cost(stats['savings'])}[/green]",
        )
    console.print(t)


def _cost_markup(c: float, peak: float) -> str:
    frac = c / peak if peak else 0
    color = "green" if frac < 0.33 else "yellow" if frac < 0.66 else "red"
    return f"[{color}]{fmt_cost(c)}[/{color}]"


def cmd_report(args) -> None:
    """Export a comprehensive dashboard to HTML or SVG."""
    if not RICH:
        print("Report export requires `pip install rich`.", file=sys.stderr)
        sys.exit(1)
    width = args.width
    console = Console(record=True, width=width, force_terminal=True, color_system="truecolor")

    records = load_records(args)

    # Optional project filter
    project_label: str | None = None
    if getattr(args, "project", None):
        q = args.project.lower()
        records = [
            r for r in records
            if q in decode_project(r.project).lower()
            or q in r.project.lower()
            or q in (r.cwd or "").lower().replace("\\", "/")
        ]
        if not records:
            print(f"No records match project: {args.project!r}", file=sys.stderr)
            sys.exit(1)
        label_counts: dict[str, int] = defaultdict(int)
        for r in records:
            label_counts[decode_project(r.project)] += 1
        project_label = shorten_path(max(label_counts.items(), key=lambda kv: kv[1])[0])

    if not records:
        console.print("No records to report.")
    else:
        title_suffix = f" — {project_label}" if project_label else ""
        console.rule(f"[bold]Claude Code Usage Report{title_suffix} — {datetime.now():%Y-%m-%d %H:%M}[/bold]")

        # Section 1: overview
        total = empty_agg()
        for r in records:
            u = r.usage
            total["in"]   += int(u.get("input_tokens") or 0)
            total["out"]  += int(u.get("output_tokens") or 0)
            total["cr"]   += int(u.get("cache_read_input_tokens") or 0)
            total["cw"]   += int(u.get("cache_creation_input_tokens") or 0)
            total["cost"] += r.cost
            total["calls"] += 1
        sessions = {r.session_id for r in records if r.session_id}
        projects_set = {r.project for r in records if r.project}
        total_input_like = total["in"] + total["cr"] + total["cw"]
        cache_hit_rate = total["cr"] / total_input_like if total_input_like else 0.0
        hit_color = "green" if cache_hit_rate >= 0.7 else "yellow" if cache_hit_rate >= 0.4 else "red"
        console.print(
            f"\n[bold]Overview[/bold]  "
            f"sessions: [cyan]{len(sessions)}[/cyan]  "
            f"projects: [magenta]{len(projects_set)}[/magenta]  "
            f"calls: [cyan]{total['calls']}[/cyan]  "
            f"cache hit: [{hit_color}]{cache_hit_rate*100:.1f}%[/{hit_color}]  "
            f"total cost: [bold green]{fmt_cost(total['cost'])}[/bold green]"
        )

        # Section 2: by model — with cache hit % and % of total cost
        by_model = aggregate(records, lambda r: r.model)
        peak_model = max((a["cost"] for a in by_model.values()), default=1.0)
        t = Table(title="By Model", box=box.HEAVY_HEAD, show_footer=True)
        t.add_column("Model", footer="[bold]TOTAL[/bold]")
        t.add_column("Calls", justify="right", footer=f"[bold]{total['calls']}[/bold]")
        t.add_column("Input", justify="right")
        t.add_column("Output", justify="right")
        t.add_column("Cache R", justify="right")
        t.add_column("Cache W", justify="right")
        t.add_column("Hit %", justify="right",
                     footer=f"[{hit_color}]{cache_hit_rate*100:.0f}%[/{hit_color}]")
        t.add_column("Cost", justify="right",
                     footer=f"[bold green]{fmt_cost(total['cost'])}[/bold green]")
        t.add_column("% cost", justify="right", footer="[bold]100%[/bold]")
        for model, a in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"]):
            inp_like = a["in"] + a["cr"] + a["cw"]
            hp = a["cr"] / inp_like * 100 if inp_like else 0.0
            hc = "green" if hp >= 70 else "yellow" if hp >= 40 else "red"
            cp = a["cost"] / total["cost"] * 100 if total["cost"] else 0.0
            t.add_row(
                model, str(a["calls"]),
                fmt_num(a["in"]), fmt_num(a["out"]),
                fmt_num(a["cr"]), fmt_num(a["cw"]),
                f"[{hc}]{hp:.0f}%[/{hc}]",
                _cost_markup(a["cost"], peak_model),
                f"{cp:.1f}%",
            )
        console.print(t)

        # Section 3: daily (last 30) — with cost bar and color
        by_day = aggregate(
            records,
            lambda r: parse_ts(r.timestamp).date().isoformat() if parse_ts(r.timestamp) else None,
        )
        days = sorted(by_day.keys(), reverse=True)[:30]
        day_costs = [by_day[d]["cost"] for d in days]
        peak_day = max(day_costs) if day_costs else 1.0
        bar_w = 12
        t = Table(title=f"Daily (last {len(days)})", box=box.HEAVY_HEAD, show_footer=True)
        t.add_column("Date", footer="[bold]TOTAL[/bold]")
        t.add_column("Calls", justify="right",
                     footer=f"[bold]{sum(by_day[d]['calls'] for d in days)}[/bold]")
        t.add_column("Input", justify="right")
        t.add_column("Output", justify="right")
        t.add_column("Cache R", justify="right")
        t.add_column("Cache W", justify="right")
        t.add_column("Cost", justify="right",
                     footer=f"[bold green]{fmt_cost(sum(day_costs))}[/bold green]")
        t.add_column("", justify="left", no_wrap=True)
        for d in days:
            a = by_day[d]
            bar = "█" * int(a["cost"] / peak_day * bar_w)
            t.add_row(
                d, str(a["calls"]),
                fmt_num(a["in"]), fmt_num(a["out"]),
                fmt_num(a["cr"]), fmt_num(a["cw"]),
                _cost_markup(a["cost"], peak_day),
                f"[cyan]{bar}[/cyan]",
            )
        console.print(t)

        # Section 4: top projects (omit when already filtered to one project)
        if not project_label:
            by_project = aggregate(records, lambda r: r.project)
            peak_proj = max((a["cost"] for a in by_project.values()), default=1.0)
            t = Table(title="Top 15 Projects", box=box.HEAVY_HEAD)
            t.add_column("Project")
            t.add_column("Calls", justify="right")
            t.add_column("Input", justify="right")
            t.add_column("Output", justify="right")
            t.add_column("Cost", justify="right")
            t.add_column("% cost", justify="right")
            t.add_column("Last active", justify="right")
            for project, a in sorted(by_project.items(), key=lambda kv: -kv[1]["cost"])[:15]:
                last = a["last"].strftime("%Y-%m-%d") if a["last"] else "-"
                cp = a["cost"] / total["cost"] * 100 if total["cost"] else 0.0
                t.add_row(
                    shorten_path(decode_project(project)), str(a["calls"]),
                    fmt_num(a["in"]), fmt_num(a["out"]),
                    _cost_markup(a["cost"], peak_proj),
                    f"{cp:.1f}%",
                    last,
                )
            console.print(t)

        # Section 5: heatmap
        grid = [[0.0] * 24 for _ in range(7)]
        for r in records:
            ts = parse_ts(r.timestamp)
            if ts is None:
                continue
            local = ts.astimezone()
            grid[local.weekday()][local.hour] += r.cost
        peak = max(max(row) for row in grid) or 1.0
        console.print(f"\n[bold]Usage Heatmap (cost, local time)[/bold]")
        hour_tens = "".join(f"{h//10 if h % 5 == 0 else ' '}" for h in range(24))
        hour_ones = "".join(str(h % 10) for h in range(24))
        console.print(f"       {hour_tens}")
        console.print(f"       {hour_ones}")
        dow_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for dow in range(7):
            cells = "".join(_heat_cell(grid[dow][h] / peak) for h in range(24))
            row_total = sum(grid[dow])
            console.print(f"  [bold]{dow_labels[dow]}[/bold]  {cells}  {fmt_cost(row_total):>8s}")

        # Section 6: tier-2 routing (routine-worker delegations)
        project_filter = getattr(args, "project", None)
        routing = collect_routing_stats(projects_root(), project_filter)
        if routing:
            matched = [d for d in routing if d["matched"]]
            total_tokens = sum(d["in"] + d["out"] + d["cr"] + d["cw"] for d in matched)
            total_actual = sum(d["cost_actual"] for d in matched)
            total_opus = sum(d["cost_opus"] for d in matched)
            total_saved = sum(d["saved"] for d in matched)

            console.print()
            console.rule("[bold]Tier-2 Routing — routine-worker delegations[/bold]")

            summary = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold")
            summary.add_column("Metric", style="bold")
            summary.add_column("Value", justify="right")
            summary.add_row("Delegations (total)", f"[cyan]{len(routing)}[/cyan]")
            if len(matched) != len(routing):
                summary.add_row("  of which linked to a subagent transcript",
                                f"{len(matched)}")
            summary.add_row("Subagent tokens (all tiers)", fmt_num(total_tokens))
            summary.add_row("Actual subagent cost", fmt_cost(total_actual))
            summary.add_row("Hypothetical cost on Opus", fmt_cost(total_opus))
            summary.add_row("[bold green]Saved[/bold green]",
                            f"[bold green]{fmt_cost(total_saved)}[/bold green]")
            console.print(summary)

            recent = sorted(routing, key=lambda d: d["timestamp"], reverse=True)[:10]
            detail = Table(
                box=box.SIMPLE_HEAVY, show_header=True, header_style="bold",
                title="Recent delegations (top 10, most recent first)"
            )
            detail.add_column("Time", style="dim", no_wrap=True)
            detail.add_column("Project", style="cyan", max_width=22, overflow="fold")
            detail.add_column("Reason", overflow="fold")
            detail.add_column("Tokens", justify="right")
            detail.add_column("Saved", justify="right")
            for d in recent:
                ts = parse_ts(d["timestamp"])
                time_str = ts.astimezone().strftime("%m-%d %H:%M") if ts else "?"
                proj = decode_project(d["project"])
                proj_short = proj.rsplit("/", 1)[-1] if "/" in proj else proj
                tokens = d["in"] + d["out"] + d["cr"] + d["cw"]
                if d["matched"]:
                    detail.add_row(
                        time_str, proj_short,
                        d["description"] or "(no description)",
                        fmt_num(tokens),
                        f"[green]{fmt_cost(d['saved'])}[/green]",
                    )
                else:
                    detail.add_row(
                        time_str, proj_short,
                        d["description"] or "(no description)",
                        "[dim]—[/dim]", "[dim]—[/dim]",
                    )
            console.print(detail)
            console.print(
                "[dim]Savings = (tokens × Opus price) − (tokens × actual subagent price). "
                "Only counts delegations whose subagent transcript could be linked "
                "by prompt fingerprint; unmatched rows show '—'.[/dim]"
            )

        # Section 7: suggestions (with large-context alert pinned above)
        console.print()
        console.rule("[bold]Efficiency Suggestions[/bold]")
        suggestions = analyze_suggestions(records)
        _render_alert_banners(console, suggestions)
        _render_suggestions(console, suggestions, top=15)

        console.print()
        console.print(
            "[dim]How savings are estimated: Opus → Sonnet ≈ 80% savings "
            "(Sonnet is ~5× cheaper across input, output, and cache tiers). "
            "ZeroCTX assumed to compress spike stdout by ~60%. "
            "Directional only, not accounting.[/dim]"
        )
        console.print(
            "[dim]External tools — "
            "[link=https://github.com/emtyty/ast-graph]ast-graph[/link]: "
            "structural code queries (symbol / hotspots / blast-radius / "
            "dead-code) for Rust · Python · JS/TS · C# · Java — replaces "
            "Read/Grep-spray during plan and analysis mode.  "
            "ZeroCTX (`zero rewrite-exec -- <cmd>`): compresses noisy stdout "
            "from cargo / npm / pytest / git diff before it reaches Claude's "
            "context.[/dim]"
        )

    # Save
    out = args.output or f"claude-usage-{date.today().isoformat()}.{args.format}"
    if args.format == "html":
        console.save_html(out, inline_styles=True)
    elif args.format == "svg":
        console.save_svg(out, title="Claude Code Usage Report")
    elif args.format == "txt":
        console.save_text(out)
    else:
        print(f"Unknown format: {args.format}", file=sys.stderr)
        sys.exit(1)
    if out != "-":
        print(f"Wrote report -> {out}", file=sys.stderr)
    if args.format == "html":
        print("Open in a browser and use File > Print > Save as PDF for a PDF copy.",
              file=sys.stderr)


def cmd_activity(args) -> None:
    """Per-day engagement: unique sessions active, unique projects touched."""
    records = load_records(args)

    @dataclass
    class DayStats:
        sessions: set
        projects: set
        project_calls: dict  # project -> call count (for top-project-of-day)
        calls: int
        cost: float

    by_day: dict[str, DayStats] = defaultdict(
        lambda: DayStats(set(), set(), defaultdict(int), 0, 0.0)
    )
    for r in records:
        ts = parse_ts(r.timestamp)
        if ts is None:
            continue
        d = ts.astimezone().date().isoformat()
        a = by_day[d]
        if r.session_id:
            a.sessions.add(r.session_id)
        if r.project:
            a.projects.add(r.project)
            a.project_calls[r.project] += 1
        a.calls += 1
        a.cost += r.cost

    if not by_day:
        print("No dated records found.")
        return

    days = sorted(by_day.keys())
    if args.days:
        days = days[-args.days:]

    # Sparkline helper (reused below)
    bars = " ▁▂▃▄▅▆▇█"
    def spark(values: list[float]) -> str:
        peak = max(values) or 1.0
        return "".join(bars[min(len(bars) - 1, int(v / peak * (len(bars) - 1)))] for v in values)

    sessions_series = [len(by_day[d].sessions) for d in days]
    projects_series = [len(by_day[d].projects) for d in days]
    calls_series    = [by_day[d].calls for d in days]
    cost_series     = [by_day[d].cost for d in days]

    if not RICH:
        print(f"Sessions  {spark(sessions_series)}")
        print(f"Projects  {spark(projects_series)}")
        print(f"Calls     {spark(calls_series)}")
        print(f"Cost      {spark(cost_series)}")
        for d in days:
            a = by_day[d]
            print(f"{d}  sessions={len(a.sessions):2d}  projects={len(a.projects):2d}  "
                  f"calls={a.calls:4d}  cost={fmt_cost(a.cost)}")
        return

    console = Console()
    console.print(f"[bold]Activity — last {len(days)} active days[/bold]")
    console.print(f"  Sessions/day  [cyan]{spark(sessions_series)}[/cyan]  "
                  f"peak [bold]{max(sessions_series)}[/bold]")
    console.print(f"  Projects/day  [magenta]{spark(projects_series)}[/magenta]  "
                  f"peak [bold]{max(projects_series)}[/bold]")
    console.print(f"  Calls/day     [green]{spark(calls_series)}[/green]  "
                  f"peak [bold]{max(calls_series)}[/bold]")
    console.print(f"  Cost/day      [yellow]{spark(cost_series)}[/yellow]  "
                  f"peak [bold]{fmt_cost(max(cost_series))}[/bold]")

    peak_sessions = max(sessions_series) or 1
    peak_projects = max(projects_series) or 1

    t = Table(title="Daily Activity", box=box.SIMPLE_HEAVY)
    t.add_column("Date", no_wrap=True)
    t.add_column("Sess", justify="right", no_wrap=True)
    t.add_column("Proj", justify="right", no_wrap=True)
    t.add_column("Calls", justify="right", no_wrap=True)
    t.add_column("Cost", justify="right", no_wrap=True)
    t.add_column("Top project (calls)", overflow="ellipsis")

    for d in days:
        a = by_day[d]
        ns, np = len(a.sessions), len(a.projects)
        # highlight busy days
        sess_str = f"[bold cyan]{ns}[/bold cyan]" if ns >= peak_sessions * 0.7 else str(ns)
        proj_str = f"[bold magenta]{np}[/bold magenta]" if np >= peak_projects * 0.7 else str(np)
        if a.project_calls:
            top_proj_enc, top_calls = max(a.project_calls.items(), key=lambda kv: kv[1])
            top_proj = shorten_path(decode_project(top_proj_enc))
            top_proj_cell = f"{top_proj} [dim]({top_calls})[/dim]"
        else:
            top_proj_cell = ""
        t.add_row(d, sess_str, proj_str, str(a.calls), fmt_cost(a.cost), top_proj_cell)
    console.print(t)

    # Rolling summary
    total_sessions = len({s for d in days for s in by_day[d].sessions})
    total_projects = len({p for d in days for p in by_day[d].projects})
    avg_proj_per_day = sum(projects_series) / len(days)
    console.print(
        f"\n[bold]Window total:[/bold] {total_sessions} unique sessions across "
        f"{total_projects} unique projects  "
        f"[bold]avg projects/day:[/bold] {avg_proj_per_day:.1f}  "
        f"[bold]cost:[/bold] [green]{fmt_cost(sum(cost_series))}[/green]"
    )


# ------------------------------ Suggestions ---------------------------- #


@dataclass
class Suggestion:
    rule: str            # short rule id
    severity: str        # "high" | "med" | "low"
    scope: str           # e.g. "project ~/Code/foo", "session abc12345", "day 2026-04-18"
    evidence: str        # one-line factual evidence
    action: str          # concrete recommendation
    est_savings: float   # USD; 0.0 if not quantified


# Opus -> Sonnet swap saves ~80% (matches uniform Sonnet/Opus price ratio of ~0.2
# across input, output, cache-read and cache-write).
_OPUS_TO_SONNET_SAVINGS = 0.80

# ast-graph language coverage (https://github.com/emtyty/ast-graph).
_ASTGRAPH_EXTS = {".rs", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
                  ".cs", ".java"}

# Tool classification for plan/explore detection.
_EXPLORE_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "LSP",
                  "NotebookRead", "Agent", "Task"}
_MUTATE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


def _is_opus(model: str) -> bool:
    return "opus" in (model or "").lower()


def _is_sonnet(model: str) -> bool:
    return "sonnet" in (model or "").lower()


def _project_lang_supported(records: list[Record]) -> bool:
    """Return True if the project's Read tool targets mostly ast-graph-supported files."""
    paths = [p for r in records for p in r.read_paths]
    if not paths:
        return False
    supported = 0
    for p in paths:
        ext = Path(p).suffix.lower()
        if ext in _ASTGRAPH_EXTS:
            supported += 1
    return supported / len(paths) >= 0.5


def _short_scope_project(project: str) -> str:
    return f"project {shorten_path(decode_project(project))}"


def _rule_opus_heavy_project(records: list[Record]) -> list[Suggestion]:
    """Rule 1: project where Opus dominates cost but avg output is small (routine edits)."""
    by_project: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        by_project[r.project].append(r)
    out: list[Suggestion] = []
    for project, recs in by_project.items():
        total_cost = sum(r.cost for r in recs)
        if total_cost < 10:
            continue
        opus_recs = [r for r in recs if _is_opus(r.model)]
        opus_cost = sum(r.cost for r in opus_recs)
        if not opus_recs or opus_cost / total_cost < 0.6:
            continue
        opus_out = sum(int(r.usage.get("output_tokens") or 0) for r in opus_recs)
        avg_output = opus_out / len(opus_recs)
        if avg_output >= 500 or len(opus_recs) < 20:
            continue
        savings = opus_cost * _OPUS_TO_SONNET_SAVINGS
        severity = "high" if savings >= 50 else "med" if savings >= 10 else "low"
        out.append(Suggestion(
            rule="opus-heavy-project",
            severity=severity,
            scope=_short_scope_project(project),
            evidence=(
                f"Opus {fmt_cost(opus_cost)} / {len(opus_recs)} calls · "
                f"avg output {int(avg_output)} tok · "
                f"Opus share {opus_cost/total_cost*100:.0f}%"
            ),
            action=(
                "Set default model to Sonnet for this project "
                "(routine edits don't need Opus reasoning)."
            ),
            est_savings=savings,
        ))
    return out


def _rule_opus_routine_session(records: list[Record]) -> list[Suggestion]:
    """Rule 2: long all-Opus session with small outputs (routine edits)."""
    by_session: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if r.session_id:
            by_session[r.session_id].append(r)
    out: list[Suggestion] = []
    for sess, recs in by_session.items():
        if len(recs) < 20:
            continue
        if not all(_is_opus(r.model) for r in recs):
            continue
        cost = sum(r.cost for r in recs)
        if cost < 5:
            continue
        total_out = sum(int(r.usage.get("output_tokens") or 0) for r in recs)
        avg_out = total_out / len(recs)
        if avg_out >= 500:
            continue
        projects = {decode_project(r.project) for r in recs}
        proj = shorten_path(next(iter(projects))) if len(projects) == 1 else "multiple"
        savings = cost * _OPUS_TO_SONNET_SAVINGS
        severity = "high" if savings >= 30 else "med" if savings >= 5 else "low"
        out.append(Suggestion(
            rule="opus-routine-session",
            severity=severity,
            scope=f"session {sess[:8]} ({proj})",
            evidence=(
                f"{len(recs)} calls · all Opus · "
                f"avg output {int(avg_out)} tok · {fmt_cost(cost)}"
            ),
            action="Rerun this kind of work on Sonnet — outputs were small, likely routine.",
            est_savings=savings,
        ))
    return out


def _rule_low_cache_hit(records: list[Record]) -> list[Suggestion]:
    """Rule 3: spendy project with low cache hit rate."""
    by_project: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        by_project[r.project].append(r)
    out: list[Suggestion] = []
    for project, recs in by_project.items():
        cost = sum(r.cost for r in recs)
        if cost < 10:
            continue
        inp = sum(int(r.usage.get("input_tokens") or 0) for r in recs)
        cr  = sum(int(r.usage.get("cache_read_input_tokens") or 0) for r in recs)
        cw  = sum(int(r.usage.get("cache_creation_input_tokens") or 0) for r in recs)
        denom = inp + cr + cw
        if denom == 0:
            continue
        hit = cr / denom
        if hit >= 0.4:
            continue
        # Rough: if hit rate rose to 80%, cache-read cost is ~1/10 of raw input at
        # same token count. Assume half of current uncached input-side tokens could
        # have been cache-reads instead.
        savings = 0.0
        for r in recs:
            p = model_price(r.model)
            raw_in = int(r.usage.get("input_tokens") or 0)
            shiftable = raw_in * 0.5
            savings += shiftable * (p["in"] - p["cr"]) / 1_000_000
        severity = "med" if savings >= 5 else "low"
        out.append(Suggestion(
            rule="low-cache-hit",
            severity=severity,
            scope=_short_scope_project(project),
            evidence=f"cache hit {hit*100:.0f}% · {fmt_cost(cost)} spent · many short sessions likely",
            action=(
                "Keep related work in one session; avoid frequent `/clear`. "
                "Each new session rebuilds the prefix cache."
            ),
            est_savings=savings,
        ))
    return out


def _rule_raw_input_spike(records: list[Record]) -> list[Suggestion]:
    """Rule 4: individual calls with huge raw input_tokens (log/diff dumps)."""
    # Aggregate per project: total raw-input tokens in "spike" calls (>50K single call).
    by_project: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        raw = int(r.usage.get("input_tokens") or 0)
        if raw >= 50_000:
            by_project[r.project].append(r)
    out: list[Suggestion] = []
    for project, spikes in by_project.items():
        if len(spikes) < 3:
            continue
        spike_tokens = sum(int(r.usage.get("input_tokens") or 0) for r in spikes)
        # ZeroCTX compresses build/test/diff output; assume ~60% reduction on spike tokens.
        savings = 0.0
        for r in spikes:
            p = model_price(r.model)
            raw = int(r.usage.get("input_tokens") or 0)
            savings += raw * 0.6 * p["in"] / 1_000_000
        severity = "high" if savings >= 20 else "med" if savings >= 5 else "low"
        max_raw = max(int(r.usage.get("input_tokens") or 0) for r in spikes)
        out.append(Suggestion(
            rule="raw-input-spike",
            severity=severity,
            scope=_short_scope_project(project),
            evidence=(
                f"{len(spikes)} calls with >50K raw input · "
                f"peak {fmt_num(max_raw)} · {fmt_num(spike_tokens)} total"
            ),
            action=(
                "Pipe build/test/diff commands through `zero rewrite-exec -- …` "
                "so ZeroCTX compresses noisy stdout before it hits Claude's context."
            ),
            est_savings=savings,
        ))
    return out


def _rule_day_spike(records: list[Record]) -> list[Suggestion]:
    """Rule 5: day cost > 3× median of the last 30 active days."""
    by_day: dict[str, float] = defaultdict(float)
    for r in records:
        ts = parse_ts(r.timestamp)
        if ts is None:
            continue
        by_day[ts.astimezone().date().isoformat()] += r.cost
    if len(by_day) < 7:
        return []
    days = sorted(by_day.keys())[-30:]
    costs = sorted(by_day[d] for d in days if by_day[d] > 0)
    if not costs:
        return []
    median = costs[len(costs) // 2]
    out: list[Suggestion] = []
    for d in days:
        c = by_day[d]
        if median <= 0 or c < median * 3 or c < 20:
            continue
        severity = "high" if c >= 100 else "med"
        out.append(Suggestion(
            rule="day-spike",
            severity=severity,
            scope=f"day {d}",
            evidence=(
                f"{fmt_cost(c)} on {d} · "
                f"{c/median:.1f}× median ({fmt_cost(median)})"
            ),
            action=(
                "Investigate this day's top session — likely a runaway context or "
                "a long session that would have benefited from `/clear` + resume."
            ),
            est_savings=0.0,
        ))
    return out


def _rule_session_fragmentation(records: list[Record]) -> list[Suggestion]:
    """Rule 6: many short sessions on same project same day → cache rebuilt repeatedly."""
    # bucket: (project, day) -> list[session_id -> call_count]
    buckets: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cw_cost: dict[tuple[str, str], float] = defaultdict(float)
    for r in records:
        ts = parse_ts(r.timestamp)
        if ts is None or not r.session_id:
            continue
        day = ts.astimezone().date().isoformat()
        key = (r.project, day)
        buckets[key][r.session_id] += 1
        p = model_price(r.model)
        cw = int(r.usage.get("cache_creation_input_tokens") or 0)
        cw_cost[key] += cw * p["cw_5m"] / 1_000_000
    out: list[Suggestion] = []
    for (project, day), sess_counts in buckets.items():
        short_sess = [s for s, n in sess_counts.items() if n < 5]
        if len(short_sess) < 3:
            continue
        if len(sess_counts) < 4:
            continue
        # savings ceiling: the cache-write cost that's roughly proportional to
        # session starts (each new session re-writes the prefix cache).
        total_cw = cw_cost[(project, day)]
        savings = total_cw * len(short_sess) / max(1, len(sess_counts)) * 0.5
        severity = "med" if savings >= 3 else "low"
        out.append(Suggestion(
            rule="session-fragmentation",
            severity=severity,
            scope=f"{_short_scope_project(project)} on {day}",
            evidence=(
                f"{len(sess_counts)} sessions ({len(short_sess)} with <5 calls) · "
                f"cache-write {fmt_cost(total_cw)}"
            ),
            action=(
                "Keep related work in a single session; starting fresh for every "
                "small task pays the cache-write cost again."
            ),
            est_savings=savings,
        ))
    return out


def _rule_cache_rebuild(records: list[Record]) -> list[Suggestion]:
    """Rule 7: session with cache_write ≫ cache_read (context kept getting rebuilt)."""
    by_session: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if r.session_id:
            by_session[r.session_id].append(r)
    out: list[Suggestion] = []
    for sess, recs in by_session.items():
        if len(recs) < 10:
            continue
        cost = sum(r.cost for r in recs)
        if cost < 5:
            continue
        cr = sum(int(r.usage.get("cache_read_input_tokens") or 0) for r in recs)
        cw = sum(int(r.usage.get("cache_creation_input_tokens") or 0) for r in recs)
        if cr == 0 or cw / cr < 0.2:
            continue
        # savings: assume healthy sessions sit at cw/cr ~0.05; excess cw is waste.
        excess_cw = cw - cr * 0.05
        if excess_cw <= 0:
            continue
        # price the excess at average per-token cw rate for the session
        total_cost_cw = sum(
            int(r.usage.get("cache_creation_input_tokens") or 0)
            * model_price(r.model)["cw_5m"] / 1_000_000
            for r in recs
        )
        rate = total_cost_cw / max(1, cw)
        savings = excess_cw * rate
        projects = {decode_project(r.project) for r in recs}
        proj = shorten_path(next(iter(projects))) if len(projects) == 1 else "multiple"
        severity = "med" if savings >= 5 else "low"
        out.append(Suggestion(
            rule="cache-rebuild",
            severity=severity,
            scope=f"session {sess[:8]} ({proj})",
            evidence=(
                f"{len(recs)} calls · cache-write/read ratio {cw/cr:.2f} "
                f"(healthy <0.1) · {fmt_cost(cost)}"
            ),
            action=(
                "Context thrashed — likely long session with growing history. "
                "Break into smaller tasks with `/clear` between unrelated goals."
            ),
            est_savings=savings,
        ))
    return out


def _rule_many_reads(records: list[Record]) -> list[Suggestion]:
    """Rule 8: session with many Read calls on ast-graph-supported languages."""
    by_session: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if r.session_id:
            by_session[r.session_id].append(r)
    out: list[Suggestion] = []
    for sess, recs in by_session.items():
        cost = sum(r.cost for r in recs)
        if cost < 5:
            continue
        all_tools = [t for r in recs for t in r.tools]
        if not all_tools:
            continue
        reads = sum(1 for t in all_tools if t == "Read")
        if reads < 30:
            continue
        if reads / len(all_tools) < 0.4:
            continue
        if not _project_lang_supported(recs):
            continue
        # savings: treat ~40% of the read-heavy portion as potentially avoidable.
        input_cost = sum(
            (int(r.usage.get("input_tokens") or 0)
             + int(r.usage.get("cache_read_input_tokens") or 0))
            * model_price(r.model)["cr"] / 1_000_000
            for r in recs
        )
        savings = input_cost * 0.4
        projects = {decode_project(r.project) for r in recs}
        proj = shorten_path(next(iter(projects))) if len(projects) == 1 else "multiple"
        severity = "med" if savings >= 3 else "low"
        out.append(Suggestion(
            rule="many-reads",
            severity=severity,
            scope=f"session {sess[:8]} ({proj})",
            evidence=(
                f"{reads} Read calls ({reads*100//len(all_tools)}% of tool use) · "
                f"{fmt_cost(cost)}"
            ),
            action=(
                "Use ast-graph (`scan` + `symbol`/`blast-radius`) for structural "
                "lookups — one query replaces many whole-file Reads."
            ),
            est_savings=savings,
        ))
    return out


def _rule_explore_on_opus(records: list[Record]) -> list[Suggestion]:
    """Rule 9: Opus session dominated by exploration tools (plan/analysis mode)."""
    by_session: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if r.session_id:
            by_session[r.session_id].append(r)
    out: list[Suggestion] = []
    for sess, recs in by_session.items():
        if len(recs) < 10:
            continue
        opus_recs = [r for r in recs if _is_opus(r.model)]
        if len(opus_recs) / len(recs) < 0.7:
            continue
        cost = sum(r.cost for r in recs)
        if cost < 5:
            continue
        all_tools = [t for r in recs for t in r.tools]
        if not all_tools:
            continue
        explore = sum(1 for t in all_tools if t in _EXPLORE_TOOLS)
        mutate = sum(1 for t in all_tools if t in _MUTATE_TOOLS)
        if explore + mutate == 0:
            continue
        if explore / (explore + mutate) < 0.85:
            continue
        opus_cost = sum(r.cost for r in opus_recs)
        savings = opus_cost * _OPUS_TO_SONNET_SAVINGS
        projects = {decode_project(r.project) for r in recs}
        proj = shorten_path(next(iter(projects))) if len(projects) == 1 else "multiple"
        lang_ok = _project_lang_supported(recs)
        action = (
            "Exploration on Opus is expensive — plan/analyze on Sonnet (or Haiku), "
            "switch to Opus only for synthesis/implementation."
        )
        if lang_ok:
            action += (
                " Pair with ast-graph for structural queries instead of Read-spray."
            )
        severity = "high" if savings >= 20 else "med" if savings >= 5 else "low"
        out.append(Suggestion(
            rule="explore-on-opus",
            severity=severity,
            scope=f"session {sess[:8]} ({proj})",
            evidence=(
                f"{len(recs)} calls · {len(opus_recs)} Opus · "
                f"{explore*100//(explore+mutate)}% explore tools · {fmt_cost(cost)}"
            ),
            action=action,
            est_savings=savings,
        ))
    return out


def _rule_plan_mode_opus(records: list[Record]) -> list[Suggestion]:
    """Rule 10: plan-mode session on Opus that spent its plan window scanning,
    not synthesizing.

    Plan mode on Opus has two cost components: synthesis (Opus's strength,
    cheap because output is small) and scan/investigate (Read/Grep/Glob
    spelunking — mechanical, expensive, no upside from Opus reasoning).
    The waste lives in the second one.

    Detection: split the session at the last `ExitPlanMode` call to get the
    plan window. Fire when the plan window was explore-dominated AND
    represents a meaningful slice of the session's cost. A synthesis-heavy
    plan window is skipped — Opus earned its keep.
    """
    by_session: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if r.session_id:
            by_session[r.session_id].append(r)
    out: list[Suggestion] = []
    for sess, recs in by_session.items():
        # Sort by timestamp; drop records whose timestamps don't parse.
        dated = [(parse_ts(r.timestamp), r) for r in recs]
        dated = [(ts, r) for ts, r in dated if ts is not None]
        if not dated:
            continue
        dated.sort(key=lambda x: x[0])
        sorted_recs = [r for _, r in dated]

        # Plan window = everything up to and including the LAST ExitPlanMode call.
        last_plan_idx = None
        for i, r in enumerate(sorted_recs):
            if "ExitPlanMode" in r.tools:
                last_plan_idx = i
        if last_plan_idx is None:
            continue
        plan_recs = sorted_recs[: last_plan_idx + 1]
        if len(plan_recs) < 5:
            continue

        # Plan window must be Opus-dominated (otherwise rule 9 covers it).
        opus_recs = [r for r in plan_recs if _is_opus(r.model)]
        if not opus_recs or len(opus_recs) / len(plan_recs) < 0.7:
            continue

        # Tool composition inside the plan window.
        plan_tools = [t for r in plan_recs for t in r.tools]
        if not plan_tools:
            continue
        explore = sum(1 for t in plan_tools if t in _EXPLORE_TOOLS)
        explore_ratio = explore / len(plan_tools)
        if explore_ratio < 0.70:
            continue  # synthesis-dominated → Opus is the right tool, no finding

        # Cost gating: plan window must be a meaningful slice of the session.
        plan_cost  = sum(r.cost for r in plan_recs)
        total_cost = sum(r.cost for r in sorted_recs)
        if total_cost <= 0 or plan_cost / total_cost < 0.40 or plan_cost < 3:
            continue

        distinct_reads = len({p for r in plan_recs for p in r.read_paths})
        plan_opus_cost = sum(r.cost for r in opus_recs)
        lang_ok = _project_lang_supported(plan_recs)

        if lang_ok:
            # Avoidable cost ≈ exploration-share of input-side spend in the plan
            # window, discounted because ast-graph replaces some but not all reads.
            input_side_cost = sum(
                (int(r.usage.get("input_tokens") or 0)
                 + int(r.usage.get("cache_read_input_tokens") or 0))
                * model_price(r.model)["cr"] / 1_000_000
                for r in plan_recs
            )
            savings = input_side_cost * explore_ratio * 0.5
            action = (
                "Opus is the right model for plan synthesis — keep it. "
                "What's expensive here is the scan/investigate phase: feed "
                "ast-graph (`symbol`, `hotspots`, `blast-radius`, `dead-code`) "
                "output into the plan input so Opus doesn't burn tokens "
                "Read/Grepping the codebase to discover structure."
            )
        else:
            # No structural-tool fix available; model downgrade is the only lever.
            savings = plan_opus_cost * 0.4
            action = (
                "Project isn't ast-graph-supported — draft the plan on Sonnet/"
                "Haiku and switch to Opus for the implementation turns."
            )

        if distinct_reads >= 10 and plan_cost >= 20:
            severity = "high"
        elif savings >= 5:
            severity = "med"
        else:
            severity = "low"

        projects = {decode_project(r.project) for r in plan_recs}
        proj = shorten_path(next(iter(projects))) if len(projects) == 1 else "multiple"
        plan_turns = sum(1 for t in plan_tools if t == "ExitPlanMode")
        out.append(Suggestion(
            rule="plan-mode-opus",
            severity=severity,
            scope=f"session {sess[:8]} ({proj})",
            evidence=(
                f"plan window {len(plan_recs)} call(s) · "
                f"{int(explore_ratio*100)}% explore tools · "
                f"{distinct_reads} distinct file(s) Read · "
                f"{plan_turns} plan turn(s) · {fmt_cost(plan_cost)} "
                f"({plan_cost/total_cost*100:.0f}% of session)"
            ),
            action=action,
            est_savings=savings,
        ))
    return out


_CTX_WARN_RATIO  = 0.75   # warn when a call hits 75% of the model's context cap
_CTX_ALERT_RATIO = 0.90   # alert at 90% — truncation/summarization imminent


def _rule_large_context(records: list[Record]) -> list[Suggestion]:
    """Rule 11: session whose single-call context (input + cache_r + cache_w)
    approaches or breaches the model's context cap.

    Thresholds are proportional to each call's model cap (see CONTEXT_CAP):
    a 750K call on Opus 4.7 (1M cap) is at 75% — the same risk profile as a
    150K call on a 200K-cap model. Calls past 90% are paying for tokens that
    get summarized away or dropped before they reach the model.
    """
    by_session: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if r.session_id:
            by_session[r.session_id].append(r)
    out: list[Suggestion] = []
    for sess, recs in by_session.items():
        # Pair each record with its raw context size and cap-relative ratio.
        ctx_recs = []
        for r in recs:
            c = (int(r.usage.get("input_tokens") or 0)
                 + int(r.usage.get("cache_read_input_tokens") or 0)
                 + int(r.usage.get("cache_creation_input_tokens") or 0))
            cap = context_cap(r.model)
            ratio = c / cap if cap else 0.0
            ctx_recs.append((r, c, ratio))
        if not ctx_recs:
            continue
        peak_ratio = max(ratio for _, _, ratio in ctx_recs)
        if peak_ratio < _CTX_WARN_RATIO:
            continue
        n_warn  = sum(1 for _, _, ratio in ctx_recs if ratio >= _CTX_WARN_RATIO)
        n_alert = sum(1 for _, _, ratio in ctx_recs if ratio >= _CTX_ALERT_RATIO)
        peak_rec, peak_ctx, _ = max(ctx_recs, key=lambda x: x[2])
        peak_cap = context_cap(peak_rec.model)
        cost = sum(r.cost for r in recs)
        projects = {decode_project(r.project) for r in recs}
        proj = shorten_path(next(iter(projects))) if len(projects) == 1 else "multiple"
        # Savings: input-side cost on warn-tier calls × 0.3 (assume `/clear`
        # mid-session would have shed ~30% of bloated input). Conservative.
        input_side_cost = sum(
            ctx * model_price(r.model)["cr"] / 1_000_000
            for r, ctx, ratio in ctx_recs if ratio >= _CTX_WARN_RATIO
        )
        savings = input_side_cost * 0.3
        severity = "high" if n_alert else "med"
        out.append(Suggestion(
            rule="large-context",
            severity=severity,
            scope=f"session {sess[:8]} ({proj})",
            evidence=(
                f"peak {fmt_num(peak_ctx)} tok ({peak_ctx/peak_cap*100:.0f}% of "
                f"{fmt_num(peak_cap)} cap) · "
                f"{n_warn} call(s) ≥{int(_CTX_WARN_RATIO*100)}%"
                + (f", {n_alert} ≥{int(_CTX_ALERT_RATIO*100)}%" if n_alert else "")
                + f" · {len(recs)} calls · {fmt_cost(cost)}"
            ),
            action=(
                f"Session is approaching the model's context cap "
                f"({fmt_num(peak_cap)} tok) — use `/clear` to reset, or split "
                "unrelated work across sessions. Tokens past the cap get "
                "summarized away (or dropped) but you still pay for them."
            ),
            est_savings=savings,
        ))
    return out


_EXPENSIVE_CALL_WARN_USD  = 5.0    # one call costs > $5 → suspicious
_EXPENSIVE_CALL_ALERT_USD = 10.0   # one call costs > $10 → almost certainly pathological


def _rule_expensive_single_call(records: list[Record]) -> list[Suggestion]:
    """Rule 12: any individual API call costing more than $5 — runaway turns,
    huge file pastes, log dumps, or a single Opus turn that hauled in a
    massive context.

    Aggregates per session so a session with N expensive calls produces ONE
    finding (not N). Severity bumps to 'high' when any call ≥ $10 — at that
    point you're virtually certain something went wrong, not just expensive
    work.
    """
    by_session: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if r.session_id:
            by_session[r.session_id].append(r)
    out: list[Suggestion] = []
    for sess, recs in by_session.items():
        expensive = [r for r in recs if r.cost > _EXPENSIVE_CALL_WARN_USD]
        if not expensive:
            continue
        peak = max(expensive, key=lambda r: r.cost)
        n_alert = sum(1 for r in expensive if r.cost >= _EXPENSIVE_CALL_ALERT_USD)
        cost = sum(r.cost for r in recs)
        projects = {decode_project(r.project) for r in recs}
        proj = shorten_path(next(iter(projects))) if len(projects) == 1 else "multiple"
        peak_ctx = (
            int(peak.usage.get("input_tokens") or 0)
            + int(peak.usage.get("cache_read_input_tokens") or 0)
            + int(peak.usage.get("cache_creation_input_tokens") or 0)
        )
        severity = "high" if n_alert else "med"
        out.append(Suggestion(
            rule="expensive-single-call",
            severity=severity,
            scope=f"session {sess[:8]} ({proj})",
            evidence=(
                f"{len(expensive)} call(s) >{fmt_cost(_EXPENSIVE_CALL_WARN_USD)}"
                + (f" ({n_alert} ≥{fmt_cost(_EXPENSIVE_CALL_ALERT_USD)})" if n_alert else "")
                + f" · peak {fmt_cost(peak.cost)} on {peak.model} "
                f"({fmt_num(peak_ctx)} tok ctx) · session total {fmt_cost(cost)}"
            ),
            action=(
                "Investigate the peak call — typical causes: an enormous file "
                "paste, a runaway tool loop, or an Opus turn that pulled in a "
                "huge context. Open the session's JSONL or use `monitor "
                "sessions --top` to drill in."
            ),
            est_savings=0.0,
        ))
    return out


_COLD_CACHE_HIT_PCT      = 0.30   # below 30% cache reuse = "cold"
_COLD_CACHE_MIN_CALLS    = 5
_COLD_CACHE_MIN_COST_USD = 2.0


def _rule_cache_cold_session(records: list[Record]) -> list[Suggestion]:
    """Rule 13: per-session cache-cold detection.

    Distinct from rule 3 (`low-cache-hit`) which fires per-project — this
    catches single sessions that ran cold even inside an otherwise
    cache-efficient project. Typical causes: started fresh and stayed
    short, repeated `/clear` mid-task, or jumping between unrelated files
    so the prefix never stabilizes.
    """
    by_session: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if r.session_id:
            by_session[r.session_id].append(r)
    out: list[Suggestion] = []
    for sess, recs in by_session.items():
        if len(recs) < _COLD_CACHE_MIN_CALLS:
            continue
        cost = sum(r.cost for r in recs)
        if cost < _COLD_CACHE_MIN_COST_USD:
            continue
        inp = sum(int(r.usage.get("input_tokens") or 0) for r in recs)
        cr  = sum(int(r.usage.get("cache_read_input_tokens") or 0) for r in recs)
        cw  = sum(int(r.usage.get("cache_creation_input_tokens") or 0) for r in recs)
        denom = inp + cr + cw
        if denom == 0:
            continue
        hit = cr / denom
        if hit >= _COLD_CACHE_HIT_PCT:
            continue
        # Savings estimate: if the hit rate had climbed to ~70%, half of the
        # current raw input tokens would have been cache-reads (≈10× cheaper).
        # Conservative — treats only raw input as shiftable.
        savings = 0.0
        for r in recs:
            p = model_price(r.model)
            raw_in = int(r.usage.get("input_tokens") or 0)
            shiftable = raw_in * 0.5
            savings += shiftable * (p["in"] - p["cr"]) / 1_000_000
        projects = {decode_project(r.project) for r in recs}
        proj = shorten_path(next(iter(projects))) if len(projects) == 1 else "multiple"
        severity = "med" if savings >= 3 else "low"
        out.append(Suggestion(
            rule="cache-cold-session",
            severity=severity,
            scope=f"session {sess[:8]} ({proj})",
            evidence=(
                f"{len(recs)} calls · cache hit {hit*100:.0f}% (cold) · "
                f"{fmt_cost(cost)}"
            ),
            action=(
                "Session ran without cache reuse — likely started fresh and "
                "did not stay long enough to amortize the prefix. Keep "
                "related work in one session; avoid mid-task `/clear`."
            ),
            est_savings=savings,
        ))
    return out


def analyze_suggestions(records: list[Record]) -> list[Suggestion]:
    rules = [
        _rule_opus_heavy_project,
        _rule_opus_routine_session,
        _rule_low_cache_hit,
        _rule_raw_input_spike,
        _rule_day_spike,
        _rule_session_fragmentation,
        _rule_cache_rebuild,
        _rule_many_reads,
        _rule_explore_on_opus,
        _rule_plan_mode_opus,
        _rule_large_context,
        _rule_expensive_single_call,
        _rule_cache_cold_session,
    ]
    out: list[Suggestion] = []
    for rule in rules:
        out.extend(rule(records))
    sev_order = {"high": 0, "med": 1, "low": 2}
    out.sort(key=lambda s: (sev_order.get(s.severity, 9), -s.est_savings))
    return out


def _render_alert_banners(console, suggestions: list[Suggestion]) -> None:
    """Print high-visibility alert banners above the suggestions table.

    Banners are reserved for rules whose findings are urgent / expensive
    enough to deserve a 'stop, look at me' visual treatment instead of a
    plain row. Add new banners here as new alert rules are introduced.
    """
    # --- large-context (rule 11) ---
    big = [s for s in suggestions if s.rule == "large-context"]
    if big:
        high = [s for s in big if s.severity == "high"]
        if high:
            console.print(
                f"\n[bold red on white] ⚠ LARGE CONTEXT ALERT [/bold red on white] "
                f"[bold red]{len(high)} session(s) ≥{int(_CTX_ALERT_RATIO*100)}% "
                f"of context cap — truncation likely.[/bold red] "
                f"Run [cyan]monitor suggest[/cyan] for details."
            )
        else:
            console.print(
                f"\n[bold yellow]⚠ Large-context warning:[/bold yellow] "
                f"{len(big)} session(s) ≥{int(_CTX_WARN_RATIO*100)}% of context cap. "
                f"Consider [cyan]/clear[/cyan] before adding more context."
            )

    # --- expensive-single-call (rule 12) — only banner the high-severity tier ---
    expensive_high = [
        s for s in suggestions
        if s.rule == "expensive-single-call" and s.severity == "high"
    ]
    if expensive_high:
        console.print(
            f"\n[bold red on white] 💸 EXPENSIVE CALL ALERT [/bold red on white] "
            f"[bold red]{len(expensive_high)} session(s) with single calls "
            f"≥{fmt_cost(_EXPENSIVE_CALL_ALERT_USD)}[/bold red] "
            f"— see suggestions table for the peak call to investigate."
        )


def _render_suggestions(console, suggestions: list[Suggestion], top: int) -> None:
    if not suggestions:
        console.print("[green]No efficiency issues detected — looking clean.[/green]")
        return
    total_savings = sum(s.est_savings for s in suggestions)
    console.print(
        f"[bold]Suggestions[/bold]  "
        f"found: [cyan]{len(suggestions)}[/cyan]  "
        f"est. potential savings: [green]{fmt_cost(total_savings)}[/green]"
    )
    t = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    t.add_column("Sev", no_wrap=True)
    t.add_column("Rule", no_wrap=True)
    t.add_column("Scope", overflow="fold", min_width=22)
    t.add_column("Evidence", overflow="fold")
    t.add_column("Save", justify="right", no_wrap=True)
    t.add_column("Action", overflow="fold")
    colors = {"high": "red", "med": "yellow", "low": "cyan"}
    for s in suggestions[:top]:
        c = colors.get(s.severity, "white")
        save_cell = fmt_cost(s.est_savings) if s.est_savings > 0 else "[dim]—[/dim]"
        t.add_row(
            f"[bold {c}]{s.severity}[/bold {c}]",
            s.rule,
            s.scope,
            s.evidence,
            save_cell,
            s.action,
        )
    console.print(t)


def cmd_suggest(args) -> None:
    records = load_records(args)
    if not records:
        print("No records.")
        return
    suggestions = analyze_suggestions(records)
    if args.min_savings > 0:
        suggestions = [
            s for s in suggestions
            if s.est_savings >= args.min_savings or s.est_savings == 0
        ]
    if not RICH:
        for s in suggestions[: args.top]:
            save = f"~{fmt_cost(s.est_savings)}" if s.est_savings > 0 else "-"
            print(f"[{s.severity.upper():4s}] {s.rule:22s} {s.scope}")
            print(f"       {s.evidence}")
            print(f"       save: {save}   → {s.action}")
        return
    console = Console()
    console.rule("[bold]Claude Code — Efficiency Suggestions[/bold]")
    _render_suggestions(console, suggestions, args.top)


def cmd_budget(args) -> None:
    """Check spend against daily / monthly / quarterly / yearly / rolling-30 / lifetime limits.

    Today and Month are always shown. Quarter / Year / Rolling-30 / Lifetime
    are only shown when their --flag is given (so the table stays tight by
    default). Any limit can be in 'tracking' mode (no --flag) to just show
    spend without a cap.
    """
    records = load_all()
    today = date.today()
    month_start = today.replace(day=1)
    quarter_num = (today.month - 1) // 3 + 1
    quarter_start = date(today.year, (quarter_num - 1) * 3 + 1, 1)
    year_start = date(today.year, 1, 1)
    rolling_30_start = today - timedelta(days=29)  # last 30 days incl today

    today_cost = month_cost = quarter_cost = year_cost = rolling_cost = lifetime_cost = 0.0
    for r in records:
        ts = parse_ts(r.timestamp)
        if ts is None:
            continue
        d = ts.astimezone().date()
        lifetime_cost += r.cost
        if d == today:
            today_cost += r.cost
        if d >= month_start:
            month_cost += r.cost
        if d >= quarter_start:
            quarter_cost += r.cost
        if d >= year_start:
            year_cost += r.cost
        if d >= rolling_30_start:
            rolling_cost += r.cost

    # Always show today + this month. Higher-period rows only render when
    # the user passed a corresponding limit flag.
    rows: list[tuple[str, float, float | None]] = [
        ("Today", today_cost, args.daily),
        (f"Month ({month_start:%b %Y})", month_cost, args.monthly),
    ]
    if args.quarterly is not None:
        rows.append((f"Quarter ({quarter_start.year}-Q{quarter_num})",
                     quarter_cost, args.quarterly))
    if args.yearly is not None:
        rows.append((f"Year {year_start.year}", year_cost, args.yearly))
    if args.rolling_30 is not None:
        rows.append(("Rolling 30d", rolling_cost, args.rolling_30))
    if args.lifetime is not None:
        rows.append(("Lifetime", lifetime_cost, args.lifetime))

    worst_frac = 0.0
    if not RICH:
        for label, spent, limit in rows:
            if limit:
                frac = spent / limit
                worst_frac = max(worst_frac, frac)
                print(f"{label:20s}  {fmt_cost(spent)} / {fmt_cost(limit)}  ({frac*100:5.1f}%)")
            else:
                print(f"{label:20s}  {fmt_cost(spent)}  (no limit set)")
    else:
        console = Console()
        t = Table(title="Cost Budget", box=box.SIMPLE_HEAVY)
        t.add_column("Scope", no_wrap=True); t.add_column("Spent", justify="right", no_wrap=True)
        t.add_column("Limit", justify="right", no_wrap=True); t.add_column("%", justify="right", no_wrap=True)
        t.add_column("Progress", justify="left", no_wrap=True); t.add_column("Status", justify="right", no_wrap=True)
        for label, spent, limit in rows:
            if limit:
                frac = spent / limit
                worst_frac = max(worst_frac, frac)
                bar_width = 16
                filled = min(bar_width, int(frac * bar_width))
                if frac >= 1.0:
                    color = "red"; status = "[bold red]OVER[/bold red]"
                elif frac >= args.warn_at:
                    color = "yellow"; status = f"[bold yellow]WARN >{int(args.warn_at*100)}%[/bold yellow]"
                else:
                    color = "green"; status = "[green]ok[/green]"
                bar = f"[{color}]{'█' * filled}[/{color}]{'░' * (bar_width - filled)}"
                t.add_row(label, fmt_cost(spent), fmt_cost(limit),
                          f"{frac*100:.1f}%", bar, status)
            else:
                t.add_row(label, fmt_cost(spent), "-", "-", "", "[dim]no limit[/dim]")
        console.print(t)
        if worst_frac >= 1.0:
            console.print(f"[bold red]Budget exceeded.[/bold red]")
        elif worst_frac >= args.warn_at:
            console.print(f"[bold yellow]Approaching budget limit "
                          f"({worst_frac*100:.0f}% of worst scope).[/bold yellow]")

    if args.strict:
        if worst_frac >= 1.0:
            sys.exit(1)
        if worst_frac >= args.warn_at:
            sys.exit(2)
    sys.exit(0)


# -------------------------------- CLI ---------------------------------- #


def main() -> None:
    p = argparse.ArgumentParser(
        prog="monitor",
        description="Monitor Claude Code token usage and estimated costs.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # Common time-window flags shared by every aggregation command.
    # Not added to `live` (real-time by definition) or `budget` (computes its
    # own per-period windows).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--since", metavar="DATE",
                        help="Filter records since YYYY-MM-DD (local) or full ISO timestamp.")
    common.add_argument("--until", metavar="DATE",
                        help="Filter records up through YYYY-MM-DD (inclusive) or full ISO timestamp.")
    common.add_argument("--last", metavar="DUR",
                        help="Shortcut for --since: e.g. 7d, 24h, 30m, 2w. Conflicts with --since.")

    sub.add_parser("summary", parents=[common],
                   help="Overall totals and per-model breakdown")

    pd = sub.add_parser("daily", parents=[common], help="Daily usage breakdown")
    pd.add_argument("--days", type=int, default=14, help="How many days to show (default 14)")

    pp = sub.add_parser("projects", parents=[common], help="Top projects by cost")
    pp.add_argument("--top", type=int, default=20)

    ps = sub.add_parser("sessions", parents=[common], help="Top sessions by cost")
    ps.add_argument("--top", type=int, default=20)

    pe = sub.add_parser("export", parents=[common], help="Export raw records")
    pe.add_argument("--format", choices=["csv", "json"], default="csv")
    pe.add_argument("--output", "-o", default="-", help="Output path or '-' for stdout")

    pl = sub.add_parser("live", help="Live auto-refreshing dashboard")
    pl.add_argument("--interval", type=float, default=5.0, help="Refresh seconds (min 1)")
    pl.add_argument("--budget-daily", type=float, dest="budget_daily",
                    help="Show projection vs daily budget in USD")
    pl.add_argument("--context-warn", type=int, default=None, dest="context_warn",
                    help="Warn when single-call context (input+cache_r+cache_w) "
                         "reaches N tokens (default: 75%% of the model's cap — "
                         "150K for 200K models, 750K for 1M models)")
    pl.add_argument("--context-alert", type=int, default=None, dest="context_alert",
                    help="Red-alert threshold for single-call context "
                         "(default: 90%% of the model's cap)")

    ph = sub.add_parser("heatmap", parents=[common],
                        help="Day-of-week x hour-of-day usage heatmap (local time)")
    ph.add_argument("--metric", choices=["cost", "calls", "tokens"], default="cost")

    pt = sub.add_parser("trend", parents=[common],
                        help="Daily trend for one project (substring match)")
    pt.add_argument("project", help="Path substring, e.g. 'ZeroCTX' or 'Desktop/Code/idea'")
    pt.add_argument("--days", type=int, default=30, help="Limit to last N active days (0 = all)")

    pa = sub.add_parser("activity", parents=[common],
                        help="Per-day unique sessions & projects active (engagement)")
    pa.add_argument("--days", type=int, default=30, help="Limit to last N active days (0 = all)")

    pw = sub.add_parser("weekly", parents=[common], help="Per-ISO-week usage (Mon-Sun buckets)")
    pw.add_argument("--weeks", type=int, default=12, help="Last N weeks (default 12)")

    pc = sub.add_parser("calendar", parents=[common], help="GitHub-style yearly activity grid")
    pc.add_argument("--year", type=int, help="Year to show (default: current year)")
    pc.add_argument("--metric", choices=["cost", "calls"], default="cost")

    pca = sub.add_parser("cache", parents=[common],
                         help="Cache hit rate and estimated savings per project")
    pca.add_argument("--top", type=int, default=15)

    pr = sub.add_parser("report", parents=[common],
                        help="Export a full dashboard (HTML/SVG/TXT)")
    pr.add_argument("--format", choices=["html", "svg", "txt"], default="txt")
    pr.add_argument("--output", "-o", help="Output path (default: claude-usage-<date>.<ext>)")
    pr.add_argument("--width", type=int, default=140, help="Render width in columns")
    pr.add_argument("--project", help="Filter to one project (substring match, e.g. 'my-app')")

    psg = sub.add_parser("suggest", parents=[common],
                         help="Detect inefficient usage patterns and suggest savings")
    psg.add_argument("--top", type=int, default=20, help="Max suggestions to show (default 20)")
    psg.add_argument("--min-savings", type=float, default=0.0,
                     help="Hide quantified suggestions with est. savings below $X")

    pb = sub.add_parser("budget",
                        help="Check spend vs daily/monthly/quarterly/yearly/rolling/lifetime limits")
    pb.add_argument("--daily", type=float, help="Daily budget in USD, e.g. 10")
    pb.add_argument("--monthly", type=float, help="Monthly budget in USD, e.g. 200")
    pb.add_argument("--quarterly", type=float, help="Quarterly budget in USD, e.g. 600")
    pb.add_argument("--yearly", type=float, help="Yearly budget in USD, e.g. 2400")
    pb.add_argument("--rolling-30", type=float, dest="rolling_30",
                    help="Rolling 30-day budget in USD")
    pb.add_argument("--lifetime", type=float, help="Lifetime cap in USD")
    pb.add_argument("--warn-at", type=float, default=0.8,
                    help="Warn when spend reaches this fraction of limit (default 0.8)")
    pb.add_argument("--strict", action="store_true",
                    help="Exit 1 if over limit, 2 if over warn threshold (for scripts)")

    args = p.parse_args()
    handlers = {
        "summary":  cmd_summary,
        "daily":    cmd_daily,
        "projects": cmd_projects,
        "sessions": cmd_sessions,
        "export":   cmd_export,
        "live":     cmd_live,
        "heatmap":  cmd_heatmap,
        "trend":    cmd_trend,
        "activity": cmd_activity,
        "weekly":   cmd_weekly,
        "calendar": cmd_calendar,
        "cache":    cmd_cache,
        "report":   cmd_report,
        "suggest":  cmd_suggest,
        "budget":   cmd_budget,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()

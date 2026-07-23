# MonitorTokenUsage

![demo](plugin/tests/record/demo-16x9.gif)

A single-file Python CLI that reads Claude Code's session JSONL logs from
`~/.claude/projects/` and reports token usage, estimated costs, trends,
and budget status.

No API calls, no daemon, no database — just parses the logs Claude Code
already writes locally.

## Install

### Quickest: via npm (no Python needed)

```bash
# Run once without installing
npx claude-code-token-monitor summary

# Or install globally
npm install -g claude-code-token-monitor
claude-code-token-monitor --help
```

The npm package downloads a pre-built native binary for your OS (macOS x64/arm64, Linux x64/arm64, Windows x64) — no Python runtime required.

### CLI from source (Python)

```bash
pip install -r requirements.txt
python monitor.py --help
```

### CLI + routing plugin (recommended)

Installs the CLI, registers the `routine-worker` Sonnet subagent globally,
and adds a complexity-tier routing directive to `~/.claude/CLAUDE.md` so
every Claude Code session delegates routine edits to Sonnet:

```bash
# macOS / Linux
bash plugin/hooks/install.sh

# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File plugin\hooks\install.ps1
```

The script is idempotent — re-run it after `git pull` to refresh.

On macOS/Linux the agent is installed as a symlink into `~/.claude/agents/`,
so repo updates propagate automatically. Windows copies the file; re-run the
installer to pick up changes.

To uninstall: remove the block between the
`<!-- claude-token-monitor:tier-routing:* -->` markers in `~/.claude/CLAUDE.md`
and delete `~/.claude/agents/routine-worker.md`.

Only Python dependency: `rich` (for tables, colours, live mode). The script
also runs without `rich` with a plain-text fallback.

Requires Python 3.10+ (uses PEP 604 `X | Y` type hints and `str.removeprefix`-era stdlib).

## Testing the routing plugin

Three test layers, fastest → slowest:

```bash
# Unit tests (stdlib unittest, ~29 tests, <1s)
python3 plugin/tests/test_check_routing.py -v

# Installer regression tests (sandbox-based, ~19 asserts, ~1s)
bash plugin/tests/test_install.sh

# Full end-to-end (install + unit + synthetic-data pipeline, ~3s)
bash plugin/tests/e2e.sh
```

`check_routing.py` also works as a live verification on your real sessions:

```bash
# Did main Claude actually delegate to routine-worker recently?
python3 plugin/tests/check_routing.py --verbose

# Scope to one project / widen window
python3 plugin/tests/check_routing.py --project my-repo --hours 168
```

Exit codes: `0` = routing confirmed · `1` = no Tier-2 invocations in window ·
`2` = invocations happened but subagent did not run on Sonnet (agent
frontmatter misconfigured).

## Usage

```bash
python monitor.py <command> [options]
```

Most commands accept the **global time-window flags** (`--since / --until / --last`)
to scope their output. See [Time-window filter](#time-window-filter) below.

### Commands

| Command | Purpose |
|---|---|
| `summary` | Grand totals and per-model breakdown (large-context alert pinned at top) |
| `daily [--days N]` | Daily breakdown (default 14 days) |
| `projects [--top N]` | Top projects by cost (default 20) |
| `sessions [--top N]` | Top sessions by cost (with AI-generated session titles) |
| `weekly [--weeks N]` | Per-ISO-week totals (Mon–Sun buckets) |
| `heatmap [--metric cost\|calls\|tokens]` | 7×24 day-of-week × hour heatmap (local time) |
| `calendar [--year YYYY] [--metric cost\|calls]` | GitHub-style yearly activity grid |
| `trend <project> [--days N]` | Daily trend for one project (substring match) |
| `activity [--days N]` | Per-day unique sessions & projects active + top project of each day |
| `cache [--top N]` | Cache hit rate + estimated savings per project |
| `agents [--top N] [--project SUBSTR]` | Subagent delegation analytics: main-loop vs delegated token split, per-agent-type cost, savings vs Opus |
| `memory [--detail]` | Per-project persistent-memory health: fact counts by type, size, staleness, MEMORY.md index integrity |
| `disk [--top N]` | Disk footprint of the `.claude` directory by folder + largest session transcripts |
| `plans` | List saved plan-mode plans in `~/.claude/plans` |
| `suggest [--top N] [--min-savings USD]` | Detect inefficient usage patterns and suggest savings |
| `budget [--daily \| --monthly \| --quarterly \| --yearly \| --rolling-30 \| --lifetime USD] [--warn-at 0.8] [--strict]` | Spend vs any combination of period limits |
| `live [--interval S] [--budget-daily USD] [--context-warn N] [--context-alert N]` | Auto-refreshing dashboard with burn rate + active-session panel |
| `export --format csv\|json [-o path]` | Raw per-call records |
| `report --format html\|svg\|txt [-o path] [--project SUBSTR] [--width N]` | Full dashboard export (filterable to one project) |

### Time-window filter

Every aggregation command (`summary`, `daily`, `weekly`, `projects`, `sessions`,
`heatmap`, `calendar`, `trend`, `activity`, `cache`, `suggest`, `report`,
`export`) accepts:

| Flag | Meaning |
|---|---|
| `--since YYYY-MM-DD` | Records on or after this local date |
| `--until YYYY-MM-DD` | Records up through this local date (inclusive) |
| `--last <duration>` | Shortcut for `--since now-<duration>`. Conflicts with `--since`. Units: `m` / `h` / `d` / `w`. Examples: `7d`, `24h`, `30m`, `2w` |

`--since` and `--until` also accept full ISO timestamps if you need
sub-day precision. `live` and `budget` deliberately ignore these — `live`
is realtime, `budget` computes its own per-period windows.

### Examples

```bash
# What have I spent?
python monitor.py summary

# Spend during the current sprint (April 1 → 30)
python monitor.py summary --since 2026-04-01 --until 2026-04-30

# Just the last 7 days, broken down by day
python monitor.py daily --last 7d

# Last 24 hours of activity, grouped by project
python monitor.py projects --last 24h

# Trend for one project (substring of path)
python monitor.py trend ZeroCTX

# When do I use Claude the most?
python monitor.py heatmap
python monitor.py heatmap --metric calls

# How many sessions / projects did I juggle per day?
python monitor.py activity --days 30

# Am I over budget? (any combination of periods)
python monitor.py budget --daily 30 --monthly 500
python monitor.py budget --quarterly 1500 --yearly 5000
python monitor.py budget --rolling-30 600 --lifetime 10000

# CI / cron guard — exit 1 if over any limit, 2 if above warn threshold
python monitor.py budget --monthly 500 --strict

# Live dashboard while coding (with burn rate + budget projection)
python monitor.py live --interval 3 --budget-daily 30

# Week-level view
python monitor.py weekly --weeks 8

# GitHub-style calendar for a year
python monitor.py calendar --year 2026

# Cache efficiency — how much did caching save you?
python monitor.py cache

# What could you be doing more efficiently? (Opus-when-Sonnet-would-do, log-dumps, day spikes, …)
python monitor.py suggest
python monitor.py suggest --top 10 --min-savings 5

# Export full dashboard to HTML (then browser Print -> Save as PDF)
python monitor.py report --format html -o usage-report.html

# Single-project dashboard — useful for shareable per-project reports
python monitor.py report --format html --project ZeroCTX -o zeroctx.html

# Or archive as SVG (color-accurate, scales cleanly)
python monitor.py report --format svg -o usage-report.svg

# Export raw per-call records to CSV (filterable by date range)
python monitor.py export --format csv -o usage.csv
python monitor.py export --format csv --since 2026-04-01 --until 2026-04-30 -o april.csv
```

## Subscription plan & recent activity

`summary` and `budget` now read `~/.claude/.credentials.json` and display a
plan banner:

```
Plan: Max 20x   · Rate tier: default_claude_max_20x   · Costs are API-equivalent — you pay a flat subscription fee.
```

For paid subscriptions (Pro / Max / Max 20x / Team / Enterprise) the banner
flags that the dollar totals are *API-equivalent cost* — what the same usage
would cost on the metered API. Your actual out-of-pocket spend is the flat
subscription fee. For Free / unknown plans (or when credentials can't be
read), the line is omitted.

> **macOS note.** Claude Code stores credentials in the Keychain on macOS
> (service `Claude Code-credentials`). The banner is not yet wired to read
> the Keychain — macOS users will simply see no banner. Costs still display
> normally.

`summary` also prints a **Recent Activity** table with today vs. yesterday vs.
30-day average, plus month-to-date and a linear projection of end-of-month
spend at the current daily pace:

```
Today            $89.92   (672 calls)   (2.3× 30-day avg)
Yesterday        $12.16   (84 calls)
30-day avg       $39.65/day   (29 active days)
MTD (May 2026)   $1,108.55   (day 24/31)
Projected EOM    $1,431.87
```

`budget` shows the same projection as an extra row in the table and, when
`--monthly USD` is set, treats the projection as a budget check — `PROJ OVER`
status when the linear extrapolation exceeds the monthly cap. The projection
also feeds into `--strict` exit codes, so a cron job can warn early if you're
on pace to blow the month before you've actually hit the cap.

## Alerts

`summary` and `report` print high-visibility banners above the suggestions
table when one of the alert rules fires. The goal is to catch your eye on
problems that are too important to leave as an ordinary row in a 50-row
suggestions list.

### Large context (rule `large-context`)

Sessions approaching the model's context cap waste tokens — anything past
the cap gets summarized away or dropped, but you still pay for it.

Thresholds are **proportional to each model's cap**, not a fixed token
count. A 750K call on Opus 4.7 (1M cap) is at 75% — the same risk profile
as a 150K call on a 200K-cap model. Caps live in the `CONTEXT_CAP` dict
in [monitor.py](monitor.py); Opus 4.6 and 4.7 default to 1M, everything
else to 200K.

| Model family | Cap | Warn (75%) | Alert (90%) |
|---|---|---|---|
| `claude-opus-4-6`, `claude-opus-4-7` | 1,000,000 | 750,000 | 900,000 |
| Everything else (default) | 200,000 | 150,000 | 180,000 |

```
 ⚠ LARGE CONTEXT ALERT  10 session(s) ≥90% of context cap — truncation likely.
```

Surfaces in three places:

1. **`summary` / `report`** — red banner above the suggestions table when
   any session has crossed the alert threshold (≥90% of its model's cap).
2. **`suggest`** — `large-context` rule rows show peak per-call context
   as both raw tokens and a percentage of the cap, plus the count of
   calls over the warn/alert thresholds.
3. **`live`** — active-session panel turns red when the current session's
   peak context crosses the alert threshold, with a
   `⚠ NEAR/OVER {cap} CAP — /clear NOW` inline warning where `{cap}` is
   the active model's context cap.

CLI overrides for the `live` command: `--context-warn N` /
`--context-alert N`. Default behavior derives both thresholds from the
active session's model cap.

> **Sonnet 1M-tier caveat:** `claude-sonnet-4-x` supports a 1M context
> window on certain plans, but the model ID in the JSONL is identical for
> 200K and 1M tiers — there's no way to tell from logs alone. Sonnet
> stays at the 200K default. If you're on the 1M-Sonnet tier, override
> with `--context-warn 750000 --context-alert 900000` for `live`, or edit
> `CONTEXT_CAP` in [monitor.py](monitor.py) for `suggest`.

### Expensive single call (rule `expensive-single-call`)

Any one API call costing more than $5 is suspicious; ≥$10 is almost
always pathological — a huge file paste, a runaway tool loop, or an Opus
turn that hauled in a massive context.

```
 💸 EXPENSIVE CALL ALERT  3 session(s) with single calls ≥$10.00
```

Surfaces in two places:

1. **`summary` / `report`** — red banner when any session has at least one
   call ≥ $10. Aggregates per session so the banner counts sessions, not
   raw call count (one bad session ≠ ten alerts).
2. **`suggest`** — `expensive-single-call` rule rows show the peak call's
   cost, model, context size, and the session total.

Thresholds are module-level constants (`_EXPENSIVE_CALL_WARN_USD = 5`,
`_EXPENSIVE_CALL_ALERT_USD = 10` in [monitor.py](monitor.py)). Edit them
if your typical work runs hotter than these defaults.

### Cache-cold session (rule `cache-cold-session`)

Per-session cache-cold detection: ≥5 calls, cost > $2, cache hit rate <30%.
Distinct from the project-level `low-cache-hit` rule — catches single
sessions that ran cold even inside an otherwise cache-efficient project.
No banner (not urgent enough); shows in the suggestions table only.

## PDF output

There is no dedicated PDF command — adding a PDF-rendering library
(WeasyPrint, ReportLab) would balloon the dependency footprint for a
one-file tool. Instead:

1. `python monitor.py report --format html -o report.html`
2. Open the HTML file in a browser.
3. Use **File > Print > Save as PDF** (Chrome, Edge, Firefox all support this).

This is typically sharper than library-rendered PDF, and needs no extra install.

## Suggestions engine

`suggest` runs 25 rules over your logs and flags concrete, dollar-quantified
recommendations. The same output is appended to `report --format html` as an
"Efficiency Suggestions" section.

| Rule | Fires when | Recommendation |
|---|---|---|
| `opus-heavy-project` | Opus ≥ 60% of project cost, avg output < 500 tok, ≥ 20 Opus calls | Default the project to Sonnet — routine edits don't need Opus |
| `opus-routine-session` | Session ≥ 20 calls, all-Opus, avg output < 500 tok | Rerun this kind of work on Sonnet |
| `low-cache-hit` | Project cost > $10, cache hit rate < 40% | Keep related work in one session; avoid frequent `/clear` |
| `raw-input-spike` | ≥ 3 calls with > 50K raw input tokens (build/diff dumps) | Pipe commands through [`zero rewrite-exec`](https://github.com/emtyty/zeroctx) to compress stdout |
| `day-spike` | Day cost > 3× median of last 30 active days | Investigate that day's top session for runaway context |
| `session-fragmentation` | ≥ 3 short sessions (< 5 calls each) on same project same day | Consolidate; each fresh session pays cache-write again |
| `cache-rebuild` | Session `cache_write / cache_read` > 0.2 | Long session with growing history — split with `/clear` |
| `many-reads` | Session ≥ 30 Read calls, ≥ 40% of tool use, supported language | Use [ast-graph](https://github.com/emtyty/ast-graph) `symbol` / `blast-radius` instead of whole-file Reads |
| `explore-on-opus` | Session ≥ 70% Opus, ≥ 85% exploration tools (Read/Grep/Glob/…) | Plan/analyze on Sonnet or Haiku; Opus only for synthesis. Pairs well with ast-graph |
| `plan-mode-opus` | Plan window (records up to last `ExitPlanMode`) ≥ 5 calls, ≥ 70% Opus, ≥ 70% explore tools, ≥ 40% of session cost | Opus is right for plan synthesis — keep it. Feed ast-graph `symbol`/`hotspots`/`blast-radius`/`dead-code` into the plan input so Opus doesn't burn tokens Read/Grepping the codebase. Falls back to Sonnet/Haiku when the project isn't ast-graph-supported |
| `large-context` | Any single call ≥ 75% of its model's context cap (warn) or ≥ 90% (alert/high). Caps: 1M for Opus 4.6/4.7, 200K otherwise — see `CONTEXT_CAP` in [monitor.py](monitor.py) | `/clear` mid-session or split unrelated work. Tokens past the cap are billed but get summarized/dropped |
| `expensive-single-call` | Session contains any single API call > $5. High severity when any call ≥ $10 | Investigate the peak call — usually a huge file paste, runaway tool loop, or Opus turn that pulled in a massive context |
| `cache-cold-session` | Session ≥ 5 calls AND cost > $2 AND cache hit rate < 30% | Keep related work in one session; avoid mid-task `/clear`. Distinct from `low-cache-hit` (per-project) — this catches single cold sessions inside an otherwise-warm project |
| `excessive-clear` | Session contains ≥ 3 `/clear` slash commands (≥ 5 = high severity) | Each `/clear` discards the prompt cache and forces the next turn to pay full cache-write rates. Read from `~/.claude/history.jsonl`. Savings estimated as `median_cache_rebuild_cost × (n_clears − 1)` |
| `truncated-output` | ≥ 2 calls in a session hit the output-token ceiling (`stop_reason: max_tokens`) | The cut-off output was paid for and regenerated — ask for smaller chunks (diffs, split writes) |
| `runaway-prompt` | One user prompt (grouped by `promptId`) spawns ≥ 20 API calls and ≥ $5 (high ≥ $15) | Points at the specific *ask* that spiraled — plan first or split the task. One finding per session (its worst prompt) |
| `cache-miss-cause` | ≥ 3 cache misses in a project share a fixable cause from the API's own `cache_miss_reason` diagnostics (`tools_changed`, `model_changed`, `system_changed`, `messages_changed`, `previous_message_not_found`) | Names the actual cause instead of inferring it — e.g. "MCP servers connecting mid-session rebuild the whole prefix". Savings from the reported re-written token counts |
| `web-search-spend` | Server-side web searches ≥ $2 in a project ($10 per 1K requests, billed on top of tokens) | Point Claude at local docs or pinned URLs. This spend is also now included in every cost figure |
| `api-error-retries` | ≥ 3 errored API calls in one session | Retry overhead, not useful work — back off or trim context instead of hammering |
| `tool-result-bloat` | ~150K+ tokens of tool output entered a session's context, or ≥ 3 single results > 15K tokens | Pipe noisy build/test commands through tail or a filter; prefer quiet flags |
| `full-file-reads` | ≥ 5 whole-file reads of ≥ 500-line files in a session | Nudge toward ranged reads (offset/limit) or Grep-first navigation |
| `hook-error-spam` | A failing hook's error text injected ≥ 5 times in a project | Fix or remove the hook — every injection costs input tokens on every affected turn |
| `permission-friction` | ≥ 3 tool calls blocked by permission rules in a project | Allowlist the safe commands in `.claude/settings.json` — each denial burns a turn on retries |
| `edit-churn` | ≥ 3 Claude edits hand-modified by the user afterwards in a project | The model's changes don't match project conventions — capture the recurring corrections in CLAUDE.md |
| `interrupted-turns` | ≥ 3 tool runs killed mid-flight in a session | The aborted output was still paid for — tighter prompts or plan mode up front is cheaper |

Rules 8, 9 and 10 check the project's `Read` file extensions against
ast-graph's supported languages (Rust, Python, JS/TS, C#, Java) — the
ast-graph suggestion only appears when ≥ 50% of the reads land on
supported files.

Plan mode is detected from the JSONL logs via the `ExitPlanMode` tool
call — Claude Code emits that tool when the user approves a plan, so its
presence in a session is a reliable marker that planning happened there.

Savings are estimated from Claude pricing: Opus → Sonnet saves ~80%
across input, output, and cache tiers (the ratio is roughly uniform).
ZeroCTX is assumed to compress spike stdout by ~60%. These are
rules-of-thumb — treat the numbers as directional, not accounting.

The `report --format html` export embeds the Suggestions table plus a
footer repeating this methodology and linking the external tools, so a
shared report is self-explanatory.

## Pricing

Per-1M-token rates live in a dict at the top of [monitor.py](monitor.py)
(`PRICING`). Models are matched by substring against the `model` field
in each JSONL entry (e.g. `claude-opus-4-6` matches the `claude-opus-4`
entry). Unknown models fall back to Sonnet-equivalent pricing.

Edit the dict if your rates differ (enterprise, batch tier, etc.).

## Vibes (opt-in, for fun)

A character-sheet view of your usage — class, level, achievements, traits, and
a 90-day activity heatmap. Opt-in only:

```bash
python monitor.py vibes                # standalone
python monitor.py summary --vibes      # appended to summary
```

Includes:

- **Class** (`Strategist`, `Resonator`, `Heavy Lifter`, `Workhorse`,
  `Sprinter`, `Cartographer`, `Marathoner`, `Quickfire`, `Generalist`) —
  derived from your dominant usage pattern.
- **Level** — a sqrt curve over `(user_msgs + 5 × dollars_spent)`, capped at 99.
- **Achievements** — 24 milestones (`Initiate`, `Veteran`, `Lifer`, `Cup of
  Coffee`, `Day's Pay`, `Mortgage Payment`, `Megaton`, `Gigaton`, `Cache
  Whisperer`, `Cache Conjurer`, `Opus Devotee`, `Sonnet Loyalist`, `Polyglot`,
  `Plan B`, `Insomniac`, `Sunrise Squad`, `Weekend Coder`, `Streak: Nd`,
  `Marathoner`, `Tactful`, `Polite Codec`, `Inquisitor`, `Volume Pasteur`,
  `First Drop`). Locked ones show as next-up challenges.
- **Traits** — `Manners`, `Composure`, `Inquiry`, each scored from regex
  counts on your user prompts.
- **Field notes** — total questions, courtesies, caps-bursts, confusions,
  celebrations.
- **90-day activity heatmap** — weekday × day-of-window grid of user message
  volume. Auto-adapts column count to your terminal width.
- **Arenas** — top 5 projects by user-message volume.

> **Privacy.** `vibes` reads user prompt text from
> `~/.claude/projects/*/*.jsonl` *only to count regex matches in memory*. No
> prompt text is ever retained or printed — only aggregate counts. The same
> contract as the `excessive-clear` rule.

## Data sources

The monitor reads three local files written by Claude Code itself. Nothing
leaves your machine.

| Path | Used for | Privacy |
|---|---|---|
| `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl` | Per-call usage, model, tools, plan-mode detection, plus `stop_reason` / reasoning `effort` / `promptId` / attributed skill / cache-miss diagnostics / web-search counts, and per-session signals from `user` + `attachment` entries (tool-output size, whole-file reads, permission denials, interrupts, failing hooks, hand-modified edits) — the primary source | Read in full (it's already on disk). Tool-output entries are measured by **length only** — the content is never retained |
| `~/.claude/.credentials.json` | Subscription tier banner (`Pro` / `Max` / `Max 20x` / `Team` / `Enterprise`). macOS Keychain not implemented yet — Mac users get no banner | Only `subscriptionType` and `rateLimitTier` are read; OAuth tokens are ignored |
| `~/.claude/history.jsonl` | Slash-command histogram in `summary` and the `excessive-clear` rule | **Prompt text is never retained.** Only the slash command (first word after `/`), timestamps, session/project IDs, and total pasted-content character count are kept. Raw `display` strings are dropped at parse time |

`summary` prints a "User Input (privacy-safe — counts only)" table summarising
top slash commands and paste volume in the active time window. Inputs and
their full text are never echoed.

## How it works

Claude Code logs every session to `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`.
Each line is one event; assistant messages carry a `message.usage` block:

```json
{
  "type": "assistant",
  "sessionId": "...",
  "timestamp": "2026-04-15T07:59:30.447Z",
  "message": {
    "model": "claude-opus-4-6",
    "id": "msg_...",
    "usage": {
      "input_tokens": 3,
      "output_tokens": 653,
      "cache_read_input_tokens": 12329,
      "cache_creation_input_tokens": 7199
    }
  }
}
```

The tool walks every `.jsonl` file, deduplicates by `(sessionId, message.id)`
— because one assistant turn with multiple content blocks logs multiple
lines that carry the same (full) usage block — and aggregates from there.

## Budget alerts in practice

`budget` supports six independent period limits — pass any combination:

| Flag | Period |
|---|---|
| `--daily USD` | Today (local) |
| `--monthly USD` | Current calendar month |
| `--quarterly USD` | Current calendar quarter (Jan-Mar / Apr-Jun / Jul-Sep / Oct-Dec) |
| `--yearly USD` | Current calendar year |
| `--rolling-30 USD` | Trailing 30 days incl. today |
| `--lifetime USD` | All-time spend cap |

Today and Month rows are always shown (without limits if no flag is given);
the others render only when their flag is set so the table stays compact.

`budget --strict` is the scripting-friendly mode. Exit codes:

- `0` — under warn threshold across **all** configured limits
- `2` — over warn threshold (default 80%) on any limit
- `1` — over limit on any configured period

Drop it into a cron / scheduled task:

```bash
# Every hour: warn if I'm close to blowing this month's budget
python /path/to/monitor.py budget --monthly 500 --strict \
    || notify-send "Claude Code approaching budget"
```

Or a Claude Code `SessionEnd` hook in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      { "command": "python C:/path/MonitorTokenUsage/monitor.py budget --daily 30" }
    ]
  }
}
```

## Notes

- **Timestamps are UTC in the logs.** Heatmap and budget convert to local
  time via `datetime.astimezone()`. `daily` / `trend` bucket by the
  local date for the same reason.
- **`<synthetic>` model entries** are Claude Code's internal zero-token
  messages — they show up in `summary` with `$0` cost.
- **Windows consoles** default to cp1252; the script reconfigures stdout
  to UTF-8 on startup so rich's box-drawing and glyphs render.
- **No network.** All data is local. The tool never calls the Anthropic
  API or sends your logs anywhere.

## License

MIT — see [LICENSE](LICENSE).

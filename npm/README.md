# claude-code-token-monitor

Read Claude Code session logs from `~/.claude/projects/` and report token usage, estimated cost, trends, and budget — no Python required.

The npm package ships a small Node shim plus a postinstall step that downloads the right native binary (built from the Python source via PyInstaller) for your platform from GitHub Releases.

## Usage

Run once without installing:

```bash
npx claude-code-token-monitor summary
npx claude-code-token-monitor daily
npx claude-code-token-monitor live
```

Or install globally:

```bash
npm install -g claude-code-token-monitor
claude-code-token-monitor --help
```

## Commands

| Command | What it does |
|---------|--------------|
| `summary` | Totals by model, with cost estimate |
| `daily` | Per-day usage table |
| `projects` | Usage grouped by Claude Code project |
| `sessions` | Detailed session list |
| `export` | Export raw data to CSV / JSON |
| `live` | Live-updating dashboard |

Run `claude-code-token-monitor <command> --help` for flags (time windows, budgets, alerts, model filters).

## Supported platforms

`darwin-x64`, `darwin-arm64`, `linux-x64`, `linux-arm64`, `win32-x64`.

If your platform isn't listed, fall back to installing the Python source from the [repo](https://github.com/emtyty/claude-token-monitor).

## How it works

Parses the JSONL logs Claude Code already writes locally. No network calls, no daemon, no API keys.

## Repository

Source, issues, and full documentation: <https://github.com/emtyty/claude-token-monitor>

## License

MIT

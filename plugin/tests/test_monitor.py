#!/usr/bin/env python3
"""
test_monitor.py — fixture-based tests for monitor.py core logic.

Covers:
  - iter_records dedup: multiple JSONL lines sharing the same (sessionId, msg.id)
    must yield exactly one Record with merged tool_use blocks.
  - calc_cost: per-model pricing applied correctly across input / output / cache.
  - filter_records: --since/--until/--last bounds applied correctly.
  - parse_window: --last conflicts with --since; YYYY-MM-DD parsed as local date.
  - _rule_large_context: fires on a session with >=180K single-call context.
  - _rule_opus_routine_session: smoke test that the suggestion engine wires up.

Usage:
    python3 plugin/tests/test_monitor.py -v
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

# Make `monitor` importable from repo root regardless of cwd.
HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import monitor  # noqa: E402


def _assistant_event(
    *,
    session_id: str,
    msg_id: str,
    timestamp: str,
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read: int = 0,
    cache_create: int = 0,
    tools: list[dict] | None = None,
    cwd: str = "/home/u/code/proj",
) -> dict:
    """Build one assistant JSONL event."""
    content: list[dict] = []
    if tools:
        content.extend(tools)
    return {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "message": {
            "id": msg_id,
            "model": model,
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
            },
        },
    }


def _write_session(project_dir: Path, session_id: str, events: list[dict]) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    f = project_dir / f"{session_id}.jsonl"
    with f.open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return f


class IterRecordsDedupTest(unittest.TestCase):
    """One assistant turn → one Record, even when the JSONL has multiple
    lines with the same (sessionId, msg.id) carrying the full usage block."""

    def test_dedup_merges_tool_uses_and_keeps_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "c--Users-u-proj"
            sess = "S1"
            mid = "msg_abc"
            ts = "2026-04-15T12:00:00.000Z"
            # Three lines, same (sess, mid). Each carries one tool_use plus
            # the full per-call usage block (Claude Code's actual behavior).
            events = [
                _assistant_event(
                    session_id=sess, msg_id=mid, timestamp=ts,
                    tools=[{"type": "tool_use", "name": "Read",
                            "input": {"file_path": "/a/b.py"}}],
                ),
                _assistant_event(
                    session_id=sess, msg_id=mid, timestamp=ts,
                    tools=[{"type": "tool_use", "name": "Edit",
                            "input": {"file_path": "/a/b.py"}}],
                ),
                _assistant_event(
                    session_id=sess, msg_id=mid, timestamp=ts,
                    tools=[{"type": "tool_use", "name": "Read",
                            "input": {"file_path": "/c/d.py"}}],
                ),
            ]
            _write_session(proj, sess, events)

            records = list(monitor.iter_records(root))
            self.assertEqual(len(records), 1, "duplicate msg.id must collapse")
            r = records[0]
            self.assertEqual(r.session_id, sess)
            self.assertEqual(r.msg_id, mid)
            self.assertEqual(sorted(r.tools), ["Edit", "Read", "Read"])
            self.assertEqual(sorted(r.read_paths), ["/a/b.py", "/c/d.py"])
            # Usage is taken once, not multiplied.
            self.assertEqual(int(r.usage["input_tokens"]), 1000)
            self.assertEqual(int(r.usage["output_tokens"]), 200)

    def test_distinct_msg_ids_are_separate_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "p1"
            sess = "S1"
            ts = "2026-04-15T12:00:00.000Z"
            _write_session(proj, sess, [
                _assistant_event(session_id=sess, msg_id="m1", timestamp=ts),
                _assistant_event(session_id=sess, msg_id="m2", timestamp=ts),
            ])
            records = list(monitor.iter_records(root))
            self.assertEqual(len(records), 2)


class CalcCostTest(unittest.TestCase):
    def test_sonnet_4_pricing(self):
        # 1M input @ $3, 1M output @ $15  →  $18 total
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        cost = monitor.calc_cost(usage, "claude-sonnet-4-6")
        self.assertAlmostEqual(cost, 18.0, places=4)

    def test_opus_4_7_pricing(self):
        # Opus 4.5+ pricing: 1M input @ $5, 1M output @ $25  →  $30
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        cost = monitor.calc_cost(usage, "claude-opus-4-7")
        self.assertAlmostEqual(cost, 30.0, places=4)

    def test_opus_4_1_pricing(self):
        # Legacy Opus 4/4.1 pricing: 1M input @ $15, 1M output @ $75  →  $90
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        cost = monitor.calc_cost(usage, "claude-opus-4-1")
        self.assertAlmostEqual(cost, 90.0, places=4)

    def test_cache_write_split_5m_vs_1h(self):
        # 1M of 5m write at $3.75 + 1M of 1h write at $6 (sonnet) = $9.75
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_input_tokens": 0,
                 "cache_creation": {
                     "ephemeral_5m_input_tokens": 1_000_000,
                     "ephemeral_1h_input_tokens": 1_000_000,
                 }}
        cost = monitor.calc_cost(usage, "claude-sonnet-4-6")
        self.assertAlmostEqual(cost, 9.75, places=4)

    def test_cache_write_legacy_field_treated_as_5m(self):
        # Legacy combined field, no nested split → priced at 5m rate
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 1_000_000}
        cost = monitor.calc_cost(usage, "claude-sonnet-4-6")
        self.assertAlmostEqual(cost, 3.75, places=4)

    def test_cache_pricing(self):
        # 10M cache_read @ $0.30/1M = $3 (sonnet)
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_input_tokens": 10_000_000,
                 "cache_creation_input_tokens": 0}
        cost = monitor.calc_cost(usage, "claude-sonnet-4-6")
        self.assertAlmostEqual(cost, 3.0, places=4)

    def test_unknown_model_falls_back(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        cost = monitor.calc_cost(usage, "claude-unknown-vNext")
        # DEFAULT_PRICE is sonnet-equivalent: $3 input → $3
        self.assertAlmostEqual(cost, 3.0, places=4)


class FilterRecordsTest(unittest.TestCase):
    """--since / --until / --last narrow the record set correctly."""

    @staticmethod
    def _ns(**kw):
        ns = argparse.Namespace(since=None, until=None, last=None)
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def _records_at(self, dates: list[str]) -> list[monitor.Record]:
        return [
            monitor.Record(
                project="p", session_id=f"s{i}", timestamp=ts,
                model="claude-sonnet-4-6",
                usage={"input_tokens": 0, "output_tokens": 0,
                       "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0},
                cost=0.0, cwd="", msg_id=f"m{i}",
            )
            for i, ts in enumerate(dates)
        ]

    def test_since_excludes_earlier(self):
        recs = self._records_at([
            "2026-04-01T10:00:00+00:00",
            "2026-04-15T10:00:00+00:00",
            "2026-04-30T10:00:00+00:00",
        ])
        kept = monitor.filter_records(recs, self._ns(since="2026-04-15"))
        self.assertEqual(len(kept), 2)

    def test_until_excludes_records_after_bound(self):
        # Use noon UTC and dates far apart so timezone offset (parser uses
        # local zone) cannot accidentally reclassify either record.
        recs = self._records_at([
            "2026-04-10T12:00:00+00:00",
            "2026-04-20T12:00:00+00:00",
        ])
        kept = monitor.filter_records(recs, self._ns(until="2026-04-15"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].timestamp, "2026-04-10T12:00:00+00:00")

    def test_last_conflicts_with_since(self):
        recs = self._records_at(["2026-04-15T10:00:00+00:00"])
        with self.assertRaises(SystemExit):
            monitor.filter_records(recs, self._ns(since="2026-04-01", last="7d"))

    def test_last_window_resolves_relative(self):
        # Make a record at "now-3d" and "now-30d". --last 7d should keep only
        # the 3-day-old one.
        now = datetime.now().astimezone()
        recs = self._records_at([
            (now - timedelta(days=3)).isoformat(),
            (now - timedelta(days=30)).isoformat(),
        ])
        kept = monitor.filter_records(recs, self._ns(last="7d"))
        self.assertEqual(len(kept), 1)

    def test_no_flags_returns_input_unchanged(self):
        recs = self._records_at(["2026-04-15T10:00:00+00:00"])
        kept = monitor.filter_records(recs, self._ns())
        self.assertEqual(kept, recs)


class ParseDurationTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(monitor._parse_duration("30m"), timedelta(minutes=30))
        self.assertEqual(monitor._parse_duration("24h"), timedelta(hours=24))
        self.assertEqual(monitor._parse_duration("7d"),  timedelta(days=7))
        self.assertEqual(monitor._parse_duration("2w"),  timedelta(weeks=2))

    def test_invalid_unit_raises(self):
        with self.assertRaises(ValueError):
            monitor._parse_duration("7y")

    def test_invalid_number_raises(self):
        with self.assertRaises(ValueError):
            monitor._parse_duration("abc")


class LargeContextRuleTest(unittest.TestCase):
    """large-context: thresholds are proportional to the model's context cap.
    200K-cap models warn at 150K / alert at 180K; 1M-cap models (Opus 4.6/4.7)
    warn at 750K / alert at 900K."""

    def _make(self, ctx_tokens: int,
              model: str = "claude-sonnet-4-6") -> list[monitor.Record]:
        # Put the whole context in cache_read so it counts toward the
        # input-side total used by the rule.
        usage = {"input_tokens": 0, "output_tokens": 200,
                 "cache_read_input_tokens": ctx_tokens,
                 "cache_creation_input_tokens": 0}
        rec = monitor.Record(
            project="p", session_id="bigctx", timestamp="2026-04-15T10:00:00+00:00",
            model=model,
            usage=usage,
            cost=monitor.calc_cost(usage, model),
            cwd="", msg_id="m1",
        )
        return [rec]

    def test_high_severity_at_180k(self):
        suggestions = monitor.analyze_suggestions(self._make(190_000))
        big = [s for s in suggestions if s.rule == "large-context"]
        self.assertEqual(len(big), 1)
        self.assertEqual(big[0].severity, "high")

    def test_med_severity_at_150k(self):
        suggestions = monitor.analyze_suggestions(self._make(160_000))
        big = [s for s in suggestions if s.rule == "large-context"]
        self.assertEqual(len(big), 1)
        self.assertEqual(big[0].severity, "med")

    def test_no_alert_below_warn_threshold(self):
        suggestions = monitor.analyze_suggestions(self._make(100_000))
        big = [s for s in suggestions if s.rule == "large-context"]
        self.assertEqual(big, [])

    def test_opus_4_7_no_alert_at_500k(self):
        # 500K on a 1M-cap model = 50% — well below the 75% warn floor.
        # The old 200K-assumption rule would have falsely flagged this as 'high'.
        suggestions = monitor.analyze_suggestions(
            self._make(500_000, model="claude-opus-4-7"))
        big = [s for s in suggestions if s.rule == "large-context"]
        self.assertEqual(big, [],
                         "1M-cap model at 50% must not fire (the bug we fixed)")

    def test_opus_4_7_med_at_750k(self):
        # 75% of 1M = warn threshold, no alert → med severity.
        suggestions = monitor.analyze_suggestions(
            self._make(760_000, model="claude-opus-4-7"))
        big = [s for s in suggestions if s.rule == "large-context"]
        self.assertEqual(len(big), 1)
        self.assertEqual(big[0].severity, "med")
        self.assertIn("1.00M cap", big[0].evidence)

    def test_opus_4_7_high_at_900k(self):
        # 90%+ of 1M = alert tier → high severity.
        suggestions = monitor.analyze_suggestions(
            self._make(950_000, model="claude-opus-4-7"))
        big = [s for s in suggestions if s.rule == "large-context"]
        self.assertEqual(len(big), 1)
        self.assertEqual(big[0].severity, "high")


class ExpensiveSingleCallRuleTest(unittest.TestCase):
    """expensive-single-call: any single API call > $5 → med, ≥ $10 → high.
    Aggregates per session so N expensive calls in one session = 1 finding."""

    @staticmethod
    def _rec(session_id: str, msg_id: str, cost: float,
             ts: str = "2026-04-15T10:00:00+00:00") -> monitor.Record:
        # Build a Record with the exact target cost — bypass calc_cost so the
        # test isn't pinned to current pricing.
        return monitor.Record(
            project="p", session_id=session_id, timestamp=ts,
            model="claude-opus-4-7",
            usage={"input_tokens": 100_000, "output_tokens": 1_000,
                   "cache_read_input_tokens": 50_000,
                   "cache_creation_input_tokens": 0},
            cost=cost, cwd="", msg_id=msg_id,
        )

    def _findings(self, recs):
        return [s for s in monitor.analyze_suggestions(recs)
                if s.rule == "expensive-single-call"]

    def test_med_when_call_over_5_dollars(self):
        recs = [self._rec("s1", "m1", 6.50)]
        f = self._findings(recs)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].severity, "med")

    def test_high_when_any_call_at_or_over_10_dollars(self):
        recs = [self._rec("s1", "m1", 6.0), self._rec("s1", "m2", 12.5)]
        f = self._findings(recs)
        self.assertEqual(len(f), 1, "two expensive calls in one session = one finding")
        self.assertEqual(f[0].severity, "high")

    def test_no_alert_under_threshold(self):
        recs = [self._rec("s1", "m1", 4.99), self._rec("s1", "m2", 0.20)]
        self.assertEqual(self._findings(recs), [])

    def test_separate_sessions_get_separate_findings(self):
        recs = [self._rec("sA", "m1", 7.0), self._rec("sB", "m2", 8.0)]
        f = self._findings(recs)
        self.assertEqual(len(f), 2)
        self.assertEqual({s.scope.split()[1] for s in f}, {"sA", "sB"})

    def test_evidence_reports_peak_cost(self):
        recs = [self._rec("s1", "m1", 6.0), self._rec("s1", "m2", 9.99)]
        f = self._findings(recs)
        self.assertIn("$9.99", f[0].evidence)


class CacheColdSessionRuleTest(unittest.TestCase):
    """cache-cold-session: hit < 30% AND ≥ 5 calls AND cost > $2."""

    @staticmethod
    def _rec(session_id: str, msg_id: str,
             input_tok: int, cache_read: int, cache_write: int = 0,
             output_tok: int = 200,
             ts: str = "2026-04-15T10:00:00+00:00") -> monitor.Record:
        usage = {"input_tokens": input_tok, "output_tokens": output_tok,
                 "cache_read_input_tokens": cache_read,
                 "cache_creation_input_tokens": cache_write}
        return monitor.Record(
            project="p", session_id=session_id, timestamp=ts,
            model="claude-sonnet-4-6",
            usage=usage,
            cost=monitor.calc_cost(usage, "claude-sonnet-4-6"),
            cwd="", msg_id=msg_id,
        )

    def _findings(self, recs):
        return [s for s in monitor.analyze_suggestions(recs)
                if s.rule == "cache-cold-session"]

    def test_fires_on_cold_session(self):
        # 6 calls × 200K raw input each, 1K cache_read → hit rate ≈ 0.5%
        # Cost: 200_000 × 6 × $3/1M = $3.60 → above $2 floor.
        recs = [
            self._rec("cold", f"m{i}", input_tok=200_000, cache_read=1_000)
            for i in range(6)
        ]
        f = self._findings(recs)
        self.assertEqual(len(f), 1)

    def test_no_finding_when_cache_warm(self):
        # 6 calls, 10K input + 100K cache_read → hit rate ≈ 91%
        recs = [
            self._rec("warm", f"m{i}", input_tok=10_000, cache_read=100_000)
            for i in range(6)
        ]
        self.assertEqual(self._findings(recs), [])

    def test_no_finding_under_5_calls(self):
        recs = [
            self._rec("short", f"m{i}", input_tok=200_000, cache_read=1_000)
            for i in range(4)
        ]
        self.assertEqual(self._findings(recs), [])

    def test_no_finding_below_cost_floor(self):
        # 5 small calls → total cost well under $2
        recs = [
            self._rec("cheap", f"m{i}", input_tok=1_000, cache_read=100)
            for i in range(5)
        ]
        self.assertEqual(self._findings(recs), [])


class PlanModeOpusRuleTest(unittest.TestCase):
    """plan-mode-opus: fires on Opus sessions whose plan window (records up to
    the last ExitPlanMode call) was scan-dominated AND a meaningful slice of
    the session's cost. Synthesis-dominated plan windows must be skipped —
    Opus is the right tool for plan synthesis."""

    @staticmethod
    def _rec(session_id: str, msg_id: str, ts: str,
             tools: list[str], read_paths: list[str], cost: float,
             input_tok: int = 50_000, cache_read: int = 0,
             model: str = "claude-opus-4-7") -> monitor.Record:
        usage = {
            "input_tokens": input_tok, "output_tokens": 200,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": 0,
        }
        return monitor.Record(
            project="p", session_id=session_id, timestamp=ts,
            model=model, usage=usage, cost=cost,
            cwd="", msg_id=msg_id,
            tools=list(tools), read_paths=list(read_paths),
        )

    def _findings(self, recs):
        return [s for s in monitor.analyze_suggestions(recs)
                if s.rule == "plan-mode-opus"]

    @staticmethod
    def _ts(i: int) -> str:
        # Strictly increasing tz-aware ISO timestamps.
        return f"2026-04-15T10:{i:02d}:00+00:00"

    def test_fires_on_scan_dominated_plan(self):
        # 6 records, all Read on .py files, last carries ExitPlanMode.
        # plan_cost = $6, total = $6 → share 100%, well above 40% gate.
        recs = [
            self._rec("plan1", f"m{i}", self._ts(i),
                      tools=["Read"], read_paths=[f"/proj/f{i}.py"], cost=1.0)
            for i in range(5)
        ]
        recs.append(self._rec("plan1", "m5", self._ts(5),
                              tools=["Read", "ExitPlanMode"],
                              read_paths=["/proj/f5.py"], cost=1.0))
        f = self._findings(recs)
        self.assertEqual(len(f), 1)
        self.assertIn("ast-graph", f[0].action,
                      "Python project → action should lead with ast-graph")

    def test_skips_synthesis_dominated_plan(self):
        # Plan window has tools but none are explore (TodoWrite is neither
        # _EXPLORE nor _MUTATE) → explore_ratio = 0 → skip.
        recs = [
            self._rec("plan2", f"m{i}", self._ts(i),
                      tools=["TodoWrite"], read_paths=[], cost=1.0)
            for i in range(5)
        ]
        recs.append(self._rec("plan2", "m5", self._ts(5),
                              tools=["TodoWrite", "ExitPlanMode"],
                              read_paths=[], cost=1.0))
        self.assertEqual(self._findings(recs), [],
                         "synthesis-dominated plan must NOT fire")

    def test_skips_when_plan_window_too_short(self):
        # Only 3 records before ExitPlanMode → below the 5-record floor.
        recs = [
            self._rec("plan3", f"m{i}", self._ts(i),
                      tools=["Read"], read_paths=[f"/p/f{i}.py"], cost=1.0)
            for i in range(2)
        ]
        recs.append(self._rec("plan3", "m2", self._ts(2),
                              tools=["Read", "ExitPlanMode"],
                              read_paths=["/p/f2.py"], cost=1.0))
        self.assertEqual(self._findings(recs), [])

    def test_skips_when_plan_cost_share_too_low(self):
        # Plan window: 5 cheap explore calls ($0.20 each = $1) + ExitPlanMode.
        # Implementation: 10 expensive Edit calls ($5 each = $50).
        # Plan share = 1/51 ≈ 2% — well below the 40% gate.
        plan = [
            self._rec("plan4", f"p{i}", self._ts(i),
                      tools=["Read"], read_paths=[f"/p/f{i}.py"], cost=0.20)
            for i in range(5)
        ]
        plan.append(self._rec("plan4", "p5", self._ts(5),
                              tools=["Read", "ExitPlanMode"],
                              read_paths=["/p/f5.py"], cost=0.20))
        impl = [
            self._rec("plan4", f"i{i}", self._ts(10 + i),
                      tools=["Edit"], read_paths=[], cost=5.0)
            for i in range(10)
        ]
        self.assertEqual(self._findings(plan + impl), [])

    def test_uses_last_exit_plan_mode_as_boundary(self):
        # Two ExitPlanMode calls; plan window must extend to the LAST one.
        # Records 0-4: scan-heavy explore. Record 2 has an ExitPlanMode.
        # Records 5-7: scan-heavy explore. Record 7 has the final ExitPlanMode.
        # All 8 should be in the plan window → 8 calls × $1 = $8 plan_cost,
        # 100% share → fires.
        recs = []
        for i in range(8):
            tools = ["Read"]
            if i in (2, 7):
                tools.append("ExitPlanMode")
            recs.append(self._rec("plan5", f"m{i}", self._ts(i),
                                   tools=tools,
                                   read_paths=[f"/p/f{i}.py"], cost=1.0))
        f = self._findings(recs)
        self.assertEqual(len(f), 1)
        self.assertIn("8 call(s)", f[0].evidence,
                      "evidence should report plan window size = 8 (last ExitPlanMode)")
        self.assertIn("2 plan turn(s)", f[0].evidence)

    def test_high_severity_with_read_spray(self):
        # 12 records with distinct file paths → distinct_reads = 12 ≥ 10
        # plan_cost = 12 × $2 = $24 ≥ $20 → severity bumps to 'high'.
        recs = [
            self._rec("plan6", f"m{i}", self._ts(i),
                      tools=["Read"], read_paths=[f"/p/distinct_{i}.py"], cost=2.0,
                      input_tok=200_000)
            for i in range(11)
        ]
        recs.append(self._rec("plan6", "m11", self._ts(11),
                              tools=["Read", "ExitPlanMode"],
                              read_paths=["/p/distinct_11.py"], cost=2.0,
                              input_tok=200_000))
        f = self._findings(recs)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].severity, "high")

    def test_skips_when_plan_window_not_opus_dominated(self):
        # Plan window mostly Sonnet → rule 9 (explore-on-opus) territory, not us.
        recs = [
            self._rec("plan7", f"m{i}", self._ts(i),
                      tools=["Read"], read_paths=[f"/p/f{i}.py"], cost=1.0,
                      model="claude-sonnet-4-6")
            for i in range(5)
        ]
        recs.append(self._rec("plan7", "m5", self._ts(5),
                              tools=["Read", "ExitPlanMode"],
                              read_paths=["/p/f5.py"], cost=1.0,
                              model="claude-sonnet-4-6"))
        self.assertEqual(self._findings(recs), [])


class FullPipelineSmokeTest(unittest.TestCase):
    """End-to-end: write fixtures, redirect projects_root, run iter_records +
    analyze_suggestions, assert at least one rule fires."""

    def test_load_and_analyze(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Synthesize an Opus-routine session: 25 calls, all Opus, small
            # outputs (<500 avg). Token sizes chosen so total cost > $5
            # (the rule's lower bound) at Opus 4.5+ rates ($5 in / $25 out).
            proj = root / "c--Users-u-proj"
            events = [
                _assistant_event(
                    session_id="sess1", msg_id=f"m{i}",
                    timestamp=f"2026-04-15T{i % 24:02d}:00:00.000Z",
                    model="claude-opus-4-6",
                    input_tokens=60_000, output_tokens=300,
                    cache_read=10_000, cache_create=0,
                )
                for i in range(25)
            ]
            _write_session(proj, "sess1", events)

            records = list(monitor.iter_records(root))
            self.assertEqual(len(records), 25)
            suggestions = monitor.analyze_suggestions(records)
            rules = {s.rule for s in suggestions}
            self.assertIn("opus-routine-session", rules,
                          f"expected opus-routine-session suggestion, got {rules}")


def _mk_rec(sess: str = "S1", msg_id: str = "m1", cost: float = 0.5,
            model: str = "claude-sonnet-4-6", **kw) -> "monitor.Record":
    """Bare Record for rule tests that don't need the JSONL pipeline."""
    usage = kw.pop("usage", {"input_tokens": 1_000, "output_tokens": 200,
                             "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0})
    return monitor.Record(
        project="c--Users-u-proj", session_id=sess,
        timestamp="2026-04-15T12:00:00.000Z", model=model,
        usage=usage, cost=cost, cwd="/home/u/code/proj", msg_id=msg_id, **kw)


class ServerToolCostTest(unittest.TestCase):
    def test_web_search_requests_billed_per_1k(self):
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "server_tool_use": {"web_search_requests": 100}}
        # 100 searches @ $10/1K = $1
        self.assertAlmostEqual(monitor.calc_cost(usage, "claude-sonnet-4-6"),
                               1.0, places=6)

    def test_absent_server_tool_use_is_free(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        self.assertAlmostEqual(monitor.calc_cost(usage, "claude-sonnet-4-6"),
                               3.0, places=6)


class NewFieldExtractionTest(unittest.TestCase):
    """iter_records must surface stop_reason, effort, promptId,
    attributionSkill, cache-miss diagnostics and web search counts."""

    def test_assistant_extras_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            e = _assistant_event(session_id="S1", msg_id="m1",
                                 timestamp="2026-04-15T12:00:00.000Z")
            e["effort"] = "high"
            e["promptId"] = "p-123"
            e["attributionSkill"] = "security-review"
            e["isApiErrorMessage"] = True
            e["message"]["stop_reason"] = "max_tokens"
            e["message"]["diagnostics"] = {
                "cache_miss_reason": {"type": "tools_changed",
                                      "cache_missed_input_tokens": 30_000},
            }
            e["message"]["usage"]["server_tool_use"] = {"web_search_requests": 4}
            _write_session(root / "p1", "S1", [e])

            r = list(monitor.iter_records(root))[0]
            self.assertEqual(r.stop_reason, "max_tokens")
            self.assertEqual(r.effort, "high")
            self.assertEqual(r.prompt_id, "p-123")
            self.assertEqual(r.skill, "security-review")
            self.assertTrue(r.api_error)
            self.assertEqual(r.cache_miss_reason, "tools_changed")
            self.assertEqual(r.cache_missed_tokens, 30_000)
            self.assertEqual(r.web_searches, 4)

    def test_stop_reason_backfilled_from_final_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ts = "2026-04-15T12:00:00.000Z"
            first = _assistant_event(session_id="S1", msg_id="m1", timestamp=ts)
            first["message"]["stop_reason"] = None
            last = _assistant_event(session_id="S1", msg_id="m1", timestamp=ts)
            last["message"]["stop_reason"] = "end_turn"
            _write_session(root / "p1", "S1", [first, last])

            r = list(monitor.iter_records(root))[0]
            self.assertEqual(r.stop_reason, "end_turn")


class SessionEventsTest(unittest.TestCase):
    """The optional events dict must accumulate counters from `user` and
    `attachment` entries without disturbing Record extraction."""

    @staticmethod
    def _user_event(session_id: str, tool_result=None, **top) -> dict:
        e = {"type": "user", "sessionId": session_id,
             "timestamp": "2026-04-15T12:00:00.000Z",
             "message": {"role": "user", "content": "x"}}
        if tool_result is not None:
            e["toolUseResult"] = tool_result
        e.update(top)
        return e

    def test_events_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sess = "S1"
            entries = [
                _assistant_event(session_id=sess, msg_id="m1",
                                 timestamp="2026-04-15T12:00:00.000Z"),
                # big stdout blob
                self._user_event(sess, {"stdout": "x" * 70_000, "stderr": ""}),
                # interrupted run + hand-modified edit
                self._user_event(sess, {"stdout": "", "interrupted": True}),
                self._user_event(sess, {"filePath": "/a.py", "userModified": True}),
                # whole-file read of a large file
                self._user_event(sess, {"file": {"filePath": "/big.py",
                                                 "numLines": 900,
                                                 "totalLines": 900}}),
                # permission denial
                self._user_event(sess, toolDenialKind="permission-rule"),
                # failing hook attachment
                {"type": "attachment", "sessionId": sess,
                 "attachment": {"type": "hook_non_blocking_error"}},
            ]
            _write_session(root / "c--Users-u-proj", sess, entries)

            events: dict = {}
            records = list(monitor.iter_records(root, events=events))
            self.assertEqual(len(records), 1)
            ev = events[sess]
            self.assertEqual(ev["project"], "c--Users-u-proj")
            self.assertGreaterEqual(ev["tool_result_chars"], 70_000)
            self.assertEqual(ev["big_results"], 1)
            self.assertEqual(ev["interrupts"], 1)
            self.assertEqual(ev["user_modified_edits"], 1)
            self.assertEqual(ev["full_reads"], 1)
            self.assertEqual(ev["full_read_lines"], 900)
            self.assertEqual(ev["denials_rule"], 1)
            self.assertEqual(ev["hook_errors"], 1)


class NewRulesTest(unittest.TestCase):
    @staticmethod
    def _by_rule(suggestions, rule):
        return [s for s in suggestions if s.rule == rule]

    def test_truncated_output_fires(self):
        recs = [_mk_rec(msg_id=f"m{i}", stop_reason="max_tokens")
                for i in range(2)]
        hits = self._by_rule(monitor.analyze_suggestions(recs), "truncated-output")
        self.assertEqual(len(hits), 1)
        self.assertGreater(hits[0].est_savings, 0)

    def test_truncated_output_single_call_silent(self):
        recs = [_mk_rec(stop_reason="max_tokens")]
        self.assertEqual(
            self._by_rule(monitor.analyze_suggestions(recs), "truncated-output"), [])

    def test_runaway_prompt_fires_once_per_session(self):
        recs = [_mk_rec(msg_id=f"m{i}", cost=0.30, prompt_id="p1")
                for i in range(22)]
        recs += [_mk_rec(msg_id=f"n{i}", cost=0.30, prompt_id="p2")
                 for i in range(21)]
        hits = self._by_rule(monitor.analyze_suggestions(recs), "runaway-prompt")
        self.assertEqual(len(hits), 1, "worst prompt only, one finding per session")

    def test_cache_miss_cause_fires_with_savings(self):
        recs = [_mk_rec(msg_id=f"m{i}", cache_miss_reason="tools_changed",
                        cache_missed_tokens=50_000)
                for i in range(3)]
        hits = self._by_rule(monitor.analyze_suggestions(recs), "cache-miss-cause")
        self.assertEqual(len(hits), 1)
        self.assertIn("tools_changed", hits[0].evidence)
        self.assertGreater(hits[0].est_savings, 0)

    def test_unfixable_miss_reason_silent(self):
        recs = [_mk_rec(msg_id=f"m{i}", cache_miss_reason="unavailable")
                for i in range(5)]
        self.assertEqual(
            self._by_rule(monitor.analyze_suggestions(recs), "cache-miss-cause"), [])

    def test_web_search_spend_fires(self):
        recs = [_mk_rec(msg_id=f"m{i}", web_searches=100) for i in range(3)]
        hits = self._by_rule(monitor.analyze_suggestions(recs), "web-search-spend")
        self.assertEqual(len(hits), 1)

    def test_api_error_retries_fires(self):
        recs = [_mk_rec(msg_id=f"m{i}", api_error=True) for i in range(3)]
        hits = self._by_rule(monitor.analyze_suggestions(recs), "api-error-retries")
        self.assertEqual(len(hits), 1)


class EventRulesTest(unittest.TestCase):
    @staticmethod
    def _ev(**kw) -> dict:
        ev = monitor.empty_session_events()
        ev["project"] = "c--Users-u-proj"
        ev.update(kw)
        return ev

    @staticmethod
    def _by_rule(suggestions, rule):
        return [s for s in suggestions if s.rule == rule]

    def test_tool_result_bloat_fires(self):
        recs = [_mk_rec()]
        events = {"S1": self._ev(tool_result_chars=800_000)}
        hits = self._by_rule(monitor.analyze_suggestions(recs, events),
                             "tool-result-bloat")
        self.assertEqual(len(hits), 1)
        self.assertGreater(hits[0].est_savings, 0)

    def test_event_rules_respect_record_window(self):
        # Session with events but no surviving records must not fire.
        recs = [_mk_rec(sess="OTHER")]
        events = {"S1": self._ev(tool_result_chars=800_000, hook_errors=10)}
        sugg = monitor.analyze_suggestions(recs, events)
        self.assertEqual(self._by_rule(sugg, "tool-result-bloat"), [])
        self.assertEqual(self._by_rule(sugg, "hook-error-spam"), [])

    def test_hook_error_spam_fires(self):
        recs = [_mk_rec()]
        events = {"S1": self._ev(hook_errors=5)}
        hits = self._by_rule(monitor.analyze_suggestions(recs, events),
                             "hook-error-spam")
        self.assertEqual(len(hits), 1)

    def test_permission_friction_fires(self):
        recs = [_mk_rec()]
        events = {"S1": self._ev(denials_rule=3, denials_user=1)}
        hits = self._by_rule(monitor.analyze_suggestions(recs, events),
                             "permission-friction")
        self.assertEqual(len(hits), 1)
        self.assertIn("rejected by hand", hits[0].evidence)

    def test_edit_churn_fires(self):
        recs = [_mk_rec()]
        events = {"S1": self._ev(user_modified_edits=3)}
        hits = self._by_rule(monitor.analyze_suggestions(recs, events),
                             "edit-churn")
        self.assertEqual(len(hits), 1)

    def test_full_file_reads_and_interrupts_fire(self):
        recs = [_mk_rec()]
        events = {"S1": self._ev(full_reads=5, full_read_lines=5_000,
                                 interrupts=3)}
        sugg = monitor.analyze_suggestions(recs, events)
        self.assertEqual(len(self._by_rule(sugg, "full-file-reads")), 1)
        self.assertEqual(len(self._by_rule(sugg, "interrupted-turns")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

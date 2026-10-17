"""An index over runs/ and runs/sweeps/. The filesystem is the database: nothing here is stored
except scorecard.json, which is written only when a caller asks for a finished run to be scored
(exactly as `rightsize report` would). Listing never scores: bootstrapping is slow, and a run that
just finished is usually being scored by the process that ran it."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from rightsize_core.calibration.sweep import resolve_run_dir
from rightsize_core.runner.run import HEARTBEAT_FILE, load_results
from rightsize_core.service import health
from rightsize_core.service.models import RungState, RunSummary, SweepSummary

SCORING_GRACE_S = 600.0  # a run that ended this recently and has no scorecard is probably being scored

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:+-]*$")
RESERVED = {"sweeps", "jobs"}


class NotFound(LookupError):
    pass


def check_id(value: str) -> str:
    """Run, sweep, and job ids name a directory directly under runs/; refuse anything else."""
    if not value or not _SAFE_ID.match(value) or ".." in value:
        raise NotFound(value)
    return value


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


class Index:
    def __init__(self, root: Path, runs_dir: Path | None = None) -> None:
        self.root = root
        self.runs_dir = runs_dir or root / "runs"
        self._cards: dict[Path, tuple[tuple, dict | None, str | None]] = {}
        self._summaries: dict[Path, tuple[tuple, RunSummary]] = {}
        self._manifests: dict[Path, tuple[float | None, dict]] = {}
        self._units: dict[Path, tuple[tuple, Any]] = {}
        self._tasks: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------------ runs
    def run_dir(self, run_id: str) -> Path:
        d = self.runs_dir / check_id(run_id)
        if run_id in RESERVED or not (d / "manifest.json").exists():
            raise NotFound(run_id)
        return d

    def run_ids(self) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        ids = [p.name for p in self.runs_dir.iterdir()
               if p.name not in RESERVED and (p / "manifest.json").exists()]
        return sorted(ids, reverse=True)  # ids start with a UTC timestamp

    def manifest(self, run_id: str) -> dict:
        f = self.run_dir(run_id) / "manifest.json"
        key = _mtime(f)
        cached = self._manifests.get(f)
        if cached is None or cached[0] != key:
            cached = (key, json.loads(f.read_text()))
            self._manifests[f] = cached
        return cached[1]

    def rows(self, run_id: str) -> list[dict]:
        d = self.run_dir(run_id)
        return load_results(d) if (d / "results.jsonl").exists() else []

    def tasks(self, suite: str, split: str) -> dict:
        """Task id -> Task for one suite and split, loaded once."""
        if (suite, split) not in self._tasks:
            from rightsize_core.integrity.dataset import load_tasks

            self._tasks[(suite, split)] = {t.id: t for t in load_tasks(self.root, suite, split)}
        return self._tasks[(suite, split)]

    def units(self, run_id: str):
        """(manifest, unit records by task type, result rows), as `runner.score.unit_records` builds them."""
        from rightsize_core.runner.score import unit_records

        d = self.run_dir(run_id)
        key = (_mtime(d / "manifest.json"), _mtime(d / "results.jsonl"))
        cached = self._units.get(d)
        if cached is None or cached[0] != key:
            cached = (key, unit_records(self.root, d))
            self._units[d] = cached
        return cached[1]

    def scorecard(self, run_id: str, compute: bool = False) -> tuple[dict | None, str | None]:
        """(scorecard, why there is none). With `compute`, scores a finished run that has none yet."""
        d = self.run_dir(run_id)
        key = (_mtime(d / "manifest.json"), _mtime(d / "results.jsonl"), _mtime(d / "scorecard.json"))
        cached = self._cards.get(d)
        if cached and cached[0] == key and (cached[1] is not None or not compute):
            return cached[1], cached[2]
        card, why = None, None
        manifest = json.loads((d / "manifest.json").read_text())
        card_mtime, results_mtime = key[2], key[1]
        if manifest["status"] == "running":
            why = "still running"
        elif card_mtime is not None and (results_mtime is None or card_mtime >= results_mtime):
            card = json.loads((d / "scorecard.json").read_text())
        elif results_mtime is None:
            why = "no results were written"
        elif not compute:
            ended = _ts(manifest.get("ended_at"))
            why = ("scoring" if ended and time.time() - ended < SCORING_GRACE_S
                   else "not scored yet: open the run to score it, or run `rightsize report`")
        else:
            from rightsize_core.runner.score import score_run

            try:
                card = score_run(self.root, d)
            except Exception as e:  # noqa: BLE001
                why = f"can't be scored here: {e}"[:300]
            key = (key[0], key[1], _mtime(d / "scorecard.json"))
        self._cards[d] = (key, card, why)
        return card, why

    def summary(self, run_id: str) -> RunSummary:
        d = self.run_dir(run_id)
        key = tuple(_mtime(d / f) for f in ("manifest.json", "results.jsonl", "scorecard.json", HEARTBEAT_FILE))
        cached = self._summaries.get(d)
        # A live run's health depends on the clock as well as the files.
        if cached and cached[0] == key and cached[1].health not in ("running",):
            return cached[1]
        summary = self._summary(run_id, d)
        self._summaries[d] = (key, summary)
        return summary

    def _summary(self, run_id: str, d: Path) -> RunSummary:
        m = self.manifest(run_id)
        rows = self.rows(run_id)
        card, why = self.scorecard(run_id) if m["status"] != "running" else (None, "still running")
        if why == "scoring":
            why = "Being scored by the process that ran it."
        beat = _mtime(d / HEARTBEAT_FILE)
        age = None if beat is None else time.time() - beat
        prog = health.progress(rows, m["dataset"].get("task_count", 0) * m.get("repeats", 1))
        groups = health.error_groups(rows)
        notes = m.get("notes", [])
        state, reason = health.classify(m["status"], card, age, prog, groups, notes, why)
        heads = health.headlines(card) if card else []
        sys_ = m.get("system", {})
        return RunSummary(
            run_id=m["run_id"], path=_rel(self.root, d), started_at=m["started_at"], ended_at=m.get("ended_at"),
            suite=m["dataset"]["suite"], split=m["dataset"]["split"],
            system=(sys_.get("info") or {}).get("name") or sys_.get("name"), transport=sys_.get("transport"),
            trusted=bool(sys_.get("trusted")), model=m.get("model"), params=m.get("params", {}),
            repeats=m.get("repeats", 1), task_types=m.get("task_types", []), sweep_id=m.get("sweep_id"),
            status=m["status"], health=state, health_reason=reason, progress=prog, headlines=heads,
            error_groups=groups, caveats=health.caveats(heads, prog, m["dataset"]["split"], state), notes=notes,
        )

    def runs(self, **filters: Any) -> list[RunSummary]:
        out = []
        for rid in self.run_ids():
            try:
                s = self.summary(rid)
            except (OSError, ValueError, KeyError):
                continue
            if all(v in (None, "") or getattr(s, k) == v for k, v in filters.items()):
                out.append(s)
        return out

    # ---------------------------------------------------------------- sweeps
    @property
    def sweeps_dir(self) -> Path:
        return self.runs_dir / "sweeps"

    def sweep_dir(self, sweep_id: str) -> Path:
        d = self.sweeps_dir / check_id(sweep_id)
        if not d.is_dir():
            raise NotFound(sweep_id)
        return d

    def sweep_ids(self) -> list[str]:
        if not self.sweeps_dir.is_dir():
            return []
        return sorted((p.name for p in self.sweeps_dir.iterdir() if p.is_dir()), reverse=True)

    def sweep_record(self, sweep_id: str) -> dict | None:
        f = self.sweep_dir(sweep_id) / "sweep.json"
        return json.loads(f.read_text()) if f.exists() else None

    def sweep_run_dir(self, run_dir: str) -> Path:
        path = resolve_run_dir(self.root, run_dir)
        if not path.exists() and (self.runs_dir / Path(run_dir).name).exists():
            path = self.runs_dir / Path(run_dir).name
        return path

    def sweep(self, sweep_id: str) -> SweepSummary:
        d = self.sweep_dir(sweep_id)
        started = _stamp(sweep_id)
        rec = self.sweep_record(sweep_id)
        if rec is None:
            return SweepSummary(sweep_id=sweep_id, path=_rel(self.root, d), started_at=started, state="empty",
                                state_reason="Died before its first rung finished. Sweeps recorded nothing "
                                             "until a rung was done, so there's no record of why.")
        by_sweep = {rid: self.summary(rid) for rid in self.run_ids()
                    if self.manifest(rid).get("sweep_id") == rec.get("sweep_id", sweep_id)}
        finished = {r["rung"]: r for r in rec.get("runs", [])}
        rungs = []
        for r in rec.get("rungs") or [{"rung": x["rung"], "model": x["model"], "status": "done"}
                                      for x in rec.get("runs", [])]:
            st = RungState(rung=r["rung"], model=r.get("model"), status=r["status"], error=r.get("error"))
            run_dir = (finished.get(r["rung"]) or {}).get("run_dir") or r.get("run_dir")
            if run_dir:
                st.run_id = Path(run_dir).name
            elif r["status"] == "running":
                live = [s for s in by_sweep.values() if s.health in ("running", "interrupted")]
                st.run_id = live[0].run_id if live else None
            if st.run_id in by_sweep:
                st.health = by_sweep[st.run_id].health
            rungs.append(st)
        spend, unknown = 0.0, 0
        for st in rungs:
            s = by_sweep.get(st.run_id)
            if s is None:
                continue
            if s.progress.unknown_cost_calls:
                unknown += 1
            spend += s.progress.spend_usd
        state, reason = _sweep_state(rec, rungs, pid_alive(rec.get("pid")))
        cal = d / "calibration.json"
        low_n = 0
        if cal.exists():
            low_n = sum(1 for x in json.loads(cal.read_text())["decisions"] if x.get("insufficient_n"))
        return SweepSummary(
            sweep_id=sweep_id, path=_rel(self.root, d), started_at=started, system=rec.get("system"),
            suite=rec.get("suite"), split=rec.get("split"), ladder=rec.get("ladder"), repeats=rec.get("repeats"),
            task_count=len(rec.get("task_ids", [])), max_cost=rec.get("max_cost"), state=state, state_reason=reason,
            rungs=rungs, spend_usd=spend, unknown_cost_rungs=unknown, calibrated=cal.exists(),
            routing=(d / "routing.yaml").exists(), low_n_decisions=low_n,
        )

    def sweeps(self) -> list[SweepSummary]:
        out = []
        for sid in self.sweep_ids():
            try:
                out.append(self.sweep(sid))
            except (OSError, ValueError, KeyError):
                continue
        return out


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _stamp(ident: str) -> str | None:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", ident)
    return f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}Z" if m else None


def _sweep_state(rec: dict, rungs: list[RungState], alive: bool = False) -> tuple[str, str]:
    done = sum(r.status == "done" for r in rungs)
    total = len(rungs)
    if "rungs" not in rec:  # written by an older version, which only listed finished rungs
        return "complete", (f"{done} rung{'' if done == 1 else 's'} recorded (an older sweep record lists "
                            "finished rungs only).")
    if failed := next((r for r in rungs if r.status == "failed"), None):
        return "failed", f"Rung {failed.rung} failed: {failed.error or 'no message'}"
    if cancelled := next((r for r in rungs if r.status == "cancelled"), None):
        return "cancelled", f"Cancelled during rung {cancelled.rung}; {done} of {total} rungs finished."
    if rec.get("finished_at"):
        if rec.get("stopped"):
            return "stopped", f"Stopped after {done} of {total} rungs: {rec['stopped']}."
        return "complete", f"All {total} rungs finished."
    running = next((r for r in rungs if r.status in ("running", "scoring")), None)
    if alive or (running and running.health == "running"):
        doing = "is being scored" if running and running.status == "scoring" else "is running"
        return "running", (f"Rung {running.rung} {doing}; {done} of {total} done." if running
                           else f"{done} of {total} rungs done.")
    where = f"during rung {running.rung}" if running else f"after {done} of {total} rungs"
    return "interrupted", f"The sweep process stopped {where} without finishing."

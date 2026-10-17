"""Publishable reports: one self-contained page per run, sweep, or comparison, rendered by the same
templates as the Workbench, and `rightsize export`, which bundles them for a static site.

A bundle is safe to publish only if nothing in it is held out, private, or local, so export refuses
instead of trying to redact:
* held-out runs and sweeps (task ids alone name the held-out targets);
* runs that aren't finished and scored;
* any file matching the public-boundary guard's rules or its private denylist (a manifest embeds
  the system's own description, so a private system's run would name it).
Local paths are rewritten relative to the checkout (or to ~) before any of that is checked.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rightsize_core import __version__
from rightsize_core.calibration.sweep import decide
from rightsize_core.report.render import environment, stylesheet
from rightsize_core.service import queries
from rightsize_core.service.index import Index

MARKER = "export.json"
VOLUMES = (1_000, 10_000, 100_000)


class ExportRefused(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _git_sha(root: Path) -> str | None:
    from rightsize_core.runner.run import _git_sha as sha

    return sha(root)


def _base(ctx: dict[str, Any], root: Path) -> dict[str, Any]:
    return {"css": stylesheet(), "version": __version__, "git_sha": _git_sha(root), "rendered_at": _now(),
            "static": True, "home": None, "heldout": False, **ctx}


# ------------------------------------------------------------------------------- pages


def run_page(ix: Index, run_id: str, href=lambda *a, **k: None, **ctx: Any) -> str:
    d = queries.run_detail(ix, run_id)
    s = d["summary"]
    return environment().get_template("run_report.html").render(**_base({
        "page_title": f"{s.system} · {s.model or 'default model'} · {s.suite}/{s.split} · Rightsize",
        "kind_label": "Run", "s": s, "d": d, "href": href, "heldout": s.split == "heldout",
        "manifest_json": json.dumps(d["manifest"], indent=2), **ctx}, ix.root))


def sensitivity(cal: dict) -> list[dict]:
    """The overall route per task type at thresholds around the one used, by calibrate's own rule."""
    rows = []
    for d in cal["decisions"]:
        if d["stratum"] != "all":
            continue
        tau = cal["thresholds"].get(d["metric"], cal["thresholds"].get(f"{d['task_type']}:{d['metric']}"))
        grid = sorted({round(min(1.0, max(0.0, tau + k)), 2) for k in (-0.1, -0.05, 0.0, 0.05, 0.1)}) \
            if tau is not None else [0.5, 0.6, 0.7, 0.8, 0.9]
        for t in [None, *grid]:
            chosen, _ = decide(d["rungs"], d["best"], t, cal["delta"], cal.get("floors"))
            rows.append({"task_type": d["task_type"], "threshold": t, "chosen": chosen,
                         "cost": d["rungs"][chosen]["cost"] if chosen else None, "current": t == tau})
    return rows


def volume_rows(cal: dict) -> list[dict]:
    rows = []
    for d in cal["decisions"]:
        if d["stratum"] == "all":
            ch = d["rungs"].get(d["chosen"]) if d["chosen"] else None
            rows.append({"task_type": d["task_type"], "chosen": d["chosen"], "best": d["best"],
                         "cost": ch["cost"] if ch else None, "best_cost": d["rungs"][d["best"]]["cost"]})
    return rows


def sweep_page(ix: Index, sweep_id: str, href=lambda *a, **k: None, **ctx: Any) -> str:
    w = ix.sweep(sweep_id)
    runs_by_id = {r.run_id: ix.summary(r.run_id) for r in w.rungs if r.run_id and _exists(ix, r.run_id)}
    cal = queries.calibration(ix, sweep_id)
    routing = ix.sweep_dir(sweep_id) / "routing.yaml"
    tau = {}
    for d in (cal or {}).get("decisions", []):
        th = cal["thresholds"]
        tau[d["task_type"]] = th.get(d["metric"], th.get(f"{d['task_type']}:{d['metric']}"))
    return environment().get_template("sweep_report.html").render(**_base({
        "page_title": f"Sweep {sweep_id} · {w.suite}/{w.split} · Rightsize", "kind_label": "Sweep",
        "w": w, "runs_by_id": runs_by_id, "cal": cal, "tau": tau, "href": href,
        "routing": routing.read_text() if routing.exists() else None, "heldout": w.split == "heldout",
        "sensitivity": sensitivity(cal) if cal else [], "volume": volume_rows(cal) if cal else [],
        "volumes": VOLUMES, **ctx}, ix.root))


def compare_page(ix: Index, run_a: str, run_b: str, href=lambda *a, **k: None, **ctx: Any) -> str:
    c = queries.compare(ix, run_a, run_b)
    return environment().get_template("compare_report.html").render(**_base({
        "page_title": f"Compare {c['a'].system} and {c['b'].system} · Rightsize", "kind_label": "Comparison",
        "c": c, "sections": c["sections"], "href": href, **ctx}, ix.root))


def _exists(ix: Index, run_id: str) -> bool:
    try:
        ix.run_dir(run_id)
        return True
    except LookupError:
        return False


def index_for(run_dir: Path) -> Index:
    """An index for a run or sweep directory under <root>/runs/ (the CLI's own report pages)."""
    runs_dir = run_dir.parent.parent if run_dir.parent.name == "sweeps" else run_dir.parent
    return Index(runs_dir.parent, runs_dir)


# ------------------------------------------------------------------------------ guards


def _guard_module(root: Path):
    """The public-boundary guard, which owns the public rules and knows where the private denylist
    lives. Loaded from the checkout, so the rules are written down in exactly one place."""
    path = root / "tests" / "guards" / "test_public_boundary.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("rightsize_public_boundary", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ImportError:
        return None
    return mod


def load_rules(root: Path, denylist: Path | None) -> tuple[list[tuple[re.Pattern, bool]], str]:
    """([(pattern, public?)], where the denylist came from). The guard's public rules always apply;
    a public rule's match may be quoted back, a denylist match never is."""
    mod = _guard_module(root)
    rules = [(p, True) for p, _ in getattr(mod, "PUBLIC_RULES", [])]
    source = "none"
    path = denylist or (Path(os.environ["RIGHTSIZE_DENYLIST"]) if os.environ.get("RIGHTSIZE_DENYLIST") else None)
    if path is None and mod is not None and Path(mod.DENYLIST).exists():
        path, source = Path(mod.DENYLIST), "the private checkout"
    elif path is not None:
        source = "configured"
    if path is not None:
        lines = [ln.strip() for ln in Path(path).read_text().splitlines()]
        rules += [(re.compile(ln, re.IGNORECASE | re.MULTILINE), False) for ln in lines
                  if ln and not ln.startswith("#")]
    return rules, source


def scrub(text: str, *bases: Path) -> str:
    """Local paths become relative to the checkout or the folder holding runs/, or else to ~."""
    for base in bases:
        for prefix in {str(base.resolve()) + "/", str(base) + "/"}:
            text = text.replace(prefix, "")
    return text.replace(str(Path.home()), "~")


def _position(path: str, runs: list[str], sweeps: list[str], compares) -> str:
    kind, _, rest = path.partition("/")
    ident = rest.split("/", 1)[0]
    if kind == "runs" and ident in runs:
        return f"run {runs.index(ident) + 1} of {len(runs)}"
    if kind == "sweeps" and ident in sweeps:
        return f"sweep {sweeps.index(ident) + 1} of {len(sweeps)}"
    if kind == "compare":
        pairs = [f"{a}__{b}" for a, b in compares]
        return f"comparison {pairs.index(ident) + 1} of {len(pairs)}" if ident in pairs else "a comparison"
    return "the index page"


# ------------------------------------------------------------------------------ bundles


def export_bundle(root: Path, ix: Index, out: Path, *, runs: list[str] = (), sweeps: list[str] = (),
                  compares: list[tuple[str, str]] = (), title: str = "Rightsize results",
                  denylist: Path | None = None) -> dict[str, Any]:
    """Write a static bundle to `out` and return its export.json. Raises ExportRefused, writing
    nothing, if anything in it must not be published."""
    problems: list[str] = []
    sweep_list = list(dict.fromkeys(sweeps))
    run_list = list(dict.fromkeys(runs))
    for sid in sweep_list:
        w = ix.sweep(sid)
        if w.split == "heldout":
            problems.append(f"sweep {sid} ran the held-out split")
        if w.state in ("empty", "running"):
            problems.append(f"sweep {sid} is {w.state}")
        run_list += [r.run_id for r in w.rungs if r.run_id and r.run_id not in run_list]
    for a, b in compares:
        run_list += [r for r in (a, b) if r not in run_list]
    summaries = {}
    for rid in run_list:
        s = ix.summary(rid)
        summaries[rid] = s
        if s.split == "heldout":
            problems.append(f"run {rid} ran the held-out split")
        if s.health in ("running", "interrupted"):
            problems.append(f"run {rid} is {s.health}")
    if problems:
        raise ExportRefused("Refusing to export: " + "; ".join(problems))
    if out.exists() and any(out.iterdir()) and not (out / MARKER).exists():
        raise ExportRefused(f"{out} has files Rightsize didn't write; choose an empty or new folder")

    def href_from(rel: str):
        def href(kind: str, ident: str | None = None, **_q) -> str | None:
            if kind == "run" and ident in summaries:
                return f"{rel}runs/{ident}/index.html"
            if kind == "sweep" and ident in sweep_list:
                return f"{rel}sweeps/{ident}/index.html"
            return None
        return href

    files: dict[str, str] = {}
    record: dict[str, Any] = {"title": title, "generated_at": _now(), "rightsize_version": __version__,
                              "git_sha": _git_sha(root), "runs": [], "sweeps": [], "compares": []}
    for rid in run_list:
        d = ix.run_dir(rid)
        queries.run_detail(ix, rid)  # scores the run if nothing has yet
        files[f"runs/{rid}/manifest.json"] = (d / "manifest.json").read_text()
        files[f"runs/{rid}/scorecard.json"] = (d / "scorecard.json").read_text()
        files[f"runs/{rid}/index.html"] = run_page(ix, rid, href_from("../../"), home="../../index.html",
                                                    manifest_href="manifest.json", scorecard_href="scorecard.json",
                                                    manifest_sha=_sha((d / "manifest.json").read_bytes()))
        s = summaries[rid]
        record["runs"].append({"run_id": rid, "page": f"runs/{rid}/index.html", "suite": s.suite, "split": s.split,
                               "system": s.system, "model": s.model, "health": s.health,
                               "source_manifest_sha256": _sha((d / "manifest.json").read_bytes())})
    sweep_rows = []
    for sid in sweep_list:
        sdir = ix.sweep_dir(sid)
        cal_file = sdir / "calibration.json"
        for name in ("sweep.json", "calibration.json", "routing.yaml"):
            if (sdir / name).exists():
                files[f"sweeps/{sid}/{name}"] = (sdir / name).read_text()
        files[f"sweeps/{sid}/index.html"] = sweep_page(
            ix, sid, href_from("../../"), home="../../index.html",
            calibration_sha=_sha(cal_file.read_bytes()) if cal_file.exists() else None)
        cal = queries.calibration(ix, sid)
        routes = [{"task_type": d["task_type"], "chosen": d["chosen"], "metric": d["metric"], "n": d["n_clusters"],
                   "insufficient_n": d["insufficient_n"],
                   "lo": d["rungs"][d["chosen"]]["lo"] if d["chosen"] else None,
                   "cost": d["rungs"][d["chosen"]]["cost"] if d["chosen"] else None}
                  for d in (cal or {}).get("decisions", []) if d["stratum"] == "all"]
        sweep_rows.append((ix.sweep(sid), routes))
        record["sweeps"].append({"sweep_id": sid, "page": f"sweeps/{sid}/index.html", "calibrated": cal is not None,
                                 "source_calibration_sha256": _sha(cal_file.read_bytes()) if cal_file.exists() else None})
    compare_links = []
    for a, b in compares:
        page = f"compare/{a}__{b}/index.html"
        files[page] = compare_page(ix, a, b, href_from("../../"), home="../../index.html")
        compare_links.append({"href": page, "label": f"{summaries[a].system} · {summaries[a].model or 'default'} → "
                                                     f"{summaries[b].system} · {summaries[b].model or 'default'}"})
        record["compares"].append({"a": a, "b": b, "page": page})
    files["index.html"] = environment().get_template("bundle_index.html").render(**_base({
        "page_title": title, "title": title, "kind_label": "Results", "href": href_from(""),
        "sweeps": sweep_rows, "runs": [summaries[r] for r in run_list], "compares": compare_links}, root))

    files = {path: scrub(text, root, ix.runs_dir.parent) for path, text in files.items()}
    rules, source = load_rules(root, denylist)
    public_hits, private_hits = [], set()
    for path, text in files.items():
        for rule, public in rules:
            if m := rule.search(text):
                if public:
                    public_hits.append(f"{path}:{text.count(chr(10), 0, m.start()) + 1} has {m.group(0)!r}")
                else:
                    private_hits.add(_position(path, run_list, sweep_list, compares))
    if private_hits:
        # Paths and ids can carry a system's name, so a denylist hit is reported by position only.
        raise ExportRefused("Refusing to export: the private denylist matches " + ", ".join(sorted(private_hits))
                            + ". Positions follow export order: --run, then sweep rungs, then compared runs.")
    if public_hits:
        raise ExportRefused("Refusing to export, public-boundary rule: " + "; ".join(public_hits[:12])
                            + (f"; and {len(public_hits) - 12} more" if len(public_hits) > 12 else ""))
    record["denylist"] = source
    record["files"] = {path: _sha(text) for path, text in sorted(files.items())}

    if out.exists():
        shutil.rmtree(out)
    for path, text in files.items():
        dest = out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    (out / MARKER).write_text(json.dumps(record, indent=2))
    return record

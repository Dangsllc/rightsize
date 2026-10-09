"""`rightsize`: build data, run systems, score, compare, serve reference systems."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Rightsize benchmark harness")
data_app = typer.Typer(no_args_is_help=True, help="Build, seed, and check suite data")
app.add_typer(data_app, name="data")


def _load_dotenv(root: Path) -> None:
    """Read KEY=VALUE lines from a gitignored .env at the repo root (never overrides real env)."""
    path = root / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("'\""))


def _root() -> Path:
    env = os.environ.get("RIGHTSIZE_ROOT")
    root = None
    if env:
        root = Path(env).resolve()
    else:
        p = Path.cwd().resolve()
        for cand in [p, *p.parents]:
            if (cand / "configs" / "suites").is_dir():
                root = cand
                break
    if root is None:
        raise typer.BadParameter("Run inside a Rightsize checkout or set RIGHTSIZE_ROOT")
    _load_dotenv(root)
    return root


def _params(items: list[str]) -> dict:
    out = {}
    for it in items:
        k, _, v = it.partition("=")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


# ------------------------------------------------------------------ data


@data_app.command("build")
def data_build(suite: str = "m1", allow_missing_seeds: bool = typer.Option(False, help="Build with whatever seeds exist")):
    """Regenerate suite files from pinned sources and frozen seeds (byte-reproducible)."""
    from rightsize_core.integrity.dataset import load_suite_config
    from rightsize_core.packs import get_pack

    root = _root()
    cfg = load_suite_config(root, suite)
    manifest = get_pack(cfg["pack"]).build_suite(root, cfg, allow_missing_seeds=allow_missing_seeds)
    typer.echo(json.dumps(manifest["counts"], indent=2))


@data_app.command("check")
def data_check(suite: str = "m1"):
    """Verify committed data against MANIFEST.json, the canary, and the split guard."""
    from rightsize_core.integrity.dataset import check_suite

    problems = check_suite(_root(), suite)
    if problems:
        for p in problems:
            typer.echo(f"FAIL {p}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{suite}: ok")


@data_app.command("seed")
def data_seed(
    what: str = typer.Argument(..., help="retrieval | manuals"),
    suite: str = "m1",
    backend: str = typer.Option("openai", help="openai (any OpenAI-compatible server) | anthropic"),
    model: str = typer.Option(..., help="Model id used to write the seeds"),
    base_url: str | None = typer.Option(None, help="For openai backend, e.g. http://localhost:11434/v1"),
    api_key_env: str | None = typer.Option(None, help="Env var holding the API key"),
    temperature: float = 0.7,
    share: str = typer.Option("0/1", help="i/n: only targets whose hash % n == i (split work across writers)"),
    base: str | None = typer.Option(None, help="manuals: which base manual to write (default: all)"),
    max_attempts: int = typer.Option(4, help="manuals: retries per chapter before giving up"),
    retry_rejected: bool = typer.Option(False, help="retrieval: add fresh candidates to slots that yielded no usable query"),
):
    """Write frozen LLM seed artifacts (run once, review, commit)."""
    from rightsize_pack_compliance.gen.corpus import Corpus
    from rightsize_pack_compliance.gen.llm import SeedLLM

    from rightsize_core.integrity.dataset import load_suite_config

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    root = _root()
    cfg = load_suite_config(root, suite)
    llm = SeedLLM(backend, model, base_url, api_key_env, temperature)
    corpus = Corpus.load([root / s for s in cfg["sources"]])
    if what == "retrieval":
        from rightsize_pack_compliance.gen import seed_queries

        i, _, n = share.partition("/")
        retry = None
        if retry_rejected:
            from rightsize_pack_compliance.gen import retrieval as _ret

            seeds = seed_queries.store_for(root, cfg, corpus).load()
            targets = {t.code: t for t in _ret.select_targets(corpus, cfg["retrieval"])}
            retry = {k for k in seeds if seeds[k].get("generator_llm") == llm.label
                     and _ret.pick_seed(targets[k[0]], k[1], seeds, cfg["retrieval"].get("overlap_filter", {}))[0] is None}
            typer.echo(f"retrying {len(retry)} slots")
        out = seed_queries.generate(corpus, cfg, llm, seed_queries.store_for(root, cfg, corpus),
                                    share=(int(i), int(n)), retry=retry)
    elif what == "manuals":
        from rightsize_pack_compliance.gen import seed_manuals

        out = seed_manuals.generate(root, corpus, cfg, llm, base_id=base, max_attempts=max_attempts)
    else:
        raise typer.BadParameter("what must be 'retrieval' or 'manuals'")
    typer.echo(f"wrote {out}")


@data_app.command("check-manual")
def data_check_manual(base: str = typer.Option(...), suite: str = "m1"):
    """Run the seed checker over a base manual file and list every problem (exit 1 if any)."""
    from rightsize_pack_compliance.gen.seed_manuals import _check_group, base_seed_path, load_elements

    from rightsize_core.integrity.dataset import load_suite_config

    root = _root()
    cfg = load_suite_config(root, suite)
    ccfg = cfg["control_classification"]
    elements = load_elements(root / ccfg["elements"])
    path = base_seed_path(root, cfg, next(b for b in ccfg["bases"] if b["id"] == base))
    seed = json.loads(path.read_text())
    problems = []
    front = seed.get("front") or {}
    for key, n in (("preamble", 1), ("distractors", 4), ("near_misses", 2)):
        if len(front.get(key) or []) < n:
            problems.append(f"front.{key}: need at least {n}")
    for g in elements["groups"]:
        reply = seed.get("groups", {}).get(g["code"])
        if reply is None:
            problems.append(f"{g['code']}: missing group")
            continue
        problems += [f"{g['code']}: {p}" for p in _check_group(reply, g)]
    for p in problems:
        typer.echo(p)
    typer.echo(f"{len(problems)} problems in {path.name}")
    raise typer.Exit(1 if problems else 0)


@data_app.command("verify")
def data_verify(
    suite: str = "m1",
    backend: str = typer.Option("anthropic"),
    model: str = typer.Option(...),
    base_url: str | None = None,
    api_key_env: str | None = None,
):
    """Multiple-choice check that each seeded query is answered by its target provision."""
    from rightsize_pack_compliance.gen import seed_queries
    from rightsize_pack_compliance.gen.corpus import Corpus
    from rightsize_pack_compliance.gen.llm import SeedLLM

    from rightsize_core.integrity.dataset import load_suite_config

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    root = _root()
    cfg = load_suite_config(root, suite)
    corpus = Corpus.load([root / s for s in cfg["sources"]])
    llm = SeedLLM(backend, model, base_url, api_key_env, temperature=0.0)
    typer.echo(f"wrote {seed_queries.verify(corpus, cfg, llm, seed_queries.store_for(root, cfg, corpus))}")


# ------------------------------------------------------------------ run / report


@app.command()
def run(
    system: str = typer.Option(..., help="URL, mcp+..., ref:<name>, plugin:<mod:attr>, or a system YAML"),
    suite: str = "m1",
    split: str = typer.Option("public", help="public | dev | heldout"),
    task_types: list[str] | None = typer.Option(None, "--task-type", help="Limit to these task types"),
    model: str | None = typer.Option(None, help="Model the system should use (if it takes one)"),
    param: list[str] = typer.Option([], help="key=value params passed to the system"),
    repeats: int = 1,
    concurrency: int = 4,
    limit: int | None = typer.Option(None, help="Only the first N tasks (smoke runs)"),
    pricing_file: Path = typer.Option(Path("configs/pricing/2026-10.yaml"), "--pricing"),
    max_cost: float | None = typer.Option(None, help="Abort once reported spend passes this (USD)"),
    skip_conformance: bool = typer.Option(False, help="Skip the conformance preflight for declarative systems"),
):
    """Run a suite against a system, score it, and write runs/<id>/."""
    from rightsize_core.integrity.dataset import load_tasks
    from rightsize_core.report.html import render
    from rightsize_core.runner.run import execute
    from rightsize_core.runner.score import score_run
    from rightsize_core.systems.loader import load_system
    from rightsize_core.usage.pricing import Pricing

    root = _root()
    sysobj = load_system(system, root, suite)
    sysobj.configure(model, _params(param))
    tasks = load_tasks(root, suite, split, task_types)
    if limit:
        tasks = tasks[:limit]
    pricing = Pricing(root / pricing_file) if (root / pricing_file).exists() else None

    async def go():
        try:
            await sysobj.preflight()
            if getattr(sysobj, "declarative", False) and not skip_conformance:
                from rightsize_core.conformance.check import run_conformance

                report = await run_conformance(root, sysobj, suite)
                if not report.ok:
                    typer.echo(report.text(), err=True)
                    raise typer.Exit(2)
            total = len(tasks) * repeats
            done = 0

            def tick():
                nonlocal done
                done += 1
                if done % max(1, total // 20) == 0 or done == total:
                    typer.echo(f"  {done}/{total}", err=True)

            return await execute(root, sysobj, tasks, suite, split, repeats, concurrency,
                                 pricing=pricing, max_cost=max_cost, progress=tick)
        finally:
            await sysobj.aclose()

    run_dir = asyncio.run(go())
    card = score_run(root, run_dir)
    render(card, run_dir / "scorecard.html")
    _summary(card)
    typer.echo(f"\n{run_dir / 'scorecard.html'}")


def _summary(card: dict) -> None:
    typer.echo(f"\n{card['system']} · {card['model'] or 'default model'} · {card['status']}")
    for tt, r in card["task_types"].items():
        pm = r["metrics"].get(r["primary_metric"])
        val = f"{pm['value']:.3f} [{pm['lo']:.3f}, {pm['hi']:.3f}]" if pm else "n/a"
        cost = r["usage"]["cost_usd_per_task"]
        typer.echo(f"  {tt:<24} {r['primary_metric']} {val}   cost/task "
                   f"{'unknown' if cost is None else f'${cost:.4f}'}")


@app.command()
def report(run_dir: Path):
    """Re-score an existing run (no system calls) and re-render its scorecard."""
    from rightsize_core.report.html import render
    from rightsize_core.runner.score import score_run

    card = score_run(_root(), run_dir)
    render(card, run_dir / "scorecard.html")
    _summary(card)


@app.command()
def diff(run_a: Path, run_b: Path, delta: float = typer.Option(0.0, help="Regression margin")):
    """Paired comparison of two runs on the items both scored (B minus A)."""
    from rightsize_core.report.diff import diff_runs

    typer.echo(diff_runs(_root(), run_a, run_b, delta))


@app.command()
def conformance(system: str = typer.Option(...), suite: str = "m1"):
    """Check that a system's responses map cleanly onto the task contracts before scoring it."""
    from rightsize_core.conformance.check import run_conformance
    from rightsize_core.systems.loader import load_system

    root = _root()
    sysobj = load_system(system, root, suite)

    async def go():
        try:
            await sysobj.preflight()
            return await run_conformance(root, sysobj, suite)
        finally:
            await sysobj.aclose()

    rep = asyncio.run(go())
    typer.echo(rep.text())
    raise typer.Exit(0 if rep.ok else 1)


@app.command()
def sweep(
    system: str = typer.Option(..., help="A system that accepts a model parameter"),
    ladder: Path = typer.Option(Path("configs/ladders/default.yaml")),
    suite: str = "m1",
    split: str = typer.Option("dev", help="Sweeps default to dev to keep cost down"),
    rungs: str | None = typer.Option(None, help="Comma-separated rung names to run (default: all)"),
    task_types: list[str] | None = typer.Option(None, "--task-type"),
    sample: int | None = typer.Option(None, help="Stratified sample of N tasks"),
    repeats: int = 1,
    concurrency: int = 4,
    max_cost: float | None = typer.Option(None, help="Abort the sweep once total reported spend passes this"),
    dry_run: bool = typer.Option(False, help="Print the plan and stop"),
    pricing_file: Path = typer.Option(Path("configs/pricing/2026-10.yaml"), "--pricing"),
):
    """Run one suite across a ladder of models (same tasks, same settings) for right-sizing."""
    import uuid
    from datetime import UTC, datetime

    from rightsize_core.calibration.sweep import load_ladder, sample_tasks
    from rightsize_core.integrity.dataset import load_tasks
    from rightsize_core.report.html import render
    from rightsize_core.runner.run import execute
    from rightsize_core.runner.score import score_run
    from rightsize_core.systems.loader import load_system
    from rightsize_core.usage.pricing import Pricing

    root = _root()
    ladder_rungs = load_ladder(root / ladder)
    if rungs:
        wanted = set(rungs.split(","))
        ladder_rungs = [r for r in ladder_rungs if r.name in wanted]
    tasks = sample_tasks(load_tasks(root, suite, split, task_types), sample)
    pricing = Pricing(root / pricing_file)
    typer.echo(f"{len(ladder_rungs)} rungs x {len(tasks)} tasks x {repeats} repeats = "
               f"{len(ladder_rungs) * len(tasks) * repeats} system calls")
    for r in ladder_rungs:
        rate = pricing.rate(r.model)
        price = f"${rate['input']:.2f}/${rate['output']:.2f} per MTok" if rate else "no price on file"
        typer.echo(f"  {r.name:<18} {r.model:<20} {r.params}  {price}")
    if dry_run:
        return
    sweep_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    sdir = root / "runs" / "sweeps" / sweep_id
    sdir.mkdir(parents=True)
    record = {"sweep_id": sweep_id, "system": system, "suite": suite, "split": split, "ladder": str(ladder),
              "task_ids": [t.id for t in tasks], "repeats": repeats, "runs": []}
    spent = 0.0

    async def one(r):
        sysobj = load_system(system, root, suite)
        sysobj.configure(r.model, r.params)
        try:
            await sysobj.preflight()
            info = await sysobj.info()
            if not info.supports_model_param:
                raise typer.BadParameter(f"{info.name} does not take a model parameter; it can't be swept")
            remaining = None if max_cost is None else max(0.0, max_cost - spent)
            return await execute(root, sysobj, tasks, suite, split, repeats, concurrency, pricing=pricing,
                                 sweep_id=sweep_id, max_cost=remaining)
        finally:
            await sysobj.aclose()

    for r in ladder_rungs:
        typer.echo(f"\n== {r.name}")
        run_dir = asyncio.run(one(r))
        card = score_run(root, run_dir)
        render(card, run_dir / "scorecard.html")
        _summary(card)
        spent += sum((t["usage"]["cost_usd_total"] or 0.0) for t in card["task_types"].values())
        record["runs"].append({"rung": r.name, "model": r.model, "params": r.params, "run_dir": str(run_dir)})
        (sdir / "sweep.json").write_text(json.dumps(record, indent=2))
        if max_cost is not None and spent > max_cost:
            typer.echo(f"Stopping: spend ${spent:.2f} passed --max-cost {max_cost}", err=True)
            break
    typer.echo(f"\nspend ${spent:.2f}. Next: rightsize calibrate \"{sdir}\"")
    typer.echo(f"sweep_dir={sdir}")


@app.command()
def calibrate(
    sweep_dir: Path,
    threshold: list[str] = typer.Option([], help="metric=value, e.g. group.macro_f1=0.8 or ndcg@10=0.7"),
    delta: float = typer.Option(0.03, help="Non-inferiority margin versus the best rung"),
    floor: list[str] = typer.Option([], help="metric=value every chosen rung must clear, e.g. spec.deficiency_recall=0.9"),
    emit: Path | None = typer.Option(None, help="Write routing.yaml here"),
):
    """Pick the cheapest good-enough rung per task type and tier; write a report and routing.yaml."""
    import yaml as _yaml

    from rightsize_core.calibration.sweep import calibrate as _cal
    from rightsize_core.calibration.sweep import load_ladder, routing_yaml
    from rightsize_core.report.calibration_html import render

    root = _root()
    th = {k: float(v) for k, _, v in (t.partition("=") for t in threshold)}
    fl = {k: float(v) for k, _, v in (t.partition("=") for t in floor)}
    result = _cal(root, sweep_dir, th, delta, fl)
    (sweep_dir / "calibration.json").write_text(json.dumps(result, indent=2, default=str))
    render(result, sweep_dir / "calibration.html")
    for d in result["decisions"]:
        flag = " (n<30)" if d["insufficient_n"] else ""
        typer.echo(f"{d['task_type']:<24} {d['stratum']:<28} -> {d['chosen'] or '—':<18} ({d['why']}){flag}")
    sweep = _yaml.safe_load((sweep_dir / "sweep.json").read_text())
    ladder = {r.name: r for r in load_ladder(root / sweep["ladder"])}
    if emit:
        emit.write_text(routing_yaml(result, ladder))
        typer.echo(f"wrote {emit}")
    typer.echo(f"{sweep_dir / 'calibration.html'}")


@app.command("spec")
def spec_export():
    """Regenerate spec/schemas/*.json from the code."""
    from rightsize_core.spec_export import export

    for p in export(_root() / "spec" / "schemas"):
        typer.echo(p)


@app.command()
def serve(
    name: str = typer.Argument(..., help="Reference system: bm25 | dense | llm | oracle"),
    suite: str = "m1",
    port: int = 8701,
    host: str = "127.0.0.1",
    transport: str = typer.Option("http", help="http | mcp-stdio | mcp-http"),
):
    """Serve a reference system over rightsize/v1."""
    from rightsize_reference_systems.registry import make
    from rightsize_reference_systems.server import http_app, mcp_server

    sysobj = make(name, _root(), suite)
    if transport == "http":
        import uvicorn

        uvicorn.run(http_app(sysobj), host=host, port=port, log_level="warning")
    elif transport == "mcp-stdio":
        mcp_server(sysobj).run("stdio")
    elif transport == "mcp-http":
        mcp_server(sysobj).run("streamable-http", host=host, port=port)
    else:
        raise typer.BadParameter("transport must be http, mcp-stdio, or mcp-http")


if __name__ == "__main__":
    app()

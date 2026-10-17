"""The Workbench: a local web UI over `rightsize_core.service`.

Every page reads the same service layer the CLI and the MCP server use, so the numbers agree.
Launching goes through a reviewed Plan: the browser posts back the plan_id it was shown, and a
plan that changed since then is shown again instead of started.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from rightsize_core.report.render import HEALTH, STATIC, SWEEP, TONE_ORDER, environment, explain, qs
from rightsize_core.runner.run import default_runs_dir
from rightsize_core.service import jobs, queries
from rightsize_core.service.env import env_present, load_dotenv
from rightsize_core.service.index import Index, NotFound
from rightsize_core.service.models import PlanRequest
from rightsize_core.service.plans import build_plan

from rightsize_ui.security import SessionGuard

HERE = Path(__file__).resolve().parent

def href(kind: str, ident: str | None = None, **query) -> str:
    """Where the Workbench shows a run, sweep, item list, item, or job (shared report templates link
    through this; an export links within its bundle instead)."""
    base = {"run": f"/runs/{ident}", "sweep": f"/sweeps/{ident}", "items": f"/runs/{ident}/items",
            "item": f"/runs/{ident}/item", "job": f"/jobs/{ident}"}[kind]
    q = qs(**query)
    return f"{base}?{q}" if q else base


# ---------------------------------------------------------------------------- the app


class Workbench:
    def __init__(self, root: Path, runs_dir: Path) -> None:
        self.root, self.runs_dir = root, runs_dir
        self.index = Index(root, runs_dir)
        self._checks: tuple[float, dict] | None = None

    def suites(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "configs" / "suites").glob("*.yaml"))

    def ladders(self) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in (self.root / "configs" / "ladders").glob("*.yaml"))

    def system_choices(self) -> list[dict]:
        """Reference systems, the shipped example configs, and any folders in RIGHTSIZE_SYSTEMS_DIRS."""
        import yaml

        out = [{"spec": f"ref:{n}", "name": n, "kind": "reference system", "trusted": True, "env": {}}
               for n in ("bm25", "llm", "oracle")]
        dirs = [self.root / "configs" / "systems"]
        dirs += [Path(d) for d in os.environ.get("RIGHTSIZE_SYSTEMS_DIRS", "").split(os.pathsep) if d]
        seen = set()
        for d in dirs:
            for p in sorted(d.rglob("*.y*ml")) if d.is_dir() else []:
                if p in seen:
                    continue
                seen.add(p)
                try:
                    cfg = yaml.safe_load(p.read_text()) or {}
                except (OSError, yaml.YAMLError):
                    continue
                spec = p.relative_to(self.root).as_posix() if p.is_relative_to(self.root) else str(p)
                text = p.read_text()
                import re

                names = set(re.findall(r"\$\{([A-Z0-9_]+)\}", text))
                if isinstance(cfg.get("auth"), dict) and cfg["auth"].get("env"):
                    names.add(cfg["auth"]["env"])
                out.append({"spec": spec, "name": cfg.get("name", p.stem),
                            "kind": "rightsize/v1 " + cfg.get("transport", "") if cfg.get("protocol") == "rightsize/v1"
                            else f"declarative {cfg.get('transport', '')}",
                            "trusted": bool(cfg.get("trusted")), "env": {n: env_present(n) for n in sorted(names)},
                            "tasks": sorted((cfg.get("tasks") or {}).keys())})
        return out

    def data_checks(self) -> dict[str, list[str]]:
        if self._checks and time.time() - self._checks[0] < 60:
            return self._checks[1]
        from rightsize_core.integrity.dataset import check_suite

        res = {}
        for s in self.suites():
            try:
                res[s] = check_suite(self.root, s)
            except Exception as e:  # noqa: BLE001
                res[s] = [f"can't check: {e}"]
        self._checks = (time.time(), res)
        return res

    def ladder_keys(self) -> list[dict]:
        """Env vars the shipped ladders need under ref:llm, and whether each is set."""
        from rightsize_core.calibration.sweep import load_ladder
        from rightsize_core.systems.loader import load_system

        try:
            sysobj = load_system("ref:llm", self.root, "m1")
        except Exception:  # noqa: BLE001
            return []
        need: dict[str, set[str]] = {}
        for lad in self.ladders():
            for r in load_ladder(self.root / lad):
                sysobj.configure(r.model, r.params)
                for v in sysobj.required_env():
                    need.setdefault(v, set()).add(f"{Path(lad).name}: {r.name}")
        return [{"name": k, "set": env_present(k), "used_by": sorted(v)} for k, v in sorted(need.items())]

    def attention(self) -> list[dict]:
        """What needs a person, most serious first. Repeated states fold into one entry."""
        items: list[dict] = []
        for s in self.index.runs():
            if s.health in ("ok", "running") or s.health_reason.startswith("Being scored"):
                continue
            glyph, label, tone = HEALTH[s.health]
            nxt = [("Open the run", f"/runs/{s.run_id}")]
            if s.error_groups and s.error_groups[0].hint:
                nxt.append((s.error_groups[0].hint, None))
            if s.health == "interrupted":
                nxt.append(("Score what finished: rightsize report " + s.path, None))
            items.append({"tone": tone, "glyph": glyph, "label": label, "href": f"/runs/{s.run_id}",
                          "title": f"{s.system or '?'} · {s.model or 'default model'} · {s.suite}/{s.split}",
                          "when": s.started_at, "cause": s.health_reason, "next": nxt})
        empty = []
        for w in self.index.sweeps():
            if w.state == "empty":
                empty.append(w)
                continue
            if w.state in ("failed", "interrupted", "cancelled", "stopped"):
                glyph, label, tone = SWEEP[w.state]
                items.append({"tone": tone, "glyph": glyph, "label": label, "href": f"/sweeps/{w.sweep_id}",
                              "title": f"sweep {w.sweep_id} · {w.system} · {w.suite}/{w.split}", "when": w.started_at,
                              "cause": w.state_reason, "next": [("Open the sweep", f"/sweeps/{w.sweep_id}")]})
            if w.low_n_decisions:
                items.append({"tone": "warning", "glyph": "▲", "label": "Thin evidence",
                              "href": f"/sweeps/{w.sweep_id}", "when": w.started_at,
                              "title": f"sweep {w.sweep_id} · routing decisions",
                              "cause": (f"{w.low_n_decisions} routing decision{'s' if w.low_n_decisions != 1 else ''} "
                                        "rest on fewer than 30 clusters; don't quote them as findings."),
                              "next": [("See the decisions", f"/sweeps/{w.sweep_id}#calibration")]})
        if empty:
            items.append({"tone": "critical", "glyph": "✕", "label": "Died before first rung", "href": "/sweeps",
                          "title": f"{len(empty)} sweep{'s' if len(empty) != 1 else ''} left no record",
                          "when": empty[0].started_at,
                          "cause": ("Older sweeps wrote sweep.json only after a rung finished, so these directories "
                                    "are empty and can't say why they stopped. New sweeps record every rung as it "
                                    "starts."),
                          "next": [("List them", "/sweeps#empty")]})
        for suite, problems in self.data_checks().items():
            if problems:
                items.append({"tone": "critical", "glyph": "✕", "label": "Data check fails", "href": None,
                              "title": f"suite {suite}", "when": None, "cause": problems[0],
                              "next": [(f"rightsize data check --suite {suite}", None)]})
        for k in self.ladder_keys():
            if not k["set"]:
                items.append({"tone": "warning", "glyph": "▲", "label": "Key not set", "href": None,
                              "title": k["name"], "when": None,
                              "cause": f"Needed by {len(k['used_by'])} ladder rung{'s' if len(k['used_by']) != 1 else ''}"
                                       f" ({', '.join(k['used_by'][:3])}{'…' if len(k['used_by']) > 3 else ''}).",
                              "next": [(f"Add {k['name']}=… to .env (the Workbench only checks that it is set)", None)]})
        return sorted(items, key=lambda i: (TONE_ORDER[i["tone"]], -(_ts(i["when"]))))


def _ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def _params(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        try:
            out[k.strip()] = json.loads(v.strip())
        except json.JSONDecodeError:
            out[k.strip()] = v.strip()
    return out


def _num(v: str | None, cast=float):
    try:
        return cast(v) if v not in (None, "") else None
    except ValueError:
        return None


def plan_request(form) -> PlanRequest:
    kind = form.get("kind", "run")
    return PlanRequest(
        kind="sweep" if kind == "sweep" else "run", system=form.get("system", "").strip(),
        suite=form.get("suite", "m1"), split=form.get("split", "public"),
        task_types=form.getlist("task_types"), model=(form.get("model") or "").strip() or None,
        params=_params(form.get("params", "")), ladder=form.get("ladder") or None, rungs=form.getlist("rungs"),
        limit=_num(form.get("limit"), int), repeats=_num(form.get("repeats"), int) or 1,
        concurrency=_num(form.get("concurrency"), int) or 4, max_cost=_num(form.get("max_cost")),
        stop_after_errors=_num(form.get("stop_after_errors"), int), ack_unpriced=form.get("ack_unpriced") == "on",
    )


def create_app(root: Path, runs_dir: Path | None = None, token: str | None = None) -> FastAPI:
    load_dotenv(root)
    wb = Workbench(root, runs_dir or default_runs_dir(root))
    env = environment(HERE / "templates")
    env.globals.update(href=href, static=False)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.wb = wb
    app.state.token = token or secrets.token_urlsafe(24)
    app.add_middleware(SessionGuard, token=app.state.token)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/core-static", StaticFiles(directory=STATIC), name="core-static")

    def page(name: str, request: Request, status: int = 200, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("nav", name.split(".")[0])
        live = [j for j in jobs.list_jobs(wb.runs_dir) if j.state == "running"]
        html = env.get_template(name).render(request=request, root=str(root), live_jobs=live, **ctx)
        return HTMLResponse(html, status_code=status)

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound):
        return page("error.html", request, 404, title="Not found", message=f"Nothing here called {exc}.")

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        runs = wb.index.runs()
        return page("home.html", request, attention=wb.attention(), recent=runs[:8],
                    jobs=jobs.list_jobs(wb.runs_dir)[:5], keys=wb.ladder_keys(), checks=wb.data_checks())

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request, suite: str = "", split: str = "", system: str = "", health: str = ""):
        all_runs = wb.index.runs()
        shown = [r for r in all_runs if (not suite or r.suite == suite) and (not split or r.split == split)
                 and (not system or r.system == system) and (not health or r.health == health)]
        return page("runs.html", request, runs=shown, total=len(all_runs),
                    f={"suite": suite, "split": split, "system": system, "health": health},
                    systems=sorted({r.system for r in all_runs if r.system}), suites=wb.suites())

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run(request: Request, run_id: str):
        d = queries.run_detail(wb.index, run_id)
        others = [r for r in wb.index.runs() if r.run_id != run_id and r.suite == d["summary"].suite]
        return page("run.html", request, nav="runs", d=d, s=d["summary"], others=others[:20],
                    manifest_json=json.dumps(d["manifest"], indent=2))

    @app.get("/runs/{run_id}/items", response_class=HTMLResponse)
    def items(request: Request, run_id: str, task_type: str = "", stratum: str = "", show: str = "all"):
        s = wb.index.summary(run_id)
        task_type = task_type or (s.task_types[0] if s.task_types else "")
        rows, problem = [], None
        try:
            rows = queries.items(wb.index, run_id, task_type, stratum or None, show)
        except Exception as e:  # noqa: BLE001
            problem = f"Items can't be read for this run: {explain(e)}"
        card = (wb.index.scorecard(run_id)[0] or {}).get("task_types", {}).get(task_type, {})
        strata = [g["stratum"] for g in queries.strata_grid(card)] if card else []
        return page("items.html", request, nav="runs", s=s, rows=rows, problem=problem, task_type=task_type,
                    stratum=stratum, show=show, strata=strata)

    @app.get("/runs/{run_id}/item", response_class=HTMLResponse)
    def item(request: Request, run_id: str, task: str, repeat: int = 0, back: str = ""):
        s = wb.index.summary(run_id)
        try:
            view, problem = queries.item_view(wb.index, run_id, task, repeat), None
        except NotFound:
            raise
        except Exception as e:  # noqa: BLE001
            view, problem = None, f"This item can't be shown: {explain(e)}"
        return page("item.html", request, nav="runs", s=s, view=view, problem=problem, task=task, repeat=repeat,
                    back=back if back.startswith("/runs/") else f"/runs/{run_id}/items")

    @app.get("/compare", response_class=HTMLResponse)
    def compare(request: Request, a: str = "", b: str = ""):
        picks = sorted(request.query_params.getlist("pick"))  # run ids start with their UTC start time
        if not (a and b) and len(picks) == 2:
            a, b = picks
        if not (a and b):
            return page("error.html", request, 400, nav="runs", title="Pick two runs",
                        message="Choose exactly two runs on the Runs page, then Compare.")
        try:
            c, problem = queries.compare(wb.index, a, b), None
        except NotFound:
            raise
        except Exception as e:  # noqa: BLE001
            c, problem = None, f"These runs can't be compared: {explain(e)}"
        return page("compare.html", request, nav="runs", c=c, problem=problem, a=a, b=b,
                    sections=c["sections"] if c else [])

    @app.get("/sweeps", response_class=HTMLResponse)
    def sweeps(request: Request):
        all_sweeps = wb.index.sweeps()
        return page("sweeps.html", request, sweeps=[w for w in all_sweeps if w.state != "empty"],
                    empty=[w for w in all_sweeps if w.state == "empty"])

    @app.get("/sweeps/{sweep_id}", response_class=HTMLResponse)
    def sweep(request: Request, sweep_id: str):
        w = wb.index.sweep(sweep_id)
        runs_by_id = {r.run_id: wb.index.summary(r.run_id) for r in w.rungs if r.run_id}
        cal = queries.calibration(wb.index, sweep_id)
        routing = (wb.index.sweep_dir(sweep_id) / "routing.yaml")
        task_metrics = {h.task_type: h.metric for r in runs_by_id.values() for h in r.headlines}
        if cal:
            task_metrics.update({d["task_type"]: d["metric"] for d in cal["decisions"]})
        tau = {}
        for tt, m in task_metrics.items():
            th = (cal or {}).get("thresholds", {})
            tau[tt] = th.get(m, th.get(f"{tt}:{m}"))
        floor_metrics = sorted({m for d in (cal or {}).get("decisions", []) for r in d["rungs"].values()
                                for m in r.get("metrics", {})})
        return page("sweep.html", request, nav="sweeps", w=w, runs_by_id=runs_by_id, cal=cal,
                    routing=routing.read_text() if routing.exists() else None, task_metrics=task_metrics, tau=tau,
                    floor_metrics=floor_metrics)

    @app.post("/sweeps/{sweep_id}/calibrate")
    async def calibrate(request: Request, sweep_id: str):
        sdir = wb.index.sweep_dir(sweep_id)
        form = await request.form()
        argv = ["calibrate", str(sdir), "--delta", f"{_num(form.get('delta')) or 0.03:g}"]
        for key, val in form.multi_items():
            metric = key.split(":", 1)[-1]
            if key.startswith("threshold:") and val not in ("", None) and form.get(f"threshold_off:{metric}") != "on":
                argv += ["--threshold", f"{metric}={float(val):g}"]
        if form.get("floor_metric") and _num(form.get("floor_value")) is not None:
            argv += ["--floor", f"{form['floor_metric']}={float(form['floor_value']):g}"]
        if form.get("emit") == "on":
            argv += ["--emit", str(sdir / "routing.yaml")]
        job = jobs.start(root, wb.runs_dir, argv, "calibrate", "ui")
        return RedirectResponse(f"/jobs/{job.job_id}", status_code=303)

    # ------------------------------------------------------------------ launching
    def launch_ctx(req: PlanRequest | None, plan=None, notice: str | None = None, pilot: bool = False) -> dict:
        from rightsize_core.calibration.sweep import load_ladder
        from rightsize_core.integrity.dataset import load_suite_config
        from rightsize_core.packs import get_pack

        req = req or PlanRequest(kind="run", system="ref:bm25")
        task_types = list(get_pack(load_suite_config(root, req.suite)["pack"]).task_types)
        ladder_rungs = []
        if req.kind == "sweep":
            try:
                ladder_rungs = load_ladder(root / (req.ladder or wb.ladders()[0]))
            except (OSError, KeyError, IndexError):
                ladder_rungs = []
        from rightsize_core.service.plans import DEFAULT_PRICING
        from rightsize_core.usage.pricing import Pricing

        pricing = Pricing(root / DEFAULT_PRICING) if (root / DEFAULT_PRICING).exists() else None
        rates = {r.name: pricing.rate(r.model) if pricing else None for r in ladder_rungs}
        return {"req": req, "plan": plan, "notice": notice, "pilot": pilot, "systems": wb.system_choices(),
                "rates": rates,
                "suites": wb.suites(), "ladders": wb.ladders(), "task_type_choices": task_types,
                "ladder_rungs": ladder_rungs,
                "params_text": "\n".join(f"{k}={json.dumps(v) if not isinstance(v, str) else v}"
                                         for k, v in req.params.items())}

    @app.get("/launch", response_class=HTMLResponse)
    def launch(request: Request, kind: str = "run", system: str = "ref:bm25", suite: str = "m1",
               ladder: str = "", model: str = ""):
        if kind == "sweep" and system == "ref:bm25":
            system = "ref:llm"  # bm25 takes no model, so it can't be swept
        req = PlanRequest(kind="sweep" if kind == "sweep" else "run", system=system, suite=suite,
                          ladder=ladder or (wb.ladders()[0] if kind == "sweep" and wb.ladders() else None),
                          model=model or None, split="dev" if kind == "sweep" else "public")
        return page("launch.html", request, **launch_ctx(req))

    @app.post("/launch/plan", response_class=HTMLResponse)
    async def launch_plan(request: Request):
        form = await request.form()
        req = plan_request(form)
        pilot = form.get("action") == "pilot"
        if form.get("action") == "refresh":  # kind, suite, or ladder changed: redraw the form only
            return page("launch.html", request, **launch_ctx(req))
        if pilot:
            req = req.model_copy(update={"limit": min(req.limit or 20, 20), "repeats": 1,
                                         "max_cost": min(req.max_cost, 5.0) if req.max_cost is not None else 5.0})
        plan = await build_plan(root, req, wb.index)
        return page("launch.html", request, **launch_ctx(req, plan, pilot=pilot))

    @app.post("/launch/start")
    async def launch_start(request: Request):
        form = await request.form()
        req = plan_request(form)
        plan = await build_plan(root, req, wb.index)
        if plan.plan_id != form.get("plan_id"):
            return page("launch.html", request, 409, **launch_ctx(
                req, plan, notice="The plan changed since you reviewed it (a key, a price, a measured cost, or a "
                                  "guard). Review it again before launching."))
        if not plan.ok:
            return page("launch.html", request, 409, **launch_ctx(req, plan, notice="The plan doesn't pass its guards."))
        job = jobs.start_plan(root, wb.runs_dir, plan, "ui")
        return RedirectResponse(f"/jobs/{job.job_id}", status_code=303)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return page("jobs.html", request, jobs=jobs.list_jobs(wb.runs_dir))

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job(request: Request, job_id: str):
        return page("job.html", request, nav="jobs", j=jobs.load(wb.runs_dir, job_id), live=job_live(job_id))

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        jobs.cancel(wb.runs_dir, job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    def job_live(job_id: str) -> dict:
        j = jobs.load(wb.runs_dir, job_id)
        runs_ = []
        for rid in j.run_ids:
            try:
                runs_.append(wb.index.summary(rid).model_dump(mode="json"))
            except NotFound:
                continue
        sweep_ = None
        if j.sweep_id:
            try:
                sweep_ = wb.index.sweep(j.sweep_id).model_dump(mode="json")
            except NotFound:
                pass
        cap = None
        if "--max-cost" in j.argv:
            cap = float(j.argv[j.argv.index("--max-cost") + 1])
        return {"job": j.model_dump(mode="json"), "runs": runs_, "sweep": sweep_, "cap": cap,
                "spend": sum(r["progress"]["spend_usd"] for r in runs_)}

    # ------------------------------------------------------------------ systems
    @app.get("/systems", response_class=HTMLResponse)
    def systems(request: Request):
        return page("systems.html", request, systems=wb.system_choices(), suites=wb.suites(), report=None)

    @app.post("/systems/conformance", response_class=HTMLResponse)
    async def conformance(request: Request):
        from rightsize_core.conformance.check import run_conformance
        from rightsize_core.systems.loader import load_system

        form = await request.form()
        spec, suite = form.get("system", ""), form.get("suite", "m1")
        report, problem = None, None
        try:
            sysobj = load_system(spec, root, suite)
            try:
                await asyncio.wait_for(sysobj.preflight(), 60)
                report = await asyncio.wait_for(run_conformance(root, sysobj, suite), 600)
            finally:
                await sysobj.aclose()
        except Exception as e:  # noqa: BLE001
            problem = f"{type(e).__name__}: {e}"
        return page("systems.html", request, systems=wb.system_choices(), suites=wb.suites(), report=report,
                    problem=problem, checked=spec, checked_suite=suite)

    # ------------------------------------------------------------------ JSON
    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str):
        return JSONResponse(job_live(job_id))

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str):
        return JSONResponse(wb.index.summary(run_id).model_dump(mode="json"))

    @app.get("/api/runs")
    def api_runs():
        return JSONResponse([r.model_dump(mode="json") for r in wb.index.runs()])

    return app


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, runs_dir: Path | None = None,
          open_browser: bool = True) -> None:
    import uvicorn

    from rightsize_ui.security import LOOPBACK

    if host not in LOOPBACK:
        raise SystemExit("The Workbench binds to loopback only (127.0.0.1, localhost, ::1).")
    app = create_app(root, runs_dir)
    url = f"http://{host}:{port}/?token={app.state.token}"
    print(f"Rightsize Workbench · {root}\n  runs: {app.state.wb.runs_dir}\n  open: {url}\n(Ctrl-C to stop)", flush=True)
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")

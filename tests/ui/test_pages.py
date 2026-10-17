"""The Workbench renders every state, never renders system output as markup, and refuses requests
it can't trust."""

import asyncio
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rightsize_core.integrity.dataset import load_tasks
from rightsize_core.runner.run import execute
from rightsize_core.schema import Outcome, OutcomeError, SystemInfo
from rightsize_core.systems.plugin import PluginSystem
from rightsize_ui.app import create_app

ROOT = Path(__file__).resolve().parents[2]
HOST = "http://127.0.0.1:8765"
SCRIPT = "<script>alert('x')</script>"


def client(runs_dir: Path, cookie: bool = True) -> TestClient:
    c = TestClient(create_app(ROOT, runs_dir, token="test-token"), base_url=HOST)
    if cookie:
        c.cookies.set("rightsize_session", "test-token")
    return c


def post(c: TestClient, url: str, data: dict, **kw):
    return c.post(url, data=data, headers={"origin": HOST}, follow_redirects=False, **kw)


@pytest.fixture(scope="module")
def web(states):
    return client(states[0])


def test_every_page_renders_for_every_state(web, states):
    ids = states[1]
    urls = ["/", "/runs", "/sweeps", "/jobs", "/launch", "/launch?kind=sweep", "/systems", "/api/runs"]
    for state, ident in ids.items():
        if state.startswith("run"):
            urls += [f"/runs/{ident}", f"/runs/{ident}/items?task_type=retrieval",
                     f"/runs/{ident}/items?task_type=retrieval&show=wrong", f"/api/runs/{ident}"]
        else:
            urls.append(f"/sweeps/{ident}")
    urls.append(f"/compare?a={ids['run_ok_small']}&b={ids['run_ok']}")
    urls.append(f"/compare?pick={ids['run_ok']}&pick={ids['run_ok_small']}")
    for url in urls:
        r = web.get(url)
        assert r.status_code == 200, (url, r.text[:300])


def test_item_pages_render_from_the_items_list(web, states):
    run = states[1]["run_ok_small"]
    html = web.get(f"/runs/{run}/items?task_type=retrieval&show=wrong").text
    links = re.findall(r'href="(/runs/[^"]+/item\?[^"]+)"', html)
    assert links
    for link in links[:5]:
        r = web.get(link.replace("&amp;", "&"))
        assert r.status_code == 200 and "Target provision" in r.text


def test_home_names_each_problem_once(web, states):
    html = web.get("/").text
    assert "No signal" in html and "Interrupted" in html and "Stopped early" in html
    assert "1 sweep left no record" in html  # empty sweeps fold into one entry
    assert "Thin evidence" in html


def test_unknown_ids_are_404(web):
    assert web.get("/runs/nope").status_code == 404
    assert web.get("/sweeps/nope").status_code == 404
    assert web.get("/jobs/nope").status_code == 404


class Hostile:
    """A system whose error messages and evidence try to inject markup."""

    def info(self):
        return SystemInfo(name=f"evil{SCRIPT}", task_types=["retrieval"])

    async def run(self, task_type, inputs, model, params):
        if "a" in inputs["query"][:3].lower():
            return Outcome(error=OutcomeError(kind="error", message=f"boom {SCRIPT}"))
        return Outcome(prediction={"hits": [{"citation": f"164.308{SCRIPT}", "text": SCRIPT}]})


def test_system_output_is_always_escaped(tmp_path):
    runs = tmp_path / "runs"
    tasks = load_tasks(ROOT, "m1", "public", ["retrieval"])[:12]
    run_dir = asyncio.run(execute(ROOT, PluginSystem(Hostile()), tasks, "m1", "public", runs_dir=runs))
    c = client(runs)
    pages = [f"/runs/{run_dir.name}", "/", "/runs", f"/runs/{run_dir.name}/items?task_type=retrieval"]
    pages += [f"/runs/{run_dir.name}/item?task={t.id}" for t in tasks[:4]]
    for url in pages:
        html = c.get(url).text
        assert SCRIPT not in html, url
    assert "&lt;script&gt;" in c.get(f"/runs/{run_dir.name}").text


def test_requests_without_the_session_are_refused(states):
    anon = client(states[0], cookie=False)
    assert anon.get("/").status_code == 401
    assert anon.get("/api/runs").status_code == 401
    r = anon.get("/runs?token=test-token", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/runs"
    assert "rightsize_session" in r.headers["set-cookie"] and "samesite=strict" in r.headers["set-cookie"].lower()


def test_foreign_hosts_and_origins_are_refused(states):
    c = client(states[0])
    assert c.get("/", headers={"host": "evil.example:8765"}).status_code == 421
    assert c.post("/launch/plan", data={"system": "ref:bm25"}).status_code == 403  # no Origin
    assert c.post("/launch/plan", data={"system": "ref:bm25"},
                  headers={"origin": "http://evil.example"}).status_code == 403
    assert "frame-ancestors 'none'" in c.get("/").headers["content-security-policy"]


def _form(**kw) -> dict:
    form = {"kind": "run", "system": "plugin:fake_ladder_system:make", "suite": "m1", "split": "public",
            "task_types": "retrieval", "model": "fake-small", "repeats": "1", "concurrency": "2", "limit": "5",
            "stop_after_errors": "5", "ack_unpriced": "on"}
    form.update(kw)
    return form


def test_launch_needs_the_plan_it_showed(plugin_path, tmp_path):
    c = client(tmp_path / "runs")
    r = post(c, "/launch/plan", _form(action="plan"))
    assert r.status_code == 200 and "Ready to launch" in r.text
    plan_id = re.search(r'name="plan_id" value="([0-9a-f]+)"', r.text).group(1)
    stale = post(c, "/launch/start", _form(plan_id=plan_id, limit="6"))  # edited after review
    assert stale.status_code == 409 and "changed since you reviewed it" in stale.text
    blocked = post(c, "/launch/start", _form(plan_id=plan_id, ack_unpriced=""))
    assert blocked.status_code == 409
    ok = post(c, "/launch/start", _form(plan_id=plan_id))
    assert ok.status_code == 303 and ok.headers["location"].startswith("/jobs/")
    job_url = ok.headers["location"]
    for _ in range(150):
        if c.get(f"/api{job_url}").json()["job"]["state"] != "running":
            break
        time.sleep(0.2)
    live = c.get(f"/api{job_url}").json()
    assert live["job"]["state"] == "finished" and live["job"]["launched_by"] == "ui"
    assert live["runs"][0]["health"] == "ok" and live["runs"][0]["progress"]["done"] == 5
    assert c.get(job_url).status_code == 200


def test_pilot_caps_the_plan(plugin_path, tmp_path):
    c = client(tmp_path / "runs")
    r = post(c, "/launch/plan", _form(action="pilot", limit="", max_cost="50", repeats="3"))
    assert "Pilot plan" in r.text
    assert 'name="max_cost" min="0" step="0.01" value="5.0"' in r.text
    assert 'name="limit" min="1" value="20"' in r.text and 'name="repeats" min="1" max="10" value="1"' in r.text


def test_calibrate_from_the_sweep_page_starts_a_job(states, tmp_path):
    import shutil

    runs = tmp_path / "runs"
    shutil.copytree(states[0], runs)
    c = client(runs)
    sweep = states[1]["sweep_complete"]
    r = post(c, f"/sweeps/{sweep}/calibrate", {"threshold:ndcg@10": "0.9", "delta": "0.05", "emit": "on"})
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    argv = c.get(f"/api{r.headers['location']}").json()["job"]["argv"]
    assert argv[0] == "calibrate" and "ndcg@10=0.9" in argv and "--emit" in argv
    off = post(c, f"/sweeps/{sweep}/calibrate", {"threshold:ndcg@10": "0.5", "threshold_off:ndcg@10": "on",
                                                  "delta": "0.03"})
    assert "--threshold" not in c.get(f"/api{off.headers['location']}").json()["job"]["argv"]
    for loc in (r.headers["location"], off.headers["location"]):
        for _ in range(150):
            if c.get(f"/api{loc}").json()["job"]["state"] != "running":
                break
            time.sleep(0.2)
        assert c.get(f"/api{loc}").json()["job"]["state"] == "finished"

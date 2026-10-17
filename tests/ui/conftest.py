import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import ui_states


@pytest.fixture(scope="session")
def states(tmp_path_factory) -> tuple[Path, dict[str, str]]:
    """(runs_dir, {state: id}): every run and sweep state the Workbench shows, built once."""
    runs = tmp_path_factory.mktemp("workbench") / "runs"
    return runs, ui_states.build(runs)


@pytest.fixture()
def index(states):
    from rightsize_core.service.index import Index

    return Index(ROOT, states[0])


@pytest.fixture()
def plugin_path(monkeypatch):
    monkeypatch.setenv("RIGHTSIZE_PLUGIN_PATH", str(ROOT / "tests" / "fixtures"))
    monkeypatch.setenv("RIGHTSIZE_ROOT", str(ROOT))


def ids_of(states) -> dict[str, str]:
    return json.loads(json.dumps(states[1]))

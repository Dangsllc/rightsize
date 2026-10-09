"""Writing, hashing, checking, and loading suites.

Public and dev files are written under data/suites/<suite>/ and hashed into MANIFEST.json.
Held-out files go to the suite's `heldout_dir` (gitignored) with their own manifest there.
Output is byte-stable: rows sorted by id, keys sorted, one JSON object per line.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from rightsize_core.schema import Task


class IntegrityError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_suite_config(root: Path, suite: str) -> dict[str, Any]:
    path = root / "configs" / "suites" / f"{suite}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No suite config at {path}")
    return yaml.safe_load(path.read_text())


def suite_dir(root: Path, suite: str) -> Path:
    return root / "data" / "suites" / suite


def heldout_dir(root: Path, cfg: dict) -> Path:
    return root / cfg.get("heldout_dir", f"heldout/{cfg['suite']}")


def _dump_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_suite(
    root: Path,
    cfg: dict,
    tasks: list[Task],
    docs: dict[str, str] | None = None,
    reports: dict[str, Any] | None = None,
    inputs: list[Path] | None = None,
) -> dict[str, Any]:
    """Write every split and the manifests. `docs` maps a relative path to document text."""
    canary = cfg["canary"]
    bad = [t.id for t in tasks if t.canary != canary]
    if bad:
        raise IntegrityError(f"{len(bad)} tasks lack the suite canary, e.g. {bad[:3]}")
    ids = [t.id for t in tasks]
    if len(ids) != len(set(ids)):
        raise IntegrityError("Duplicate task ids")

    sdir = suite_dir(root, cfg["suite"])
    hdir = heldout_dir(root, cfg)
    for base in (sdir, hdir):  # stale files from an earlier build must not linger in either place
        for old in base.glob("*.jsonl") if base.exists() else []:
            old.unlink()
        for old in (base / "docs").glob("*.md") if (base / "docs").exists() else []:
            old.unlink()

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in sorted(tasks, key=lambda t: t.id):
        grouped[(t.task_type, t.split)].append(t.model_dump(mode="json"))

    public_files: dict[str, str] = {}
    heldout_files: dict[str, str] = {}
    for (task_type, split), rows in sorted(grouped.items()):
        if split == "heldout":
            path = hdir / f"{task_type}.heldout.jsonl"
            _dump_rows(path, rows)
            heldout_files[path.name] = sha256_file(path)
        else:
            path = sdir / f"{task_type}.{split}.jsonl"
            _dump_rows(path, rows)
            public_files[str(path.relative_to(root))] = sha256_file(path)

    for rel, text in sorted((docs or {}).items()):
        split, _, name = rel.partition("/")
        base = hdir if split == "heldout" else sdir
        path = base / "docs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        if split == "heldout":
            heldout_files[f"docs/{name}"] = sha256_file(path)
        else:
            public_files[str(path.relative_to(root))] = sha256_file(path)

    for p in inputs or []:
        public_files[str(p.relative_to(root))] = sha256_file(p)

    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in tasks:
        if t.split != "heldout":  # the public manifest must not depend on private data
            counts[t.task_type][t.split] += 1
    manifest = {
        "suite": cfg["suite"],
        "pack": cfg["pack"],
        "canary": canary,
        "edition": cfg.get("edition"),
        "counts": {k: dict(v) for k, v in sorted(counts.items())},
        "files": dict(sorted(public_files.items())),
    }
    (sdir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if reports:
        (sdir / "BUILD_REPORT.json").write_text(json.dumps(reports, indent=2, sort_keys=True) + "\n")
    if heldout_files:
        hdir.mkdir(parents=True, exist_ok=True)
        hcounts: dict[str, int] = defaultdict(int)
        for t in tasks:
            if t.split == "heldout":
                hcounts[t.task_type] += 1
        (hdir / "MANIFEST.json").write_text(
            json.dumps({"suite": cfg["suite"], "files": heldout_files, "counts": dict(sorted(hcounts.items()))},
                       indent=2, sort_keys=True) + "\n"
        )
    return manifest


def check_suite(root: Path, suite: str) -> list[str]:
    """Return a list of problems; empty means the committed data matches its manifest."""
    cfg = load_suite_config(root, suite)
    sdir = suite_dir(root, suite)
    mpath = sdir / "MANIFEST.json"
    if not mpath.exists():
        return [f"missing {mpath.relative_to(root)}; run `rightsize data build --suite {suite}`"]
    manifest = json.loads(mpath.read_text())
    problems = []
    if manifest.get("canary") != cfg["canary"]:
        problems.append("manifest canary differs from suite config")
    for rel, digest in manifest["files"].items():
        p = root / rel
        if not p.exists():
            problems.append(f"missing file {rel}")
        elif sha256_file(p) != digest:
            problems.append(f"hash mismatch {rel}")
    for p in sdir.glob("*.jsonl"):
        rel = str(p.relative_to(root))
        if rel not in manifest["files"]:
            problems.append(f"unlisted file {rel}")
        if ".heldout." in p.name:
            problems.append(f"held-out file in public data: {rel}")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
            row = json.loads(line)
            if row.get("canary") != cfg["canary"]:
                problems.append(f"{rel}:{i + 1} missing canary")
            if row.get("split") == "heldout":
                problems.append(f"{rel}:{i + 1} held-out row in public file")
    return problems


def load_tasks(
    root: Path, suite: str, split: str = "public", task_types: list[str] | None = None
) -> list[Task]:
    cfg = load_suite_config(root, suite)
    base = heldout_dir(root, cfg) if split == "heldout" else suite_dir(root, suite)
    tasks = []
    for p in sorted(base.glob(f"*.{split}.jsonl")):
        task_type = p.name.split(".")[0]
        if task_types and task_type not in task_types:
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                tasks.append(Task.model_validate_json(line))
    if not tasks:
        raise FileNotFoundError(f"No {split} tasks for suite {suite} under {base}")
    return tasks


def resolve_document(root: Path, cfg: dict, task: Task) -> str:
    """Inline a task's document text if the task stores a reference instead of the text."""
    ref = task.inputs.get("document_ref")
    if not ref:
        return task.inputs.get("document", "")
    base = heldout_dir(root, cfg) if task.split == "heldout" else suite_dir(root, cfg["suite"])
    return (base / "docs" / ref).read_text(encoding="utf-8")

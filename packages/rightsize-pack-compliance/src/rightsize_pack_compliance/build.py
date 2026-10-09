"""Pack entry point for `rightsize data build`: turn sources + frozen seeds into suite files."""

from __future__ import annotations

from pathlib import Path

from rightsize_core.integrity.dataset import write_suite

from rightsize_pack_compliance.gen import retrieval, seed_queries
from rightsize_pack_compliance.gen.corpus import Corpus

RETRIEVAL_SEEDS = "data/seeds/retrieval_queries.v1.jsonl"


def build_suite(root: Path, cfg: dict, allow_missing_seeds: bool = False) -> dict:
    sources = [root / s for s in cfg["sources"]]
    corpus = Corpus.load(sources)
    tasks = []
    reports: dict = {}
    inputs = list(sources)

    store = seed_queries.store_for(root, cfg, corpus)
    seeds = store.load()
    seed_path = root / RETRIEVAL_SEEDS
    if seed_path.exists():
        inputs.append(seed_path)
    has_private = store.private is not None and store.private.exists()
    if not has_private:
        # Public clone: build public/dev only. Held-out tasks need the private seeds.
        seeds = {k: v for k, v in seeds.items() if k[0] not in store.heldout}
    rtasks, rreport = retrieval.build(corpus, cfg, seeds, RETRIEVAL_SEEDS if seeds else None,
                                      include_heldout=has_private)
    missing = [r for r in rreport if not r["ok"]]
    # Slots whose every candidate failed the filters are a result, recorded in the report. A slot
    # with no seed row at all means generation hasn't run, and that blocks the build.
    unseeded = [r for r in missing if r.get("reason") == "no seed"]
    if unseeded and not allow_missing_seeds:
        raise RuntimeError(
            f"{len(unseeded)} retrieval items have no seed (e.g. {unseeded[0]['target']} "
            f"{unseeded[0]['style']}). Generate seeds with "
            "`rightsize data seed` or pass --allow-missing-seeds."
        )
    tasks += rtasks
    reports["retrieval"] = {
        "items": sum(t.split != "heldout" for t in rtasks),
        "missing": [{k: r.get(k) for k in ("target", "style", "reason")} for r in missing],
        "rejected_candidates": sum(len(r.get("rejected", [])) for r in rreport if r["target"] not in store.heldout),
    }

    if "control_classification" in cfg:
        from rightsize_pack_compliance.gen import manuals

        ctasks, docs, creport, cinputs = manuals.build(root, corpus, cfg)
        tasks += ctasks
        reports["control_classification"] = creport
        inputs += cinputs
    else:
        docs = {}

    public_reports = _public_only(reports, store.heldout)
    return write_suite(root, cfg, tasks, docs=docs, reports=public_reports, inputs=inputs)


def _public_only(reports: dict, heldout_targets: set[str]) -> dict:
    """The build report is committed, so it must not describe held-out items."""
    out = dict(reports)
    r = dict(out.get("retrieval", {}))
    r["missing"] = [m for m in r.get("missing", []) if m.get("target") not in heldout_targets]
    out["retrieval"] = r
    if "control_classification" in out:
        c = dict(out["control_classification"])
        c["manuals"] = [m for m in c.get("manuals", []) if m.get("split") != "heldout"]
        c["leaks"] = [lk for lk in c.get("leaks", []) if lk["manual"] in {m["manual"] for m in c["manuals"]}]
        out["control_classification"] = c
    return out

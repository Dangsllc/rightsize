from __future__ import annotations

from rightsize_pack_compliance.gen.text import stable_hash


def assign_splits(keys: list[str], seed: int, ratios: dict[str, float]) -> dict[str, str]:
    """Deterministic split by cluster key. Every item sharing a key lands in the same split."""
    order = sorted(set(keys), key=lambda k: stable_hash(seed, k))
    n = len(order)
    n_public = round(n * ratios["public"])
    n_dev = round(n * ratios["dev"])
    out = {}
    for i, k in enumerate(order):
        out[k] = "public" if i < n_public else "dev" if i < n_public + n_dev else "heldout"
    return out

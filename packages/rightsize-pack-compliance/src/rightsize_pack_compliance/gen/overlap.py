"""Keep generated queries from copying the regulation's wording.

A paraphrase that reuses the rule's words turns retrieval into string matching. A candidate is
rejected if it shares a content-word n-gram with the rule text, if its word overlap with the
title + rule text is too high, or if it uses any content word from the title.
"""

from __future__ import annotations

from dataclasses import dataclass

from rightsize_pack_compliance.gen.text import content_words, jaccard, ngrams


@dataclass
class OverlapCheck:
    ok: bool
    jaccard: float
    shared_ngram: bool
    title_words: list[str]

    @property
    def reason(self) -> str:
        if self.ok:
            return "ok"
        bits = []
        if self.shared_ngram:
            bits.append("shared n-gram")
        if self.title_words:
            bits.append(f"title words {self.title_words}")
        bits.append(f"jaccard={self.jaccard:.2f}")
        return ", ".join(bits)


def check(
    candidate: str,
    title: str,
    rule_text: str,
    allow: set[str],
    max_jaccard: float = 0.25,
    n: int = 4,
) -> OverlapCheck:
    cand = content_words(candidate, allow)
    rule = content_words(rule_text, allow)
    title_cw = set(content_words(title, allow))
    j = jaccard(set(cand), set(rule) | title_cw)
    shared = bool(ngrams(cand, n) & ngrams(rule, n)) if len(cand) >= n else False
    tw = sorted(title_cw & set(cand))
    return OverlapCheck(ok=(j < max_jaccard and not shared and not tw), jaccard=j, shared_ngram=shared, title_words=tw)

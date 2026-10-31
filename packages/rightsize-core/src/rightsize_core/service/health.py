"""Did a run measure anything, and how far can its numbers be trusted?

Health is derived from what a run left on disk, never stored, so it is the same whichever
process launched the run:

* running      the run's heartbeat is fresh
* interrupted  the manifest still says running, but the heartbeat stopped
* failed       it finished, but every call errored (manifest status `failed: all N calls errored`)
* no_signal    it finished, but no task type has a primary metric (e.g. a run recorded before
               Rightsize marked all-error runs failed)
* aborted      a guard stopped it (spend cap, cancel, crash)
* degraded     more than 2% of answers errored or went missing
* ok
* unscored     finished, but it can't be scored here (e.g. its split isn't on this machine)
"""

from __future__ import annotations

import re
from collections import OrderedDict

from rightsize_core.calibration.sweep import MIN_ITEMS
from rightsize_core.schema import is_failed
from rightsize_core.service.models import ErrorGroup, Headline, Progress

STALE_AFTER_S = 30.0
TINY_N = 10

# (pattern over "kind: message", hint). First match wins. Hints say what to check, not what broke.
_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^mapping:", re.IGNORECASE),
     "The response didn't match the task contract. Run `rightsize conformance` on this system."),
    (re.compile(r"^refusal:", re.IGNORECASE),
     "The model declined. Refusals are counted on their own, never as wrong answers."),
    (re.compile(r"^unsupported:", re.IGNORECASE), "The system doesn't accept this task type."),
    (re.compile(r"\b401\b|unauthori[sz]ed|invalid.{0,20}api.?key|authentication", re.IGNORECASE),
     ("The server rejected the credentials. Check that the key's env var (for example LM_API_TOKEN, "
      "GEMINI_API_KEY, ANTHROPIC_API_KEY) is set in .env and belongs to this server.")),
    (re.compile(r"\b403\b|forbidden|permission denied", re.IGNORECASE),
     "The key was accepted but isn't allowed to do this: check the project, billing tier, or model access."),
    (re.compile(r"\b429\b|rate.?limit|quota|resource.?exhausted", re.IGNORECASE),
     "Rate limited. Lower the rung's concurrency, or wait for the quota to reset."),
    (re.compile(r"\b404\b|not found|no such model|model.{0,40}not (?:found|loaded)", re.IGNORECASE),
     ("The endpoint or model wasn't found. Check base_url, and that the model is loaded "
      "(lms load ..., ollama pull ...).")),
    (re.compile(r"connect(?:ion)?\s?(?:refused|error)|connecterror|nodename|name or service not known", re.IGNORECASE),
     "Nothing answered at that address. Is the local server (LM Studio, Ollama) or the API reachable?"),
    (re.compile(r"timeout|timed out", re.IGNORECASE),
     "The call timed out. Long documents on local models may need a longer timeout or a smaller model."),
    (re.compile(r"context.{0,20}(?:length|window)|too long|maximum.{0,20}tokens|n_ctx", re.IGNORECASE),
     ("The prompt didn't fit. Raise the model's context length (lms load --context-length 32768, "
      "or Ollama's num_ctx).")),
]


def hint_for(kind: str, message: str) -> str | None:
    text = f"{kind}: {message}"
    return next((h for p, h in _HINTS if p.search(text)), None)


def _first_line(message: str) -> str:
    """The first line of an error message. Messages are often a repr(), whose newlines are a
    literal backslash-n."""
    text = (message or "").strip()
    return re.split(r"\n|\\n", text, maxsplit=1)[0][:200] if text else ""


def error_groups(rows: list[dict], limit: int = 8) -> list[ErrorGroup]:
    """Failed calls grouped by kind and first line of the message, most frequent first."""
    groups: OrderedDict[tuple[str, str], ErrorGroup] = OrderedDict()
    for row in rows:
        err = (row.get("outcome") or {}).get("error")
        if not err:
            continue
        kind, msg = err.get("kind", "error"), _first_line(err.get("message", ""))
        g = groups.get((kind, msg))
        if g is None:
            groups[(kind, msg)] = ErrorGroup(kind=kind, message=msg, count=1, first_task_id=row["task_id"],
                                             hint=hint_for(kind, err.get("message", "")))
        else:
            g.count += 1
    return sorted(groups.values(), key=lambda g: -g.count)[:limit]


def progress(rows: list[dict], total: int) -> Progress:
    p = Progress(done=len(rows), total=total)
    for row in rows:
        if (row.get("outcome") or {}).get("error"):
            p.errors += 1
        if row.get("cost_usd") is None:
            p.unknown_cost_calls += 1
        else:
            p.spend_usd += row["cost_usd"]
    return p


def headlines(card: dict) -> list[Headline]:
    out = []
    for tt, r in card.get("task_types", {}).items():
        pm_name = r["primary_metric"]
        pm = r["metrics"].get(pm_name)
        n = int(pm["n_clusters"]) if pm else int(next(iter(r["metrics"].values()), {}).get("n_clusters", 0))
        usage = r.get("usage", {})
        cost = usage.get("cost_usd_per_task")
        errs = [m["value"] for k, m in r["metrics"].items() if k.endswith("error_rate")]
        floors = {name: f[pm_name] for name, f in (r.get("floors") or {}).items() if f.get(pm_name) is not None}
        out.append(Headline(
            task_type=tt, metric=pm_name,
            value=pm["value"] if pm else None, lo=pm["lo"] if pm else None, hi=pm["hi"] if pm else None,
            n_clusters=n, low_n=n < MIN_ITEMS,
            tiny_n=bool(pm) and n < TINY_N,
            floors=floors, clears_floors=(pm["lo"] > max(floors.values())) if pm and floors else None,
            cost_per_task=cost, cost_total=usage.get("cost_usd_total"),
            cost_state="unknown" if cost is None else "zero" if cost == 0 else "known",
            calls=int(usage.get("calls", 0)), error_rate=max(errs, default=0.0),
            latency_ms_p50=usage.get("latency_ms_p50"), latency_ms_p95=usage.get("latency_ms_p95"),
            latency_kind=usage.get("latency_kind"),
        ))
    return out


def classify(status: str, card: dict | None, heartbeat_age: float | None, prog: Progress,
             groups: list[ErrorGroup], notes: list[str], unscored_reason: str | None = None) -> tuple[str, str]:
    """(health, one-sentence reason)."""
    top = groups[0] if groups else None
    failed = f"{prog.errors} of {prog.done} calls failed" if prog.done else "no call completed"
    cause = f": {top.kind}, {top.message}" if top else ""
    if status == "running":
        if heartbeat_age is not None and heartbeat_age <= STALE_AFTER_S:
            return "running", f"{prog.done} of {prog.total} calls done"
        return "interrupted", (f"The process stopped after {prog.done} of {prog.total} calls without finishing "
                               "(no heartbeat).")
    abort = next((n for n in notes if n.startswith("aborted")), None)
    if is_failed(status):
        why = status.removeprefix("failed: ")
        stopped = f" The run stopped early ({abort.removeprefix('aborted: ')})." if abort else ""
        return "failed", f"{why[:1].upper()}{why[1:]}{cause}.{stopped}"
    if card is None:
        if status == "aborted":
            why = (abort or "aborted").removeprefix("aborted: ")
            return "aborted", why[:1].upper() + why[1:] + f" after {prog.done} of {prog.total} calls."
        reason = unscored_reason or "This run hasn't been scored."
        return "unscored", reason[:1].upper() + reason[1:]
    if not card.get("task_types") or all(
            r["metrics"].get(r["primary_metric"]) is None for r in card["task_types"].values()):
        why = f"{failed[:1].upper()}{failed[1:]}{cause}."
        return "no_signal", why + (f" ({abort})" if abort else "")
    if status == "aborted":
        why = (abort or "aborted").removeprefix("aborted: ")
        return "aborted", why[:1].upper() + why[1:] + "."
    if card.get("status") == "degraded":
        worst = max((h.error_rate for h in headlines(card)), default=0.0)
        return "degraded", f"{worst:.0%} of answers errored or went missing{cause}."
    return "ok", ""


def caveats(heads: list[Headline], prog: Progress, split: str, health: str) -> list[str]:
    """Sentences anyone relaying these numbers can quote as written."""
    out = []
    if split == "heldout":
        out.append("Held-out split: never export, publish, or share these results or their items.")
    if health in ("running", "interrupted"):
        out.append(f"Incomplete: {prog.done} of {prog.total} calls are in; scores aren't final.")
    if prog.errors:
        out.append(f"{prog.errors} of {prog.done} calls failed. Errors are counted separately, never as wrong answers.")
    for h in heads:
        if h.value is None:
            continue
        if h.tiny_n:
            out.append(f"{h.task_type}: {h.n_clusters} clusters is too few to estimate uncertainty; "
                       "the interval shown is not meaningful.")
        elif h.low_n:
            out.append(f"{h.task_type}: {h.n_clusters} clusters is below {MIN_ITEMS}, so calibrate won't make "
                       "a routing claim from it.")
        if h.cost_state == "unknown":
            out.append(f"{h.task_type}: cost is unknown because the system reported no usage (not $0).")
        elif h.cost_state == "zero":
            out.append(f"{h.task_type}: $0 at the margin; the model is priced at zero (e.g. local hardware).")
    return out

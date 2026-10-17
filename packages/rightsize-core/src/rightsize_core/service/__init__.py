"""One read/query/job layer for every surface: the Workbench, the MCP server, and `--json` output.

Everything here returns pydantic models or plain dicts built from what runs/ already holds, so a
number shown in the browser, relayed by an agent, or printed by the CLI is the same number.
"""

from rightsize_core.service.index import Index, NotFound
from rightsize_core.service.models import Job, Plan, PlanRequest, RunSummary, SweepSummary

__all__ = ["Index", "Job", "NotFound", "Plan", "PlanRequest", "RunSummary", "SweepSummary"]

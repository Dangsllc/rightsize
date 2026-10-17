"""Run one job's CLI command and record how it exited: `python -m rightsize_core.service.jobrunner <job_dir>`.

The runner ignores SIGINT itself (with a handler, not SIG_IGN, which the child would inherit), so
when a job is cancelled the CLI stops cleanly and the runner is still here to write exit.json.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def main(job_dir: Path) -> int:
    signal.signal(signal.SIGINT, lambda *_: None)
    job = json.loads((job_dir / "job.json").read_text())
    proc = subprocess.Popen([sys.executable, "-m", "rightsize_core.cli", *job["argv"]], stdin=subprocess.DEVNULL)
    code = proc.wait()
    (job_dir / "exit.json").write_text(json.dumps(
        {"code": code, "ended_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}))
    return code


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))

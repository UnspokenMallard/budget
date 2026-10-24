#!/Users/anzes/.budget/.venv/bin/python
"""usage-refresh job: refresh usage/status.json (Claude 5h/7d estimate from local
transcripts), then write usage/usage.json. Both run under this interpreter - the
daemon's own. Moved from orchestrator/scripts/ 2026-10-01."""

import sys as _sys
if _sys.version_info < (3, 11):  # /usr/bin/python3 is 3.9 (no tomllib); the daemon runs .venv (3.13)
    raise SystemExit(f"{__file__}: needs Python 3.11+, got {_sys.version.split()[0]}; "
                     "run it with ~/.budget/.venv/bin/python")
import subprocess
import sys
from pathlib import Path

here = Path(__file__).parent
subprocess.run([sys.executable, str(here / "update_status.py")], check=False)
sys.exit(subprocess.run([sys.executable, str(here / "update-usage.py"), *sys.argv[1:]]).returncode)

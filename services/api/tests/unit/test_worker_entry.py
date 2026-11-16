"""The worker process must know every table, or flushing a row with a foreign key fails."""

import subprocess
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]


def test_the_worker_entry_point_registers_every_table():
    # A fresh interpreter: in the test process other imports would already have registered them.
    code = (
        "import app.worker_main\n"
        "from app.core.db import Base\n"
        "tables = Base.metadata.sorted_tables\n"  # raises NoReferencedTableError if one is missing
        "assert 'projects' in Base.metadata.tables\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=API_ROOT, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr[-1500:]

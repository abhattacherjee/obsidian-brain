"""Guards for the `tests-passed` aggregator job in .github/workflows/ci.yml (#505).

`develop` requires `tests-passed` instead of each test job, so the matrix
names (`timed-tests (3.12, 1)` and so on) never sit in branch protection.
These tests keep the aggregator honest:

- its `needs:` lists every test job, so a new job cannot merge unchecked;
- its `if:` keeps `always()`: GitHub skips a job whose need failed unless
  it has `always()`, and a skipped required check counts as passing;
- its script passes only when every need succeeded.

The workflow is read as text (stdlib only, no YAML parser).
"""

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

CI_YML = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
AGGREGATOR = "tests-passed"
# PR jobs that are not test jobs, so the aggregator does not need them.
NOT_TEST_JOBS = {"secret-scan", "changelog-check", AGGREGATOR}

_REQUIRES_BASH = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _jobs():
    """Return {job name: list of its lines} for the top-level `jobs:` map."""
    lines = CI_YML.read_text(encoding="utf-8").splitlines()
    start = lines.index("jobs:") + 1
    jobs, name = {}, None
    for line in lines[start:]:
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            name = m.group(1)
            jobs[name] = []
        elif name is not None:
            jobs[name].append(line)
    return jobs


def _field(job_lines, key):
    for line in job_lines:
        m = re.match(rf"^    {key}:\s*(.*)$", line)
        if m:
            return m.group(1).strip()
    return None


def _needs(job_lines):
    raw = _field(job_lines, "needs")
    assert raw and raw.startswith("[") and raw.endswith("]"), raw
    return {n.strip() for n in raw[1:-1].split(",") if n.strip()}


def _run_script(job_lines):
    """The aggregator's single `run: |` block, dedented."""
    for i, line in enumerate(job_lines):
        if re.match(r"^\s+run: \|\s*$", line):
            indent = len(line) - len(line.lstrip()) + 2
            body = []
            for nxt in job_lines[i + 1:]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) < indent:
                    break
                body.append(nxt)
            return textwrap.dedent("\n".join(body))
    raise AssertionError("no run: | block in the aggregator job")


def test_aggregator_needs_every_pr_test_job():
    jobs = _jobs()
    pr_jobs = {
        name
        for name, body in jobs.items()
        if "github.event_name == 'pull_request'" in (_field(body, "if") or "")
    }
    assert _needs(jobs[AGGREGATOR]) == pr_jobs - NOT_TEST_JOBS


def test_aggregator_runs_always_on_pull_requests():
    cond = _field(_jobs()[AGGREGATOR], "if")
    assert cond is not None
    assert "always()" in cond
    assert "github.event_name == 'pull_request'" in cond


@_REQUIRES_BASH
@pytest.mark.parametrize(
    "results, code",
    [
        ("success success success success success", 0),
        ("success", 0),
        ("success failure success success success", 1),
        ("success skipped success success success", 1),
        ("cancelled success success success success", 1),
        ("", 1),
    ],
)
def test_aggregator_script_passes_only_when_every_need_succeeded(results, code):
    script = _run_script(_jobs()[AGGREGATOR])
    proc = subprocess.run(
        ["bash", "-c", script],
        env={"RESULTS": results, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == code, proc.stdout + proc.stderr

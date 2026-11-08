"""Guards for the `tests-passed` aggregator job in .github/workflows/ci.yml (#505).

`develop` requires `tests-passed` instead of each test job, so the matrix
names (`timed-tests (3.12, 1)` and so on) never sit in branch protection.
These tests keep the aggregator honest:

- its `needs:` lists every test job, so a new job cannot merge unchecked;
- its `if:` keeps `always()`: GitHub skips a job whose need failed unless
  it has `always()`, and a skipped required check counts as passing;
- it has one step, whose env is exactly the join of every need's result;
- `pull_request` has no `paths` filter, or the workflow may never start and
  the required check stays pending;
- its script passes only when every need succeeded.

PyYAML is a test-only dependency (plugin code stays stdlib-only). CI installs
it in the pip step of the pytest jobs. The workflow used to be read with a
hand-written text parser, and reviews kept finding ways round it (flow-style
`env: {RESULTS: success}`, a quoted `'RESULTS':` key, `on:` below `jobs:`).
A real YAML parser closes that whole class.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only where PyYAML is missing
    yaml = None

CI_YML = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
AGGREGATOR = "tests-passed"
# Every job in ci.yml is a test job unless named here, so a new job (with or
# without an `if:`) must be added to the aggregator's `needs:` or to this set.
NOT_TEST_JOBS = {"secret-scan", "changelog-check", "detect-release-merge", AGGREGATOR}
EXPECTED_RESULTS_ENV = {"RESULTS": "${{ join(needs.*.result, ' ') }}"}

_REQUIRES_BASH = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
_REQUIRES_YAML = pytest.mark.skipif(yaml is None, reason="needs PyYAML")


def _workflow():
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))


def _aggregator(wf):
    return wf["jobs"][AGGREGATOR]


def _triggers(wf):
    # PyYAML follows YAML 1.1, which reads the key `on` as boolean True.
    return wf.get("on", wf.get(True))


def test_yaml_is_installed_in_ci():
    """CI must never skip the workflow guards because PyYAML is missing."""
    if os.environ.get("CI") and yaml is None:
        pytest.fail("PyYAML is missing in CI: add it to the workflow's pip install step")


@_REQUIRES_YAML
def test_aggregator_needs_every_pr_test_job():
    jobs = _workflow()["jobs"]
    # A renamed or deleted entry must fail here, not silently shrink the set.
    assert NOT_TEST_JOBS <= set(jobs), NOT_TEST_JOBS - set(jobs)
    test_jobs = set(jobs) - NOT_TEST_JOBS
    assert test_jobs, "no test jobs found"
    needs = jobs[AGGREGATOR]["needs"]
    assert isinstance(needs, list), needs
    assert set(needs) == test_jobs


@_REQUIRES_YAML
def test_aggregator_runs_always_on_pull_requests():
    agg = _aggregator(_workflow())
    assert agg["if"] == "always() && github.event_name == 'pull_request'"


@_REQUIRES_YAML
def test_aggregator_collects_every_need_result():
    wf = _workflow()
    agg = _aggregator(wf)
    # A workflow- or job-level RESULTS would leak into the step.
    assert "RESULTS" not in (wf.get("env") or {}), wf.get("env")
    assert "RESULTS" not in (agg.get("env") or {}), agg.get("env")
    # One step only, so a second step cannot replace the gate.
    steps = agg["steps"]
    assert len(steps) == 1, steps
    assert steps[0].get("env") == EXPECTED_RESULTS_ENV, steps[0].get("env")
    assert isinstance(steps[0].get("run"), str), steps[0]


@_REQUIRES_YAML
def test_aggregator_cannot_pass_without_running_its_check():
    # Each of these lets the job end green while the check never fails it:
    # a skipped step, an ignored failure, or a shell that does not run bash.
    agg = _aggregator(_workflow())
    step = agg["steps"][0]
    assert "continue-on-error" not in agg, agg
    assert "continue-on-error" not in step, step
    assert "if" not in step, step
    assert step.get("shell", "bash") == "bash", step


@_REQUIRES_YAML
def test_pull_requests_have_no_paths_filter():
    # A workflow that never starts leaves a required check pending (#505).
    triggers = _triggers(_workflow())
    assert "pull_request" in triggers, triggers
    pr = triggers["pull_request"]
    if isinstance(pr, dict):
        assert "paths" not in pr, pr
        assert "paths-ignore" not in pr, pr


@_REQUIRES_BASH
@_REQUIRES_YAML
@pytest.mark.parametrize(
    "results, code, message",
    [
        ("success success success success success", 0, "all 5 test jobs succeeded"),
        ("success", 0, "all 1 test jobs succeeded"),
        ("success failure success success success", 1, "a test job ended 'failure'"),
        ("success skipped success success success", 1, "a test job ended 'skipped'"),
        ("cancelled success success success success", 1, "a test job ended 'cancelled'"),
        # The message matters, not just the exit code: macOS bash 3.2 exits 1
        # on an empty array under `set -u` even without the guard, while
        # bash 5 on the CI runner would exit 0.
        ("", 1, "no test job results"),
    ],
)
def test_aggregator_script_passes_only_when_every_need_succeeded(results, code, message):
    script = _aggregator(_workflow())["steps"][0]["run"]
    proc = subprocess.run(
        ["bash", "-c", script],
        env={"RESULTS": results, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == code, proc.stdout + proc.stderr
    assert message in proc.stdout, proc.stdout + proc.stderr

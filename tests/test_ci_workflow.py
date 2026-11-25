"""Guards for the `tests-passed` aggregator job in .github/workflows/ci.yml (#505).

`develop` requires `tests-passed` instead of each test job, so the matrix
names (`timed-tests (3.12, 1)` and so on) never sit in branch protection.
These tests keep the aggregator honest:

- its `needs:` lists every test job, so a new job cannot merge unchecked;
- its `if:` keeps `always()`: GitHub skips a job whose need failed unless
  it has `always()`, and a skipped required check counts as passing;
- harden-repo's draft gate may AND a draft condition onto each `if:`, and
  then `ready_for_review` must start a run (#532);
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

import copy
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
    # A needed job with continue-on-error reports `success` when it fails,
    # so the aggregator would stay green.
    for name in test_jobs:
        assert "continue-on-error" not in jobs[name], name


AGGREGATOR_BASE_IF = "always() && github.event_name == 'pull_request'"
DRAFT_FIELD = "github.event.pull_request.draft"
# A PR opened as a draft gets skipped runs; marking it ready must start a
# new one, or the skipped (and so "passing") required check would stand.
READY_TYPES = {"opened", "synchronize", "reopened", "ready_for_review"}


def _split_draft_gate(expr):
    """(base, gate) for harden-repo's `(<base>) && (<gate>)` draft-gate
    rewrite (v1.5.0, #532), or (expr, None) for an ungated `if:`. The base
    is what #505 guards; the gate may only add the draft condition."""
    expr = expr.strip()
    if expr.startswith("(") and expr.endswith(")") and ") && (" in expr:
        base, gate = expr[1:-1].split(") && (", 1)
        return base, gate
    return expr, None


# The only gates that skip drafts and nothing else: what harden-repo v1.5.0's
# doctor writes, and the bare draft test. Anything else is a reason to look.
_ALLOWED_GATES = (
    f"github.event_name != 'pull_request' || {DRAFT_FIELD} == false",
    f"{DRAFT_FIELD} == false",
)


def _draft_gate_problem(gate):
    """None for a gate that only skips drafts, else the reason it is not one.
    An allowlist: whitespace collapsed and one pair of outer parentheses
    stripped, the gate must equal an allowed text. A denylist missed
    `... && false` and `... || true`."""
    flat = " ".join(str(gate).split())
    if flat.startswith("(") and flat.endswith(")"):
        flat = flat[1:-1].strip()
    if flat in _ALLOWED_GATES:
        return None
    return f"the gate is not one of {_ALLOWED_GATES}: {flat}"


@_REQUIRES_YAML
def test_aggregator_runs_always_on_pull_requests():
    """The #505 condition stays whole. The draft gate may be ANDed on, but
    only as a condition that skips drafts."""
    base, gate = _split_draft_gate(_aggregator(_workflow())["if"])
    assert base == AGGREGATOR_BASE_IF, base
    if gate is not None:
        assert _draft_gate_problem(gate) is None, _draft_gate_problem(gate)


@_REQUIRES_YAML
def test_every_draft_gate_only_skips_drafts():
    """Every gated job, not just the aggregator, gets the same check."""
    gated = {name: job["if"] for name, job in _workflow()["jobs"].items()
             if DRAFT_FIELD in str(job.get("if", ""))}
    if not gated:
        pytest.skip("no draft gate in ci.yml")
    for name, expr in gated.items():
        base, gate = _split_draft_gate(expr)
        assert gate is not None, (name, expr)
        assert _draft_gate_problem(gate) is None, (name, _draft_gate_problem(gate))
        assert base.strip() not in ("", "true", "always()"), (name, expr)


def _ready_trigger_problem(wf):
    """None when every draft-gated job comes with the READY_TYPES triggers
    (or no job is gated), else the reason. Every draft-gated job is skipped
    on a draft, and GitHub counts a skipped required check as passing. Only a
    run on `ready_for_review` replaces it before the PR can merge."""
    gated = sorted(name for name, job in wf["jobs"].items()
                   if DRAFT_FIELD in str(job.get("if", "")))
    if not gated:
        return None
    pull = (_triggers(wf) or {}).get("pull_request") or {}
    types = set(pull.get("types") or [])
    if not READY_TYPES <= types:
        return f"{gated} are draft-gated but pull_request types are {sorted(types)}"
    return None


@_REQUIRES_YAML
def test_a_draft_gate_comes_with_the_ready_for_review_trigger():
    wf = _workflow()
    if not any(DRAFT_FIELD in str(job.get("if", "")) for job in wf["jobs"].values()):
        pytest.skip("no draft gate in ci.yml")
    assert _ready_trigger_problem(wf) is None, _ready_trigger_problem(wf)


@_REQUIRES_YAML
def test_a_missing_ready_for_review_is_caught():
    """Mutation control: drop `ready_for_review` and the real check must fail."""
    wf = copy.deepcopy(_workflow())
    if not any(DRAFT_FIELD in str(job.get("if", "")) for job in wf["jobs"].values()):
        pytest.skip("no draft gate in ci.yml")
    pull = _triggers(wf)["pull_request"]
    pull["types"] = [t for t in pull.get("types") or [] if t != "ready_for_review"]
    assert _ready_trigger_problem(wf) is not None


def test_the_draft_gate_split_keeps_the_base():
    gated = ("(always() && github.event_name == 'pull_request') && "
             "(github.event_name != 'pull_request' || github.event.pull_request.draft == false)")
    assert _split_draft_gate(gated)[0] == AGGREGATOR_BASE_IF
    assert _split_draft_gate(AGGREGATOR_BASE_IF) == (AGGREGATOR_BASE_IF, None)
    # A rewrite that drops always() from the base is not the #505 base.
    assert _split_draft_gate(
        "(github.event_name == 'pull_request') && (github.event.pull_request.draft == false)"
    )[0] != AGGREGATOR_BASE_IF


REAL_DOCTOR_GATE = "github.event_name != 'pull_request' || github.event.pull_request.draft == false"


@pytest.mark.parametrize("gate", [
    REAL_DOCTOR_GATE,
    "github.event.pull_request.draft ==\n  false",
    "(github.event.pull_request.draft == false)",
])
def test_a_draft_gate_that_skips_drafts_is_accepted(gate):
    assert _draft_gate_problem(gate) is None


@pytest.mark.parametrize("gate", [
    "github.event.pull_request.draft == true",
    "github.event.pull_request.draft != false",
    # Each bad part next to the good one: only an exact match can reject these.
    "github.event.pull_request.draft == false || github.event.pull_request.draft == true",
    "github.event.pull_request.draft == false || github.event.pull_request.draft != false",
    "github.event_name != 'pull_request' || github.event.pull_request.draft == true",
    "!github.event.pull_request.draft == false",
    "always() || github.event.pull_request.draft == false",
    "success() && github.event.pull_request.draft == false",
    "github.event_name == 'pull_request'",
    "github.event.pull_request.draft == false && false",
    "github.event.pull_request.draft == false || true",
    "(github.event.pull_request.draft == false) || true",
    f"{REAL_DOCTOR_GATE} && false",
    f"{REAL_DOCTOR_GATE} || true",
])
def test_a_reversed_or_widened_draft_gate_is_rejected(gate):
    assert _draft_gate_problem(gate) is not None


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
    # A `defaults: run: shell:` at job or workflow level would replace bash.
    assert "defaults" not in agg, agg.get("defaults")
    assert "defaults" not in _workflow(), _workflow().get("defaults")


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


# The jobs that run the skill-block harness (GH #526). Each must install bwrap,
# lift Ubuntu's AppArmor user-namespace limit before pytest, and run pytest with
# OB_REQUIRE_SANDBOX=1, so a missing sandbox fails the job instead of skipping.
SANDBOX_JOBS = ("python-tests", "python-tests-py39")


def _harness_job_problems(workflow, job):
    """What is wrong with how ``job`` installs and requires the sandbox."""
    problems = []
    spec = workflow["jobs"][job]
    steps = spec["steps"]
    runs = [str(step.get("run", "")) for step in steps]
    install = next(
        (i for i, run in enumerate(runs) if "apt-get install" in run and "bubblewrap" in run), None
    )
    pytest_steps = [i for i, run in enumerate(runs) if "pytest tests/" in run]
    if install is None:
        return ["no bubblewrap install step"]
    if "kernel.apparmor_restrict_unprivileged_userns=0" not in runs[install]:
        problems.append("the install step does not lift the AppArmor limit")
    if not pytest_steps:
        problems.append("no pytest step")
    for i in pytest_steps:
        # Strictly before: an install and pytest in one step has no ordering
        # the check can see, and a failed install would not stop the pytest.
        if i <= install:
            problems.append(f"step {i} does not run pytest in a step after the install")
        if steps[i].get("env", {}).get("OB_REQUIRE_SANDBOX") != "1":
            problems.append(f"step {i} does not set OB_REQUIRE_SANDBOX=1")
    # Any value but a literal false could pass the job on a failure (`true`,
    # `"true"`, an expression).
    for name, holder in [("job", spec)] + [
        (f"step {i}", steps[i]) for i in [install, *pytest_steps]
    ]:
        if holder.get("continue-on-error", False) is not False:
            problems.append(f"{name} has continue-on-error")
    return problems


@_REQUIRES_YAML
@pytest.mark.parametrize("job", SANDBOX_JOBS)
def test_harness_jobs_install_and_require_the_sandbox(job):
    assert _harness_job_problems(_workflow(), job) == [], job


@_REQUIRES_YAML
@pytest.mark.parametrize("job", SANDBOX_JOBS)
@pytest.mark.parametrize("where", ["job", "install", "pytest"])
def test_continue_on_error_on_a_harness_job_is_caught(job, where):
    """Fail-first: the same check on an in-memory copy with `continue-on-error: true`."""
    workflow = copy.deepcopy(_workflow())
    spec = workflow["jobs"][job]
    steps = spec["steps"]
    holder = {
        "job": spec,
        "install": next(s for s in steps if "bubblewrap" in str(s.get("run", ""))),
        "pytest": next(s for s in steps if "pytest tests/" in str(s.get("run", ""))),
    }[where]
    assert _harness_job_problems(workflow, job) == []
    holder["continue-on-error"] = True
    assert _harness_job_problems(workflow, job) != [], (job, where)


@_REQUIRES_YAML
@pytest.mark.parametrize("job", SANDBOX_JOBS)
def test_an_install_and_pytest_in_one_step_is_caught(job):
    """Fail-first: merge the install commands and pytest into one step."""
    workflow = copy.deepcopy(_workflow())
    steps = workflow["jobs"][job]["steps"]
    install = next(s for s in steps if "bubblewrap" in str(s.get("run", "")))
    pytest_step = next(s for s in steps if "pytest tests/" in str(s.get("run", "")))
    assert _harness_job_problems(workflow, job) == []
    install["run"] = install["run"] + "\n" + pytest_step["run"]
    install["env"] = {"OB_REQUIRE_SANDBOX": "1"}
    steps.remove(pytest_step)
    problems = _harness_job_problems(workflow, job)
    assert any("after the install" in problem for problem in problems), problems

"""Container stdout stays inside OPERATIONAL_MAX.

The kubelet keeps each container's stdout until its pod is removed, so the
time bound on it is how often every pod is replaced:
``k8s/cron-log-restart.yaml`` restarts every Deployment weekly, and Job pods
are deleted by ``ttlSecondsAfterFinished``. docs/data-retention.md states the
bound; these tests pin the manifests to it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from smarter_dev.shared.retention_policy import OPERATIONAL_MAX

REPO_ROOT = Path(__file__).resolve().parents[1]
K8S = REPO_ROOT / "k8s"
RESTART_MANIFEST = K8S / "cron-log-restart.yaml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yaml"
RETENTION_DOC = REPO_ROOT / "docs" / "data-retention.md"

# Deployed from the proactive-agent repo into the same namespace.
EXTERNAL_DEPLOYMENTS = {"smarter-dev-proactive-agent"}

STATED_BOUND = timedelta(days=8)
STATED_BOUND_ONE_MISSED = timedelta(days=15)


def max_cron_gap(schedule: str) -> timedelta:
    """The longest time between two firings of a daily or weekly ``schedule``.

    Only a fixed minute and hour on every day or on listed weekdays is
    understood; anything else raises rather than guess at a bound.
    """
    minute, hour, day_of_month, month, day_of_week = schedule.split()
    if not (minute.isdigit() and hour.isdigit()):
        raise ValueError(f"minute and hour must be fixed: {schedule!r}")
    if day_of_month != "*" or month != "*":
        raise ValueError(f"day of month and month must be '*': {schedule!r}")
    if day_of_week == "*":
        return timedelta(days=1)
    days = sorted({int(day) % 7 for day in day_of_week.split(",")})
    gaps = [
        (later - earlier) % 7 or 7 for earlier, later in zip(days, days[1:] + days[:1])
    ]
    return timedelta(days=max(gaps))


def worst_case_age(cron: dict, missed_runs: int = 0) -> timedelta:
    """How old a line can get before the restart that removes it finishes."""
    spec = cron["spec"]
    gap = max_cron_gap(spec["schedule"])
    late_start = timedelta(seconds=spec["startingDeadlineSeconds"])
    run = timedelta(seconds=spec["jobTemplate"]["spec"]["activeDeadlineSeconds"])
    return gap * (1 + missed_runs) + late_start + run


def manifests() -> list[tuple[Path, dict]]:
    return [
        (path, doc)
        for path in sorted(K8S.glob("*.yaml"))
        for doc in yaml.safe_load_all(path.read_text())
        if doc
    ]


def restart_docs() -> dict[str, dict]:
    return {
        doc["kind"]: doc
        for doc in yaml.safe_load_all(RESTART_MANIFEST.read_text())
        if doc
    }


def deployment_names() -> set[str]:
    return {
        doc["metadata"]["name"] for _, doc in manifests() if doc["kind"] == "Deployment"
    }


def restart_steps(cron: dict) -> list[tuple[str, str]]:
    """(verb, deployment) for each kubectl step, in the order they run."""
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    steps = []
    for container in pod.get("initContainers", []) + pod["containers"]:
        args = container["args"]
        assert args[0] == "rollout", container["name"]
        assert args[args.index("-n") + 1] == "smarter-dev", container["name"]
        steps.append((args[1], args[2].removeprefix("deployment/")))
    return steps


@pytest.fixture(scope="module")
def cron() -> dict:
    return restart_docs()["CronJob"]


class TestScheduleBound:
    def test_weekly_restart_keeps_lines_under_the_stated_bound(self, cron):
        assert max_cron_gap(cron["spec"]["schedule"]) == timedelta(days=7)
        assert worst_case_age(cron) <= STATED_BOUND < OPERATIONAL_MAX

    def test_one_failed_run_still_stays_inside_operational_max(self, cron):
        assert (
            worst_case_age(cron, missed_runs=1)
            < STATED_BOUND_ONE_MISSED
            < OPERATIONAL_MAX
        )

    def test_runs_in_utc_one_at_a_time(self, cron):
        assert cron["spec"]["timeZone"] == "UTC"
        assert cron["spec"]["concurrencyPolicy"] == "Forbid"

    def test_the_doc_states_the_bound(self):
        doc = RETENTION_DOC.read_text()
        assert "Known exceptions" not in doc
        assert "no fixed time limit" not in doc
        assert "`k8s/cron-log-restart.yaml`" in doc
        assert "each Sunday at 09:00 UTC" in doc
        assert (
            f"at most {STATED_BOUND.days} days, and under {STATED_BOUND_ONE_MISSED.days} days"
            in doc
        )


class TestMaxCronGap:
    """Negative controls: the parser is what stands between a slipped
    schedule and a passing test."""

    @pytest.mark.parametrize(
        ("schedule", "gap"),
        [
            ("0 9 * * 0", 7),
            ("0 9 * * *", 1),
            ("0 9 * * 0,3", 4),
            ("0 9 * * 6,0", 6),
            ("0 9 * * 7", 7),
        ],
    )
    def test_gaps(self, schedule, gap):
        assert max_cron_gap(schedule) == timedelta(days=gap)

    @pytest.mark.parametrize(
        "schedule",
        ["0 9 1 * *", "0 9 * 1 0", "*/5 9 * * 0", "0 */6 * * 0", "0 9 */2 * *"],
    )
    def test_anything_it_cannot_bound_is_refused(self, schedule):
        with pytest.raises(ValueError):
            max_cron_gap(schedule)

    def test_a_late_start_or_long_run_counts_against_the_bound(self, cron):
        slow = {
            "spec": {
                **cron["spec"],
                "startingDeadlineSeconds": int(timedelta(days=1).total_seconds()),
            }
        }
        assert worst_case_age(slow) > STATED_BOUND


class TestRestartCoversEveryDeployment:
    def test_every_deployment_is_restarted_then_waited_on(self, cron):
        expected = deployment_names() | EXTERNAL_DEPLOYMENTS
        steps = restart_steps(cron)
        restarted = [name for verb, name in steps if verb == "restart"]
        assert sorted(restarted) == sorted(expected)
        assert steps == [
            (verb, name) for name in restarted for verb in ("restart", "status")
        ]

    def test_the_external_deployment_goes_last(self, cron):
        assert restart_steps(cron)[-1] == ("status", "smarter-dev-proactive-agent")

    def test_the_role_names_exactly_those_deployments_and_verbs(self, cron):
        role = restart_docs()["Role"]
        (rule,) = role["rules"]
        assert rule["apiGroups"] == ["apps"]
        assert rule["resources"] == ["deployments"]
        assert sorted(rule["resourceNames"]) == sorted(
            name for verb, name in restart_steps(cron) if verb == "restart"
        )
        assert sorted(rule["verbs"]) == ["get", "list", "patch", "watch"]

    def test_the_job_runs_as_the_restart_service_account(self, cron):
        docs = restart_docs()
        pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == docs["ServiceAccount"]["metadata"]["name"]
        assert docs["RoleBinding"]["subjects"][0]["name"] == pod["serviceAccountName"]
        assert (
            docs["RoleBinding"]["roleRef"]["name"] == docs["Role"]["metadata"]["name"]
        )

    def test_the_deploy_workflow_applies_it(self):
        assert (
            "kubectl apply -f $GITHUB_WORKSPACE/k8s/cron-log-restart.yaml"
            in DEPLOY_WORKFLOW.read_text()
        )


class TestJobPodsAreDeleted:
    def test_every_job_pod_goes_within_a_day_of_finishing(self):
        jobs = [
            (path, doc)
            for path, doc in manifests()
            if doc["kind"] in {"Job", "CronJob"}
        ]
        assert jobs
        for path, doc in jobs:
            spec = (
                doc["spec"]["jobTemplate"]["spec"]
                if doc["kind"] == "CronJob"
                else doc["spec"]
            )
            ttl = spec.get("ttlSecondsAfterFinished")
            assert ttl is not None and ttl <= 86400, path.name

    def test_every_cron_job_run_has_a_deadline(self):
        for path, doc in manifests():
            if doc["kind"] == "CronJob":
                assert "activeDeadlineSeconds" in doc["spec"]["jobTemplate"]["spec"], (
                    path.name
                )

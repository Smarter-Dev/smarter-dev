"""Rollout ordering, failure recovery and admission checks without a cluster."""

import copy
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from scripts import capture_identity_receipts as capture
from scripts import identity_rollout_control as control
from scripts import identity_web_gate as web
from scripts import identity_worker_gate as worker
from scripts import migrate_and_pin_identity as migration
from smarter_dev.web import identity_admission as admission

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "key,phase",
    [(None, "held"), ("short", "held"), ("k" * 32, None), ("k" * 32, "oops")],
)
def test_migration_fails_before_any_command(monkeypatch, key, phase, capsys):
    monkeypatch.delenv("SITE_IDENTITY_FENCE_KEY", raising=False)
    monkeypatch.delenv("SITE_IDENTITY_ROLLOUT_PHASE", raising=False)
    if key:
        monkeypatch.setenv("SITE_IDENTITY_FENCE_KEY", key)
    if phase:
        monkeypatch.setenv("SITE_IDENTITY_ROLLOUT_PHASE", phase)
    monkeypatch.setattr(
        migration, "run_migrations", lambda: pytest.fail("must not migrate")
    )
    assert migration.main() == 1
    assert "k" * 32 not in capsys.readouterr().err


@pytest.mark.parametrize("failure", [None, "migration", "pin"])
@pytest.mark.parametrize("phase", ["held", "complete"])
def test_migration_then_pin_order_and_failure(monkeypatch, failure, phase, capsys):
    monkeypatch.setenv("SITE_IDENTITY_FENCE_KEY", "k" * 32)
    monkeypatch.setenv("SITE_IDENTITY_ROLLOUT_PHASE", phase)
    calls = []
    monkeypatch.setattr(migration, "assert_claims_clear", AsyncMock())

    def migrate():
        calls.append("migration")
        if failure == "migration":
            raise RuntimeError("sensitive detail")

    async def pin(*, allow_initialize):
        calls.append(("pin", allow_initialize))
        if failure == "pin":
            raise RuntimeError("sensitive detail")

    monkeypatch.setattr(migration, "run_migrations", migrate)
    monkeypatch.setattr(migration, "pin_identity_key", pin)
    assert migration.main() == int(failure is not None)
    assert calls == (
        ["migration"]
        if failure == "migration"
        else ["migration", ("pin", phase == "held")]
    )
    assert "sensitive detail" not in capsys.readouterr().err


@pytest.mark.parametrize("entry", [web, worker])
@pytest.mark.parametrize("failure", ["key", "missing", "invalid"])
def test_writer_failures_never_exec(monkeypatch, tmp_path, entry, failure):
    phase = tmp_path / "phase"
    if failure != "missing":
        phase.write_text("oops" if failure == "invalid" else "held")
    monkeypatch.setattr(worker, "PHASE", phase)
    monkeypatch.setattr(worker, "READY", tmp_path / "ready")
    check = AsyncMock(side_effect=RuntimeError() if failure == "key" else None)
    monkeypatch.setattr(entry, "pin_identity_key", check)
    monkeypatch.setattr(
        os, "execv", lambda *a: pytest.fail("writer started on failure")
    )
    assert entry.main() == 1
    assert not worker.READY.exists()


def test_worker_waits_key_readiness_release_and_preserves_command(
    monkeypatch, tmp_path
):
    phase, ready = tmp_path / "phase", tmp_path / "ready"
    phase.write_text("held")
    monkeypatch.setattr(worker, "PHASE", phase)
    monkeypatch.setattr(worker, "READY", ready)
    monkeypatch.setattr(worker, "pin_identity_key", AsyncMock())
    monkeypatch.setattr(worker.sys, "argv", ["gate", "/worker", "--queue", "agents"])
    calls = []

    def wait(_):
        assert ready.exists()
        assert not calls
        phase.write_text("complete")

    monkeypatch.setattr(worker.time, "sleep", wait)
    monkeypatch.setattr(os, "execv", lambda *args: calls.append(args))
    assert worker.main() == 0
    assert calls == [("/worker", ["/worker", "--queue", "agents"])]


@pytest.mark.parametrize(
    "path",
    [
        "/account/delete",
        "/account/delete/",
        "/api/site/privacy/delete-account/123",
        "/api/site/identities/123",
        "//account//delete",
    ],
)
@pytest.mark.parametrize("phase", [None, "held", "bad", "complete"])
async def test_admission_all_hosts_direct_paths(monkeypatch, tmp_path, path, phase):
    file = tmp_path / "phase"
    if phase:
        file.write_text(phase)
    monkeypatch.setattr(admission, "PHASE", file)
    app = AsyncMock()
    sent = []

    async def send(msg):
        sent.append(msg)

    # The gate deliberately does not depend on Host or auth (NodePort/ClusterIP).
    await admission.IdentityAdmissionGate(app)(
        {"type": "http", "path": path, "headers": []}, AsyncMock(), send
    )
    assert app.await_count == (phase == "complete")
    if phase != "complete":
        assert sent[0]["status"] == 503
        assert (b"cache-control", b"private, no-store") in sent[0]["headers"]


async def test_other_routes_and_lifespan_pass_while_held(monkeypatch, tmp_path):
    monkeypatch.setattr(admission, "PHASE", tmp_path / "absent")
    app = AsyncMock()
    for scope in [
        {"type": "http", "path": "/api/health"},
        {"type": "http", "path": "/account"},
        {"type": "lifespan"},
    ]:
        await admission.IdentityAdmissionGate(app)(scope, AsyncMock(), AsyncMock())
    assert app.await_count == 3


class Cluster:
    def __init__(self):
        self.data = {
            "phase": "held",
            "transition-mode": "full-stop",
            "transition-evidence": "review-reference",
            "capture-verified": "true",
            "reconciliation-verified": "true",
        }
        self.deployments = {
            name: {
                "spec": {"replicas": 0},
                "metadata": {"generation": 2},
                "status": {"observedGeneration": 2},
            }
            for name in control.WRITERS
        }
        self.pods = {name: [] for name in control.WRITERS}
        self.calls = []
        self.fail_exec = False

    def command(self, *args):
        self.calls.append(args)
        if args[0] == "get":
            kind, name = args[1:3]
            if kind == "configmap":
                return json.dumps({"data": self.data})
            if kind == "deployment":
                return json.dumps(self.deployments[name])
            return json.dumps({"items": self.pods[args[3].removeprefix("app=")]})
        if args[0] == "exec" and self.fail_exec:
            raise RuntimeError("mismatch")
        return ""

    def ready(self):
        for name in control.WRITERS:
            self.deployments[name]["spec"]["replicas"] = 1
            self.pods[name] = [
                {
                    "metadata": {"name": name + "-new"},
                    "spec": {"containers": [{"image": "approved"}]},
                    "status": {"containerStatuses": [{"ready": True}]},
                }
            ]


def test_preflight_requires_old_pods_gone_and_evidence(monkeypatch):
    cluster = Cluster()
    monkeypatch.setattr(control, "kubectl", cluster.command)
    control.preflight()
    cluster.pods[control.WRITERS[0]] = [{"metadata": {"deletionTimestamp": "now"}}]
    with pytest.raises(RuntimeError):
        control.preflight()
    cluster.pods[control.WRITERS[0]] = []
    cluster.data.pop("capture-verified")
    with pytest.raises(RuntimeError):
        control.preflight()


@pytest.mark.parametrize(
    "failure",
    [None, "key", "old-pod", "not-ready", "image", "reconciliation", "generation"],
)
def test_release_checks_every_writer_before_mutating(monkeypatch, failure):
    cluster = Cluster()
    cluster.ready()
    monkeypatch.setattr(control, "kubectl", cluster.command)
    if failure == "key":
        cluster.fail_exec = True
    elif failure == "old-pod":
        cluster.pods[control.WRITERS[1]].append(
            copy.deepcopy(cluster.pods[control.WRITERS[1]][0])
        )
    elif failure == "not-ready":
        cluster.pods[control.WRITERS[1]][0]["status"]["containerStatuses"][0][
            "ready"
        ] = False
    elif failure == "image":
        cluster.pods[control.WRITERS[1]][0]["spec"]["containers"][0]["image"] = "old"
    elif failure == "reconciliation":
        cluster.data["reconciliation-verified"] = "false"
    elif failure == "generation":
        cluster.deployments[control.WRITERS[1]]["status"]["observedGeneration"] = 1
    if failure:
        with pytest.raises(RuntimeError):
            control.release("approved")
        assert not any(c[0] == "patch" for c in cluster.calls)
    else:
        control.release("approved")
        assert sum(c[0] == "exec" for c in cluster.calls) == 2
        assert cluster.calls[-1][0] == "patch"


def test_snapshot_durable_exclusive_and_failed_fsync(monkeypatch, tmp_path):
    target = tmp_path / "capture.json"
    digest = capture.persist_snapshot(target, {"receipts": []})
    assert len(digest) == 64
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        capture.persist_snapshot(target, {})
    monkeypatch.setattr(
        os, "fsync", lambda _: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError):
        capture.persist_snapshot(tmp_path / "failed.json", {})


def test_reconciliation_detects_transition_loss_not_history():
    before = {
        "receipts": [
            {"id": "a", "user_id": "original", "recoverable": True},
            {"id": "h", "user_id": "historical-random", "recoverable": False},
        ]
    }
    after = copy.deepcopy(before)
    after["receipts"][0] = {"id": "a", "user_id": "anonymized", "recoverable": False}
    assert capture.reconcile_captures(before, after) == ["original"]
    after["receipts"].append({"id": "new", "user_id": "unknown", "recoverable": False})
    with pytest.raises(RuntimeError, match="during transition"):
        capture.reconcile_captures(before, after)


def test_manifest_secret_scope_and_workflow_order():
    keyed = []
    for path in (ROOT / "k8s").glob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if not isinstance(doc, dict):
                continue
            template = doc.get("spec", {}).get("template", {}).get("spec", {})
            for container in template.get("containers", []):
                for env in container.get("env", []):
                    if env["name"] == "SITE_IDENTITY_FENCE_KEY":
                        assert env["valueFrom"]["secretKeyRef"] == {
                            "name": "smarter-dev-secrets",
                            "key": "site-identity-fence-key",
                        }
                        keyed.append(path.name)
    assert sorted(keyed) == ["deploy-worker.yaml", "deploy.yaml", "migrate-job.yaml"]
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yaml").read_text())
    steps = workflow["jobs"]["build-and-deploy"]["steps"]
    names = [s["name"] for s in steps]
    assert (
        names.index("Verify identity transition prerequisite")
        < names.index("Run database migrations and trusted identity key pin/check")
        < names.index("Deploy and verify workers")
        < names.index("Verify web deployment")
    )
    assert all(not s.get("continue-on-error") for s in steps)


async def test_trusted_pin_checks_real_registry_and_never_bootstraps_at_startup(
    monkeypatch, test_engine
):
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smarter_dev.shared import database
    from smarter_dev.web import site_identity as service
    from smarter_dev.web.api_native.errors import BotApiException
    from smarter_dev.web.site_identity_models import SiteIdentityKey

    async with test_engine.begin() as conn:
        await conn.run_sync(SiteIdentityKey.__table__.create, checkfirst=True)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(database, "get_session_maker", lambda: maker)
    key = SimpleNamespace(
        site_identity_fence_key="stable-test-fence-key-that-is-long-enough"
    )
    monkeypatch.setattr(service, "get_settings", lambda: key)
    with pytest.raises(RuntimeError, match="not been pinned"):
        await migration.pin_identity_key()
    await migration.pin_identity_key(allow_initialize=True)
    await migration.pin_identity_key()
    key.site_identity_fence_key = "different-test-fence-key-that-is-long-enough"
    with pytest.raises(BotApiException):
        await migration.pin_identity_key()
    key.site_identity_fence_key = None
    with pytest.raises(BotApiException):
        await migration.pin_identity_key()


def test_worker_first_preflight_allows_web_but_never_old_consumers(monkeypatch):
    cluster = Cluster()
    cluster.data["transition-mode"] = "worker-first"
    cluster.deployments[control.WRITERS[0]]["spec"]["replicas"] = 2
    cluster.pods[control.WRITERS[0]] = [{"metadata": {"name": "old-web"}}]
    monkeypatch.setattr(control, "kubectl", cluster.command)
    control.preflight()
    cluster.pods[control.WRITERS[1]] = [{"metadata": {"name": "terminating-worker"}}]
    with pytest.raises(RuntimeError):
        control.preflight()


def test_writer_commands_and_phase_volumes_are_wired():
    for name, module in [
        ("deploy.yaml", "scripts.identity_web_gate"),
        ("deploy-worker.yaml", "scripts.identity_worker_gate"),
    ]:
        spec = yaml.safe_load((ROOT / "k8s" / name).read_text())["spec"]["template"][
            "spec"
        ]
        container = spec["containers"][0]
        assert container["command"][:3] == ["/app/.venv/bin/python", "-m", module]
        assert container["volumeMounts"][0] == {
            "name": "identity-rollout",
            "mountPath": "/var/run/site-identity-rollout",
            "readOnly": True,
        }
        assert spec["volumes"][0]["configMap"] == {
            "name": "site-identity-rollout",
            "items": [{"key": "phase", "path": "phase"}],
        }


def test_control_change_during_checks_never_releases(monkeypatch):
    cluster = Cluster()
    cluster.ready()

    def command(*args):
        result = cluster.command(*args)
        if args[0] == "exec":
            cluster.data["reconciliation-verified"] = "false"
        return result

    monkeypatch.setattr(control, "kubectl", command)
    with pytest.raises(RuntimeError):
        control.release("approved")
    assert not any(c[0] == "patch" for c in cluster.calls)


def test_live_claim_aborts_before_migrations(monkeypatch):
    monkeypatch.setenv("SITE_IDENTITY_FENCE_KEY", "k" * 32)
    monkeypatch.setenv("SITE_IDENTITY_ROLLOUT_PHASE", "held")
    monkeypatch.setattr(
        migration, "assert_claims_clear", AsyncMock(side_effect=RuntimeError())
    )
    monkeypatch.setattr(
        migration, "run_migrations", lambda: pytest.fail("claims still active")
    )
    assert migration.main() == 1


@pytest.mark.parametrize("original_recoverable", [True, False])
def test_reconciliation_rejects_reused_or_previously_anonymized_receipt(
    original_recoverable,
):
    before = {
        "receipts": [{"id": "r", "user_id": "old", "recoverable": original_recoverable}]
    }
    after = {"receipts": [{"id": "r", "user_id": "different", "recoverable": True}]}
    with pytest.raises(RuntimeError, match="identity changed"):
        capture.reconcile_captures(before, after)


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous deployment YAML instead of silently keeping the last key."""

    def construct_mapping(self, node, deep=False):
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in keys:
                raise yaml.constructor.ConstructorError(
                    "while reading rollout YAML",
                    node.start_mark,
                    f"duplicate key: {key}",
                    key_node.start_mark,
                )
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


def test_strict_manifest_loader_detects_duplicate_nested_probe():
    with pytest.raises(
        yaml.constructor.ConstructorError, match="duplicate key: readinessProbe"
    ):
        yaml.load(
            "container:\n  readinessProbe: {}\n  readinessProbe: {}\n",
            Loader=UniqueKeyLoader,
        )


@pytest.mark.parametrize(
    "name",
    [
        "k8s/deploy.yaml",
        "k8s/deploy-worker.yaml",
        "k8s/migrate-job.yaml",
        "k8s/secrets.template.yaml",
        ".github/workflows/deploy.yaml",
    ],
)
def test_rollout_yaml_has_no_duplicate_keys(name):
    list(yaml.load_all((ROOT / name).read_text(), Loader=UniqueKeyLoader))


def test_effective_worker_readiness_requires_verified_key_marker():
    manifest = yaml.safe_load((ROOT / "k8s/deploy-worker.yaml").read_text())
    worker_container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert worker_container["readinessProbe"] == {
        "exec": {"command": ["test", "-f", "/tmp/site-identity-key-ready"]},
        "periodSeconds": 5,
    }

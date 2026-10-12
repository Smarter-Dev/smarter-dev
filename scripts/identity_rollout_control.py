"""Fail-closed Kubernetes checks. Only explicit release mutates the phase.

This does not establish the initial hold: an operator must provide reviewed
transition evidence after admission closure, durable capture and old-pod drain.
Never run against a live cluster without the separate operator window approval.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

NAMESPACE = "smarter-dev"
WRITERS = ("smarter-dev-website", "smarter-dev-agent-worker")
KEY_VERSION = "smarter.dev/identity-key-version"
EXPECTED_COMMANDS = {
    WRITERS[0]: ["/app/.venv/bin/python", "-m", "scripts.identity_web_gate"],
    WRITERS[1]: [
        "/app/.venv/bin/python",
        "-m",
        "scripts.identity_worker_gate",
        "/app/.venv/bin/skrift",
        "workers",
        "run",
        "--queue",
        "agents",
        "--concurrency",
        "8",
    ],
}


def kubectl(*args):
    return subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout


def get(kind, name):
    return json.loads(kubectl("get", kind, name, "-o", "json"))


def control():
    data = get("configmap", "site-identity-rollout").get("data", {})
    if data.get("phase") not in {"held", "complete"}:
        raise RuntimeError("Invalid phase")
    return data


def pods(writer):
    return json.loads(kubectl("get", "pods", "-l", f"app={writer}", "-o", "json"))[
        "items"
    ]


def preflight():
    data = control()
    if data["phase"] == "held":
        if (
            not data.get("transition-evidence")
            or data.get("capture-verified") != "true"
        ):
            raise RuntimeError("Reviewed transition/capture evidence required")
        mode = data.get("transition-mode")
        if mode not in {"worker-first", "full-stop"}:
            raise RuntimeError("Reviewed transition mode required")
        # Worker-first retains normal HTTP service while old web pods enqueue
        # durable receipts. Only the stopped agents consumer can anonymize them.
        required = WRITERS if mode == "full-stop" else (WRITERS[1],)
        for writer in required:
            if get("deployment", writer)["spec"].get("replicas", 1) != 0 or pods(
                writer
            ):
                raise RuntimeError("Old writers have not cleared")
    # phase=complete may only CHECK an existing verifier in the migration Job;
    # it can never bootstrap one, even if an operator sets this prematurely.


def secret_version():
    # Project metadata only; never retrieve/log Secret data in this process.
    version = kubectl(
        "get",
        "secret",
        "smarter-dev-secrets",
        "-o",
        "jsonpath={.metadata.resourceVersion}",
    ).strip()
    if not version:
        raise RuntimeError("Secret version unavailable")
    return version  # opaque equality token, not a timestamp or ordered number


def stamp(manifest_root):
    version = secret_version()
    for filename in ("deploy.yaml", "deploy-worker.yaml", "migrate-job.yaml"):
        path = manifest_root / filename
        source = path.read_text()
        if "<IDENTITY_KEY_VERSION>" not in source:
            raise RuntimeError("Unstaged writer template")
        path.write_text(source.replace("<IDENTITY_KEY_VERSION>", json.dumps(version)))


def writer_evidence(image, version):
    evidence = []
    for writer in WRITERS:
        deployment = get("deployment", writer)
        count = deployment["spec"].get("replicas", 1)
        current = pods(writer)
        if count < 1 or len(current) != count:
            raise RuntimeError("Writer count mismatch")
        if (
            deployment["status"].get("observedGeneration", 0)
            < deployment["metadata"]["generation"]
        ):
            raise RuntimeError("Writer deployment not observed")
        template = deployment["spec"]["template"]
        if template["metadata"].get("annotations", {}).get(KEY_VERSION) != version:
            raise RuntimeError("Deployment Secret generation mismatch")
        for pod in current:
            metadata = pod["metadata"]
            if metadata.get("deletionTimestamp") or not metadata.get("uid"):
                raise RuntimeError("Old or unidentified writer")
            if metadata.get("annotations", {}).get(KEY_VERSION) != version:
                raise RuntimeError("Pod Secret generation mismatch")
            containers = pod["spec"]["containers"]
            if len(containers) != 1 or containers[0]["image"] != image:
                raise RuntimeError("Unexpected writer image")
            container = containers[0]
            desired = template["spec"]["containers"]
            if len(desired) != 1 or any(
                container.get(field) != desired[0].get(field)
                for field in ("image", "command", "readinessProbe", "env")
            ):
                raise RuntimeError("Writer differs from observed deployment template")
            if container.get("command") != EXPECTED_COMMANDS[writer]:
                raise RuntimeError("Writer bypasses startup verification")
            probe = container.get("readinessProbe", {})
            if writer == WRITERS[1]:
                if probe.get("exec", {}).get("command") != [
                    "test",
                    "-f",
                    "/tmp/site-identity-key-ready",
                ]:
                    raise RuntimeError("Worker lacks key-ready probe")
            else:
                http_probe = dict(probe.get("httpGet", {}))
                http_probe.setdefault("scheme", "HTTP")
                if http_probe != {
                    "path": "/api/health",
                    "port": 8000,
                    "scheme": "HTTP",
                }:
                    raise RuntimeError("Web lacks post-startup readiness")
            key_refs = [
                env
                for env in container.get("env", [])
                if env["name"] == "SITE_IDENTITY_FENCE_KEY"
            ]
            if len(key_refs) != 1 or key_refs[0].get("valueFrom", {}).get(
                "secretKeyRef"
            ) != {"name": "smarter-dev-secrets", "key": "site-identity-fence-key"}:
                raise RuntimeError("Writer key source mismatch")
            states = pod.get("status", {}).get("containerStatuses", [])
            if (
                len(states) != 1
                or not states[0].get("ready")
                or not states[0].get("started")
            ):
                raise RuntimeError("Writer not ready")
            state = states[0]
            started = state.get("state", {}).get("running", {}).get("startedAt")
            if (
                not started
                or not state.get("containerID")
                or state.get("restartCount") is None
            ):
                raise RuntimeError("Current container evidence missing")
            ready = [
                condition
                for condition in pod["status"].get("conditions", [])
                if condition.get("type") == "Ready"
                and condition.get("status") == "True"
            ]
            if len(ready) != 1 or datetime.fromisoformat(
                ready[0]["lastTransitionTime"]
            ) < datetime.fromisoformat(started):
                raise RuntimeError("Readiness predates current container")
            evidence.append(
                (
                    writer,
                    deployment["metadata"]["generation"],
                    metadata["uid"],
                    state["containerID"],
                    state["restartCount"],
                    started,
                    ready[0]["lastTransitionTime"],
                )
            )
    return sorted(evidence)


def release(image):
    data = control()
    if data["phase"] != "held" or not data.get("transition-evidence"):
        raise RuntimeError("Release requires reviewed held transition")
    if (
        data.get("capture-verified") != "true"
        or data.get("reconciliation-verified") != "true"
    ):
        raise RuntimeError("Capture/reconciliation evidence required")
    version = secret_version()
    evidence = writer_evidence(image, version)
    # Read the same current container/start/readiness evidence again. A restart,
    # replacement or Secret update requires a new release check, never an exec.
    if writer_evidence(image, version) != evidence or secret_version() != version:
        raise RuntimeError("Writer or Secret changed during release checks")
    # Re-read to catch an operator abort during checks. No automatic restoration
    # is performed on any error; held pods remain held.
    if control() != data:
        raise RuntimeError("Control changed during release checks")
    kubectl(
        "patch",
        "configmap",
        "site-identity-rollout",
        "--type=json",
        "-p",
        json.dumps(
            [
                {"op": "test", "path": "/data", "value": data},
                {"op": "replace", "path": "/data/phase", "value": "complete"},
            ]
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "stamp", "release"))
    parser.add_argument("--image")
    parser.add_argument("--manifest-root", type=Path, default=Path("k8s"))
    args = parser.parse_args()
    try:
        if args.action == "preflight":
            preflight()
        elif args.action == "stamp":
            stamp(args.manifest_root)
        else:
            if not args.image or not args.image.startswith(
                "zzmmrmn/smarter-dev-website:"
            ):
                raise RuntimeError("Exact approved image required")
            release(args.image)
    except Exception:
        print(
            "Identity rollout gate failed; retain the hold and inspect operator evidence.",
            file=sys.stderr,
        )
        return 1
    print("Identity rollout gate succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

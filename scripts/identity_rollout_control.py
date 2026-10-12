"""Fail-closed Kubernetes checks. Only explicit release mutates the phase.

This does not establish the initial hold: an operator must provide reviewed
transition evidence after admission closure, durable capture and old-pod drain.
Never run against a live cluster without the separate operator window approval.
"""

import argparse
import json
import subprocess
import sys

NAMESPACE = "smarter-dev"
WRITERS = ("smarter-dev-website", "smarter-dev-agent-worker")


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


def release(image):
    data = control()
    if data["phase"] != "held" or not data.get("transition-evidence"):
        raise RuntimeError("Release requires reviewed held transition")
    if (
        data.get("capture-verified") != "true"
        or data.get("reconciliation-verified") != "true"
    ):
        raise RuntimeError("Capture/reconciliation evidence required")
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
        for pod in current:
            if pod["metadata"].get("deletionTimestamp"):
                raise RuntimeError("Old writer terminating")
            containers = pod["spec"]["containers"]
            if len(containers) != 1 or containers[0]["image"] != image:
                raise RuntimeError("Unexpected writer image")
            states = pod.get("status", {}).get("containerStatuses", [])
            if len(states) != 1 or not states[0].get("ready"):
                raise RuntimeError("Writer not ready")
            # Independently verify each running writer's environment, not merely
            # the migration Job's secretRef or a health endpoint.
            kubectl(
                "exec",
                pod["metadata"]["name"],
                "--",
                "/app/.venv/bin/python",
                "-c",
                "import asyncio; from scripts.migrate_and_pin_identity import pin_identity_key; "
                "asyncio.run(pin_identity_key())",
            )
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
    parser.add_argument("action", choices=("preflight", "release"))
    parser.add_argument("--image")
    args = parser.parse_args()
    try:
        if args.action == "preflight":
            preflight()
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

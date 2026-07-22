# Kubernetes Deployments: rollout status and rolling updates

Source: https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
Captured through Jina Reader on 2026-07-22. This is a focused snapshot of the
Deployment status, RollingUpdate, and progress-deadline sections.

## Deployment status

A Deployment can be progressing, complete, or failed to progress. It is marked
progressing while creating a new ReplicaSet, scaling the newest ReplicaSet up,
scaling older ReplicaSets down, or making new Pods ready or available. The
Deployment controller records a `Progressing=True` condition with reasons such
as `NewReplicaSetCreated`, `FoundNewReplicaSet`, or `ReplicaSetUpdated`.

A Deployment is complete when all replicas are updated to the requested
version, all replicas are available, and no old replicas remain. Its condition
has `type: Progressing`, `status: "True"`, and `reason:
NewReplicaSetAvailable`. `kubectl rollout status` returns exit code zero after a
successful rollout.

A rollout can become stuck because of insufficient quota, readiness probe
failures, image pull errors, insufficient permissions, limit ranges, or runtime
misconfiguration. `kubectl rollout status` returns a non-zero exit code when a
Deployment exceeds its progression deadline.

## RollingUpdate strategy

`spec.strategy.type` can be `Recreate` or `RollingUpdate`; `RollingUpdate` is
the default. Rolling updates gradually scale old ReplicaSets down and new ones
up. `maxUnavailable` and `maxSurge` control the process.

`maxUnavailable` is the maximum number of Pods that may be unavailable during
an update. It can be an absolute count or a percentage of desired Pods.
Percentages are rounded down. It cannot be zero when `maxSurge` is also zero,
and its default is 25 percent. At 30 percent, an old ReplicaSet can initially be
scaled down to 70 percent of desired Pods; further replacement proceeds as new
Pods become ready.

`maxSurge` is the maximum number of Pods that may be created above the desired
count. It can be an absolute count or percentage. Percentages are rounded up.
It cannot be zero when `maxUnavailable` is also zero, and its default is 25
percent. At 30 percent, old and new non-terminating Pods are kept at no more
than 130 percent of the desired count while replacement proceeds.

Terminating Pods are not counted when Kubernetes calculates
`availableReplicas`, which must remain between `replicas - maxUnavailable` and
`replicas + maxSurge`. Therefore total resources can temporarily exceed
`replicas + maxSurge` until terminating Pods finish their
`terminationGracePeriodSeconds`.

## Progress deadline

`spec.progressDeadlineSeconds` sets how long the controller waits for progress
before reporting that the Deployment has failed progressing. The status then
contains `type: Progressing`, `status: "False"`, and `reason:
ProgressDeadlineExceeded`. The default is 600 seconds, and an explicitly set
value must be greater than `minReadySeconds`.

Exceeding the deadline reports the stalled rollout but does not make the
Deployment controller stop trying; the controller continues retrying the
Deployment. Higher-level orchestrators can observe the condition and act on it.

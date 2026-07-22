# Coroutines and Tasks: cancellation and task groups

Source: https://docs.python.org/3/library/asyncio-task.html
Captured through Jina Reader on 2026-07-22. This is a focused snapshot of the
Task cancellation, Task groups, and Task cancellation-state sections.

## Task cancellation

Tasks can easily and safely be cancelled. When a task is cancelled,
`asyncio.CancelledError` will be raised in the task at the next opportunity.

It is recommended that coroutines use `try/finally` blocks to robustly perform
clean-up logic. If `asyncio.CancelledError` is explicitly caught, it should
generally be propagated when clean-up is complete. `asyncio.CancelledError`
directly subclasses `BaseException`, so most code will not need to be aware of
it.

The asyncio components that enable structured concurrency, such as
`asyncio.TaskGroup` and `asyncio.timeout()`, are implemented using cancellation
internally and might misbehave if a coroutine swallows `CancelledError`.
Similarly, user code should not generally call `uncancel()`. However, when
suppressing `CancelledError` is truly desired, it is necessary to also call
`uncancel()` to completely remove the cancellation state.

## Task groups

Task groups combine a task creation API with a convenient and reliable way to
wait for all tasks in the group to finish. `asyncio.TaskGroup` is an
asynchronous context manager holding a group of tasks. Tasks can be added with
`create_task()`. All tasks are awaited when the context manager exits.

The first time any task in the group fails with an exception other than
`CancelledError`, the remaining tasks in the group are cancelled and no
further tasks can be added. If the body of the `async with` statement is still
active, the task directly containing it is also cancelled. The resulting
`CancelledError` interrupts an `await`, but does not bubble out of the
containing `async with` statement.

After all tasks finish, non-cancellation failures are combined into an
`ExceptionGroup` or `BaseExceptionGroup` and raised. `KeyboardInterrupt` and
`SystemExit` are special: the group still cancels and waits for the remaining
tasks, then re-raises the original base exception instead of grouping it.

If the body of the `async with` statement exits with an exception, that is
treated like a child-task failure: remaining tasks are cancelled and awaited,
and non-cancellation exceptions are grouped. The body exception is also
included unless it is `CancelledError`.

Task groups distinguish the internal cancellation used to wake up `__aexit__`
from cancellation requests made externally. If a group is cancelled externally
while it also needs to raise an `ExceptionGroup`, it calls the parent task's
`cancel()` method so a `CancelledError` is raised at the next `await` and the
external cancellation is not lost. Task groups preserve the cancellation count
reported by `Task.cancelling()`.

## Task cancellation state

Calling `Task.cancel()` arranges for `CancelledError` to be thrown into the
wrapped coroutine on the next event-loop cycle. The coroutine can clean up or
even suppress the exception, so unlike `Future.cancel()`, `Task.cancel()` does
not guarantee that the task will be cancelled. Completely suppressing
cancellation is uncommon and actively discouraged. A coroutine that does so
must call `Task.uncancel()` in addition to catching the exception.

A task is considered cancelled when cancellation was requested and its wrapped
coroutine propagated the `CancelledError`. `Task.uncancel()` decrements the
count of cancellation requests. It is used by asyncio internals and is not
generally expected in end-user code. Once execution of a cancelled task has
completed, further calls to `uncancel()` are ineffective.

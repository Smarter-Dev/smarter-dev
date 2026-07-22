# PostgreSQL transaction isolation

Source: https://www.postgresql.org/docs/current/transaction-iso.html
Captured through Jina Reader on 2026-07-22. This is a focused snapshot of the
Read Committed, Repeatable Read, and Serializable sections.

The SQL standard defines four isolation levels. Serializable is the strictest:
concurrent execution must have the same effect as running transactions one at
a time in some order. PostgreSQL accepts all four standard names, but implements
three distinct levels because Read Uncommitted behaves like Read Committed.
PostgreSQL Repeatable Read also prevents phantom reads, which is stronger than
the minimum required by the standard.

## Read Committed

Read Committed is PostgreSQL's default. A plain `SELECT` sees only data
committed before that query began. It does not see uncommitted data or changes
committed by concurrent transactions while the query is running, but it does
see earlier updates made by its own transaction. Each command gets a new
snapshot, so two successive `SELECT` commands in one transaction can see
different data when another transaction commits between them.

`UPDATE`, `DELETE`, `SELECT FOR UPDATE`, and `SELECT FOR SHARE` search using the
command's starting snapshot. If a target row was already changed by another
transaction, the would-be updater waits. After that transaction ends, the
search condition is re-evaluated against the updated row before proceeding.

These rules let an updating command see an inconsistent snapshot: it may see a
concurrent command's effects on the rows it updates without seeing that
command's effects on other rows. This makes Read Committed unsuitable for some
complex search conditions, although it is fast, simple, and adequate for many
applications. Subsequent commands see newly committed changes; the concern is
that one command is not guaranteed an absolutely consistent database view.

## Repeatable Read

Repeatable Read sees data committed before the transaction began and never sees
changes committed by concurrent transactions during its execution. Its
snapshot is established at the first non-transaction-control statement in the
transaction, rather than at each statement. Successive `SELECT` commands in the
same transaction therefore see the same data.

An attempt to update a row changed since the transaction began waits for the
concurrent updater. If that updater commits, the Repeatable Read transaction is
rolled back with `ERROR: could not serialize access due to concurrent update`.
The application must abort and retry the entire transaction from the beginning.

Repeatable Read gives each transaction a stable view, but that view is not
necessarily consistent with any serial execution of concurrent transactions.
Business rules can still fail without careful explicit locking. PostgreSQL
implements this level using Snapshot Isolation, and applications must be
prepared to retry after serialization failures.

## Serializable

Serializable provides the strictest isolation and emulates serial execution for
all committed transactions. It behaves like Repeatable Read but additionally
monitors for combinations of read/write dependencies that could produce a
result inconsistent with every possible serial ordering. Monitoring adds some
overhead but no blocking beyond Repeatable Read. When a dangerous pattern is
detected, one transaction fails with a serialization failure.

Applications must retry the whole transaction after a serialization failure.
Data read from a permanent table should not be treated as valid until the
reading transaction successfully commits, even for most read-only transactions.
A `SERIALIZABLE READ ONLY DEFERRABLE` transaction is the exception: it waits for
a snapshot known to be safe, so its data is valid as soon as it is read.

PostgreSQL uses predicate locking to identify when a concurrent write would
have affected a previous read. These `SIReadLock` predicate locks do not block
and do not cause deadlocks; they identify dependencies that can produce
serialization anomalies. They may be tuple-, page-, or relation-level and can
remain after commit until overlapping read/write transactions finish.

Serialization failures always use SQLSTATE `40001`. Applications using
Serializable should provide a generalized retry mechanism because it is hard
to predict which transaction will be rolled back. Serializable can still
report unique-constraint violations that would not occur in a truly serial run
if transactions that insert potentially conflicting keys do not all follow a
consistent check-before-insert protocol.

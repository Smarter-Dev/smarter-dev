"""Content for /resources/patterns-of-practice.

The Patterns layer of the resources index. Pairs with /resources/system-architecture
(the What), /resources/infrastructure-hosting (the Where), /resources/software-delivery
(the Shipping), /resources/production-operations (the Keep-it-healthy), and
/resources/agent-engineering-patterns (the Age-of-Agents shapes, kept separate
because they evolve on a different cadence).

Source dossier: docs/patterns-of-practice-research.md.
Flagged URLs to skip: docs/patterns-of-practice-urls-needed.md.
"""

from __future__ import annotations

from datetime import date

from smarter_dev.web.system_architecture_data import (
    ArchCategory,
    ArchResource,
    ArchTool,
    ArchToolResource,
)
from smarter_dev.web.vibe_courses_data import FAQ, Person

_INDEXED = date(2026, 5, 13)


def _r(title, url, source, key, tool_slugs, learning_type, blurb=""):
    return ArchToolResource(
        title=title, url=url, source=source, key=key,
        tool_slugs=tuple(tool_slugs), learning_type=learning_type,
        first_indexed_at=_INDEXED, blurb=blurb,
    )


def _s(title, url, source, key, learning_type, blurb=""):
    return ArchResource(
        title=title, url=url, source=source, key=key,
        learning_type=learning_type, first_indexed_at=_INDEXED, blurb=blurb,
    )


# ─── CATEGORIES ──────────────────────────────────────────────────────────────
#
# Each category lives under one H2 section. The template groups consecutive
# categories with the same `section` value under one H2 heading. Sections,
# in order: Code Patterns, Architecture Patterns, Patterns of Discipline,
# Anti-Patterns.

POP_SECTIONS_BY_SLUG: dict[str, str] = {
    "composition-construction": "Code Patterns",
    "control-flow": "Code Patterns",
    "boundaries-abstraction": "Code Patterns",
    "error-handling-resilience": "Code Patterns",
    "concurrency": "Code Patterns",
    "data-state": "Architecture Patterns",
    "messaging-coordination": "Architecture Patterns",
    "topology": "Architecture Patterns",
    "resilience-at-scale": "Architecture Patterns",
    "evolution-migration": "Architecture Patterns",
    "change-patterns": "Patterns of Discipline",
    "review-verification": "Patterns of Discipline",
    "operational-patterns": "Patterns of Discipline",
    "anti-patterns": "Anti-Patterns",
}


POP_CATEGORIES: list[ArchCategory] = [
    # ── Code Patterns ────────────────────────────────────────────────────────
    ArchCategory(
        slug="composition-construction",
        name="Composition & Construction",
        intro=(
            "Code Patterns. How objects and modules come together. Builder, "
            "Fluent Interface, Dependency Injection, Factory variants, and "
            "Newtype — the moves that decide whether a system feels assembled "
            "or merely concatenated. Useful when construction logic is "
            "obscuring the thing being constructed."
        ),
        tools=(
            ArchTool("builder", "Builder",
                     "https://refactoring.guru/design-patterns/builder",
                     "patterns:tool:builder:home",
                     "Step-by-step construction of a complex object that separates the recipe from the result."),
            ArchTool("fluent-interface", "Fluent Interface",
                     "https://martinfowler.com/bliki/FluentInterface.html",
                     "patterns:tool:fluent-interface:home",
                     "Method chaining that reads like a sentence describing the operation rather than commanding it."),
            ArchTool("dependency-injection", "Dependency Injection",
                     "https://martinfowler.com/articles/injection.html",
                     "patterns:tool:dependency-injection:home",
                     "Hand a component its collaborators instead of letting it build them, so its seams are visible."),
            ArchTool("factory", "Factory Variants",
                     "https://refactoring.com/catalog/replaceConstructorWithFactoryFunction.html",
                     "patterns:tool:factory:home",
                     "Replace a constructor with a function that names the variant being made and hides the constructor's noise."),
            ArchTool("newtype", "Newtype",
                     "https://doc.rust-lang.org/rust-by-example/generics/new_types.html",
                     "patterns:tool:newtype:home",
                     "Wrap a primitive in a single-field type so the type system can stop you from mixing UserId and OrderId."),
        ),
    ),
    ArchCategory(
        slug="control-flow",
        name="Control Flow",
        intro=(
            "Code Patterns. How the next step is chosen. Strategy, Command, "
            "Pipeline/Chain of Responsibility, Middleware, Visitor, and State "
            "Machines turn nested conditionals into named operations. Reach "
            "for them when the if-tree has grown its own gravitational pull."
        ),
        tools=(
            ArchTool("strategy", "Strategy",
                     "https://refactoring.guru/design-patterns/strategy",
                     "patterns:tool:strategy:home",
                     "Swap an algorithm at runtime by hiding the family of behaviors behind a single interface."),
            ArchTool("command", "Command",
                     "https://martinfowler.com/bliki/DecoratedCommand.html",
                     "patterns:tool:command:home",
                     "Wrap an operation as an object so it can be queued, logged, undone, or shipped across a network."),
            ArchTool("pipeline-cor", "Pipeline / Chain of Responsibility",
                     "https://refactoring.guru/design-patterns/chain-of-responsibility",
                     "patterns:tool:pipeline-cor:home",
                     "Hand a request down a sequence of handlers until one accepts it, instead of branching on type."),
            ArchTool("middleware", "Middleware",
                     "https://expressjs.com/en/guide/using-middleware.html",
                     "patterns:tool:middleware:home",
                     "Compose request-handling concerns as a stack of functions that each get a turn before and after the next."),
            ArchTool("visitor", "Visitor",
                     "https://refactoring.guru/design-patterns/visitor",
                     "patterns:tool:visitor:home",
                     "Separate an operation from the data structure it walks, so new operations don't perturb the structure."),
            ArchTool("state-machines", "State Machines",
                     "https://statecharts.dev/",
                     "patterns:tool:state-machines:home",
                     "Make legal states and transitions explicit, so impossible states stop being a defensive-coding problem."),
        ),
    ),
    ArchCategory(
        slug="boundaries-abstraction",
        name="Boundaries & Abstraction",
        intro=(
            "Code Patterns. Where one model ends and another begins. Adapter, "
            "Facade, Anti-Corruption Layer, Ports and Adapters, and Repository "
            "are the load-bearing options for keeping a clean core from being "
            "infected by whatever shape the outside world arrived in."
        ),
        tools=(
            ArchTool("adapter", "Adapter",
                     "https://martinfowler.com/eaaCatalog/gateway.html",
                     "patterns:tool:adapter:home",
                     "Translate between an interface your code wants and one that an existing component provides."),
            ArchTool("facade", "Facade",
                     "https://refactoring.guru/design-patterns/facade",
                     "patterns:tool:facade:home",
                     "Put a simple front door on a complicated subsystem so callers don't pay for everything inside."),
            ArchTool("anti-corruption-layer", "Anti-Corruption Layer",
                     "https://learn.microsoft.com/en-us/azure/architecture/patterns/anti-corruption-layer",
                     "patterns:tool:anti-corruption-layer:home",
                     "Translate between bounded contexts so a foreign model can't quietly contaminate yours."),
            ArchTool("ports-and-adapters", "Ports and Adapters",
                     "https://alistair.cockburn.us/hexagonal-architecture/",
                     "patterns:tool:ports-and-adapters:home",
                     "Talk to the outside world through narrow interfaces (ports) implemented by swappable adapters."),
            ArchTool("repository", "Repository",
                     "https://martinfowler.com/eaaCatalog/repository.html",
                     "patterns:tool:repository:home",
                     "Hide persistence behind a collection-like interface so domain code can pretend storage doesn't exist."),
        ),
    ),
    ArchCategory(
        slug="error-handling-resilience",
        name="Error Handling & Resilience",
        intro=(
            "Code Patterns. How failure is named and recovered from at the "
            "function and module level. Result/Either, errors-as-values, "
            "Retry with Backoff, and code-level Circuit Breaker and Bulkhead "
            "are about making partial failure a thing the program can reason "
            "about instead of a thing that escapes upward."
        ),
        tools=(
            ArchTool("result-either", "Result / Either",
                     "https://doc.rust-lang.org/std/result/",
                     "patterns:tool:result-either:home",
                     "Return success or a typed error so callers must acknowledge failure instead of inheriting an exception."),
            ArchTool("errors-as-values", "Errors as Values",
                     "https://go.dev/blog/error-handling-and-go",
                     "patterns:tool:errors-as-values:home",
                     "Treat failures as ordinary return values, making error flow as legible as success flow."),
            ArchTool("retry-with-backoff", "Retry with Backoff",
                     "https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/",
                     "patterns:tool:retry-with-backoff:home",
                     "Re-attempt a failed call with exponentially growing delays and jitter so a thundering herd doesn't form."),
            ArchTool("circuit-breaker-code", "Circuit Breaker (code-level)",
                     "https://martinfowler.com/bliki/CircuitBreaker.html",
                     "patterns:tool:circuit-breaker-code:home",
                     "Stop calling a dependency that's clearly broken, fail fast for a window, then probe before reopening."),
            ArchTool("bulkhead-code", "Bulkhead (code-level)",
                     "https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead",
                     "patterns:tool:bulkhead-code:home",
                     "Partition resources so a runaway caller can drain its own pool without sinking everything else."),
        ),
    ),
    ArchCategory(
        slug="concurrency",
        name="Concurrency",
        intro=(
            "Code Patterns. How parallel work is structured and survived. "
            "Actor model, Producer/Consumer, Fan-out/Fan-in, Supervisor Trees, "
            "and the Borrow-checker discipline are the patterns that turn "
            "concurrency from a bug source into a working assumption."
        ),
        tools=(
            ArchTool("actor-model", "Actor Model",
                     "https://www.brianstorti.com/the-actor-model/",
                     "patterns:tool:actor-model:home",
                     "Independent units of state and behavior that communicate only by messages, never by shared memory."),
            ArchTool("producer-consumer", "Producer / Consumer",
                     "https://gobyexample.com/channels",
                     "patterns:tool:producer-consumer:home",
                     "Decouple work generation from work execution with a queue, so each side can scale on its own pace."),
            ArchTool("fan-out-fan-in", "Fan-out / Fan-in",
                     "https://go.dev/blog/pipelines",
                     "patterns:tool:fan-out-fan-in:home",
                     "Split a job across many parallel workers and merge their results back into a single ordered stream."),
            ArchTool("supervisor-trees", "Supervisor Trees",
                     "https://erlang.org/doc/system/sup_princ.html",
                     "patterns:tool:supervisor-trees:home",
                     "Organize processes into a tree where parents restart failing children, letting one die to keep the rest alive."),
            ArchTool("borrow-checker", "Borrow Checker as Pattern",
                     "https://doc.rust-lang.org/book/ch04-02-references-and-borrowing.html",
                     "patterns:tool:borrow-checker:home",
                     "Encode ownership and aliasing in the type system so concurrent access becomes a compile-time error."),
        ),
    ),

    # ── Architecture Patterns ────────────────────────────────────────────────
    ArchCategory(
        slug="data-state",
        name="Data & State",
        intro=(
            "Architecture Patterns. How a system holds memory and tells the "
            "truth about it. Event Sourcing, CQRS, Outbox, Saga, Materialized "
            "Views, and OLTP/OLAP separation are the moves that decide "
            "whether your data model can evolve once it has users."
        ),
        tools=(
            ArchTool("event-sourcing", "Event Sourcing",
                     "https://martinfowler.com/eaaDev/EventSourcing.html",
                     "patterns:tool:event-sourcing:home",
                     "Persist the sequence of events that produced state instead of just the latest state."),
            ArchTool("cqrs", "CQRS",
                     "https://martinfowler.com/bliki/CQRS.html",
                     "patterns:tool:cqrs:home",
                     "Split the read model from the write model so each can be optimized for its own access pattern."),
            ArchTool("outbox", "Outbox Pattern",
                     "https://microservices.io/patterns/data/transactional-outbox.html",
                     "patterns:tool:outbox:home",
                     "Write the event into the same transaction as the state change, then ship it from the table later."),
            ArchTool("saga", "Saga",
                     "https://microservices.io/patterns/data/saga.html",
                     "patterns:tool:saga:home",
                     "Coordinate a long-running transaction as a sequence of local steps with compensations instead of a global commit."),
            ArchTool("materialized-views", "Materialized Views",
                     "https://www.postgresql.org/docs/current/rules-materializedviews.html",
                     "patterns:tool:materialized-views:home",
                     "Precompute the result of an expensive query and refresh it on a schedule or on demand."),
            ArchTool("oltp-olap", "OLTP / OLAP Separation",
                     "https://www.snowflake.com/guides/oltp-vs-olap/",
                     "patterns:tool:oltp-olap:home",
                     "Run transactions and analytics in different stores, replicating between them, so neither starves the other."),
        ),
    ),
    ArchCategory(
        slug="messaging-coordination",
        name="Messaging & Coordination",
        intro=(
            "Architecture Patterns. How services agree on what happened. "
            "Request/Response, Work Queues, Dead Letter Queues, Idempotency "
            "Keys, and Choreography vs. Orchestration are the wiring "
            "decisions behind any system that crosses a network and still "
            "needs to be reasoned about."
        ),
        tools=(
            ArchTool("request-response", "Request / Response",
                     "https://www.enterpriseintegrationpatterns.com/patterns/messaging/RequestReply.html",
                     "patterns:tool:request-response:home",
                     "The synchronous backbone of distributed systems: send a message, block until the answer arrives."),
            ArchTool("work-queues", "Work Queues",
                     "https://www.rabbitmq.com/tutorials/tutorial-two-python",
                     "patterns:tool:work-queues:home",
                     "Decouple producers from consumers with a queue and competing-consumer workers, so load and rate are separate."),
            ArchTool("dead-letter-queue", "Dead Letter Queue",
                     "https://aws.amazon.com/builders-library/avoiding-insurmountable-queue-backlogs/",
                     "patterns:tool:dead-letter-queue:home",
                     "Move messages that keep failing into a side queue so the main queue keeps draining and you can investigate later."),
            ArchTool("idempotency-keys", "Idempotency Keys",
                     "https://brandur.org/idempotency-keys",
                     "patterns:tool:idempotency-keys:home",
                     "Let callers safely retry by tagging each attempt with a key the server uses to deduplicate effects."),
            ArchTool("choreography-orchestration", "Choreography vs. Orchestration",
                     "https://microservices.io/patterns/data/saga.html",
                     "patterns:tool:choreography-orchestration:home",
                     "Decide whether services react to events on their own (choreography) or follow a conductor (orchestration)."),
        ),
    ),
    ArchCategory(
        slug="topology",
        name="Topology",
        intro=(
            "Architecture Patterns. The shape of the deployment. Monolith, "
            "Microservices, Modular Monolith, Backends for Frontends, "
            "Strangler Fig boundary, Service Mesh, and Sidecar are about "
            "where the seams between teams and runtimes actually live."
        ),
        tools=(
            ArchTool("monolith", "Monolith",
                     "https://martinfowler.com/bliki/MonolithFirst.html",
                     "patterns:tool:monolith:home",
                     "One deployable for the whole system; the default that earns its critics only at certain sizes of team and traffic."),
            ArchTool("microservices", "Microservices",
                     "https://martinfowler.com/articles/microservices.html",
                     "patterns:tool:microservices:home",
                     "Independently deployable services drawn around team boundaries, paid for with operational overhead."),
            ArchTool("modular-monolith", "Modular Monolith",
                     "https://www.kamilgrzybek.com/blog/posts/modular-monolith-primer",
                     "patterns:tool:modular-monolith:home",
                     "One deployable, hard module boundaries inside it: the alternative most teams should try before splitting."),
            ArchTool("bff", "Backends for Frontends",
                     "https://samnewman.io/patterns/architectural/bff/",
                     "patterns:tool:bff:home",
                     "A thin per-client backend that shapes APIs for one frontend instead of forcing one API to serve all."),
            ArchTool("strangler-fig-boundary", "Strangler Fig (boundary tactic)",
                     "https://martinfowler.com/bliki/StranglerFigApplication.html",
                     "patterns:tool:strangler-fig-boundary:home",
                     "Wrap the old system behind a façade and grow the new one inside it until the old can be deleted."),
            ArchTool("service-mesh", "Service Mesh",
                     "https://istio.io/latest/about/service-mesh/",
                     "patterns:tool:service-mesh:home",
                     "Move retries, mTLS, and routing into a sidecar layer so application code stops re-implementing them per service."),
            ArchTool("sidecar", "Sidecar",
                     "https://learn.microsoft.com/en-us/azure/architecture/patterns/sidecar",
                     "patterns:tool:sidecar:home",
                     "Run a helper process next to your service to add capabilities (proxying, telemetry) without changing the service."),
        ),
    ),
    ArchCategory(
        slug="resilience-at-scale",
        name="Resilience at Scale",
        intro=(
            "Architecture Patterns. How a system bends without breaking when "
            "things go sideways. Bulkhead between services, Circuit Breaker "
            "across services, Back-pressure, Load Shedding, and Graceful "
            "Degradation are the moves that turn partial failure into a "
            "user-acceptable outcome."
        ),
        tools=(
            ArchTool("bulkhead-service", "Bulkhead (service-level)",
                     "https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/use-bulkhead-architectures-to-limit-scope-of-impact.html",
                     "patterns:tool:bulkhead-service:home",
                     "Isolate workloads across deployments or pools so one tenant or workload can't sink the others."),
            ArchTool("circuit-breaker-service", "Circuit Breaker (between services)",
                     "https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker",
                     "patterns:tool:circuit-breaker-service:home",
                     "Stop sending traffic to a downstream that's clearly hurting, then probe before declaring it healthy again."),
            ArchTool("back-pressure", "Back-pressure",
                     "https://www.reactivemanifesto.org/glossary#Back-Pressure",
                     "patterns:tool:back-pressure:home",
                     "Tell upstream callers to slow down when you're full, instead of silently absorbing or dropping."),
            ArchTool("load-shedding", "Load Shedding",
                     "https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/",
                     "patterns:tool:load-shedding:home",
                     "Drop or reject low-priority work near capacity so the critical path stays inside its latency budget."),
            ArchTool("graceful-degradation", "Graceful Degradation",
                     "https://aws.amazon.com/builders-library/static-stability-using-availability-zones/",
                     "patterns:tool:graceful-degradation:home",
                     "Decide in advance what features get turned off when dependencies fail, so 'works' has a defined floor."),
        ),
    ),
    ArchCategory(
        slug="evolution-migration",
        name="Evolution & Migration",
        intro=(
            "Architecture Patterns. How a system changes shape without "
            "stopping. Strangler Fig at the system level, Expand/Contract "
            "Schema, Parallel Run, Dark Launches, and Feature Flags as "
            "architecture are the patterns of taking a running system from "
            "one shape to a better one without an outage."
        ),
        tools=(
            ArchTool("strangler-fig-system", "Strangler Fig (system migration)",
                     "https://martinfowler.com/bliki/StranglerFigApplication.html",
                     "patterns:tool:strangler-fig-system:home",
                     "Replace a legacy system incrementally by routing traffic to the new implementation feature by feature."),
            ArchTool("expand-contract-schema", "Expand / Contract Schema",
                     "https://martinfowler.com/articles/evodb.html",
                     "patterns:tool:expand-contract-schema:home",
                     "Add the new shape, dual-write, migrate readers, then remove the old shape — never with all four at once."),
            ArchTool("parallel-run", "Parallel Run",
                     "https://martinfowler.com/bliki/ParallelChange.html",
                     "patterns:tool:parallel-run:home",
                     "Run the old and new implementations side by side against real traffic until results agree, then switch."),
            ArchTool("dark-launches", "Dark Launches",
                     "https://launchdarkly.com/blog/what-are-dark-launches-and-how-do-they-work/",
                     "patterns:tool:dark-launches:home",
                     "Ship the new code into production behind a flag and exercise it with real traffic before any user sees it."),
            ArchTool("feature-flags-architecture", "Feature Flags (as architecture)",
                     "https://martinfowler.com/articles/feature-toggles.html",
                     "patterns:tool:feature-flags-architecture:home",
                     "Use long-lived flags to keep architectural choices reversible, not just to gate user-visible features."),
        ),
    ),

    # ── Patterns of Discipline ───────────────────────────────────────────────
    ArchCategory(
        slug="change-patterns",
        name="Change Patterns",
        intro=(
            "Patterns of Discipline. How big changes ship in small, "
            "reversible steps. Branch by Abstraction, Expand/Contract "
            "Schema+API, Parallel Change, and Feature Toggles are about "
            "keeping main shippable while a multi-week refactor or migration "
            "is in flight."
        ),
        tools=(
            ArchTool("branch-by-abstraction", "Branch by Abstraction",
                     "https://martinfowler.com/bliki/BranchByAbstraction.html",
                     "patterns:tool:branch-by-abstraction:home",
                     "Hide the thing being replaced behind an interface, swap implementations behind it, then collapse the seam."),
            ArchTool("expand-contract-discipline", "Expand / Contract (Schema & API)",
                     "https://www.tim-evans.co.uk/expand-and-contract/",
                     "patterns:tool:expand-contract-discipline:home",
                     "A discipline for changing schemas and APIs without downtime: add the new shape, migrate, then remove the old."),
            ArchTool("parallel-change", "Parallel Change",
                     "https://martinfowler.com/bliki/ParallelChange.html",
                     "patterns:tool:parallel-change:home",
                     "Make a breaking change in three steps — expand, migrate callers, contract — so the tree always compiles."),
            ArchTool("feature-toggles-discipline", "Feature Toggles (as discipline)",
                     "https://martinfowler.com/articles/feature-toggles.html",
                     "patterns:tool:feature-toggles-discipline:home",
                     "Keep work-in-progress in main behind toggles, separating deploy from release and shortening branch lifetimes."),
        ),
    ),
    ArchCategory(
        slug="review-verification",
        name="Review & Verification",
        intro=(
            "Patterns of Discipline. How a team agrees the work is done. "
            "Review-as-Conversation, Characterization Tests, Golden/Snapshot "
            "tests, Property-Based Testing, and Mutation Testing are about "
            "raising the floor of trust without lengthening the queue."
        ),
        tools=(
            ArchTool("review-as-conversation", "Review-as-Conversation vs. Review-as-Gate",
                     "https://google.github.io/eng-practices/review/reviewer/",
                     "patterns:tool:review-as-conversation:home",
                     "Treat review as the moment a second engineer understands the change, not as the moment correctness is decided."),
            ArchTool("characterization-tests", "Characterization Tests",
                     "https://wiki.c2.com/?CharacterizationTest",
                     "patterns:tool:characterization-tests:home",
                     "Pin down the actual behavior of legacy code with tests before you change anything, so a regression is detectable."),
            ArchTool("golden-snapshot-tests", "Golden / Snapshot Tests",
                     "https://jestjs.io/docs/snapshot-testing",
                     "patterns:tool:golden-snapshot-tests:home",
                     "Record the current output as truth and fail when it changes, useful when the spec is 'whatever it does today.'"),
            ArchTool("property-based-testing", "Property-Based Testing",
                     "https://hypothesis.works/articles/what-is-property-based-testing/",
                     "patterns:tool:property-based-testing:home",
                     "State invariants and let the runner generate inputs to break them, instead of guessing the edge cases yourself."),
            ArchTool("mutation-testing", "Mutation Testing",
                     "https://pitest.org/",
                     "patterns:tool:mutation-testing:home",
                     "Modify the program in small ways and check that some test fails, measuring how much your suite actually catches."),
        ),
    ),
    ArchCategory(
        slug="operational-patterns",
        name="Operational Patterns",
        intro=(
            "Patterns of Discipline. How a team keeps the running system "
            "trustworthy. Runbooks as Code, the Four Golden Signals, Error "
            "Budgets, Blameless Postmortems, and On-Call as Pattern are the "
            "small habits that compound into operational excellence."
        ),
        tools=(
            ArchTool("runbooks-as-code", "Runbooks as Code",
                     "https://github.com/SkeltonThatcher/run-book-template",
                     "patterns:tool:runbooks-as-code:home",
                     "Version, review, and execute runbooks alongside the code they describe, so they age with the system."),
            ArchTool("four-golden-signals", "Four Golden Signals",
                     "https://sre.google/sre-book/monitoring-distributed-systems/",
                     "patterns:tool:four-golden-signals:home",
                     "Latency, traffic, errors, and saturation: the minimum monitoring vocabulary every service should publish."),
            ArchTool("error-budgets", "Error Budgets",
                     "https://sre.google/workbook/error-budget-policy/",
                     "patterns:tool:error-budgets:home",
                     "Turn 'how reliable' into a budget the team spends on changes versus burns on outages, with a written policy."),
            ArchTool("blameless-postmortems", "Blameless Postmortems",
                     "https://www.etsy.com/codeascraft/blameless-postmortems",
                     "patterns:tool:blameless-postmortems:home",
                     "Investigate incidents by asking how each action made sense to the operator in the moment, then fix the system."),
            ArchTool("on-call-as-pattern", "On-Call as Pattern",
                     "https://sre.google/sre-book/being-on-call/",
                     "patterns:tool:on-call-as-pattern:home",
                     "Treat the rotation, alert quality, and follow-up as the artifact, not just the schedule of who's paged."),
        ),
    ),

    # ── Anti-Patterns ────────────────────────────────────────────────────────
    ArchCategory(
        slug="anti-patterns",
        name="Anti-Patterns",
        intro=(
            "Anti-Patterns. The shapes worth naming so you can refuse them. "
            "Each entry below is something that solved a real problem at "
            "first and then quietly went toxic. Recognizing the shape is the "
            "whole point — the cure is usually one of the patterns above."
        ),
        tools=(
            ArchTool("distributed-monolith", "Distributed Monolith",
                     "https://www.jimmybogard.com/avoiding-the-distributed-monolith/",
                     "patterns:tool:distributed-monolith:home",
                     "Microservices that must be deployed together: you've paid the cost of distribution and kept the coupling of a monolith."),
            ArchTool("god-object", "God Object",
                     "https://wiki.c2.com/?GodClass",
                     "patterns:tool:god-object:home",
                     "One class that knows everything and is touched by every change — the gravitational center the rest of the system orbits."),
            ArchTool("primitive-obsession", "Primitive Obsession",
                     "https://refactoring.guru/smells/primitive-obsession",
                     "patterns:tool:primitive-obsession:home",
                     "Modeling domain concepts with bare strings, ints, and dicts so the type system can't help catch a misuse."),
            ArchTool("anemic-domain-model", "Anemic Domain Model",
                     "https://martinfowler.com/bliki/AnemicDomainModel.html",
                     "patterns:tool:anemic-domain-model:home",
                     "Domain objects with no behavior, just getters and setters, with business logic scattered across service classes instead."),
            ArchTool("microservices-too-early", "Microservices Too Early",
                     "https://martinfowler.com/bliki/MonolithFirst.html",
                     "patterns:tool:microservices-too-early:home",
                     "Drawing service boundaries before you know the domain, locking in coupling that later proves expensive to move."),
            ArchTool("premature-abstraction", "Premature Abstraction",
                     "https://sandimetz.com/blog/2016/1/20/the-wrong-abstraction",
                     "patterns:tool:premature-abstraction:home",
                     "Pulling out an interface before the second concrete case exists, freezing a guess as if it were a constraint."),
            ArchTool("dry-at-all-costs", "DRY at All Costs",
                     "https://verraes.net/2014/08/dry-is-about-knowledge/",
                     "patterns:tool:dry-at-all-costs:home",
                     "Deduplicating code that only looks alike, gluing unrelated concepts together so changes in one place break the other."),
            ArchTool("config-driven-development", "Configuration-Driven Development",
                     "https://nedbatchelder.com/blog/202104/code_is_just_data.html",
                     "patterns:tool:config-driven-development:home",
                     "A homegrown DSL in YAML or JSON that grows control flow until it's a language with no compiler, debugger, or tests."),
            ArchTool("big-ball-of-mud", "Big Ball of Mud",
                     "http://www.laputan.org/mud/",
                     "patterns:tool:big-ball-of-mud:home",
                     "The system shape that emerges when no shape is enforced: tangled, unstructured, and somehow still in production."),
            ArchTool("singleton-as-global-state", "Singleton-as-Global-State",
                     "https://wiki.c2.com/?SingletonsAreEvil",
                     "patterns:tool:singleton-as-global-state:home",
                     "Hidden shared mutable state dressed up as a design pattern, so dependencies stop showing up in signatures."),
            ArchTool("stringly-typed-code", "Stringly-Typed Code",
                     "https://wiki.c2.com/?StringlyTyped",
                     "patterns:tool:stringly-typed-code:home",
                     "Encoding meaning into strings the compiler can't see, so misspellings and confusions only show up at runtime."),
            ArchTool("speculative-generality", "Speculative Generality",
                     "https://refactoring.guru/smells/speculative-generality",
                     "patterns:tool:speculative-generality:home",
                     "Hooks, parameters, and indirection added for a case that never arrives, paid for in confusion by every reader since."),
        ),
    ),

]


# ─── SPINE ───────────────────────────────────────────────────────────────────

POP_SPINE_RESOURCES: list[ArchResource] = [
    _s("The Timeless Way of Building",
       "https://www.patternlanguage.com/archive/twtw.html",
       "Christopher Alexander · Oxford University Press", "patterns:spine:timeless-way", "Tutorial",
       "Alexander's argument that patterns are the language a living system uses to describe itself — the philosophical root the rest sits on."),
    _s("A Pattern Language",
       "https://www.patternlanguage.com/aptl/aplsample/aplsample.htm",
       "Alexander, Ishikawa, Silverstein · Oxford University Press", "patterns:spine:pattern-language", "Tutorial",
       "253 architectural patterns at every scale; the structural model every software pattern book has imitated since."),
    _s("Design Patterns: Elements of Reusable Object-Oriented Software",
       "https://en.wikipedia.org/wiki/Design_Patterns",
       "Gamma, Helm, Johnson, Vlissides · Addison-Wesley", "patterns:spine:gof", "Tutorial",
       "The 'Gang of Four' book: 23 patterns that defined the shared vocabulary of object-oriented design for a generation."),
    _s("Pattern-Oriented Software Architecture, Vol 2: Patterns for Concurrent and Networked Objects",
       "https://www.wiley.com/en-us/Pattern+Oriented+Software+Architecture%2C+Volume+2%2C+Patterns+for+Concurrent+and+Networked+Objects-p-9780471606956",
       "Schmidt, Stal, Rohnert, Buschmann · Wiley", "patterns:spine:posa2", "Best Practices",
       "The canonical reference for Reactor, Proactor, Active Object, and the rest of the concurrency-pattern vocabulary."),
    _s("Implementation Patterns",
       "https://www.oreilly.com/library/view/implementation-patterns/9780321413093/",
       "Kent Beck · Addison-Wesley", "patterns:spine:implementation-patterns", "Tutorial",
       "Beck on the small daily moves that make code communicate: naming, control flow, classes, methods — the line-level patterns."),
    _s("Working Effectively with Legacy Code",
       "https://www.oreilly.com/library/view/working-effectively-with/0131177052/",
       "Michael Feathers · Prentice Hall", "patterns:spine:legacy-code", "Tutorial",
       "Feathers on the seam-and-test techniques for changing code you didn't write and don't fully trust — the toolkit of careful change."),
    _s("Simple Made Easy",
       "https://www.infoq.com/presentations/Simple-Made-Easy/",
       "Rich Hickey · Strange Loop / InfoQ", "patterns:spine:simple-made-easy", "Talk",
       "Hickey's distinction between simple (one fold) and easy (familiar), and why we keep choosing easy and paying for it later."),
    _s("Hammock Driven Development",
       "https://www.youtube.com/watch?v=f84n5oFoZBc",
       "Rich Hickey · Clojure Conj", "patterns:spine:hammock-driven", "Talk",
       "Hickey on solving by thinking: the case for slowing down before the keyboard and the case against optimizing typing speed."),
    _s("Data on the Outside vs. Data on the Inside",
       "https://www.cidrdb.org/cidr2005/papers/P12.pdf",
       "Pat Helland · CIDR 2005", "patterns:spine:data-outside-inside", "Discussion",
       "Helland's frame for why data crossing service boundaries has different rules than data inside one — the basis of CQRS and Outbox."),
    _s("Life Beyond Distributed Transactions",
       "https://www.ics.uci.edu/~cs223/papers/cidr07p15.pdf",
       "Pat Helland · CIDR 2007", "patterns:spine:life-beyond-distributed-txns", "Discussion",
       "Helland's argument that scalable systems must compose around idempotent messages and entities, not global transactions."),
    _s("Database in Depth",
       "https://www.oreilly.com/library/view/database-in-depth/0596100124/",
       "C. J. Date · O'Reilly", "patterns:spine:database-in-depth", "Tutorial",
       "Date's relational primer, the through-line back to Codd's 1970 paper that still shapes how we think about data shape."),
    _s("Symmathesy: A Word in Progress",
       "https://norabateson.wordpress.com/2015/11/03/symmathesy-a-word-in-progress/",
       "Nora Bateson", "patterns:spine:symmathesy", "Discussion",
       "Bateson's coinage for a system that learns together with its parts — Jessica Kerr's frame for living software teams."),
    _s("Hints for Computer System Design",
       "https://www.microsoft.com/en-us/research/wp-content/uploads/1983/10/HintsForComputerSystemDesign.pdf",
       "Butler Lampson · ACM", "patterns:spine:lampson-hints", "Best Practices",
       "Lampson's 1983 pattern-shaped collection of design hints — separation of concerns, end-to-end principle, get it working first."),
    _s("How to Solve It",
       "https://press.princeton.edu/books/paperback/9780691164076/how-to-solve-it",
       "George Pólya · Princeton University Press", "patterns:spine:polya-how-to-solve-it", "Tutorial",
       "Pólya's 1945 method for problem-solving — understand the problem, plan, execute, review — the meta-pattern under every pattern below."),
]


# ─── PER-TOOL RESOURCES ─────────────────────────────────────────────────────

POP_TOOL_RESOURCES: list[ArchToolResource] = [
    # ── Composition & Construction ──
    _r("FluentInterface",
       "https://martinfowler.com/bliki/FluentInterface.html",
       "Martin Fowler", "patterns:res:builder:fowler-fluent", ["builder", "fluent-interface"], "Discussion",
       "Fowler's bliki entry on chained methods that read like a sentence — the canonical naming of the pattern."),
    _r("Builder Pattern",
       "https://refactoring.guru/design-patterns/builder",
       "refactoring.guru", "patterns:res:builder:refactoring-guru", ["builder"], "Tutorial",
       "Step-by-step explanation of Builder with structural diagrams and language-specific examples."),
    _r("Effective Java, Item 2: Consider a builder when faced with many constructor parameters",
       "https://www.oreilly.com/library/view/effective-java-3rd/9780134686097/",
       "Joshua Bloch · O'Reilly", "patterns:res:builder:effective-java", ["builder"], "Best Practices",
       "Bloch's reference treatment: builders beat telescoping constructors and JavaBean setters once you cross a handful of fields."),
    _r("Inversion of Control Containers and the Dependency Injection pattern",
       "https://martinfowler.com/articles/injection.html",
       "Martin Fowler", "patterns:res:di:fowler-injection", ["dependency-injection"], "Discussion",
       "Fowler's definitive essay separating IoC, DI, and Service Locator and naming the three injection variants."),
    _r("Dependency Injection Principles, Practices, and Patterns",
       "https://www.manning.com/books/dependency-injection-principles-practices-patterns",
       "Seemann & van Deursen · Manning", "patterns:res:di:seemann-book", ["dependency-injection"], "Tutorial",
       "Book-length treatment of DI: composition roots, lifetimes, and the patterns that pay off without a container."),
    _r("Dependency Injection Demystified",
       "https://www.jamesshore.com/v2/blog/2006/dependency-injection-demystified",
       "James Shore", "patterns:res:di:shore-demystified", ["dependency-injection"], "Discussion",
       "Shore's short, plain-language piece that strips DI down to what it actually is — handing collaborators in, by name."),
    _r("Replace Constructor with Factory Function",
       "https://refactoring.com/catalog/replaceConstructorWithFactoryFunction.html",
       "Refactoring (Fowler)", "patterns:res:factory:refactoring-catalog", ["factory"], "Tutorial",
       "Catalog entry from Refactoring 2e for swapping noisy constructors for variant-named factory functions."),
    _r("Factory Method Pattern",
       "https://refactoring.guru/design-patterns/factory-method",
       "refactoring.guru", "patterns:res:factory:refactoring-guru", ["factory"], "Tutorial",
       "Walkthrough of Factory Method vs. Abstract Factory with code in multiple languages."),
    _r("New Type Idiom",
       "https://doc.rust-lang.org/rust-by-example/generics/new_types.html",
       "Rust by Example", "patterns:res:newtype:rust-by-example", ["newtype"], "Tutorial",
       "Rust's canonical demonstration of wrapping a primitive in a tuple struct to gain compile-time distinction."),
    _r("Parse, don't validate",
       "https://lexi-lambda.github.io/blog/2019/11/05/parse-don-t-validate/",
       "Alexis King", "patterns:res:newtype:parse-dont-validate", ["newtype"], "Discussion",
       "King's foundational essay: lift validation to the type level so invalid states cannot be represented."),

    # ── Control Flow ──
    _r("Strategy Pattern",
       "https://refactoring.guru/design-patterns/strategy",
       "refactoring.guru", "patterns:res:strategy:refactoring-guru", ["strategy"], "Tutorial",
       "Diagrammed walkthrough of swappable algorithms behind a single interface, with multi-language examples."),
    _r("Replace Conditional with Polymorphism",
       "https://refactoring.com/catalog/replaceConditionalWithPolymorphism.html",
       "Refactoring (Fowler)", "patterns:res:strategy:replace-conditional", ["strategy"], "Tutorial",
       "Refactoring catalog move that often produces Strategy as its destination shape."),
    _r("Command Pattern",
       "https://refactoring.guru/design-patterns/command",
       "refactoring.guru", "patterns:res:command:refactoring-guru", ["command"], "Tutorial",
       "Encapsulating a request as an object so it can be parameterized, queued, logged, and undone."),
    _r("DecoratedCommand",
       "https://martinfowler.com/bliki/DecoratedCommand.html",
       "Martin Fowler", "patterns:res:command:fowler-decorated", ["command"], "Discussion",
       "Fowler on stacking concerns (logging, auth, retry) onto Command objects via decoration."),
    _r("Chain of Responsibility Pattern",
       "https://refactoring.guru/design-patterns/chain-of-responsibility",
       "refactoring.guru", "patterns:res:pipeline-cor:refactoring-guru", ["pipeline-cor"], "Tutorial",
       "Diagrammed walkthrough of handler chains, where each link decides whether to act or pass."),
    _r("Pipeline pattern in Go",
       "https://go.dev/blog/pipelines",
       "Go Blog", "patterns:res:pipeline-cor:go-blog", ["pipeline-cor", "fan-out-fan-in"], "Tutorial",
       "The Go team's reference text on composing concurrent stages with channels, including fan-out and cancellation."),
    _r("Using Express middleware",
       "https://expressjs.com/en/guide/using-middleware.html",
       "Express docs", "patterns:res:middleware:express", ["middleware"], "Tutorial",
       "Canonical worked example of a middleware stack: each function gets next(), runs code before and after, composes."),
    _r("Architecture: Middleware",
       "https://docs.djangoproject.com/en/stable/topics/http/middleware/",
       "Django docs", "patterns:res:middleware:django", ["middleware"], "Tutorial",
       "Django's framework-side view of middleware as ordered request/response hooks with explicit lifecycle."),
    _r("Visitor Pattern",
       "https://refactoring.guru/design-patterns/visitor",
       "refactoring.guru", "patterns:res:visitor:refactoring-guru", ["visitor"], "Tutorial",
       "Double-dispatch walkthrough of separating an operation from the data structure it traverses."),
    _r("The Expression Problem",
       "https://homepages.inf.ed.ac.uk/wadler/papers/expression/expression.txt",
       "Philip Wadler", "patterns:res:visitor:expression-problem", ["visitor"], "Discussion",
       "Wadler's framing of the design tension Visitor addresses — adding cases vs. adding operations."),
    _r("Statecharts: A Visual Formalism for Complex Systems",
       "https://www.sciencedirect.com/science/article/pii/0167642387900359",
       "David Harel", "patterns:res:state-machines:harel", ["state-machines"], "Discussion",
       "Harel's 1987 paper introducing statecharts — the formal extension of finite state machines for real software."),
    _r("Statecharts.dev",
       "https://statecharts.dev/",
       "statecharts.dev", "patterns:res:state-machines:statecharts-dev", ["state-machines"], "Tutorial",
       "Living catalog of statechart concepts with examples, used by XState and other practical libraries."),

    # ── Boundaries & Abstraction ──
    _r("Gateway",
       "https://martinfowler.com/eaaCatalog/gateway.html",
       "Martin Fowler", "patterns:res:adapter:fowler-gateway", ["adapter"], "Discussion",
       "Fowler's PoEAA catalog entry for the gateway role — the architectural sibling of the Adapter pattern."),
    _r("Façade Pattern",
       "https://refactoring.guru/design-patterns/facade",
       "refactoring.guru", "patterns:res:facade:refactoring-guru", ["facade"], "Tutorial",
       "Walkthrough of presenting a single, simpler interface in front of a tangled subsystem."),
    _r("Anti-Corruption Layer pattern",
       "https://learn.microsoft.com/en-us/azure/architecture/patterns/anti-corruption-layer",
       "Microsoft Learn", "patterns:res:anti-corruption-layer:msft", ["anti-corruption-layer"], "Best Practices",
       "Microsoft's pattern catalog entry: when to insert a translation layer between legacy and new bounded contexts."),
    _r("Domain-Driven Design: Tackling Complexity in the Heart of Software",
       "https://www.oreilly.com/library/view/domain-driven-design-tackling/0321125215/",
       "Eric Evans · Addison-Wesley", "patterns:res:anti-corruption-layer:ddd-blue-book", ["anti-corruption-layer"], "Tutorial",
       "Original source of Anti-Corruption Layer in the DDD blue book; chapters on context maps remain the reference."),
    _r("Hexagonal Architecture",
       "https://alistair.cockburn.us/hexagonal-architecture/",
       "Alistair Cockburn", "patterns:res:ports-and-adapters:cockburn", ["ports-and-adapters"], "Discussion",
       "Cockburn's original Hexagonal Architecture essay — the source paper for Ports and Adapters."),
    _r("Ports and Adapters with Hexagonal Architecture",
       "https://jmgarridopaz.github.io/content/hexagonalarchitecture.html",
       "Juan Manuel Garrido Paz", "patterns:res:ports-and-adapters:garrido", ["ports-and-adapters"], "Tutorial",
       "Practical walkthrough of designing primary and secondary ports with concrete adapters and dependency direction."),
    _r("Repository",
       "https://martinfowler.com/eaaCatalog/repository.html",
       "Martin Fowler · PoEAA", "patterns:res:repository:fowler-eaa", ["repository"], "Discussion",
       "Fowler's PoEAA catalog entry: a collection-like interface mediating between the domain and the data mapping layer."),

    # ── Error Handling & Resilience ──
    _r("std::result — Result type",
       "https://doc.rust-lang.org/std/result/",
       "Rust std docs", "patterns:res:result-either:rust-std", ["result-either"], "Tutorial",
       "Rust's reference treatment of Result<T, E> — the canonical typed-error API in mainstream programming."),
    _r("Railway Oriented Programming",
       "https://fsharpforfunandprofit.com/posts/recipe-part2/",
       "Scott Wlaschin · F# for Fun and Profit", "patterns:res:result-either:railway-oriented", ["result-either", "errors-as-values"], "Tutorial",
       "Wlaschin's diagrams-and-prose argument for chaining Result-returning functions — the strongest accessible essay on the model."),
    _r("Error handling and Go",
       "https://go.dev/blog/error-handling-and-go",
       "Go Blog", "patterns:res:errors-as-values:go-blog", ["errors-as-values"], "Discussion",
       "The Go team's defense of returning errors as values — the foundational case for the style outside Haskell/ML."),
    _r("Errors are values",
       "https://go.dev/blog/errors-are-values",
       "Rob Pike · Go Blog", "patterns:res:errors-as-values:pike-errors-are-values", ["errors-as-values"], "Discussion",
       "Pike's follow-up post on actually using error values as ordinary data, not as a try/catch substitute."),
    _r("Timeouts, retries, and backoff with jitter",
       "https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/",
       "Marc Brooker · AWS Builders' Library", "patterns:res:retry-with-backoff:aws-builders", ["retry-with-backoff"], "Best Practices",
       "Brooker on why jitter is mandatory, how to think about retry budgets, and where retries make outages worse."),
    _r("Exponential Backoff And Jitter",
       "https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/",
       "AWS Architecture Blog", "patterns:res:retry-with-backoff:aws-jitter-post", ["retry-with-backoff"], "Tutorial",
       "Simulator-driven walkthrough of full jitter vs. decorrelated jitter and why naive exponential backoff isn't enough."),
    _r("CircuitBreaker",
       "https://martinfowler.com/bliki/CircuitBreaker.html",
       "Martin Fowler · bliki", "patterns:res:circuit-breaker-code:fowler", ["circuit-breaker-code", "circuit-breaker-service"], "Discussion",
       "Fowler's bliki entry codifying Michael Nygard's circuit breaker into the cross-language pattern vocabulary."),
    _r("Release It! Second Edition",
       "https://pragprog.com/titles/mnee2/release-it-second-edition/",
       "Michael T. Nygard · Pragmatic Bookshelf", "patterns:res:circuit-breaker-code:nygard", ["circuit-breaker-code", "bulkhead-code", "circuit-breaker-service", "bulkhead-service"], "Tutorial",
       "The original source for Circuit Breaker, Bulkhead, and most operational resilience patterns now in common use."),
    _r("Bulkhead pattern",
       "https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead",
       "Microsoft Learn", "patterns:res:bulkhead-code:msft", ["bulkhead-code", "bulkhead-service"], "Best Practices",
       "Microsoft's pattern catalog entry on isolating workloads so one runaway can't sink the others."),

    # ── Concurrency ──
    _r("The Actor Model in 10 Minutes",
       "https://www.brianstorti.com/the-actor-model/",
       "Brian Storti", "patterns:res:actor-model:storti", ["actor-model"], "Tutorial",
       "Compact introduction to actors, mailboxes, and supervision — the lightest accessible primer in print."),
    _r("Erlang/OTP Design Principles",
       "https://www.erlang.org/doc/system/design_principles.html",
       "Erlang docs", "patterns:res:actor-model:erlang-otp", ["actor-model", "supervisor-trees"], "Tutorial",
       "The canonical reference for actors and supervision trees — what every later actor framework is reimplementing."),
    _r("Channels in Go",
       "https://gobyexample.com/channels",
       "Go by Example", "patterns:res:producer-consumer:gobyexample", ["producer-consumer"], "Tutorial",
       "Hello-world treatment of channels as queues between producer and consumer goroutines."),
    _r("Go Concurrency Patterns: Pipelines and cancellation",
       "https://go.dev/blog/pipelines",
       "Sameer Ajmani · Go Blog", "patterns:res:fan-out-fan-in:go-pipelines", ["fan-out-fan-in", "producer-consumer"], "Tutorial",
       "The reference text on fan-out / fan-in with channels, including correct cancellation semantics."),
    _r("Supervision Principles",
       "https://www.erlang.org/doc/system/sup_princ.html",
       "Erlang docs", "patterns:res:supervisor-trees:erlang", ["supervisor-trees"], "Tutorial",
       "Erlang's documentation of supervisor strategies (one_for_one, rest_for_one, etc.) — the source of the vocabulary."),
    _r("References and Borrowing",
       "https://doc.rust-lang.org/book/ch04-02-references-and-borrowing.html",
       "The Rust Programming Language", "patterns:res:borrow-checker:rust-book", ["borrow-checker"], "Tutorial",
       "The reference chapter on Rust's ownership and borrowing rules — the move that turned data races into compile errors."),
    _r("Fearless Concurrency",
       "https://doc.rust-lang.org/book/ch16-00-concurrency.html",
       "The Rust Programming Language", "patterns:res:borrow-checker:fearless-concurrency", ["borrow-checker"], "Tutorial",
       "How Send/Sync and the borrow checker compose into the 'fearless concurrency' guarantee Rust advertises."),

    # ── Data & State ──
    _r("Event Sourcing",
       "https://martinfowler.com/eaaDev/EventSourcing.html",
       "Martin Fowler", "patterns:res:event-sourcing:fowler", ["event-sourcing"], "Discussion",
       "Fowler's foundational essay defining event sourcing as the persistence model for the audit-as-truth approach."),
    _r("Versioning in an Event Sourced System",
       "https://leanpub.com/esversioning",
       "Greg Young · Leanpub", "patterns:res:event-sourcing:young-versioning", ["event-sourcing"], "Tutorial",
       "Young's free book on the operational reality of event sourcing — schema evolution, snapshots, and event upcasting."),
    _r("CQRS",
       "https://martinfowler.com/bliki/CQRS.html",
       "Martin Fowler", "patterns:res:cqrs:fowler", ["cqrs"], "Discussion",
       "Fowler's bliki entry — the warning label as much as the definition, naming when CQRS is a load-bearing choice."),
    _r("CQRS Documents",
       "https://cqrs.files.wordpress.com/2010/11/cqrs_documents.pdf",
       "Greg Young", "patterns:res:cqrs:young-pdf", ["cqrs"], "Discussion",
       "Young's original collected writings on CQRS — the source the rest of the literature footnotes."),
    _r("Pattern: Transactional outbox",
       "https://microservices.io/patterns/data/transactional-outbox.html",
       "microservices.io", "patterns:res:outbox:microservices-io", ["outbox"], "Best Practices",
       "Richardson's canonical catalog entry: write the event in the same DB transaction, then relay it."),
    _r("Reliable Microservices Data Exchange With the Outbox Pattern",
       "https://debezium.io/blog/2019/02/19/reliable-microservices-data-exchange-with-the-outbox-pattern/",
       "Debezium blog", "patterns:res:outbox:debezium", ["outbox"], "Tutorial",
       "Worked example of the outbox pattern using Debezium and Kafka to ship events from a Postgres table."),
    _r("Pattern: Saga",
       "https://microservices.io/patterns/data/saga.html",
       "microservices.io", "patterns:res:saga:microservices-io", ["saga", "choreography-orchestration"], "Best Practices",
       "Richardson on coordinating long-running cross-service transactions with local steps and compensations."),
    _r("Sagas",
       "https://www.cs.cornell.edu/andru/cs711/2002fa/reading/sagas.pdf",
       "Garcia-Molina & Salem · 1987", "patterns:res:saga:garcia-molina", ["saga"], "Discussion",
       "The original 1987 paper introducing the Saga as a model for long-lived transactions."),
    _r("Materialized Views",
       "https://www.postgresql.org/docs/current/rules-materializedviews.html",
       "PostgreSQL docs", "patterns:res:materialized-views:postgres", ["materialized-views"], "Tutorial",
       "Postgres reference for declaring, populating, and refreshing materialized views — the no-frills mechanism reference."),
    _r("OLTP vs. OLAP",
       "https://www.snowflake.com/guides/oltp-vs-olap/",
       "Snowflake", "patterns:res:oltp-olap:snowflake", ["oltp-olap"], "Discussion",
       "Clear vendor-side primer on transactional vs. analytical workloads and why they want different stores."),
    _r("Designing Data-Intensive Applications",
       "https://dataintensive.net/",
       "Martin Kleppmann · O'Reilly", "patterns:res:oltp-olap:ddia", ["oltp-olap", "event-sourcing", "cqrs", "outbox"], "Tutorial",
       "Kleppmann's reference text on the data-shape decisions that underpin OLTP/OLAP, event sourcing, and CQRS."),

    # ── Messaging & Coordination ──
    _r("Request-Reply",
       "https://www.enterpriseintegrationpatterns.com/patterns/messaging/RequestReply.html",
       "Hohpe & Woolf · Enterprise Integration Patterns", "patterns:res:request-response:eip", ["request-response"], "Discussion",
       "The canonical EIP catalog entry — synchronous request/response in messaging terms, including correlation IDs."),
    _r("Work Queues",
       "https://www.rabbitmq.com/tutorials/tutorial-two-python",
       "RabbitMQ docs", "patterns:res:work-queues:rabbitmq", ["work-queues"], "Tutorial",
       "Reference tutorial on competing-consumer worker pools backed by a durable queue."),
    _r("Avoiding insurmountable queue backlogs",
       "https://aws.amazon.com/builders-library/avoiding-insurmountable-queue-backlogs/",
       "David Yanacek · AWS Builders' Library", "patterns:res:dead-letter-queue:aws-builders", ["dead-letter-queue", "work-queues", "load-shedding"], "Best Practices",
       "Yanacek on queue design that survives backlogs — dead-letter queues, prioritization, and shedding."),
    _r("Amazon SQS Dead-Letter Queues",
       "https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html",
       "AWS docs", "patterns:res:dead-letter-queue:sqs-docs", ["dead-letter-queue"], "Tutorial",
       "The mechanism reference: how DLQs are configured, when messages move there, and how to drain them."),
    _r("Implementing Stripe-like idempotency keys in Postgres",
       "https://brandur.org/idempotency-keys",
       "Brandur Leach", "patterns:res:idempotency-keys:brandur", ["idempotency-keys"], "Tutorial",
       "Brandur's worked-example essay on building Stripe-style idempotency at the application level."),
    _r("API Idempotency",
       "https://stripe.com/docs/api/idempotent_requests",
       "Stripe API docs", "patterns:res:idempotency-keys:stripe-docs", ["idempotency-keys"], "Best Practices",
       "Stripe's public API documentation on idempotency keys — the de facto reference clients are built against."),
    _r("Choreography vs Orchestration",
       "https://microservices.io/patterns/data/saga.html",
       "microservices.io", "patterns:res:choreography-orchestration:microservices-io", ["choreography-orchestration"], "Best Practices",
       "Richardson's framing of the two saga coordination styles, the trade-offs, and when each one collapses under load."),

    # ── Topology ──
    _r("MonolithFirst",
       "https://martinfowler.com/bliki/MonolithFirst.html",
       "Martin Fowler", "patterns:res:monolith:fowler-monolith-first", ["monolith", "microservices-too-early"], "Discussion",
       "Fowler's case for starting with a monolith and only splitting once the domain has been understood."),
    _r("Microservices",
       "https://martinfowler.com/articles/microservices.html",
       "Lewis & Fowler", "patterns:res:microservices:fowler-microservices", ["microservices"], "Discussion",
       "The 2014 article that named and characterized the style — still the most-cited definitional reference."),
    _r("Building Microservices, 2nd Edition",
       "https://samnewman.io/books/building_microservices_2nd_edition/",
       "Sam Newman · O'Reilly", "patterns:res:microservices:newman-book", ["microservices", "bff"], "Tutorial",
       "Newman's reference text covering boundaries, deployment, integration, and operational reality of microservices."),
    _r("Modular Monolith: A Primer",
       "https://www.kamilgrzybek.com/blog/posts/modular-monolith-primer",
       "Kamil Grzybek", "patterns:res:modular-monolith:grzybek-primer", ["modular-monolith"], "Tutorial",
       "Grzybek's canonical primer with worked example showing module boundaries enforced inside one deployable."),
    _r("Pattern: Backends for Frontends",
       "https://samnewman.io/patterns/architectural/bff/",
       "Sam Newman", "patterns:res:bff:newman", ["bff"], "Discussion",
       "Newman's original BFF essay — the case for per-client backends rather than one API to rule them all."),
    _r("StranglerFigApplication",
       "https://martinfowler.com/bliki/StranglerFigApplication.html",
       "Martin Fowler", "patterns:res:strangler-fig-boundary:fowler", ["strangler-fig-boundary", "strangler-fig-system"], "Discussion",
       "Fowler's bliki entry naming the pattern after Alexander's strangler fig — wrap, replace, delete."),
    _r("What is Istio?",
       "https://istio.io/latest/about/service-mesh/",
       "Istio docs", "patterns:res:service-mesh:istio-about", ["service-mesh", "sidecar"], "Discussion",
       "Istio's own framing of the service-mesh problem and how a control plane plus sidecars solve it."),
    _r("Linkerd Overview",
       "https://linkerd.io/2-edge/overview/",
       "Linkerd docs", "patterns:res:service-mesh:linkerd-overview", ["service-mesh"], "Tutorial",
       "Linkerd's overview — the lighter, Rust-based alternative to Istio with the same shape."),
    _r("Sidecar pattern",
       "https://learn.microsoft.com/en-us/azure/architecture/patterns/sidecar",
       "Microsoft Learn", "patterns:res:sidecar:msft", ["sidecar"], "Best Practices",
       "Microsoft's catalog entry on the sidecar deployment pattern, with concrete examples and trade-offs."),

    # ── Resilience at Scale ──
    _r("Bulkhead architectures",
       "https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/use-bulkhead-architectures-to-limit-scope-of-impact.html",
       "AWS Well-Architected Framework", "patterns:res:bulkhead-service:aws-wa", ["bulkhead-service"], "Best Practices",
       "AWS Well-Architected guidance on partitioning so a failure in one cell doesn't take down the others."),
    _r("Cell-based Architecture",
       "https://docs.aws.amazon.com/wellarchitected/latest/reducing-scope-of-impact-with-cell-based-architecture/reducing-scope-of-impact-with-cell-based-architecture.html",
       "AWS Well-Architected Framework", "patterns:res:bulkhead-service:cell-based", ["bulkhead-service"], "Best Practices",
       "AWS's whitepaper on cell-based architecture — the scaled version of the bulkhead pattern at AWS itself."),
    _r("Circuit Breaker pattern",
       "https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker",
       "Microsoft Learn", "patterns:res:circuit-breaker-service:msft", ["circuit-breaker-service"], "Best Practices",
       "Microsoft's reference for circuit breaker at the service level, with state diagram and trade-offs."),
    _r("Reactive Manifesto: Back-Pressure",
       "https://www.reactivemanifesto.org/glossary#Back-Pressure",
       "Reactive Manifesto", "patterns:res:back-pressure:reactive-manifesto", ["back-pressure"], "Discussion",
       "The reactive-systems glossary's definition of back-pressure — the place the term entered the working vocabulary."),
    _r("Handling overload",
       "https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/",
       "David Yanacek · AWS Builders' Library", "patterns:res:load-shedding:aws-builders", ["load-shedding"], "Best Practices",
       "Yanacek on load shedding: pick what to drop near capacity so the critical path stays inside its budget."),
    _r("Static stability using Availability Zones",
       "https://aws.amazon.com/builders-library/static-stability-using-availability-zones/",
       "Becky Weiss & Mike Furr · AWS Builders' Library", "patterns:res:graceful-degradation:aws-static-stability", ["graceful-degradation"], "Best Practices",
       "AWS's framing of static stability — design so a dependency's failure changes nothing observable."),

    # ── Evolution & Migration ──
    _r("Evolutionary Database Design",
       "https://martinfowler.com/articles/evodb.html",
       "Sadalage & Fowler", "patterns:res:expand-contract-schema:evodb", ["expand-contract-schema", "expand-contract-discipline"], "Discussion",
       "Sadalage and Fowler's foundational article on evolving schemas alongside code via expand/contract."),
    _r("Refactoring Databases",
       "https://databaserefactoring.com/",
       "Scott Ambler & Pramod Sadalage", "patterns:res:expand-contract-schema:db-refactoring", ["expand-contract-schema", "expand-contract-discipline"], "Tutorial",
       "Companion catalog to the Refactoring Databases book — concrete database-level refactorings with steps."),
    _r("ParallelChange",
       "https://martinfowler.com/bliki/ParallelChange.html",
       "Danilo Sato (on Fowler's bliki)", "patterns:res:parallel-change:fowler-parallel-change", ["parallel-change", "parallel-run"], "Discussion",
       "The definitional entry: expand, migrate, contract — making breaking changes a three-step backward-compatible process."),
    _r("What are dark launches and how do they work?",
       "https://launchdarkly.com/blog/what-are-dark-launches-and-how-do-they-work/",
       "LaunchDarkly blog", "patterns:res:dark-launches:launchdarkly", ["dark-launches"], "Tutorial",
       "LaunchDarkly's working definition of dark launches and where they sit alongside feature flags and canaries."),
    _r("Feature Toggles (aka Feature Flags)",
       "https://martinfowler.com/articles/feature-toggles.html",
       "Pete Hodgson · Fowler's site", "patterns:res:feature-flags-architecture:hodgson", ["feature-flags-architecture", "feature-toggles-discipline"], "Tutorial",
       "Hodgson's reference taxonomy — release, ops, experiment, and permission toggles, and how their lifetimes differ."),

    # ── Change Patterns ──
    _r("BranchByAbstraction",
       "https://martinfowler.com/bliki/BranchByAbstraction.html",
       "Paul Hammant (on Fowler's bliki)", "patterns:res:branch-by-abstraction:hammant", ["branch-by-abstraction"], "Discussion",
       "The bliki entry codifying the technique — hide what's being replaced, swap, then collapse the seam."),
    _r("Continuous Delivery",
       "https://martinfowler.com/books/continuousDelivery.html",
       "Humble & Farley · Addison-Wesley", "patterns:res:branch-by-abstraction:humble-farley", ["branch-by-abstraction", "feature-toggles-discipline", "parallel-change"], "Tutorial",
       "The book that put trunk-based development, feature toggles, and branch by abstraction into the mainstream."),
    _r("Expand and Contract",
       "https://www.tim-evans.co.uk/expand-and-contract/",
       "Tim Evans", "patterns:res:expand-contract-discipline:evans", ["expand-contract-discipline"], "Tutorial",
       "Working-engineer walkthrough of the discipline as a three-step ritual for schema and API changes."),
    _r("API Versioning at Stripe",
       "https://stripe.com/blog/api-versioning",
       "Brandur Leach \u00b7 Stripe", "patterns:res:expand-contract-discipline:brandur-versioning", ["expand-contract-discipline"], "Discussion",
       "Stripe's approach to additive API versioning and deprecation windows \u2014 the API-layer expression of expand/contract."),
    _r("Feature Toggles in Practice",
       "https://www.split.io/blog/feature-flag-best-practices/",
       "Split.io blog", "patterns:res:feature-toggles-discipline:split", ["feature-toggles-discipline"], "Best Practices",
       "Working-engineer guide to keeping flags healthy in production: ownership, retirement, and limits."),

    # ── Review & Verification ──
    _r("Code Review Developer Guide",
       "https://google.github.io/eng-practices/review/",
       "Google Engineering Practices", "patterns:res:review-as-conversation:google", ["review-as-conversation"], "Best Practices",
       "Google's published guidance on review as a fast, collaborative pass that's primarily about understanding."),
    _r("On Being a Senior Engineer",
       "https://blog.danslimmon.com/2015/12/01/on-being-a-senior-engineer/",
       "Dan Slimmon", "patterns:res:review-as-conversation:slimmon-senior", ["review-as-conversation"], "Discussion",
       "Slimmon on the reviewer's job as shared understanding, not gatekeeping \u2014 the texture beneath 'review-as-conversation.'"),
    _r("What we've learned from doing code review at Stripe",
       "https://stripe.com/blog/code-review",
       "Stripe Engineering", "patterns:res:review-as-conversation:stripe", ["review-as-conversation"], "Discussion",
       "Stripe's reflection on tuning review for a high-trust IC-heavy org and the queue-time cost of gate-shaped review."),
    _r("Working Effectively with Legacy Code, Chapter on Characterization Tests",
       "https://www.oreilly.com/library/view/working-effectively-with/0131177052/",
       "Michael Feathers · Prentice Hall", "patterns:res:characterization-tests:feathers", ["characterization-tests"], "Tutorial",
       "Feathers' canonical chapter introducing characterization tests as the safety harness for changing untrusted code."),
    _r("Snapshot Testing",
       "https://jestjs.io/docs/snapshot-testing",
       "Jest docs", "patterns:res:golden-snapshot-tests:jest", ["golden-snapshot-tests", "snapshot-golden-agents"], "Tutorial",
       "Jest's documentation — the working-engineer reference for snapshot tests in mainstream JavaScript projects."),
    _r("Approval Testing",
       "https://approvaltests.com/",
       "ApprovalTests.com", "patterns:res:golden-snapshot-tests:approval", ["golden-snapshot-tests"], "Tutorial",
       "Cross-language site for approval/golden testing as a refactoring scaffold \u2014 the form Emily Bache popularized."),
    _r("Refactoring Legacy Code with the Gilded Rose Kata",
       "https://github.com/emilybache/GildedRose-Refactoring-Kata",
       "Emily Bache", "patterns:res:golden-snapshot-tests:bache-gilded-rose", ["golden-snapshot-tests"], "Tutorial",
       "Bache's most-cited kata \u2014 approval tests as the refactoring scaffold that lets you change legacy code safely."),
    _r("What is Property Based Testing?",
       "https://hypothesis.works/articles/what-is-property-based-testing/",
       "Hypothesis blog", "patterns:res:property-based-testing:hypothesis", ["property-based-testing", "property-tests-agent-fence"], "Tutorial",
       "Hypothesis project's working-engineer essay on what property-based testing actually is and why it pays off."),
    _r("QuickCheck: A Lightweight Tool for Random Testing of Haskell Programs",
       "https://www.cs.tufts.edu/~nr/cs257/archive/john-hughes/quick.pdf",
       "Claessen & Hughes \u00b7 ICFP 2000", "patterns:res:property-based-testing:quickcheck", ["property-based-testing"], "Discussion",
       "The original QuickCheck paper \u2014 the source of property-based testing as a working idea."),
    _r("Property Testing Like AFL",
       "https://www.hillelwayne.com/post/property-testing-afl/",
       "Hillel Wayne", "patterns:res:property-based-testing:wayne-afl", ["property-based-testing"], "Discussion",
       "Wayne on PBT as a design tool \u2014 writing the property is the work that pays off whether or not a bug is caught."),
    _r("Pitest: Real World Mutation Testing",
       "https://pitest.org/",
       "Pitest project", "patterns:res:mutation-testing:pitest", ["mutation-testing"], "Tutorial",
       "Henry Coles' Pitest documentation — the most pragmatic working-engineer source on mutation testing today."),

    # ── Operational Patterns ──
    _r("Runbook Template",
       "https://github.com/SkeltonThatcher/run-book-template",
       "Skelton & Thatcher", "patterns:res:runbooks-as-code:skelton-thatcher", ["runbooks-as-code"], "Tutorial",
       "Skelton & Thatcher's open-source runbook template — the working starting point for codifying ops knowledge."),
    _r("Monitoring Distributed Systems",
       "https://sre.google/sre-book/monitoring-distributed-systems/",
       "Google · SRE book", "patterns:res:four-golden-signals:sre-book", ["four-golden-signals"], "Best Practices",
       "The chapter that introduced 'four golden signals' as the minimum monitoring vocabulary for any service."),
    _r("Implementing SLOs",
       "https://sre.google/workbook/implementing-slos/",
       "Google · SRE workbook", "patterns:res:error-budgets:slo-workbook", ["error-budgets"], "Tutorial",
       "The canonical step-by-step for defining SLIs, SLOs, and burn-rate alerts that translate into error budgets."),
    _r("Error Budget Policies",
       "https://sre.google/workbook/error-budget-policy/",
       "Google · SRE workbook", "patterns:res:error-budgets:budget-policy", ["error-budgets"], "Best Practices",
       "The follow-up chapter — what to actually do when the budget is gone, written as a policy template."),
    _r("Blameless PostMortems and a Just Culture",
       "https://www.etsy.com/codeascraft/blameless-postmortems",
       "John Allspaw · Code as Craft", "patterns:res:blameless-postmortems:allspaw-etsy", ["blameless-postmortems"], "Discussion",
       "Allspaw's foundational essay — the source the rest of the postmortem literature footnotes."),
    _r("Being On-Call",
       "https://sre.google/sre-book/being-on-call/",
       "Google \u00b7 SRE book", "patterns:res:on-call-as-pattern:sre-book", ["on-call-as-pattern"], "Best Practices",
       "The SRE book chapter on on-call as a designed practice \u2014 page quality, rotations, and follow-up as artifacts."),
    _r("Distributed Systems Observability",
       "https://www.oreilly.com/library/view/distributed-systems-observability/9781492033431/",
       "Cindy Sridharan \u00b7 O'Reilly", "patterns:res:on-call-as-pattern:sridharan-observability", ["on-call-as-pattern"], "Discussion",
       "Sridharan's working-engineer treatment of what good observability buys an on-call rotation: alerts you can act on."),
    _r("PagerDuty Incident Response",
       "https://response.pagerduty.com/",
       "PagerDuty", "patterns:res:on-call-as-pattern:pagerduty-response", ["on-call-as-pattern"], "Best Practices",
       "PagerDuty's open-source incident-response training \u2014 IC roles, communications, and handoff as a working artifact."),

    # ── Anti-Patterns ──
    _r("Avoiding the Distributed Monolith",
       "https://www.jimmybogard.com/avoiding-the-distributed-monolith/",
       "Jimmy Bogard", "patterns:res:distributed-monolith:bogard", ["distributed-monolith"], "Discussion",
       "Bogard names the failure mode: microservices that must deploy together — the worst of both shapes."),
    _r("Microservices",
       "https://martinfowler.com/articles/microservices.html",
       "Lewis & Fowler", "patterns:res:distributed-monolith:fowler", ["distributed-monolith", "microservices-too-early"], "Discussion",
       "The original article — read with hindsight, contains the warnings about premature service boundaries baked in."),
    _r("God Class",
       "https://wiki.c2.com/?GodClass",
       "WikiWikiWeb / c2.com", "patterns:res:god-object:c2", ["god-object"], "Discussion",
       "The original portland-pattern-repository entry naming the anti-pattern of one omniscient class."),
    _r("Primitive Obsession",
       "https://refactoring.guru/smells/primitive-obsession",
       "refactoring.guru", "patterns:res:primitive-obsession:refactoring-guru", ["primitive-obsession"], "Tutorial",
       "Smell catalog entry on modeling domain concepts with raw primitives, with refactoring moves to escape it."),
    _r("AnemicDomainModel",
       "https://martinfowler.com/bliki/AnemicDomainModel.html",
       "Martin Fowler", "patterns:res:anemic-domain-model:fowler", ["anemic-domain-model"], "Discussion",
       "Fowler's bliki entry naming the anti-pattern: domain objects reduced to data bags with logic scattered into services."),
    _r("The Wrong Abstraction",
       "https://sandimetz.com/blog/2016/1/20/the-wrong-abstraction",
       "Sandi Metz", "patterns:res:premature-abstraction:metz", ["premature-abstraction", "dry-at-all-costs"], "Discussion",
       "Metz's essay: duplication is far cheaper than the wrong abstraction — the working-engineer answer to over-DRY."),
    _r("DRY is about Knowledge",
       "https://verraes.net/2014/08/dry-is-about-knowledge/",
       "Mathias Verraes", "patterns:res:dry-at-all-costs:verraes", ["dry-at-all-costs"], "Discussion",
       "Verraes restating DRY in terms of knowledge — the case that 'two pieces of code look alike' isn't a reason to merge them."),
    _r("Code is Data",
       "https://nedbatchelder.com/blog/202104/code_is_just_data.html",
       "Ned Batchelder", "patterns:res:config-driven-development:batchelder", ["config-driven-development"], "Discussion",
       "Batchelder's essay arguing that 'just config' is just code without the tooling that makes code maintainable."),
    _r("Big Ball of Mud",
       "http://www.laputan.org/mud/",
       "Foote & Yoder", "patterns:res:big-ball-of-mud:foote-yoder", ["big-ball-of-mud"], "Discussion",
       "The 1997 paper that named the most common production architecture — emergent, unstructured, somehow still shipping."),
    _r("Singletons are Evil",
       "https://wiki.c2.com/?SingletonsAreEvil",
       "WikiWikiWeb / c2.com", "patterns:res:singleton-as-global-state:c2", ["singleton-as-global-state"], "Discussion",
       "Long-running c2 wiki thread cataloguing why singletons end up being global state by another name."),
    _r("Stringly Typed",
       "https://wiki.c2.com/?StringlyTyped",
       "WikiWikiWeb / c2.com", "patterns:res:stringly-typed-code:c2", ["stringly-typed-code"], "Discussion",
       "The naming of the anti-pattern: encoding meaning into strings the compiler can't see — joke and warning at once."),
    _r("Speculative Generality",
       "https://refactoring.guru/smells/speculative-generality",
       "refactoring.guru", "patterns:res:speculative-generality:refactoring-guru", ["speculative-generality"], "Tutorial",
       "Smell catalog entry — hooks and indirection added 'just in case' that arrives only as confusion for later readers."),

]


# ─── CREATORS ────────────────────────────────────────────────────────────────

POP_PEOPLE: list[Person] = [
    Person(
        "Martin Fowler", "martinfowler", "blog", "https://martinfowler.com/",
        "patterns:person:blog:fowler",
        "The reference voice on enterprise patterns, refactoring, and the bliki that names half this directory.",
    ),
    Person(
        "Marc Brooker", "MarcJBrooker", "blog", "https://brooker.co.za/blog/",
        "patterns:person:blog:brooker",
        "AWS distinguished engineer. The canonical working-voice on retries, jitter, queues, and resilience trade-offs.",
    ),
    Person(
        "Brandur Leach", "brandur", "blog", "https://brandur.org/",
        "patterns:person:blog:brandur",
        "Ex-Stripe engineer writing concrete walkthroughs of idempotency, API versioning, and database-shaped reliability.",
    ),
    Person(
        "Pete Hodgson", "ph1", "blog", "https://blog.thepete.net/",
        "patterns:person:blog:hodgson",
        "Independent consultant; author of the canonical feature toggles taxonomy and writer on incremental change.",
    ),
    Person(
        "Jessica Kerr", "jessitron", "blog", "https://jessitron.com/",
        "patterns:person:blog:jessitron",
        "Systems-thinker and Honeycomb dev advocate. Symmathesy, socio-technical patterns, and code-as-conversation.",
    ),
    Person(
        "Hillel Wayne", "hillelogram", "blog", "https://www.hillelwayne.com/",
        "patterns:person:blog:hillel-wayne",
        "Formal-methods working engineer writing accessibly about TLA+, property testing, and types-as-fences.",
    ),
    Person(
        "Will Larson", "lethain", "blog", "https://lethain.com/",
        "patterns:person:blog:lethain",
        "Engineering leader writing concrete frameworks for tech-debt strategy, on-call, and platform investment.",
    ),
    Person(
        "Sam Newman", "samnewman", "blog", "https://samnewman.io/",
        "patterns:person:blog:newman",
        "Author of Building Microservices and Monolith to Microservices; the working reference on service boundaries.",
    ),
    Person(
        "Cindy Sridharan", "copyconstruct", "blog", "https://copyconstruct.medium.com/",
        "patterns:person:blog:copyconstruct",
        "Distributed-systems and observability writing; essays on testing in production and reliability patterns.",
    ),
    Person(
        "Pat Helland", "phelland", "blog", "https://pathelland.substack.com/",
        "patterns:person:blog:helland",
        "Amazon Distinguished Engineer; canonical papers on data-on-the-outside, distributed transactions, and idempotency.",
    ),
    Person(
        "Michael Feathers", "mfeathers", "blog", "https://michaelfeathers.silvrback.com/",
        "patterns:person:blog:feathers",
        "Author of Working Effectively with Legacy Code; the working-engineer voice on safe change in untrusted code.",
    ),
    Person(
        "Kent Beck", "KentBeck", "blog", "https://tidyfirst.substack.com/",
        "patterns:person:blog:beck",
        "Author of Implementation Patterns and XP. Writing now at Tidy First on small structural moves that compound.",
    ),
]


# ─── FAQs ────────────────────────────────────────────────────────────────────

POP_FAQS: list[FAQ] = [
    FAQ(
        "How do I choose a pattern instead of writing one from scratch?",
        "The honest version: read the catalog before you start, then ignore "
        "it while you write the obvious solution, then come back when the "
        "obvious solution starts to hurt. Patterns are most useful as names "
        "for a shape you've already drawn at least once — they shorten the "
        "next conversation about it, they don't replace the first attempt. "
        "Fowler's Patterns of Enterprise Application Architecture is the "
        "best 'read once, refer back forever' starting place.",
        source_label="Martin Fowler: Patterns of Enterprise Application Architecture",
        source_url="https://martinfowler.com/books/eaa.html",
        source_key="patterns:faq:fowler-patterns",
    ),
    FAQ(
        "When is a 'microservice' actually a distributed monolith in disguise?",
        "When changing one service requires deploying others in lockstep, "
        "you're paying the operational cost of distribution and keeping the "
        "coupling of a monolith. The diagnostic: if every release ships "
        "more than one service, or if your release notes are organized by "
        "'changes in services A, B, and C this week,' the seams are wrong. "
        "Fowler's MonolithFirst remains the cleanest argument for delaying "
        "the split until you've actually learned where the boundaries are.",
        source_label="Martin Fowler: MonolithFirst",
        source_url="https://martinfowler.com/bliki/MonolithFirst.html",
        source_key="patterns:faq:fowler-monolith-first",
    ),
    FAQ(
        "How do I migrate a schema without an outage?",
        "Expand and contract: add the new shape, dual-write to both, "
        "migrate readers, then remove the old. Each step is a deploy in "
        "its own right; you never do more than one shape-changing step at "
        "a time. The same dance applies to APIs (parallel change) and to "
        "system-level rewrites (strangler fig). Sadalage and Fowler's "
        "evolutionary database design essay is the reference, and Tim "
        "Evans' walkthrough is the cleanest working-engineer post on the "
        "discipline.",
        source_label="Evolutionary Database Design (Sadalage & Fowler)",
        source_url="https://martinfowler.com/articles/evodb.html",
        source_key="patterns:faq:evodb",
    ),
    FAQ(
        "Are feature flags worth the complexity they add?",
        "If you treat them as code with a lifecycle, yes; if you treat "
        "them as runtime configuration with no expiry, no. The flags that "
        "pay off are short-lived release toggles that come out within a "
        "release or two, and a small handful of long-lived ops toggles "
        "that you actually exercise. Pete Hodgson's reference taxonomy "
        "separates the four kinds and is the right place to start. The "
        "anti-pattern is a flag graveyard where nobody knows which "
        "branches are live in production.",
        source_label="Pete Hodgson: Feature Toggles",
        source_url="https://martinfowler.com/articles/feature-toggles.html",
        source_key="patterns:faq:hodgson-toggles",
    ),
    FAQ(
        "When should I reach for property-based testing instead of examples?",
        "When you can state an invariant — a sentence of the form 'for "
        "any input that looks like X, the output must satisfy Y' — and "
        "the cost of finding the edge cases yourself is too high. PBT is "
        "especially good for parsers, serializers, and pure functions on "
        "domain types; less good when the function's behavior is "
        "irreducibly example-shaped. The Hypothesis project's intro essay "
        "is the working-engineer entry point, and Wlaschin's railway "
        "oriented programming pieces compose well with it.",
        source_label="What is Property Based Testing?",
        source_url="https://hypothesis.works/articles/what-is-property-based-testing/",
        source_key="patterns:faq:pbt-hypothesis",
    ),
    FAQ(
        "Why does refactoring legacy code keep producing regressions?",
        "Because you're changing behavior without a harness, and you "
        "don't yet have a definition of what 'unchanged' means. The "
        "answer Michael Feathers gave twenty years ago still holds: "
        "characterize the current behavior with tests before you change "
        "anything, even when those tests pin down behavior nobody likes. "
        "Then change the system in seams the tests cover. The Working "
        "Effectively with Legacy Code chapters on characterization tests "
        "and seams are the canonical reference.",
        source_label="Working Effectively with Legacy Code",
        source_url="https://www.oreilly.com/library/view/working-effectively-with/0131177052/",
        source_key="patterns:faq:feathers-legacy-code",
    ),
    FAQ(
        "Where does an anti-pattern stop being a smell and start being a crisis?",
        "When the cost of moving away from it exceeds the cost of "
        "anything else you'd do this quarter, and when each new feature "
        "is paying interest on the same structural debt. Anti-patterns "
        "rarely cause incidents; they cause slowdown that's invisible "
        "until you compare your delivery cadence to a peer team's. The "
        "Big Ball of Mud paper is the most honest description of how the "
        "shape arrives — by no decision, by every decision being local "
        "and time-pressured. The cure is always a sequence of small, "
        "named patterns above, applied with patience.",
        source_label="Foote & Yoder: Big Ball of Mud",
        source_url="http://www.laputan.org/mud/",
        source_key="patterns:faq:big-ball-of-mud",
    ),
]

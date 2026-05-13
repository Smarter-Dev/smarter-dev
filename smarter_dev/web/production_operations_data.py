"""Curated content for /resources/production-operations.

Receives the Observability + Auth & Secrets sections from the System
Architecture directory, plus the spine entries, creators, and FAQs that
fit operations rather than design. Early draft — incident response,
logging pipelines, the modern auth wave, and secret managers will land
as that work is done.
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

_INDEXED = date(2026, 5, 12)


def _r(title, url, source, key, tool_slugs, learning_type, blurb=""):
    return ArchToolResource(
        title=title, url=url, source=source, key=key,
        tool_slugs=tuple(tool_slugs), learning_type=learning_type,
        first_indexed_at=_INDEXED, blurb=blurb,
    )


# ─── CATEGORIES ──────────────────────────────────────────────────────────────

OPS_CATEGORIES: list[ArchCategory] = [
    ArchCategory(
        slug="observability",
        name="Observability",
        intro=(
            "The three pillars (metrics, logs, traces) are a taxonomy, not a "
            "plan. What you want is structured events you can slice "
            "arbitrarily: OpenTelemetry to instrument, plus a backend that "
            "handles high-cardinality (Honeycomb, Logfire) or a predictable "
            "metrics stack (Prometheus + Grafana + Loki). Start with one "
            "signal you'll actually look at."
        ),
        tools=(
            ArchTool("prometheus", "Prometheus",
                     "https://prometheus.io/",
                     "ops:tool:prometheus:home",
                     "Pull-based metrics collection. Deeply integrated with the Kubernetes ecosystem."),
            ArchTool("grafana", "Grafana",
                     "https://grafana.com/",
                     "ops:tool:grafana:home",
                     "Dashboard tool that fronts Prometheus, Loki, and most observability backends."),
            ArchTool("opentelemetry", "OpenTelemetry",
                     "https://opentelemetry.io/",
                     "ops:tool:opentelemetry:home",
                     "Vendor-neutral instrumentation standard for traces, metrics, and logs."),
            ArchTool("loki", "Grafana Loki",
                     "https://grafana.com/oss/loki/",
                     "ops:tool:loki:home",
                     "Log aggregation system designed to pair with Prometheus and Grafana."),
            ArchTool("logfire", "Pydantic Logfire",
                     "https://pydantic.dev/logfire",
                     "ops:tool:logfire:home",
                     "Observability platform from the Pydantic team. Structured tracing, OpenTelemetry-native."),
            ArchTool("honeycomb", "Honeycomb",
                     "https://www.honeycomb.io/",
                     "ops:tool:honeycomb:home",
                     "High-cardinality structured-event observability. Pioneered the \"observability 2.0\" frame."),
        ),
    ),
    ArchCategory(
        slug="auth",
        name="Auth & secrets",
        intro=(
            "Auth is one of the few things worth outsourcing early. Auth0 if "
            "you need it now. Keycloak or Ory for OSS you self-host. Vault is "
            "for secrets at scale; you'll outgrow .env faster than you expect."
        ),
        tools=(
            ArchTool("vault", "HashiCorp Vault",
                     "https://www.vaultproject.io/",
                     "ops:tool:vault:home",
                     "Secrets management: dynamic credentials, encryption-as-a-service, deep audit."),
            ArchTool("keycloak", "Keycloak",
                     "https://www.keycloak.org/",
                     "ops:tool:keycloak:home",
                     "Self-hostable identity and access management. Full OAuth, OIDC, SAML support."),
            ArchTool("ory", "Ory",
                     "https://www.ory.sh/",
                     "ops:tool:ory:home",
                     "Modern OSS identity stack split into composable services (Kratos, Hydra, Oathkeeper)."),
            ArchTool("auth0", "Auth0",
                     "https://auth0.com/",
                     "ops:tool:auth0:home",
                     "Managed identity-as-a-service. Fastest to integrate; expensive at scale."),
        ),
    ),
]


# ─── SPINE ───────────────────────────────────────────────────────────────────

OPS_SPINE_RESOURCES: list[ArchResource] = [
    ArchResource(
        "Performance Analysis Methodology",
        "https://www.brendangregg.com/methodology.html",
        "Brendan Gregg",
        "ops:spine:gregg-methodology",
        learning_type="Best Practices",
        first_indexed_at=_INDEXED,
        blurb="USE Method and other systematic approaches to finding real performance bottlenecks fast.",
    ),
    ArchResource(
        "Static Stability Using Availability Zones",
        "https://aws.amazon.com/builders-library/static-stability-using-availability-zones/",
        "AWS Builders' Library",
        "ops:spine:static-stability",
        learning_type="Best Practices",
        first_indexed_at=_INDEXED,
        blurb="Design so a dependency's failure changes nothing. Pre-provision instead of reacting.",
    ),
]


# ─── PER-TOOL RESOURCES ──────────────────────────────────────────────────────

OPS_TOOL_RESOURCES: list[ArchToolResource] = [
    # Prometheus
    _r("Prometheus Getting Started",
       "https://prometheus.io/docs/prometheus/latest/getting_started/",
       "Prometheus docs", "ops:res:prometheus:getting-started", ["prometheus"], "Tutorial",
       "Install Prometheus, scrape targets, run PromQL queries, and configure your first alert."),
    _r("PromQL for Mere Mortals",
       "https://grafana.com/blog/2020/02/04/introduction-to-promql-the-prometheus-query-language/",
       "Grafana Labs blog", "ops:res:prometheus:promql-intro", ["prometheus"], "Tutorial",
       "Approachable intro to PromQL data types, selectors, rate, and aggregation operators."),

    # Grafana
    _r("Grafana Getting Started",
       "https://grafana.com/docs/grafana/latest/getting-started/",
       "Grafana docs", "ops:res:grafana:getting-started", ["grafana"], "Tutorial",
       "Install Grafana, connect a data source, build dashboards, and configure alerting."),

    # OpenTelemetry
    _r("OpenTelemetry Getting Started",
       "https://opentelemetry.io/docs/getting-started/",
       "OpenTelemetry docs", "ops:res:opentelemetry:getting-started", ["opentelemetry"], "Tutorial",
       "Instrument an app with traces, metrics, and logs using the Collector and language SDKs."),
    _r("OpenTelemetry Demo",
       "https://opentelemetry.io/docs/demo/",
       "OpenTelemetry docs", "ops:res:opentelemetry:demo", ["opentelemetry"], "Tutorial",
       "Microservices reference app showing real instrumentation across many languages and signals."),

    # Loki
    _r("Grafana Loki Get Started",
       "https://grafana.com/docs/loki/latest/get-started/",
       "Grafana docs", "ops:res:loki:get-started", ["loki"], "Tutorial",
       "Install Loki, ship logs with Promtail or Alloy, and query them with LogQL in Grafana."),

    # Logfire
    _r("Pydantic Logfire Documentation",
       "https://logfire.pydantic.dev/docs/",
       "Pydantic docs", "ops:res:logfire:docs", ["logfire"], "Tutorial",
       "Install Logfire, instrument Python apps, and view structured traces and logs in the UI."),

    # Honeycomb
    _r("Honeycomb Get Started",
       "https://docs.honeycomb.io/get-started/",
       "Honeycomb docs", "ops:res:honeycomb:get-started", ["honeycomb"], "Tutorial",
       "Send events via OpenTelemetry, run BubbleUp queries, and investigate production issues."),
    _r("Observability Engineering",
       "https://www.honeycomb.io/wp-content/uploads/2022/05/observability-engineering-honeycomb.pdf",
       "Honeycomb · O'Reilly", "ops:res:honeycomb:observability-engineering", ["honeycomb"], "Best Practices",
       "Charity Majors et al. on high-cardinality events, SLOs, and modern observability practice."),

    # Vault
    _r("Vault Tutorials",
       "https://developer.hashicorp.com/vault/tutorials",
       "HashiCorp Developer", "ops:res:vault:tutorials", ["vault"], "Tutorial",
       "Hands-on tutorials for KV secrets, dynamic database creds, transit encryption, and auth methods."),
    _r("Vault Production Hardening",
       "https://developer.hashicorp.com/vault/tutorials/operations/production-hardening",
       "HashiCorp Developer", "ops:res:vault:production-hardening", ["vault"], "Best Practices",
       "Official checklist: end-to-end TLS, root token rotation, auditing, and least-privilege policies."),

    # Keycloak
    _r("Keycloak Getting Started (Docker)",
       "https://www.keycloak.org/getting-started/getting-started-docker",
       "Keycloak docs", "ops:res:keycloak:docker-start", ["keycloak"], "Tutorial",
       "Run Keycloak in Docker, create a realm, register a client, and secure a sample app."),
    _r("Keycloak Server Administration Guide",
       "https://www.keycloak.org/docs/latest/server_admin/",
       "Keycloak docs", "ops:res:keycloak:server-admin", ["keycloak"], "Best Practices",
       "Reference for realms, clients, identity brokering, user federation, and authentication flows."),

    # Ory
    _r("Ory Documentation",
       "https://www.ory.sh/docs/",
       "Ory docs", "ops:res:ory:docs", ["ory"], "Tutorial",
       "Get started with Ory Kratos identities, Hydra OAuth2/OIDC, Keto permissions, and Oathkeeper."),

    # Auth0
    _r("Auth0 Get Started",
       "https://auth0.com/docs/get-started",
       "Auth0 docs", "ops:res:auth0:get-started", ["auth0"], "Tutorial",
       "Set up a tenant, create applications, and integrate login via Universal Login and SDKs."),
    _r("Auth0 Architecture Scenarios",
       "https://auth0.com/docs/get-started/architecture-scenarios",
       "Auth0 docs", "ops:res:auth0:architecture-scenarios", ["auth0"], "Best Practices",
       "Reference architectures for SPA+API, mobile+API, and B2B/B2C identity scenarios."),
]


# ─── CREATORS ────────────────────────────────────────────────────────────────

OPS_PEOPLE: list[Person] = [
    Person(
        "Brendan Gregg", "brendangregg", "blog", "https://www.brendangregg.com/",
        "ops:person:blog:brendangregg",
        "The reference for Linux performance, flame graphs, and BPF observability.",
    ),
    Person(
        "Charity Majors", "mipsytipsy", "blog", "https://charity.wtf/",
        "ops:person:blog:charity",
        "Honeycomb co-founder. Defines what modern observability means and where it's going.",
    ),
    Person(
        "Jessica Kerr", "jessitron", "blog", "https://jessitron.com/",
        "ops:person:blog:jessitron",
        "Honeycomb dev advocate; systems-thinking essays on observability and socio-technical design.",
    ),
]


# ─── FAQs ────────────────────────────────────────────────────────────────────

OPS_FAQS: list[FAQ] = [
    FAQ(
        "What's the smallest observability stack that's actually useful?",
        "One signal you'll actually look at, instrumented well, beats four "
        "signals you glance at during incidents. Charity Majors frames this "
        "as observability 1.0 vs. 2.0: the three pillars (metrics, logs, "
        "traces) are 1.0. Structured wide events you can slice arbitrarily "
        "are 2.0. For most teams, OpenTelemetry to instrument plus one "
        "backend that handles high-cardinality (Honeycomb, Logfire) is the "
        "smallest setup that pays off. Add the other pillars once you're "
        "actually using the first one.",
        source_label="Charity Majors: Observability 1.0 vs 2.0",
        source_url="https://www.honeycomb.io/blog/one-key-difference-observability1dot0-2dot0",
        source_key="ops:faq:charity-observability-2",
    ),
    FAQ(
        "How do I think about reliability without overengineering?",
        "Static stability is the cleanest mental model: design so that when "
        "a dependency fails, your system behaves the same. Pre-provision "
        "instead of reacting. Pre-build instead of pulling at request time. "
        "Decide what works looks like when half your dependencies are down. "
        "Most overengineering is reacting to abstract failures instead of "
        "ones that have actually hurt you. Re-read your last three "
        "postmortems, then design for those.",
        source_label="AWS Builders' Library: Static Stability",
        source_url="https://aws.amazon.com/builders-library/static-stability-using-availability-zones/",
        source_key="ops:faq:aws-static-stability",
    ),
]

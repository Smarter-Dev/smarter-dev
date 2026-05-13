"""Curated content for /resources/production-operations.

The Keep-it-healthy layer of the resources index. Pairs with
/resources/system-architecture (the What) and /resources/infrastructure-hosting
(the Where).
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


def _s(title, url, source, key, learning_type, blurb=""):
    return ArchResource(
        title=title, url=url, source=source, key=key,
        learning_type=learning_type, first_indexed_at=_INDEXED, blurb=blurb,
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
            "signal you'll actually look at. What to instrument is a design "
            "decision (see "
            "<a href=\"/resources/system-architecture\">System Architecture</a>); "
            "this list is about the tools that store and query what you've chosen."
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
            ArchTool("tempo", "Grafana Tempo",
                     "https://grafana.com/oss/tempo/",
                     "ops:tool:tempo:home",
                     "Grafana's distributed tracing backend. Pairs with Loki and Mimir as the LGTM stack."),
            ArchTool("mimir", "Grafana Mimir",
                     "https://grafana.com/oss/mimir/",
                     "ops:tool:mimir:home",
                     "Horizontally-scalable Prometheus-compatible metrics backend for very large deployments."),
            ArchTool("logfire", "Pydantic Logfire",
                     "https://pydantic.dev/logfire",
                     "ops:tool:logfire:home",
                     "Observability platform from the Pydantic team. Structured tracing, OpenTelemetry-native."),
            ArchTool("honeycomb", "Honeycomb",
                     "https://www.honeycomb.io/",
                     "ops:tool:honeycomb:home",
                     "High-cardinality structured-event observability. Pioneered the \"observability 2.0\" frame."),
            ArchTool("datadog", "Datadog",
                     "https://www.datadoghq.com/",
                     "ops:tool:datadog:home",
                     "The commercial heavyweight. Observability plus APM plus security, with pricing to match."),
            ArchTool("sentry", "Sentry",
                     "https://sentry.io/",
                     "ops:tool:sentry:home",
                     "Error and performance monitoring. The default for catching frontend and backend exceptions."),
            ArchTool("new-relic", "New Relic",
                     "https://newrelic.com/",
                     "ops:tool:new-relic:home",
                     "Long-time APM vendor reorganized around usage-based pricing and a single observability platform."),
            ArchTool("better-stack", "Better Stack",
                     "https://betterstack.com/",
                     "ops:tool:better-stack:home",
                     "Combined logs, uptime, and dashboards in one commercial offering. Lightweight pricing."),
            ArchTool("axiom", "Axiom",
                     "https://axiom.co/",
                     "ops:tool:axiom:home",
                     "Log management and analytics with structured-event focus. The cheap-for-volume alternative."),
            ArchTool("signoz", "SigNoz",
                     "https://signoz.io/",
                     "ops:tool:signoz:home",
                     "Open-source full observability platform. The Datadog alternative if you want to self-host."),
        ),
    ),
    ArchCategory(
        slug="incident-response",
        name="Incident response & alerting",
        intro=(
            "The page going off at 3 AM is one piece. The other pieces are "
            "knowing who responds, what runbook they follow, how the team "
            "coordinates during the incident, what gets fixed afterward, "
            "and how customers find out. PagerDuty and Opsgenie are the "
            "legacy defaults; FireHydrant and Incident.io are the new wave "
            "with deeper opinions about coordination. Statuspage handles "
            "the outward-facing part."
        ),
        tools=(
            ArchTool("pagerduty", "PagerDuty",
                     "https://www.pagerduty.com/",
                     "ops:tool:pagerduty:home",
                     "The original on-call platform. Deepest integration ecosystem; expensive at scale."),
            ArchTool("opsgenie", "Opsgenie",
                     "https://www.atlassian.com/software/opsgenie",
                     "ops:tool:opsgenie:home",
                     "Atlassian's PagerDuty competitor. Strong if you're already in the Atlassian stack."),
            ArchTool("grafana-oncall", "Grafana OnCall",
                     "https://grafana.com/products/oncall/",
                     "ops:tool:grafana-oncall:home",
                     "OSS on-call scheduling. Pairs cleanly with Grafana Alertmanager."),
            ArchTool("firehydrant", "FireHydrant",
                     "https://firehydrant.com/",
                     "ops:tool:firehydrant:home",
                     "Incident response with strong runbook automation and Slack coordination."),
            ArchTool("incident-io", "Incident.io",
                     "https://incident.io/",
                     "ops:tool:incident-io:home",
                     "Slack-native incident management; opinionated about coordination patterns."),
            ArchTool("statuspage", "Atlassian Statuspage",
                     "https://www.atlassian.com/software/statuspage",
                     "ops:tool:statuspage:home",
                     "The outward-facing status-communication standard."),
            ArchTool("better-stack-incident", "Better Stack (incident management)",
                     "https://betterstack.com/incident-management",
                     "ops:tool:better-stack-incident:home",
                     "Combined uptime, on-call rotations, and status page in one lightweight offering."),
        ),
    ),
    ArchCategory(
        slug="logging",
        name="Logging pipelines",
        intro=(
            "Shipping logs from your servers to your backend is its own "
            "discipline. Vector and Fluent Bit dominate the open-source "
            "side. Fluentd is the older sibling that's still widely "
            "deployed. Cribl is the commercial heavyweight when you need "
            "to filter, transform, or route to multiple backends. The "
            "choice is usually about throughput and how much processing "
            "you want at the edge before logs hit storage."
        ),
        tools=(
            ArchTool("vector", "Vector",
                     "https://vector.dev/",
                     "ops:tool:vector:home",
                     "Datadog's Rust-based log and metric pipeline. The high-performance default for modern stacks."),
            ArchTool("fluent-bit", "Fluent Bit",
                     "https://fluentbit.io/",
                     "ops:tool:fluent-bit:home",
                     "Lightweight log collector and forwarder. The OpenTelemetry-adjacent default in Kubernetes."),
            ArchTool("fluentd", "Fluentd",
                     "https://www.fluentd.org/",
                     "ops:tool:fluentd:home",
                     "The older Ruby + C log aggregator. Still widely deployed; broader plugin ecosystem."),
            ArchTool("bento", "Bento",
                     "https://warpstreamlabs.github.io/bento/",
                     "ops:tool:bento:home",
                     "Stream-processing toolkit (formerly Benthos). Filter, transform, and route at the edge."),
            ArchTool("cribl", "Cribl Stream",
                     "https://cribl.io/stream/",
                     "ops:tool:cribl:home",
                     "Commercial heavyweight for routing logs to many backends with filter, transform, and replay."),
        ),
    ),
    ArchCategory(
        slug="performance",
        name="Performance & profiling",
        intro=(
            "Performance is the discipline observability won't teach you. "
            "Metrics tell you something's slow. Profiles tell you why. "
            "Tools split into continuous profiling (always running, "
            "sampling) and on-demand profiling (you reach for them during "
            "an incident). eBPF unlocks the kernel-level view that used "
            "to be a Brendan Gregg exclusive."
        ),
        tools=(
            ArchTool("pyroscope", "Grafana Pyroscope",
                     "https://grafana.com/oss/pyroscope/",
                     "ops:tool:pyroscope:home",
                     "Grafana's continuous profiling backend. eBPF-powered, language-agnostic."),
            ArchTool("parca", "Parca",
                     "https://www.parca.dev/",
                     "ops:tool:parca:home",
                     "OSS continuous profiling. Polar Signals' open-source foundation."),
            ArchTool("polar-signals", "Polar Signals",
                     "https://www.polarsignals.com/",
                     "ops:tool:polar-signals:home",
                     "Continuous profiling as a service. Pyroscope-compatible, hosted offering."),
            ArchTool("pixie", "Pixie",
                     "https://px.dev/",
                     "ops:tool:pixie:home",
                     "eBPF-based observability for Kubernetes. Auto-instrumented; no code changes."),
            ArchTool("datadog-profiler", "Datadog Continuous Profiler",
                     "https://docs.datadoghq.com/profiler/",
                     "ops:tool:datadog-profiler:home",
                     "Datadog's profiling product, deeply integrated with their APM and request tracing."),
            ArchTool("perf-flamegraph", "perf / flamegraph",
                     "https://www.brendangregg.com/flamegraphs.html",
                     "ops:tool:perf-flamegraph:home",
                     "Brendan Gregg's flamegraph tooling. The Linux profiling stack practitioners reach for."),
            ArchTool("bpftrace", "BCC / bpftrace",
                     "https://github.com/iovisor/bpftrace",
                     "ops:tool:bpftrace:home",
                     "eBPF-based dynamic tracing. Kernel-level visibility without recompiling the kernel."),
        ),
    ),
    ArchCategory(
        slug="identity",
        name="Identity & auth",
        intro=(
            "Auth is one of the few things worth outsourcing early. Auth0 "
            "if you need it now. Clerk and WorkOS are the new generation "
            "with better B2B ergonomics. Keycloak or Ory if you self-host. "
            "Supertokens, FusionAuth, and Logto are the lighter-weight "
            "self-hostable options. Decision is usually how much B2B "
            "complexity you have (SAML, SCIM, RBAC) and how much you want "
            "to pay someone else to manage it. The choice of how much "
            "identity you build into your domain model is an API decision "
            "(see "
            "<a href=\"/resources/system-architecture#apis\">APIs &amp; protocols</a>); "
            "this section is about who runs identity for you."
        ),
        tools=(
            ArchTool("auth0", "Auth0",
                     "https://auth0.com/",
                     "ops:tool:auth0:home",
                     "Managed identity-as-a-service. Fastest to integrate; expensive at scale."),
            ArchTool("clerk", "Clerk",
                     "https://clerk.com/",
                     "ops:tool:clerk:home",
                     "Developer-first auth-as-a-service. Excellent React/Next.js story and UI components."),
            ArchTool("workos", "WorkOS",
                     "https://workos.com/",
                     "ops:tool:workos:home",
                     "B2B-first auth: SSO, SCIM, audit logs. The \"enterprise-ready\" middleware."),
            ArchTool("stytch", "Stytch",
                     "https://stytch.com/",
                     "ops:tool:stytch:home",
                     "Passwordless-first auth platform. Strong on email magic links and B2B SSO."),
            ArchTool("keycloak", "Keycloak",
                     "https://www.keycloak.org/",
                     "ops:tool:keycloak:home",
                     "Self-hostable identity and access management. Full OAuth, OIDC, SAML support."),
            ArchTool("ory", "Ory",
                     "https://www.ory.sh/",
                     "ops:tool:ory:home",
                     "Modern OSS identity stack split into composable services (Kratos, Hydra, Oathkeeper)."),
            ArchTool("supertokens", "Supertokens",
                     "https://supertokens.com/",
                     "ops:tool:supertokens:home",
                     "Self-hostable auth library. Open source, modern, language-agnostic."),
            ArchTool("fusionauth", "FusionAuth",
                     "https://fusionauth.io/",
                     "ops:tool:fusionauth:home",
                     "Self-hostable auth platform. Generous free tier; deep CIAM features."),
            ArchTool("logto", "Logto",
                     "https://logto.io/",
                     "ops:tool:logto:home",
                     "Modern OSS auth and identity platform. The lighter alternative to Keycloak."),
        ),
    ),
    ArchCategory(
        slug="secrets",
        name="Secrets management",
        intro=(
            "Everyone outgrows .env files. The question is where you go "
            "next. Cloud-native secrets managers (AWS, GCP, Azure) win on "
            "integration if you're already in that cloud. Vault is the "
            "heavyweight when you need dynamic credentials and deep audit. "
            "Doppler and Infisical are the modern alternatives with better "
            "DX. SOPS and age handle the \"encrypt at rest, decrypt at "
            "deploy\" pattern for Git-backed secrets. Secrets at deploy "
            "time is a delivery concern (Software Delivery directory "
            "coming soon)."
        ),
        tools=(
            ArchTool("vault", "HashiCorp Vault",
                     "https://www.vaultproject.io/",
                     "ops:tool:vault:home",
                     "Secrets management: dynamic credentials, encryption-as-a-service, deep audit."),
            ArchTool("aws-secrets-manager", "AWS Secrets Manager",
                     "https://aws.amazon.com/secrets-manager/",
                     "ops:tool:aws-secrets-manager:home",
                     "AWS-native secrets storage with rotation. Deep IAM integration if you're in AWS."),
            ArchTool("gcp-secret-manager", "Google Cloud Secret Manager",
                     "https://cloud.google.com/secret-manager",
                     "ops:tool:gcp-secret-manager:home",
                     "GCP-native secrets storage. Versioned, IAM-controlled, integrated with Cloud Build."),
            ArchTool("1password-secrets", "1Password Secrets Automation",
                     "https://1password.com/developers",
                     "ops:tool:1password-secrets:home",
                     "1Password for app secrets. Strong developer UX; CLI + service tokens for CI."),
            ArchTool("doppler", "Doppler",
                     "https://www.doppler.com/",
                     "ops:tool:doppler:home",
                     "Modern secrets manager with team workflows. Sync to most platforms."),
            ArchTool("infisical", "Infisical",
                     "https://infisical.com/",
                     "ops:tool:infisical:home",
                     "Open-source Doppler alternative. Self-hostable, modern UI, growing ecosystem."),
            ArchTool("sops", "SOPS",
                     "https://getsops.io/",
                     "ops:tool:sops:home",
                     "Mozilla's secret-encryption tool. Encrypt YAML/JSON files in Git."),
            ArchTool("age", "age",
                     "https://github.com/FiloSottile/age",
                     "ops:tool:age:home",
                     "Modern file encryption tool. The cryptographic primitive under SOPS and many others."),
        ),
    ),
    ArchCategory(
        slug="network-security",
        name="Network security & firewalls",
        intro=(
            "The perimeter is everywhere. WAFs (Cloudflare, AWS) handle "
            "the edge. Host firewalls (iptables, nftables, ufw) handle "
            "the server. mTLS and service-mesh policies handle "
            "service-to-service. Let's Encrypt handles TLS certificates. "
            "Each layer is mandatory if you care about the layer beneath."
        ),
        tools=(
            ArchTool("cloudflare-waf", "Cloudflare WAF",
                     "https://www.cloudflare.com/application-services/products/waf/",
                     "ops:tool:cloudflare-waf:home",
                     "Edge WAF rules. The default for most teams already behind Cloudflare."),
            ArchTool("aws-waf", "AWS WAF",
                     "https://aws.amazon.com/waf/",
                     "ops:tool:aws-waf:home",
                     "AWS's WAF for CloudFront, ALB, and API Gateway. AWS-native rules and managed rule groups."),
            ArchTool("gcp-armor", "Google Cloud Armor",
                     "https://cloud.google.com/security/products/armor",
                     "ops:tool:gcp-armor:home",
                     "GCP's WAF and DDoS protection. Integrates with Load Balancing."),
            ArchTool("iptables", "iptables / nftables",
                     "https://netfilter.org/projects/nftables/",
                     "ops:tool:iptables:home",
                     "Linux's host firewall. The packet-filtering foundation everything else builds on."),
            ArchTool("ufw", "ufw (Uncomplicated Firewall)",
                     "https://help.ubuntu.com/community/UFW",
                     "ops:tool:ufw:home",
                     "Friendlier wrapper around iptables. The Ubuntu/Debian default for simple host firewall rules."),
            ArchTool("pfsense", "pfSense",
                     "https://www.pfsense.org/",
                     "ops:tool:pfsense:home",
                     "Open-source firewall and router OS. The default for serious on-prem firewalls."),
            ArchTool("opnsense", "OPNsense",
                     "https://opnsense.org/",
                     "ops:tool:opnsense:home",
                     "Fork of pfSense with a different update cadence and license posture."),
            ArchTool("letsencrypt", "Let's Encrypt",
                     "https://letsencrypt.org/",
                     "ops:tool:letsencrypt:home",
                     "Free, automated TLS certificate authority. The reason HTTPS is the default now."),
            ArchTool("linkerd", "Linkerd",
                     "https://linkerd.io/",
                     "ops:tool:linkerd:home",
                     "Service mesh with built-in mTLS. The lightweight alternative to Istio."),
        ),
    ),
]


# ─── SPINE ───────────────────────────────────────────────────────────────────
# Populated below — research filled in URLs and blurbs.

OPS_SPINE_RESOURCES: list[ArchResource] = []


# ─── PER-TOOL RESOURCES ─────────────────────────────────────────────────────
# Preserved existing entries for migrated tools; new tools' resources will
# be added once research returns.

OPS_TOOL_RESOURCES: list[ArchToolResource] = [
    # Prometheus (existing)
    _r("Prometheus Getting Started",
       "https://prometheus.io/docs/prometheus/latest/getting_started/",
       "Prometheus docs", "ops:res:prometheus:getting-started", ["prometheus"], "Tutorial",
       "Install Prometheus, scrape targets, run PromQL queries, and configure your first alert."),
    _r("PromQL for Mere Mortals",
       "https://grafana.com/blog/2020/02/04/introduction-to-promql-the-prometheus-query-language/",
       "Grafana Labs blog", "ops:res:prometheus:promql-intro", ["prometheus"], "Tutorial",
       "Approachable intro to PromQL data types, selectors, rate, and aggregation operators."),

    # Grafana (existing)
    _r("Grafana Getting Started",
       "https://grafana.com/docs/grafana/latest/getting-started/",
       "Grafana docs", "ops:res:grafana:getting-started", ["grafana"], "Tutorial",
       "Install Grafana, connect a data source, build dashboards, and configure alerting."),

    # OpenTelemetry (existing)
    _r("OpenTelemetry Getting Started",
       "https://opentelemetry.io/docs/getting-started/",
       "OpenTelemetry docs", "ops:res:opentelemetry:getting-started", ["opentelemetry"], "Tutorial",
       "Instrument an app with traces, metrics, and logs using the Collector and language SDKs."),
    _r("OpenTelemetry Demo",
       "https://opentelemetry.io/docs/demo/",
       "OpenTelemetry docs", "ops:res:opentelemetry:demo", ["opentelemetry"], "Tutorial",
       "Microservices reference app showing real instrumentation across many languages and signals."),

    # Loki (existing)
    _r("Grafana Loki Get Started",
       "https://grafana.com/docs/loki/latest/get-started/",
       "Grafana docs", "ops:res:loki:get-started", ["loki"], "Tutorial",
       "Install Loki, ship logs with Promtail or Alloy, and query them with LogQL in Grafana."),

    # Logfire (existing)
    _r("Pydantic Logfire Documentation",
       "https://logfire.pydantic.dev/docs/",
       "Pydantic docs", "ops:res:logfire:docs", ["logfire"], "Tutorial",
       "Install Logfire, instrument Python apps, and view structured traces and logs in the UI."),

    # Honeycomb (existing)
    _r("Honeycomb Get Started",
       "https://docs.honeycomb.io/get-started/",
       "Honeycomb docs", "ops:res:honeycomb:get-started", ["honeycomb"], "Tutorial",
       "Send events via OpenTelemetry, run BubbleUp queries, and investigate production issues."),

    # Vault (existing — now in Secrets category)
    _r("Vault Tutorials",
       "https://developer.hashicorp.com/vault/tutorials",
       "HashiCorp Developer", "ops:res:vault:tutorials", ["vault"], "Tutorial",
       "Hands-on tutorials for KV secrets, dynamic database creds, transit encryption, and auth methods."),
    _r("Vault Production Hardening",
       "https://developer.hashicorp.com/vault/tutorials/operations/production-hardening",
       "HashiCorp Developer", "ops:res:vault:production-hardening", ["vault"], "Best Practices",
       "Official checklist: end-to-end TLS, root token rotation, auditing, and least-privilege policies."),

    # Keycloak (existing — now in Identity category)
    _r("Keycloak Getting Started (Docker)",
       "https://www.keycloak.org/getting-started/getting-started-docker",
       "Keycloak docs", "ops:res:keycloak:docker-start", ["keycloak"], "Tutorial",
       "Run Keycloak in Docker, create a realm, register a client, and secure a sample app."),
    _r("Keycloak Server Administration Guide",
       "https://www.keycloak.org/docs/latest/server_admin/",
       "Keycloak docs", "ops:res:keycloak:server-admin", ["keycloak"], "Best Practices",
       "Reference for realms, clients, identity brokering, user federation, and authentication flows."),

    # Ory (existing — now in Identity category)
    _r("Ory Documentation",
       "https://www.ory.sh/docs/",
       "Ory docs", "ops:res:ory:docs", ["ory"], "Tutorial",
       "Get started with Ory Kratos identities, Hydra OAuth2/OIDC, Keto permissions, and Oathkeeper."),

    # Auth0 (existing — now in Identity category)
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
    Person(
        "Cindy Sridharan", "copyconstruct", "blog", "https://copyconstruct.medium.com/",
        "ops:person:blog:copyconstruct",
        "Distributed systems and observability writing; essays on testing in production and reliability.",
    ),
    Person(
        "Liz Fong-Jones", "lizthegrey", "x", "https://x.com/lizthegrey",
        "ops:person:x:liz-fong-jones",
        "Honeycomb principal developer advocate. SRE practice and production-excellence framework.",
    ),
    Person(
        "John Allspaw", "allspaw", "blog", "https://www.adaptivecapacitylabs.com/blog/",
        "ops:person:blog:allspaw",
        "Adaptive Capacity Labs; canonical voice on incident analysis and learning from failure.",
    ),
    Person(
        "Lorin Hochstein", "norootcause", "blog", "https://surfingcomplexity.blog/",
        "ops:person:blog:lorin-hochstein",
        "Netflix SRE alum; chaos engineering, postmortem culture, and resilience engineering.",
    ),
    Person(
        "Casey Rosenthal", "caseyrosenthal", "blog", "https://verica.io/blog/",
        "ops:person:blog:casey-rosenthal",
        "Formerly Netflix Chaos team; co-author of the canonical chaos engineering book.",
    ),
    Person(
        "Kelly Shortridge", "swagitda_", "blog", "https://kellyshortridge.com/",
        "ops:person:blog:kelly-shortridge",
        "Security chaos engineering; author of the canonical book of the same name.",
    ),
    Person(
        "Will Larson", "lethain", "blog", "https://lethain.com/",
        "ops:person:blog:lethain",
        "Engineering leader writing concrete frameworks for ops strategy, on-call, and platform investment.",
    ),
]


# ─── FAQs ────────────────────────────────────────────────────────────────────

OPS_FAQS: list[FAQ] = [
    FAQ(
        "What's the smallest observability stack that's actually useful?",
        "One signal you'll actually look at, instrumented well, beats four "
        "signals you glance at during incidents. Charity Majors frames "
        "this as observability 1.0 vs. 2.0: the three pillars (metrics, "
        "logs, traces) are 1.0. Structured wide events you can slice "
        "arbitrarily are 2.0. For most teams, OpenTelemetry to instrument "
        "plus one backend that handles high-cardinality (Honeycomb, "
        "Logfire) is the smallest setup that pays off. Add the other "
        "pillars once you're actually using the first one.",
        source_label="Charity Majors: Observability 1.0 vs 2.0",
        source_url="https://www.honeycomb.io/blog/one-key-difference-observability1dot0-2dot0",
        source_key="ops:faq:charity-observability-2",
    ),
    FAQ(
        "How do I think about reliability without overengineering?",
        "Static stability is the cleanest mental model: design so that "
        "when a dependency fails, your system behaves the same. "
        "Pre-provision instead of reacting. Pre-build instead of pulling "
        "at request time. Decide what works looks like when half your "
        "dependencies are down. Most overengineering is reacting to "
        "abstract failures instead of ones that have actually hurt you. "
        "Re-read your last three postmortems, then design for those.",
        source_label="AWS Builders' Library: Static Stability",
        source_url="https://aws.amazon.com/builders-library/static-stability-using-availability-zones/",
        source_key="ops:faq:aws-static-stability",
    ),
    FAQ(
        "How do I think about SLOs without overcomplicating them?",
        "Start with one SLO on the one user-facing thing you'd be paged "
        "for: requests successful within some latency budget. Pick a "
        "target you'd actually defend in a meeting (99% is fine; 99.99% "
        "is a research project), and burn-rate alerts that page you "
        "before the budget runs out, not after. The Google SRE Workbook "
        "is still the canonical step-by-step. Skip the elaborate "
        "multi-SLO error-budget machinery until you have one SLO running "
        "for six months.",
        source_label="Google SRE Workbook: Implementing SLOs",
        source_url="https://sre.google/workbook/implementing-slos/",
        source_key="ops:faq:google-slo-workbook",
    ),
    FAQ(
        "Do I need a real incident management tool, or is Slack enough?",
        "Slack is enough until your team is big enough that the absence "
        "of structure costs more than the cost of the tool. Concretely: "
        "when you have multiple concurrent incidents, a postmortem "
        "backlog that's not getting written, or new on-call engineers "
        "asking \"what do I do first?\", Slack alone isn't covering you. "
        "Incident.io and FireHydrant give you channel orchestration, "
        "role tracking, and a clean handoff to the postmortem. The cost "
        "is per-responder pricing; the value is consistency.",
        source_label="Incident.io: The case for incident management software",
        source_url="https://incident.io/blog",
        source_key="ops:faq:incident-mgmt-tool",
    ),
    FAQ(
        "When does my team need a formal on-call rotation?",
        "When the alternative is one person fielding every page, or "
        "nobody fielding pages at night. Both happen earlier than teams "
        "admit. A formal rotation buys predictable handoffs, escalation, "
        "and a clear answer to \"who's the primary?\" — but it only "
        "works if there's a real runbook for the common pages and a "
        "review of what's actually paging you. The cost of a bad "
        "rotation (alert fatigue, burnout) is higher than the cost of "
        "no rotation. Build the page-quality discipline first.",
        source_label="PagerDuty: Setting up your first on-call rotation",
        source_url="https://www.pagerduty.com/resources/learn/call-rotations-schedules/",
        source_key="ops:faq:on-call-rotation",
    ),
    FAQ(
        "Should I outsource auth, or roll my own?",
        "Outsource. Auth is one of the highest blast-radius things you'll "
        "ever ship, and the cost of getting it wrong is everyone else's "
        "data. The strongest argument for rolling your own is when your "
        "domain model genuinely needs identity primitives the vendors "
        "don't offer (rare). For most teams, Auth0 / Clerk / WorkOS / "
        "Stytch are worth the price; Keycloak / Ory / Supertokens are "
        "the self-hostable middle ground if you want OSS without writing "
        "your own.",
        source_label="thoughtbot: Why we use Clerk for auth",
        source_url="https://thoughtbot.com/blog/why-we-use-clerk-for-auth",
        source_key="ops:faq:outsource-auth",
    ),
    FAQ(
        "How do I run a blameless postmortem that doesn't devolve into blame?",
        "Blameless doesn't mean accountability-free. It means the goal "
        "is learning, not punishment, and the operator's actions made "
        "sense given what they knew at the time. Three habits: write "
        "the timeline before the meeting; ask \"how did this make sense \"to do, in the moment?\" instead of \"who did this?\"; and end "
        "with concrete, owned action items that target the system, not "
        "the operator. John Allspaw's writing is the canonical reference.",
        source_label="John Allspaw: Blameless PostMortems and a Just Culture",
        source_url="https://www.etsy.com/codeascraft/blameless-postmortems",
        source_key="ops:faq:blameless-postmortems",
    ),
]

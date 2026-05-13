"""Curated content for /resources/infrastructure-hosting.

Receives the Ingress + Orchestration sections from the System Architecture
directory. Early draft — more sections (cloud providers, PaaS, managed data
services, networking, edge) will land as that work is done.

Shares its dataclasses with system_architecture_data so the render shape is
identical across directories.
"""

from __future__ import annotations

from datetime import date

from smarter_dev.web.system_architecture_data import (
    ArchCategory,
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

INFRA_CATEGORIES: list[ArchCategory] = [
    ArchCategory(
        slug="ingress",
        name="Ingress, proxies & routing",
        intro=(
            "The proxy is where your traffic story lives: TLS, routing, rate "
            "limits, auth. Nginx and HAProxy are the battle-tested defaults. "
            "Caddy makes TLS automatic. Reach for Envoy when you're building "
            "a service mesh, Traefik when you're already in orchestrated containers."
        ),
        tools=(
            ArchTool("nginx", "Nginx",
                     "https://nginx.org/",
                     "infra:tool:nginx:home",
                     "Battle-tested high-performance web server and reverse proxy."),
            ArchTool("traefik", "Traefik",
                     "https://traefik.io/traefik/",
                     "infra:tool:traefik:home",
                     "Modern proxy designed for container orchestrators; auto-discovers services."),
            ArchTool("caddy", "Caddy",
                     "https://caddyserver.com/",
                     "infra:tool:caddy:home",
                     "Web server with automatic TLS by default. Written in Go. Simplest config of the bunch."),
            ArchTool("haproxy", "HAProxy",
                     "https://www.haproxy.org/",
                     "infra:tool:haproxy:home",
                     "High-performance TCP/HTTP proxy with deep tuning surface for serious load."),
            ArchTool("envoy", "Envoy",
                     "https://www.envoyproxy.io/",
                     "infra:tool:envoy:home",
                     "Proxy designed for service meshes. Powers Istio, Consul Connect, Linkerd."),
        ),
    ),
    ArchCategory(
        slug="orchestration",
        name="Orchestration & runtime",
        intro=(
            "Kubernetes is right when scaling out makes operating it cheaper "
            "than not. That's later than most teams reach for it. Docker "
            "Compose on a VM is often enough. Nomad is a serious alternative "
            "if you want orchestration without the YAML universe."
        ),
        tools=(
            ArchTool("kubernetes", "Kubernetes",
                     "https://kubernetes.io/",
                     "infra:tool:kubernetes:home",
                     "The dominant container orchestrator. Complex but ubiquitous; managed offerings everywhere."),
            ArchTool("nomad", "HashiCorp Nomad",
                     "https://www.nomadproject.io/",
                     "infra:tool:nomad:home",
                     "Simpler orchestrator supporting more than containers. Pairs well with Consul and Vault."),
            ArchTool("docker-compose", "Docker Compose",
                     "https://docs.docker.com/compose/",
                     "infra:tool:docker-compose:home",
                     "Multi-container apps on a single host. Often enough for small deployments."),
        ),
    ),
]


# ─── PER-TOOL RESOURCES ──────────────────────────────────────────────────────

INFRA_TOOL_RESOURCES: list[ArchToolResource] = [
    # Nginx
    _r("Nginx Beginner's Guide",
       "https://nginx.org/en/docs/beginners_guide.html",
       "Nginx docs", "infra:res:nginx:beginners", ["nginx"], "Tutorial",
       "Official intro: serving static content, reverse proxy, FastCGI, and load balancing basics."),
    _r("Nginx Admin's Handbook",
       "https://github.com/trimstray/nginx-admins-handbook",
       "GitHub · trimstray", "infra:res:nginx:admins-handbook", ["nginx"], "Best Practices",
       "Operator guide covering configuration patterns, hardening, performance, and debugging."),

    # Traefik
    _r("Traefik Quick Start",
       "https://doc.traefik.io/traefik/getting-started/quick-start/",
       "Traefik docs", "infra:res:traefik:quick-start", ["traefik"], "Tutorial",
       "Run Traefik with Docker, discover services automatically, and route HTTP traffic."),

    # Caddy
    _r("Caddy Getting Started",
       "https://caddyserver.com/docs/getting-started",
       "Caddy docs", "infra:res:caddy:getting-started", ["caddy"], "Tutorial",
       "Run Caddy as a static file server, reverse proxy, and HTTPS terminator with automatic TLS."),
    _r("Caddyfile Concepts",
       "https://caddyserver.com/docs/caddyfile/concepts",
       "Caddy docs", "infra:res:caddy:caddyfile", ["caddy"], "Tutorial",
       "Caddyfile syntax, matchers, directives, and snippets for typical reverse-proxy setups."),

    # HAProxy
    _r("HAProxy Starter Guide",
       "https://docs.haproxy.org/3.0/intro.html",
       "HAProxy docs", "infra:res:haproxy:intro", ["haproxy"], "Tutorial",
       "Introduction to load balancing concepts, frontends, backends, and ACLs in HAProxy."),
    _r("HAProxy Configuration Manual",
       "https://docs.haproxy.org/3.0/configuration.html",
       "HAProxy docs", "infra:res:haproxy:config-manual", ["haproxy"], "Best Practices",
       "Canonical reference for every config directive: timeouts, health checks, stick tables, SSL."),

    # Envoy
    _r("Envoy Getting Started",
       "https://www.envoyproxy.io/docs/envoy/latest/start/start",
       "Envoy docs", "infra:res:envoy:start", ["envoy"], "Tutorial",
       "Run Envoy in Docker, configure listeners, clusters, and basic HTTP routing."),
    _r("Envoy Sandboxes",
       "https://www.envoyproxy.io/docs/envoy/latest/start/sandboxes/sandboxes",
       "Envoy docs", "infra:res:envoy:sandboxes", ["envoy"], "Tutorial",
       "Working Docker Compose examples for front proxy, gRPC bridge, JWT auth, and more."),

    # Kubernetes
    _r("Kubernetes Tutorials",
       "https://kubernetes.io/docs/tutorials/",
       "Kubernetes docs", "infra:res:kubernetes:tutorials", ["kubernetes"], "Tutorial",
       "Official tutorials: Kubernetes Basics, stateful apps, services, and ConfigMaps."),
    _r("Kubernetes The Hard Way",
       "https://github.com/kelseyhightower/kubernetes-the-hard-way",
       "GitHub · Kelsey Hightower", "infra:res:kubernetes:hard-way", ["kubernetes"], "Tutorial",
       "Bootstrap a cluster from scratch to understand every component without abstractions."),
    _r("Kubernetes Production Best Practices",
       "https://learnk8s.io/production-best-practices",
       "Learnk8s", "infra:res:kubernetes:learnk8s-prod", ["kubernetes"], "Best Practices",
       "Checklist covering app health, scalability, observability, security, and resource governance."),

    # Nomad
    _r("Nomad Tutorials",
       "https://developer.hashicorp.com/nomad/tutorials",
       "HashiCorp Developer", "infra:res:nomad:tutorials", ["nomad"], "Tutorial",
       "Official learning path: install, run jobs, schedule services, batch jobs, and integrate Consul."),

    # Docker Compose
    _r("Docker Compose Overview",
       "https://docs.docker.com/compose/",
       "Docker docs", "infra:res:docker-compose:overview", ["docker-compose"], "Tutorial",
       "Get started defining multi-container apps with compose.yaml, networks, volumes, and profiles."),
    _r("Awesome Compose",
       "https://github.com/docker/awesome-compose",
       "GitHub · Docker", "infra:res:docker-compose:awesome", ["docker-compose"], "Tutorial",
       "Official sample compose files: Django+Postgres, Flask+Redis, Nginx, and other common stacks."),
]


# Placeholders for future expansion.
INFRA_SPINE_RESOURCES = []
INFRA_PEOPLE: list[Person] = []
INFRA_FAQS: list[FAQ] = []

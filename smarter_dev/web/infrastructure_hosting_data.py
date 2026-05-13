"""Curated content for /resources/infrastructure-hosting.

The Where layer of the resources index. Pairs with /resources/system-architecture
(the What) and /resources/production-operations (the Keep-it-healthy).
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

INFRA_CATEGORIES: list[ArchCategory] = [
    ArchCategory(
        slug="cloud-providers",
        name="Cloud providers",
        intro=(
            "The choice is rarely \"which cloud.\" It's how locked-in you can "
            "stomach. AWS has the deepest service catalog and the most "
            "painful exit cost. GCP and Azure offer competitive primitives "
            "with different pain points. The second tier (DigitalOcean, "
            "Hetzner, Vultr) wins when you're trading service depth for "
            "simpler billing and lower margins. Each major provider has its "
            "own managed data services in the section below."
        ),
        tools=(
            ArchTool("aws", "Amazon Web Services (AWS)",
                     "https://aws.amazon.com/",
                     "infra:tool:aws:home",
                     "The dominant cloud. Deepest service catalog and the most painful exit cost."),
            ArchTool("gcp", "Google Cloud Platform (GCP)",
                     "https://cloud.google.com/",
                     "infra:tool:gcp:home",
                     "Google's cloud. Strongest on Kubernetes (GKE), BigQuery, and global networking."),
            ArchTool("azure", "Microsoft Azure",
                     "https://azure.microsoft.com/",
                     "infra:tool:azure:home",
                     "Microsoft's cloud. Best identity story and the deepest enterprise relationships."),
            ArchTool("digitalocean", "DigitalOcean",
                     "https://www.digitalocean.com/",
                     "infra:tool:digitalocean:home",
                     "Predictable pricing, simple ops, generous free egress on managed offerings."),
            ArchTool("hetzner", "Hetzner Cloud",
                     "https://www.hetzner.com/cloud",
                     "infra:tool:hetzner:home",
                     "German provider with the cheapest serious-grade compute money can buy."),
            ArchTool("vultr", "Vultr",
                     "https://www.vultr.com/",
                     "infra:tool:vultr:home",
                     "Wide region coverage, competitive on bare-metal and high-CPU instances."),
            ArchTool("oracle-cloud", "Oracle Cloud",
                     "https://www.oracle.com/cloud/",
                     "infra:tool:oracle-cloud:home",
                     "Generous always-free tier. Deepest discount you'll find on dedicated workloads."),
        ),
    ),
    ArchCategory(
        slug="paas",
        name="App hosting / PaaS",
        intro=(
            "Hosting where you don't think about hosting. Fly, Railway, "
            "Render, Vercel each let you push code and get a URL. The "
            "differences are data tier, regional control, and how much they "
            "abstract. Pick by whether you want to think about regions, "
            "networking, and persistence, or specifically not think about them."
        ),
        tools=(
            ArchTool("fly", "Fly.io",
                     "https://fly.io/",
                     "infra:tool:fly:home",
                     "Edge-native PaaS that keeps multi-region in mind. Best-in-class data tier."),
            ArchTool("railway", "Railway",
                     "https://railway.com/",
                     "infra:tool:railway:home",
                     "Push code, get a URL. Honest pricing per service-hour with a nice DX."),
            ArchTool("render", "Render",
                     "https://render.com/",
                     "infra:tool:render:home",
                     "Managed-but-flexible Heroku replacement; first-class background workers and cron."),
            ArchTool("vercel", "Vercel",
                     "https://vercel.com/",
                     "infra:tool:vercel:home",
                     "Next.js production company. Best edge story for frontend frameworks."),
            ArchTool("netlify", "Netlify",
                     "https://www.netlify.com/",
                     "infra:tool:netlify:home",
                     "Long-running Jamstack PaaS; still strong for static sites plus functions."),
            ArchTool("heroku", "Heroku",
                     "https://www.heroku.com/",
                     "infra:tool:heroku:home",
                     "The original PaaS. Pricing's higher; the ergonomics are still battle-tested."),
            ArchTool("cloudflare-pages", "Cloudflare Pages",
                     "https://pages.cloudflare.com/",
                     "infra:tool:cloudflare-pages:home",
                     "Generous free tier; pairs naturally with Cloudflare Workers."),
        ),
    ),
    ArchCategory(
        slug="managed-data",
        name="Managed data services",
        intro=(
            "Postgres-as-a-service is the new default for most teams. Neon, "
            "Supabase, Tiger Data, and Crunchy Bridge each take different "
            "trade-offs on branching, vector, time-series, and pricing. "
            "PlanetScale runs MySQL with serverless branching. Turso runs "
            "distributed SQLite. Decision is usually which workload you're "
            "optimizing for, plus how much you trust their backup story. "
            "The database itself is a "
            "<a href=\"/resources/system-architecture#databases\">System "
            "Architecture decision</a>; this list is about who runs it for you."
        ),
        tools=(
            ArchTool("neon", "Neon",
                     "https://neon.com/",
                     "infra:tool:neon:home",
                     "Serverless Postgres with branching. The new default for ephemeral environments."),
            ArchTool("supabase", "Supabase",
                     "https://supabase.com/",
                     "infra:tool:supabase:home",
                     "Postgres plus auth, storage, realtime, and edge functions as one open-source stack."),
            ArchTool("tiger-data", "Tiger Data",
                     "https://www.tigerdata.com/",
                     "infra:tool:tiger-data:home",
                     "Time-series Postgres (formerly Timescale). The team behind hypertables."),
            ArchTool("crunchy-bridge", "Crunchy Bridge",
                     "https://www.crunchybridge.com/",
                     "infra:tool:crunchy-bridge:home",
                     "Hosted Postgres from the Crunchy Data team. Deep Postgres expertise, conservative defaults."),
            ArchTool("planetscale", "PlanetScale",
                     "https://planetscale.com/",
                     "infra:tool:planetscale:home",
                     "Serverless MySQL with branching. The Vitess-based reference offering."),
            ArchTool("turso", "Turso",
                     "https://turso.tech/",
                     "infra:tool:turso:home",
                     "Distributed SQLite. Edge databases that replicate close to users."),
            ArchTool("upstash", "Upstash",
                     "https://upstash.com/",
                     "infra:tool:upstash:home",
                     "Serverless Redis and Kafka with per-request pricing for in-memory workloads."),
            ArchTool("aiven", "Aiven",
                     "https://aiven.io/",
                     "infra:tool:aiven:home",
                     "Multi-database managed offering: Postgres, Kafka, Redis, OpenSearch, ClickHouse, more."),
        ),
    ),
    ArchCategory(
        slug="containers",
        name="Containers & registries",
        intro=(
            "The container is the unit of deployment everywhere except where "
            "it isn't. Docker still owns mindshare; Podman is the rootless "
            "alternative. The registry choice matters more than the build "
            "tool. That's where bandwidth, image scanning, and supply-chain "
            "attacks live."
        ),
        tools=(
            ArchTool("docker", "Docker",
                     "https://www.docker.com/",
                     "infra:tool:docker:home",
                     "Still owns the mindshare. The default for building and running containers."),
            ArchTool("podman", "Podman",
                     "https://podman.io/",
                     "infra:tool:podman:home",
                     "Daemonless and rootless. Drop-in for most Docker workflows."),
            ArchTool("buildah", "Buildah",
                     "https://buildah.io/",
                     "infra:tool:buildah:home",
                     "Focused on building OCI images. Often used alongside Podman in non-Docker stacks."),
            ArchTool("docker-hub", "Docker Hub",
                     "https://hub.docker.com/",
                     "infra:tool:docker-hub:home",
                     "The original public registry. Rate-limited free tier; ubiquitous official images."),
            ArchTool("ghcr", "GitHub Container Registry",
                     "https://ghcr.io/",
                     "infra:tool:ghcr:home",
                     "Free for public images, deep GitHub Actions integration, no rate limits for auth users."),
            ArchTool("ecr", "Amazon ECR",
                     "https://aws.amazon.com/ecr/",
                     "infra:tool:ecr:home",
                     "Amazon's container registry. Tight IAM integration if you're AWS-native."),
        ),
    ),
    ArchCategory(
        slug="orchestration",
        name="Orchestration & runtime",
        intro=(
            "Kubernetes is right when scaling out makes operating it cheaper "
            "than not. That's later than most teams reach for it. Docker "
            "Compose on a VM is often enough. Nomad is a serious alternative "
            "if you want orchestration without the YAML universe. Cloud Run "
            "and ECS sit in between: orchestration without the operational "
            "tax. Coolify and Kamal are the new wave for teams that want a "
            "single command to deploy."
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
            ArchTool("amazon-ecs", "Amazon ECS",
                     "https://aws.amazon.com/ecs/",
                     "infra:tool:amazon-ecs:home",
                     "Managed container orchestration on AWS. Simpler than EKS; AWS-locked."),
            ArchTool("cloud-run", "Google Cloud Run",
                     "https://cloud.google.com/run",
                     "infra:tool:cloud-run:home",
                     "Run containers without thinking about clusters. Per-request billing, scale-to-zero."),
            ArchTool("coolify", "Coolify",
                     "https://coolify.io/",
                     "infra:tool:coolify:home",
                     "Self-hosted Heroku alternative. PaaS ergonomics on your own infrastructure."),
            ArchTool("kamal", "Kamal",
                     "https://kamal-deploy.org/",
                     "infra:tool:kamal:home",
                     "Basecamp's deployment tool. SSH plus Docker; no orchestrator required."),
            ArchTool("dokku", "Dokku",
                     "https://dokku.com/",
                     "infra:tool:dokku:home",
                     "Single-server PaaS. Heroku's buildpack experience on a Linux box."),
        ),
    ),
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
        slug="networking",
        name="Networking",
        intro=(
            "Networking is the layer everyone forgets until it bites them. "
            "The big three are DNS (where your domain lives), CDN (where "
            "users hit before your origin), and private networking (how "
            "your services find each other without going through the "
            "internet). Tailscale changed the calculus on the last one: "
            "operating a private network is now as easy as adding users "
            "to a group."
        ),
        tools=(
            ArchTool("cloudflare", "Cloudflare",
                     "https://www.cloudflare.com/",
                     "infra:tool:cloudflare:home",
                     "DNS, CDN, WAF, edge compute as one stack. The default for most teams."),
            ArchTool("route53", "AWS Route 53",
                     "https://aws.amazon.com/route53/",
                     "infra:tool:route53:home",
                     "Amazon's DNS service. Strong health checks; tight Route53 ↔ ALB integration."),
            ArchTool("cloudfront", "AWS CloudFront",
                     "https://aws.amazon.com/cloudfront/",
                     "infra:tool:cloudfront:home",
                     "AWS's CDN. Deepest integration with the rest of the AWS ecosystem."),
            ArchTool("fastly", "Fastly",
                     "https://www.fastly.com/",
                     "infra:tool:fastly:home",
                     "Programmable CDN. VCL gives you real control at the edge."),
            ArchTool("bunny", "Bunny.net",
                     "https://bunny.net/",
                     "infra:tool:bunny:home",
                     "Low-cost, high-quality CDN with simple pricing. The new wave."),
            ArchTool("tailscale", "Tailscale",
                     "https://tailscale.com/",
                     "infra:tool:tailscale:home",
                     "Mesh VPN built on WireGuard. Operating a private network became trivial."),
            ArchTool("twingate", "Twingate",
                     "https://www.twingate.com/",
                     "infra:tool:twingate:home",
                     "Zero-trust network access. Closer to enterprise than Tailscale's developer-first feel."),
            ArchTool("wireguard", "WireGuard",
                     "https://www.wireguard.com/",
                     "infra:tool:wireguard:home",
                     "Modern VPN protocol. Underlies Tailscale; run it raw if you want full control."),
            ArchTool("zerotier", "ZeroTier",
                     "https://www.zerotier.com/",
                     "infra:tool:zerotier:home",
                     "Mesh networking with a different design point than Tailscale. Layer-2 virtual networks."),
        ),
    ),
    ArchCategory(
        slug="edge",
        name="Edge & functions",
        intro=(
            "The functional argument for serverless was \"don't manage "
            "servers.\" The actual argument that won was \"don't manage cold "
            "starts.\" Edge pushes compute closer to users at the cost of "
            "less local state. Pick when latency-to-user matters more than "
            "compute density, or when you specifically want stateless "
            "scale-to-zero pricing."
        ),
        tools=(
            ArchTool("cloudflare-workers", "Cloudflare Workers",
                     "https://workers.cloudflare.com/",
                     "infra:tool:cloudflare-workers:home",
                     "Runs JavaScript and TypeScript on Cloudflare's edge. Sub-millisecond cold starts."),
            ArchTool("durable-objects", "Cloudflare Durable Objects",
                     "https://www.cloudflare.com/developer-platform/products/durable-objects/",
                     "infra:tool:durable-objects:home",
                     "Single-instance, stateful objects on Workers. The coordinator pattern at the edge."),
            ArchTool("vercel-functions", "Vercel Functions",
                     "https://vercel.com/docs/functions",
                     "infra:tool:vercel-functions:home",
                     "Edge and serverless functions tied to Next.js apps. Hot path for the Vercel stack."),
            ArchTool("lambda", "AWS Lambda",
                     "https://aws.amazon.com/lambda/",
                     "infra:tool:lambda:home",
                     "The original serverless platform. Deepest service-integration story in the AWS ecosystem."),
            ArchTool("fastly-compute", "Fastly Compute",
                     "https://www.fastly.com/products/edge-compute",
                     "infra:tool:fastly-compute:home",
                     "WebAssembly at the edge. Higher cold-start floor than Workers; broader language support."),
            ArchTool("deno-deploy", "Deno Deploy",
                     "https://deno.com/deploy",
                     "infra:tool:deno-deploy:home",
                     "Edge platform built around the Deno runtime. TypeScript first-class."),
        ),
    ),
    ArchCategory(
        slug="server-os",
        name="Server OS",
        intro=(
            "For 99% of teams, the OS is whatever the platform gives you. "
            "The real decision points are long-term support cycles (Ubuntu "
            "LTS, Debian stable, RHEL/Rocky), minimal surface area (Alpine, "
            "Wolfi for hardened images), and reproducibility (NixOS if "
            "you've drunk that kool-aid)."
        ),
        tools=(
            ArchTool("ubuntu", "Ubuntu Server",
                     "https://ubuntu.com/server",
                     "infra:tool:ubuntu:home",
                     "The most common server OS. LTS releases every two years; sane defaults."),
            ArchTool("debian", "Debian",
                     "https://www.debian.org/",
                     "infra:tool:debian:home",
                     "The stable foundation Ubuntu is built on. Slower-moving, very predictable."),
            ArchTool("alpine", "Alpine Linux",
                     "https://alpinelinux.org/",
                     "infra:tool:alpine:home",
                     "Minimal distro built on musl libc. Tiny container images; smaller attack surface."),
            ArchTool("rhel-rocky", "RHEL / Rocky Linux",
                     "https://rockylinux.org/",
                     "infra:tool:rhel-rocky:home",
                     "Red Hat's enterprise distro and its community-driven rebuild. The compliance default."),
            ArchTool("nixos", "NixOS",
                     "https://nixos.org/",
                     "infra:tool:nixos:home",
                     "Declarative, reproducible OS. Steep learning curve; deep payoff once you're in."),
            ArchTool("wolfi", "Wolfi",
                     "https://wolfi.dev/",
                     "infra:tool:wolfi:home",
                     "Container-focused secure base images from Chainguard. Built for SBOM and CVE hygiene."),
        ),
    ),
]


# ─── SPINE: cross-cutting hosting resources ─────────────────────────────────


def _s(title, url, source, key, learning_type, blurb=""):
    return ArchResource(
        title=title, url=url, source=source, key=key,
        learning_type=learning_type, first_indexed_at=_INDEXED, blurb=blurb,
    )


INFRA_SPINE_RESOURCES: list[ArchResource] = [
    _s("The Twelve-Factor App",
       "https://12factor.net/",
       "Adam Wiggins · Heroku", "infra:spine:twelve-factor", "Best Practices",
       "Twelve principles for portable, declaratively-configured services that thrive across modern hosting environments."),
    _s("AWS Well-Architected Framework",
       "https://aws.amazon.com/architecture/well-architected/",
       "AWS", "infra:spine:aws-well-architected", "Best Practices",
       "Six pillars (ops, security, reliability, performance, cost, sustainability) for evaluating any cloud workload."),
    _s("CNCF Landscape",
       "https://landscape.cncf.io/",
       "Cloud Native Computing Foundation", "infra:spine:cncf-landscape", "Discussion",
       "Interactive map of cloud-native projects categorized by layer, maturity, and license."),
    _s("Kubernetes The Hard Way",
       "https://github.com/kelseyhightower/kubernetes-the-hard-way",
       "Kelsey Hightower", "infra:spine:k8s-hard-way", "Tutorial",
       "Bootstrap a Kubernetes cluster manually, lab by lab, to learn what the abstractions hide."),
    _s("The Datacenter as a Computer (3rd ed.)",
       "https://library.oapen.org/handle/20.500.12657/61844",
       "Barroso, Hölzle, Ranganathan · OAPEN", "infra:spine:datacenter-as-computer", "Course",
       "Open-access book treating warehouse-scale machines as the unit of design for modern cloud computing."),
    _s("Last Week in AWS",
       "https://www.lastweekinaws.com/",
       "Corey Quinn · The Duckbill Group", "infra:spine:last-week-aws", "Discussion",
       "Weekly newsletter filtering AWS announcements through cost-economist snark and practitioner skepticism."),
    _s("Google SRE Books (free online)",
       "https://sre.google/books/",
       "Google SRE", "infra:spine:sre-books", "Course",
       "Three free books on running planet-scale systems: SRE, the Workbook, and Building Secure & Reliable Systems."),
    _s("Best practices for building containers",
       "https://cloud.google.com/architecture/best-practices-for-building-containers",
       "Google Cloud Architecture Center", "infra:spine:container-best-practices", "Best Practices",
       "Signal handling, layer caching, tagging, and image hygiene rules from Google's container team."),
    _s("Why we're leaving the cloud",
       "https://world.hey.com/dhh/why-we-re-leaving-the-cloud-654b47e0",
       "DHH · 37signals", "infra:spine:dhh-leaving-cloud", "Discussion",
       "37signals' founder argues cloud economics break down for stable, mid-sized workloads worth owning."),
    _s("Hetzner Server Comparison (with benchmarks)",
       "https://www.achromatic.dev/blog/hetzner-server-comparison",
       "Achromatic", "infra:spine:hetzner-comparison", "Discussion",
       "Benchmarked value comparison across Hetzner's Intel, AMD, and Ampere fleets with price-per-score tables."),
    _s("Choose Boring Technology",
       "https://mcfunley.com/choose-boring-technology",
       "Dan McKinley", "infra:spine:choose-boring-tech", "Talk",
       "Spend your limited innovation tokens carefully; default to well-understood infrastructure for everything else."),
    _s("The Fly.io Blog",
       "https://fly.io/blog/",
       "Fly.io", "infra:spine:flyio-blog", "Discussion",
       "Engineering writeups on global app runtimes, anycast, Postgres replication, and edge compute trade-offs."),
]


# ─── PER-TOOL RESOURCES ─────────────────────────────────────────────────────

INFRA_TOOL_RESOURCES: list[ArchToolResource] = [
    # Cloud providers
    _r("Getting started with AWS",
       "https://aws.amazon.com/getting-started/",
       "AWS", "infra:res:aws:getting-started", ["aws"], "Tutorial",
       "Official onboarding hub with decision guides, cloud essentials, and first-build tutorials across AWS services."),
    _r("Google Cloud documentation",
       "https://cloud.google.com/docs",
       "Google Cloud", "infra:res:gcp:docs", ["gcp"], "Tutorial",
       "Central hub for GCP product docs, quickstarts, architecture references, and code samples."),
    _r("Azure documentation",
       "https://learn.microsoft.com/en-us/azure/",
       "Microsoft Learn", "infra:res:azure:docs", ["azure"], "Tutorial",
       "Microsoft's full Azure docs hub: getting-started paths, product catalog, SDKs, and architecture guidance."),
    _r("DigitalOcean documentation",
       "https://docs.digitalocean.com/",
       "DigitalOcean", "infra:res:digitalocean:docs", ["digitalocean"], "Tutorial",
       "Product docs for Droplets, App Platform, Managed Databases, Kubernetes, and developer tooling."),
    _r("Hetzner Cloud documentation",
       "https://docs.hetzner.com/cloud/",
       "Hetzner", "infra:res:hetzner:docs", ["hetzner"], "Tutorial",
       "Hetzner's cloud product docs covering servers, networks, volumes, firewalls, billing, and API."),
    _r("Hetzner Server Comparison",
       "https://www.achromatic.dev/blog/hetzner-server-comparison",
       "Achromatic", "infra:res:hetzner:comparison", ["hetzner"], "Discussion",
       "Independent benchmarks across Hetzner's CPX, CCX, and CAX fleets with cost-per-score recommendations."),
    _r("Vultr Docs",
       "https://docs.vultr.com/",
       "Vultr", "infra:res:vultr:docs", ["vultr"], "Tutorial",
       "Quickstarts, guides, and references for Vultr Compute, Managed Database, Kubernetes, and Object Storage."),
    _r("Get started with Oracle Cloud Infrastructure",
       "https://docs.oracle.com/en-us/iaas/Content/GSG/Concepts/baremetalintro.htm",
       "Oracle", "infra:res:oracle-cloud:gsg", ["oracle-cloud"], "Tutorial",
       "Welcome path: learn OCI basics, create your first instances, and explore role-specific guides."),

    # PaaS
    _r("Fly.io documentation",
       "https://fly.io/docs/",
       "Fly.io", "infra:res:fly:docs", ["fly"], "Tutorial",
       "Install flyctl, fly launch, then learn Machines, Volumes, networking, and language-specific deploy guides."),
    _r("Railway documentation",
       "https://docs.railway.com/",
       "Railway", "infra:res:railway:docs", ["railway"], "Tutorial",
       "Quick start, CLI, templates, and framework guides for deploying apps and databases on Railway."),
    _r("Render documentation",
       "https://render.com/docs",
       "Render", "infra:res:render:docs", ["render"], "Tutorial",
       "Ship-your-first-app quickstarts plus configure and operate guides for services, databases, and Docker."),
    _r("Vercel documentation",
       "https://vercel.com/docs",
       "Vercel", "infra:res:vercel:docs", ["vercel"], "Tutorial",
       "Framework deploys, Functions, Image Optimization, environments, and the broader AI-cloud platform."),
    _r("Netlify documentation",
       "https://docs.netlify.com/",
       "Netlify", "infra:res:netlify:docs", ["netlify"], "Tutorial",
       "Build, deploy, manage, and extend sites with Netlify's frameworks, Functions, and Edge Functions."),
    _r("Heroku Dev Center",
       "https://devcenter.heroku.com/",
       "Heroku", "infra:res:heroku:dev-center", ["heroku"], "Tutorial",
       "Language-organized guides for deploying apps, Postgres, pipelines, and the original Procfile / buildpack model."),
    _r("Cloudflare Pages documentation",
       "https://developers.cloudflare.com/pages/",
       "Cloudflare", "infra:res:cloudflare-pages:docs", ["cloudflare-pages"], "Tutorial",
       "Deploy static and full-stack apps with Git integration, Pages Functions, and Cloudflare's global network."),

    # Managed data services
    _r("Neon documentation",
       "https://neon.com/docs",
       "Neon", "infra:res:neon:docs", ["neon"], "Tutorial",
       "Serverless Postgres with autoscaling, branching, and instant restore; framework quickstarts included."),
    _r("Supabase documentation",
       "https://supabase.com/docs",
       "Supabase", "infra:res:supabase:docs", ["supabase"], "Tutorial",
       "Postgres-backed BaaS: Database, Auth, Storage, Realtime, Edge Functions, and per-framework quickstarts."),
    _r("Tiger Data (TimescaleDB) documentation",
       "https://www.tigerdata.com/docs/",
       "Tiger Data", "infra:res:tiger-data:docs", ["tiger-data"], "Tutorial",
       "Time-series Postgres: hypertables, continuous aggregates, columnstore compression, and Tiger Cloud."),
    _r("Crunchy Bridge documentation",
       "https://docs.crunchybridge.com/",
       "Crunchy Data", "infra:res:crunchy-bridge:docs", ["crunchy-bridge"], "Tutorial",
       "Fully managed Postgres with dashboard, cb CLI, and REST API for connections, networking, and logging."),
    _r("PlanetScale documentation",
       "https://planetscale.com/docs",
       "PlanetScale", "infra:res:planetscale:docs", ["planetscale"], "Tutorial",
       "Docs for PlanetScale's Vitess-based MySQL and PostgreSQL platforms, deployments, branching, and pricing."),
    _r("Turso documentation",
       "https://docs.turso.tech/",
       "Turso", "infra:res:turso:docs", ["turso"], "Tutorial",
       "Embedded and cloud SQLite-compatible databases with vector search, sync, and AgentFS."),
    _r("Upstash documentation",
       "https://upstash.com/docs",
       "Upstash", "infra:res:upstash:docs", ["upstash"], "Tutorial",
       "Serverless Redis, Vector, QStash, and Workflow with scale-to-zero, per-request pricing."),
    _r("Aiven documentation",
       "https://aiven.io/docs",
       "Aiven", "infra:res:aiven:docs", ["aiven"], "Tutorial",
       "Managed open-source data services (Postgres, Kafka, ClickHouse, OpenSearch) across multiple clouds."),

    # Containers & registries
    _r("Get started with Docker",
       "https://docs.docker.com/get-started/",
       "Docker", "infra:res:docker:get-started", ["docker"], "Tutorial",
       "Essential learning path: install, build, run, and ship containers with the canonical Docker tooling."),
    _r("Podman documentation",
       "https://podman.io/docs",
       "Podman", "infra:res:podman:docs", ["podman"], "Tutorial",
       "Daemonless, rootless container engine; install, run, manage, network, and checkpoint containers."),
    _r("Buildah",
       "https://buildah.io/",
       "containers project", "infra:res:buildah:home", ["buildah"], "Tutorial",
       "Script OCI image builds without a daemon or Dockerfile. Install, tutorials, and release news."),
    _r("Docker Hub documentation",
       "https://docs.docker.com/docker-hub/",
       "Docker", "infra:res:docker-hub:docs", ["docker-hub"], "Tutorial",
       "Push, pull, and manage public/private images; webhooks, CI/CD integrations, and Trusted Content."),
    _r("Working with the GitHub Container Registry",
       "https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry",
       "GitHub Docs", "infra:res:ghcr:docs", ["ghcr"], "Tutorial",
       "Authenticate, push, pull, tag, and label OCI images at ghcr.io tied to your GitHub repos."),
    _r("Amazon Elastic Container Registry documentation",
       "https://docs.aws.amazon.com/ecr/",
       "AWS", "infra:res:ecr:docs", ["ecr"], "Tutorial",
       "User guides and API references for ECR private and public registries, IAM, and CLI usage."),

    # Orchestration (preserve existing migrated entries + new ones)
    _r("Kubernetes Tutorials",
       "https://kubernetes.io/docs/tutorials/",
       "Kubernetes docs", "infra:res:kubernetes:tutorials", ["kubernetes"], "Tutorial",
       "Official tutorials: Kubernetes Basics, stateful apps, services, and ConfigMaps."),
    _r("Kubernetes Production Best Practices",
       "https://learnk8s.io/production-best-practices",
       "Learnk8s", "infra:res:kubernetes:learnk8s-prod", ["kubernetes"], "Best Practices",
       "Checklist covering app health, scalability, observability, security, and resource governance."),
    _r("Nomad Tutorials",
       "https://developer.hashicorp.com/nomad/tutorials",
       "HashiCorp Developer", "infra:res:nomad:tutorials", ["nomad"], "Tutorial",
       "Official learning path: install, run jobs, schedule services, batch jobs, and integrate Consul."),
    _r("Docker Compose Overview",
       "https://docs.docker.com/compose/",
       "Docker docs", "infra:res:docker-compose:overview", ["docker-compose"], "Tutorial",
       "Get started defining multi-container apps with compose.yaml, networks, volumes, and profiles."),
    _r("Awesome Compose",
       "https://github.com/docker/awesome-compose",
       "GitHub · Docker", "infra:res:docker-compose:awesome", ["docker-compose"], "Tutorial",
       "Official sample compose files: Django + Postgres, Flask + Redis, Nginx, and other common stacks."),
    _r("What is Amazon ECS?",
       "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/Welcome.html",
       "AWS", "infra:res:amazon-ecs:welcome", ["amazon-ecs"], "Tutorial",
       "ECS developer guide covering capacity (EC2, Fargate, Anywhere), task definitions, services, and scaling."),
    _r("Cloud Run documentation",
       "https://cloud.google.com/run/docs",
       "Google Cloud", "infra:res:cloud-run:docs", ["cloud-run"], "Tutorial",
       "Run request- and event-driven containers serverlessly with quickstarts, custom domains, and authentication."),
    _r("Coolify documentation",
       "https://coolify.io/docs",
       "Coolify", "infra:res:coolify:docs", ["coolify"], "Tutorial",
       "Self-hosted PaaS for apps, databases, and services. Heroku/Netlify-style UX on your own servers."),
    _r("Kamal installation guide",
       "https://kamal-deploy.org/docs/installation/",
       "Basecamp · 37signals", "infra:res:kamal:install", ["kamal"], "Tutorial",
       "Install Kamal, run kamal init/setup, and deploy Dockerized apps to bare servers with zero downtime."),
    _r("Dokku getting started",
       "https://dokku.com/docs/getting-started/installation/",
       "Dokku", "infra:res:dokku:getting-started", ["dokku"], "Tutorial",
       "Install Dokku, configure SSH, and deploy your first app to a single-server open-source PaaS."),

    # Ingress, proxies & routing (preserve from prior migration)
    _r("Nginx Beginner's Guide",
       "https://nginx.org/en/docs/beginners_guide.html",
       "Nginx docs", "infra:res:nginx:beginners", ["nginx"], "Tutorial",
       "Official intro: serving static content, reverse proxy, FastCGI, and load balancing basics."),
    _r("Nginx Admin's Handbook",
       "https://github.com/trimstray/nginx-admins-handbook",
       "GitHub · trimstray", "infra:res:nginx:admins-handbook", ["nginx"], "Best Practices",
       "Operator guide covering configuration patterns, hardening, performance, and debugging."),
    _r("Traefik Quick Start",
       "https://doc.traefik.io/traefik/getting-started/quick-start/",
       "Traefik docs", "infra:res:traefik:quick-start", ["traefik"], "Tutorial",
       "Run Traefik with Docker, discover services automatically, and route HTTP traffic."),
    _r("Caddy Getting Started",
       "https://caddyserver.com/docs/getting-started",
       "Caddy docs", "infra:res:caddy:getting-started", ["caddy"], "Tutorial",
       "Run Caddy as a static file server, reverse proxy, and HTTPS terminator with automatic TLS."),
    _r("Caddyfile Concepts",
       "https://caddyserver.com/docs/caddyfile/concepts",
       "Caddy docs", "infra:res:caddy:caddyfile", ["caddy"], "Tutorial",
       "Caddyfile syntax, matchers, directives, and snippets for typical reverse-proxy setups."),
    _r("HAProxy Starter Guide",
       "https://docs.haproxy.org/3.0/intro.html",
       "HAProxy docs", "infra:res:haproxy:intro", ["haproxy"], "Tutorial",
       "Introduction to load balancing concepts, frontends, backends, and ACLs in HAProxy."),
    _r("HAProxy Configuration Manual",
       "https://docs.haproxy.org/3.0/configuration.html",
       "HAProxy docs", "infra:res:haproxy:config-manual", ["haproxy"], "Best Practices",
       "Canonical reference for every config directive: timeouts, health checks, stick tables, SSL."),
    _r("Envoy Getting Started",
       "https://www.envoyproxy.io/docs/envoy/latest/start/start",
       "Envoy docs", "infra:res:envoy:start", ["envoy"], "Tutorial",
       "Run Envoy in Docker, configure listeners, clusters, and basic HTTP routing."),
    _r("Envoy Sandboxes",
       "https://www.envoyproxy.io/docs/envoy/latest/start/sandboxes/sandboxes",
       "Envoy docs", "infra:res:envoy:sandboxes", ["envoy"], "Tutorial",
       "Working Docker Compose examples for front proxy, gRPC bridge, JWT auth, and more."),

    # Networking
    _r("Cloudflare developer docs",
       "https://developers.cloudflare.com/",
       "Cloudflare", "infra:res:cloudflare:docs", ["cloudflare"], "Tutorial",
       "Unified portal for DNS, CDN, WAF, Workers, R2, and Zero Trust products with code-first examples."),
    _r("What is Amazon Route 53?",
       "https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/Welcome.html",
       "AWS", "infra:res:route53:welcome", ["route53"], "Tutorial",
       "Domain registration, authoritative DNS routing, health checks, traffic flow, and VPC resolver."),
    _r("What is Amazon CloudFront?",
       "https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/Introduction.html",
       "AWS", "infra:res:cloudfront:intro", ["cloudfront"], "Tutorial",
       "Distribute content from AWS edges with origins, distributions, caching, and SaaS multi-tenant modes."),
    _r("Fastly documentation",
       "https://docs.fastly.com/",
       "Fastly", "infra:res:fastly:docs", ["fastly"], "Tutorial",
       "CDN, security, and edge Compute reference covering VCL, configuration, and platform APIs."),
    _r("Bunny.net Developer Hub",
       "https://docs.bunny.net/",
       "Bunny.net", "infra:res:bunny:docs", ["bunny"], "Tutorial",
       "CDN, Stream, Storage, Optimizer, DNS, and Magic Containers quickstarts and reference docs."),
    _r("Tailscale quickstart",
       "https://tailscale.com/kb/1017/install",
       "Tailscale", "infra:res:tailscale:install", ["tailscale"], "Tutorial",
       "Create a tailnet, install clients, add devices, and configure your first mesh in minutes."),
    _r("How Tailscale works",
       "https://tailscale.com/blog/how-tailscale-works",
       "Tailscale", "infra:res:tailscale:how-it-works", ["tailscale"], "Discussion",
       "Architecture writeup on WireGuard, the coordination server, NAT traversal, and DERP relay fallbacks."),
    _r("Twingate documentation",
       "https://www.twingate.com/docs",
       "Twingate", "infra:res:twingate:docs", ["twingate"], "Tutorial",
       "Zero Trust access docs: connectors, resources, identity, policies, and replacing traditional VPNs."),
    _r("WireGuard quick start",
       "https://www.wireguard.com/quickstart/",
       "WireGuard", "infra:res:wireguard:quickstart", ["wireguard"], "Tutorial",
       "Generate keys, configure interfaces, traverse NAT, and bring up a minimal WireGuard tunnel."),
    _r("ZeroTier: Create a Network",
       "https://docs.zerotier.com/start/",
       "ZeroTier", "infra:res:zerotier:start", ["zerotier"], "Tutorial",
       "Sign up, create a network, install the client, authorize devices, and verify mesh connectivity."),

    # Edge & functions
    _r("Cloudflare Workers documentation",
       "https://developers.cloudflare.com/workers/",
       "Cloudflare", "infra:res:cloudflare-workers:docs", ["cloudflare-workers"], "Tutorial",
       "Serverless edge compute with Wrangler CLI, multi-language runtimes, and bindings to Cloudflare services."),
    _r("Cloudflare Durable Objects",
       "https://developers.cloudflare.com/durable-objects/",
       "Cloudflare", "infra:res:durable-objects:docs", ["durable-objects"], "Tutorial",
       "Stateful Workers combining compute with storage, WebSocket hibernation, and scheduled alarms."),
    _r("Vercel Functions documentation",
       "https://vercel.com/docs/functions",
       "Vercel", "infra:res:vercel-functions:docs", ["vercel-functions"], "Tutorial",
       "Run server-side code on Vercel with Fluid compute, autoscaling, and region-aware data locality."),
    _r("What is AWS Lambda?",
       "https://docs.aws.amazon.com/lambda/latest/dg/welcome.html",
       "AWS", "infra:res:lambda:welcome", ["lambda"], "Tutorial",
       "Lambda developer guide: triggers, runtimes, permissions, scaling, layers, SnapStart, and VPC integration."),
    _r("Fastly Compute",
       "https://docs.fastly.com/products/compute",
       "Fastly", "infra:res:fastly-compute:docs", ["fastly-compute"], "Tutorial",
       "Serverless WebAssembly edge runtime; supported languages, deploy tooling, and logging integrations."),
    _r("Deno Deploy documentation",
       "https://docs.deno.com/deploy/",
       "Deno", "infra:res:deno-deploy:docs", ["deno-deploy"], "Tutorial",
       "Serverless JS/TS platform with apps, KV, cron, environments, and the REST API reference."),

    # Server OS
    _r("Ubuntu Server documentation",
       "https://ubuntu.com/server/docs",
       "Canonical", "infra:res:ubuntu:docs", ["ubuntu"], "Tutorial",
       "Install, configure, secure, and administer Ubuntu Server LTS, covering networking, virtualization, and HA."),
    _r("Debian documentation",
       "https://www.debian.org/doc/",
       "Debian Project", "infra:res:debian:docs", ["debian"], "Tutorial",
       "Installation guide, FAQ, release notes, admin handbook, and the broader Debian Documentation Project."),
    _r("Alpine Linux Wiki",
       "https://wiki.alpinelinux.org/wiki/Main_Page",
       "Alpine Linux Project", "infra:res:alpine:wiki", ["alpine"], "Tutorial",
       "Install, configure, and develop with musl/BusyBox-based Alpine. The de facto minimal container base."),
    _r("Rocky Linux documentation",
       "https://docs.rockylinux.org/",
       "Rocky Enterprise Software Foundation", "infra:res:rhel-rocky:docs", ["rhel-rocky"], "Tutorial",
       "Guides, books, and labs for installing and operating the community RHEL-compatible enterprise Linux."),
    _r("Learn Nix and NixOS",
       "https://nixos.org/learn/",
       "NixOS Foundation", "infra:res:nixos:learn", ["nixos"], "Tutorial",
       "Install Nix, take first steps, and dig into the Nix, Nixpkgs, and NixOS manuals plus Nix Pills."),
    _r("Wolfi overview",
       "https://edu.chainguard.dev/open-source/wolfi/overview/",
       "Chainguard Academy", "infra:res:wolfi:overview", ["wolfi"], "Tutorial",
       "Wolfi is a container-native Linux undistro built for supply-chain security; intro and how it differs."),
]


# ─── CREATORS ────────────────────────────────────────────────────────────────

INFRA_PEOPLE: list[Person] = [
    Person(
        "Kelsey Hightower", "kelseyhightower", "x", "https://x.com/kelseyhightower",
        "infra:person:x:kelseyhightower",
        "Ex-Google; the Kubernetes whisperer who argues constantly for simplicity over orchestration.",
    ),
    Person(
        "Corey Quinn", "QuinnyPig", "newsletter", "https://www.lastweekinaws.com/",
        "infra:person:newsletter:corey-quinn",
        "Last Week in AWS; the canonical commentator on AWS strategy, cost, and absurdity.",
    ),
    Person(
        "Daniel Vassallo", "dvassallo", "blog", "https://dvassallo.medium.com/",
        "infra:person:blog:daniel-vassallo",
        "Ex-AWS; writes on \"the good and bad of AWS\" and small-team hosting trade-offs.",
    ),
    Person(
        "Forrest Brazeal", "forrestbrazeal", "blog", "https://goodtechthings.com/",
        "infra:person:blog:forrest-brazeal",
        "Cloud educator and cartoonist; the most accessible explainer of AWS architecture decisions.",
    ),
    Person(
        "Bret Fisher", "BretFisher", "youtube", "https://www.youtube.com/@bretfisher",
        "infra:person:youtube:bret-fisher",
        "Docker Captain; the canonical Docker and containers teacher with deep practical workshops.",
    ),
    Person(
        "Liz Rice", "lizrice", "blog", "https://lizrice.com/",
        "infra:person:blog:liz-rice",
        "CNCF Chief Open Source Officer; container security and eBPF foundations.",
    ),
    Person(
        "Charity Majors", "mipsytipsy", "blog", "https://charity.wtf/",
        "infra:person:blog:charity",
        "Honeycomb co-founder; writes on hosting cost, reliability, and the case against premature K8s.",
    ),
    Person(
        "Will Larson", "lethain", "blog", "https://lethain.com/",
        "infra:person:blog:lethain",
        "Engineering leader writing concrete frameworks for hosting and infra strategy decisions.",
    ),
    Person(
        "Jérôme Petazzoni", "jpetazzo", "blog", "https://jpetazzo.github.io/",
        "infra:person:blog:jerome-petazzoni",
        "Long-time Docker educator; deep container internals and pragmatic Kubernetes guidance.",
    ),
    Person(
        "Fly.io Engineering", "flydotio", "blog", "https://fly.io/blog/",
        "infra:person:blog:flyio",
        "Thomas Ptacek and the Fly engineering team write some of the best hosting prose on the web.",
    ),
]


# ─── FAQs ────────────────────────────────────────────────────────────────────
# Source URLs filled in from research; placeholder URLs used until the
# background agent returns.

INFRA_FAQS: list[FAQ] = [
    FAQ(
        "Do I really need Kubernetes?",
        "Probably not yet. Kubernetes pays off when scaling out makes "
        "operating it cheaper than not (multi-region, many services, a "
        "dedicated platform team). For most teams, Docker Compose on a VM, "
        "a managed PaaS, or Cloud Run / ECS will run the same workload "
        "with a fraction of the operational tax. The honest question is "
        "whether you're choosing Kubernetes for the workload or for the "
        "résumé.",
        source_label="Matthias Endler: Maybe you don't need Kubernetes",
        source_url="https://endler.dev/2019/maybe-you-dont-need-kubernetes/",
        source_key="infra:faq:k8s-do-i-need",
    ),
    FAQ(
        "Should I default to AWS, or are the alternatives real?",
        "The alternatives are real. Hetzner gives you serious-grade compute "
        "at a fraction of AWS prices. Fly and Render handle most of what "
        "Elastic Beanstalk did with a tenth of the surface area. The "
        "honest reasons to choose AWS now are deep service integrations "
        "(RDS, S3, Lambda, EventBridge), enterprise contracts you're "
        "locked into, or a specific compliance bar. For most teams, the "
        "lock-in cost has gotten bigger relative to the alternatives.",
        source_label="DHH: Why we're leaving the cloud",
        source_url="https://world.hey.com/dhh/why-we-re-leaving-the-cloud-654b47e0",
        source_key="infra:faq:aws-vs-alternatives",
    ),
    FAQ(
        "When does managed Postgres beat self-hosting?",
        "Almost always when you weigh in operational cost. Neon, Supabase, "
        "Tiger Data, and Crunchy Bridge each handle backups, point-in-time "
        "recovery, replication, and version upgrades. Self-hosting wins on "
        "cost at significant scale and when you need extensions the hosts "
        "don't support. The break-even is later than most teams think; "
        "the day you need an unplanned recovery, you'll be glad you "
        "outsourced.",
        source_label="Crunchy Data: Postgres hosting checklist",
        source_url="https://www.crunchydata.com/postgres-hosting-checklist",
        source_key="infra:faq:managed-postgres",
    ),
    FAQ(
        "Should I host on a VM, a PaaS, containers, or serverless?",
        "Each is the right answer at a different point. A VM is the "
        "simplest substrate: predictable cost, full control, you do the "
        "ops. PaaS wins when you'd rather pay for someone else's ops. "
        "Containers are the unit of deployment if you have multiple "
        "services or environments. Serverless wins on idle cost and "
        "burst-able workloads, loses on long-running connections and "
        "warm-state. Default to the simplest thing that scales to your "
        "year-2 traffic, not your year-5 fantasy.",
        source_label="Dan McKinley: Choose Boring Technology",
        source_url="https://mcfunley.com/choose-boring-technology",
        source_key="infra:faq:vm-paas-containers-serverless",
    ),
    FAQ(
        "How do I think about vendor lock-in without overdoing it?",
        "Lock-in is a cost. Every cost is fine if the value's higher. The "
        "question isn't avoiding lock-in, it's matching depth-of-integration "
        "to your exit cost tolerance. Use standard interfaces (Postgres, "
        "S3-compatible, OAuth, OpenTelemetry) wherever the value of "
        "vendor-specific features doesn't justify the migration cost. "
        "AWS's value is exactly the inverse: deep proprietary integrations. "
        "If you don't need them, don't pay the lock-in tax.",
        source_label="Gregor Hohpe: Don't get locked up into avoiding lock-in",
        source_url="https://martinfowler.com/articles/oss-lockin.html",
        source_key="infra:faq:vendor-lock-in",
    ),
    FAQ(
        "What's the smallest production-ready hosting setup that scales?",
        "A single VM running Docker Compose, a managed Postgres, a managed "
        "Redis, and Cloudflare in front. That setup runs more production "
        "traffic than most teams will ever ship. Add secrets in a real "
        "manager (Vault, Doppler, 1Password), observability via Honeycomb "
        "or Logfire, and deploys via Kamal or GitHub Actions. You can run "
        "a real company on this stack and only outgrow it when you "
        "actually have to.",
        source_label="Ably Engineering: No, we don't use Kubernetes",
        source_url="https://ably.com/blog/no-we-dont-use-kubernetes",
        source_key="infra:faq:smallest-production-ready",
    ),
    FAQ(
        "When does it make sense to add a private mesh like Tailscale?",
        "As soon as you have more than a couple of servers that need to "
        "talk to each other without going through the public internet. "
        "Tailscale gives you SSH access, internal services, and database "
        "connections from a developer's laptop without managing a bastion "
        "or a VPN. The cost is per-user pricing at scale; the value is "
        "that \"connect to the internal network\" becomes the same thing "
        "as \"log in.\"",
        source_label="Tailscale: How Tailscale works",
        source_url="https://tailscale.com/blog/how-tailscale-works",
        source_key="infra:faq:tailscale-mesh",
    ),
]

"""Curated content for /resources/system-architecture.

Hand-maintained. Edit and redeploy.

Structure mirrors the agentic-coding page but with a 2-level Tools section:
peer tools that solve the same problem are grouped under decision categories
(Databases, Caching, Queues, etc.). Each category has an editorial intro that
articulates the decision space, then a flat list of the tools inside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from smarter_dev.web.vibe_courses_data import FAQ, Person


@dataclass(frozen=True)
class ArchTool:
    """A specific tool inside an architecture category."""

    slug: str         # "postgres", "redis", ...
    name: str         # display name
    url: str          # official site
    home_key: str     # track_key for the home link
    blurb: str        # one-line description


@dataclass(frozen=True)
class ArchCategory:
    """A decision category that groups peer tools."""

    slug: str         # URL anchor: "databases", "caching", ...
    name: str         # display: "Databases & data stores"
    intro: str        # editorial intro on the decision space
    tools: tuple[ArchTool, ...]


@dataclass(frozen=True)
class ArchResource:
    """Cross-cutting system-design resource (the spine of the page)."""

    title: str
    url: str
    source: str
    key: str
    learning_type: str           # Tutorial / Course / Discussion / Best Practices / Talk
    first_indexed_at: date
    published_at: date | None = None
    blurb: str = ""

    @property
    def sort_date(self) -> date:
        return self.published_at or self.first_indexed_at

    @property
    def category_slug(self) -> str:
        return self.learning_type.lower().replace(" ", "-")


@dataclass(frozen=True)
class ArchToolResource:
    """Per-tool learning resource: docs, tutorial, walkthrough, talk."""

    title: str
    url: str
    source: str
    key: str
    tool_slugs: tuple[str, ...]  # which tool(s) this resource teaches
    learning_type: str
    first_indexed_at: date
    published_at: date | None = None
    blurb: str = ""

    @property
    def category_slug(self) -> str:
        return self.learning_type.lower().replace(" ", "-")


CATEGORIES: tuple[str, ...] = (
    "Tutorial",
    "Course",
    "Discussion",
    "Best Practices",
    "Talk",
)


_INDEXED = date(2026, 5, 12)


# ─── CATEGORIES (the middle of the page) ────────────────────────────────────

ARCH_CATEGORIES: list[ArchCategory] = [
    ArchCategory(
        slug="databases",
        name="Databases & data stores",
        intro=(
            "Postgres handles 99% of what most teams need. Specialized stores "
            "buy specific things: ClickHouse for analytics over billions of "
            "rows, DuckDB for local-first columnar work, DynamoDB for flat "
            "scaling, SQLite for embedded. Pick by what you'll do too much of."
        ),
        tools=(
            ArchTool("postgres", "PostgreSQL",
                     "https://www.postgresql.org/",
                     "arch:tool:postgres:home",
                     "The default for almost everything: relational, JSON, full-text, geospatial, vectors."),
            ArchTool("mysql", "MySQL",
                     "https://www.mysql.com/",
                     "arch:tool:mysql:home",
                     "Relational alternative to Postgres with slightly different operational characteristics."),
            ArchTool("sqlite", "SQLite",
                     "https://www.sqlite.org/",
                     "arch:tool:sqlite:home",
                     "Embedded relational, single file, no server. The most widely deployed database on earth."),
            ArchTool("mongodb", "MongoDB",
                     "https://www.mongodb.com/",
                     "arch:tool:mongodb:home",
                     "Document database with flexible schemas. Hosted offering (Atlas) is the common path."),
            ArchTool("dynamodb", "DynamoDB",
                     "https://aws.amazon.com/dynamodb/",
                     "arch:tool:dynamodb:home",
                     "Managed key-value with predictable performance at any scale. Real lock-in to AWS."),
            ArchTool("duckdb", "DuckDB",
                     "https://duckdb.org/",
                     "arch:tool:duckdb:home",
                     "SQLite for analytics. Columnar, in-process, embarrassingly fast on local data."),
            ArchTool("clickhouse", "ClickHouse",
                     "https://clickhouse.com/",
                     "arch:tool:clickhouse:home",
                     "Column-store OLAP for billions of rows. The fastest analytical database in widespread use."),
        ),
    ),
    ArchCategory(
        slug="caching",
        name="Caching & in-memory",
        intro=(
            "A cache is a second source of truth with worse durability. The "
            "decision isn't Redis or Memcached. It's what you're caching, how "
            "stale it can be, and what happens when the cache fails. Most "
            "cache-related outages are about the failure mode, not throughput."
        ),
        tools=(
            ArchTool("redis", "Redis",
                     "https://redis.io/",
                     "arch:tool:redis:home",
                     "In-memory data structure store. Defaults to caching but also queues, streams, pub/sub."),
            ArchTool("valkey", "Valkey",
                     "https://valkey.io/",
                     "arch:tool:valkey:home",
                     "Linux Foundation Redis fork; BSD-licensed, growing fast since Redis's license shift."),
            ArchTool("memcached", "Memcached",
                     "https://memcached.org/",
                     "arch:tool:memcached:home",
                     "Minimal in-memory cache. No persistence, no data types, no fuss."),
        ),
    ),
    ArchCategory(
        slug="queues",
        name="Queues & streams",
        intro=(
            "Queues let work outlive the request that asked for it. Streams "
            "let multiple consumers read the same log with their own pointers. "
            "Pick a queue when downstream is slower than upstream. Pick a "
            "stream when you need to replay."
        ),
        tools=(
            ArchTool("rabbitmq", "RabbitMQ",
                     "https://www.rabbitmq.com/",
                     "arch:tool:rabbitmq:home",
                     "Mature message broker with multiple protocols. The default work-queue choice."),
            ArchTool("kafka", "Apache Kafka",
                     "https://kafka.apache.org/",
                     "arch:tool:kafka:home",
                     "Durable append-only log. The canonical event-streaming platform."),
            ArchTool("nats", "NATS",
                     "https://nats.io/",
                     "arch:tool:nats:home",
                     "Lightweight high-performance messaging. Queues, streams, request/reply, key-value."),
            ArchTool("sqs", "Amazon SQS",
                     "https://aws.amazon.com/sqs/",
                     "arch:tool:sqs:home",
                     "Managed queue from AWS. Simple, reliable, no infrastructure to operate."),
            ArchTool("redis-streams", "Redis Streams",
                     "https://redis.io/docs/latest/develop/data-types/streams/",
                     "arch:tool:redis-streams:home",
                     "Redis-native append-only log. Lightweight Kafka alternative if you already run Redis."),
            ArchTool("bullmq", "BullMQ",
                     "https://docs.bullmq.io/",
                     "arch:tool:bullmq:home",
                     "Node.js job queue library on top of Redis. Most popular choice in the JS ecosystem."),
            ArchTool("sidekiq", "Sidekiq",
                     "https://sidekiq.org/",
                     "arch:tool:sidekiq:home",
                     "Ruby background job processor; uses Redis. The standard in Rails apps."),
            ArchTool("celery", "Celery",
                     "https://docs.celeryq.dev/",
                     "arch:tool:celery:home",
                     "Python distributed task queue. Uses Redis or RabbitMQ as broker."),
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
                     "arch:tool:nginx:home",
                     "Battle-tested high-performance web server and reverse proxy."),
            ArchTool("traefik", "Traefik",
                     "https://traefik.io/traefik/",
                     "arch:tool:traefik:home",
                     "Modern proxy designed for container orchestrators; auto-discovers services."),
            ArchTool("caddy", "Caddy",
                     "https://caddyserver.com/",
                     "arch:tool:caddy:home",
                     "Web server with automatic TLS by default. Written in Go. Simplest config of the bunch."),
            ArchTool("haproxy", "HAProxy",
                     "https://www.haproxy.org/",
                     "arch:tool:haproxy:home",
                     "High-performance TCP/HTTP proxy with deep tuning surface for serious load."),
            ArchTool("envoy", "Envoy",
                     "https://www.envoyproxy.io/",
                     "arch:tool:envoy:home",
                     "Proxy designed for service meshes. Powers Istio, Consul Connect, Linkerd."),
        ),
    ),
    ArchCategory(
        slug="search",
        name="Search",
        intro=(
            "Most teams reach for Elasticsearch before they need it. Postgres "
            "full-text handles more than people think. When you actually need "
            "search (relevance tuning, facets, real-time indexing over millions "
            "of docs), pick between heavyweight (Elastic, OpenSearch) and "
            "lightweight (Meili, Typesense)."
        ),
        tools=(
            ArchTool("elasticsearch", "Elasticsearch",
                     "https://www.elastic.co/elasticsearch",
                     "arch:tool:elasticsearch:home",
                     "Heavyweight distributed search engine. License shifted to SSPL/Elastic License."),
            ArchTool("opensearch", "OpenSearch",
                     "https://opensearch.org/",
                     "arch:tool:opensearch:home",
                     "Apache-2.0 fork of Elasticsearch maintained by AWS and the community."),
            ArchTool("meilisearch", "Meilisearch",
                     "https://www.meilisearch.com/",
                     "arch:tool:meilisearch:home",
                     "Lightweight typo-tolerant search. Trivial to operate, great defaults."),
            ArchTool("typesense", "Typesense",
                     "https://typesense.org/",
                     "arch:tool:typesense:home",
                     "Lightweight search alternative to Meili with slightly different feature surface."),
        ),
    ),
    ArchCategory(
        slug="storage",
        name="Object storage",
        intro=(
            "Object storage is solved. The decisions are cost and lock-in. R2 "
            "and B2 have no egress fees, S3 has the deepest ecosystem, MinIO "
            "runs on your own hardware. For most workloads, S3-compatible is "
            "the only spec that matters."
        ),
        tools=(
            ArchTool("s3", "Amazon S3",
                     "https://aws.amazon.com/s3/",
                     "arch:tool:s3:home",
                     "The canonical object storage service. Deepest ecosystem; you'll integrate with this."),
            ArchTool("r2", "Cloudflare R2",
                     "https://www.cloudflare.com/developer-platform/products/r2/",
                     "arch:tool:r2:home",
                     "S3-compatible with no egress fees. Great when you serve files directly to users."),
            ArchTool("b2", "Backblaze B2",
                     "https://www.backblaze.com/cloud-storage",
                     "arch:tool:b2:home",
                     "S3-compatible with low storage prices and free egress to many CDNs."),
            ArchTool("minio", "MinIO",
                     "https://min.io/",
                     "arch:tool:minio:home",
                     "Self-hosted S3-compatible storage. Run on your own hardware or k8s."),
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
                     "arch:tool:kubernetes:home",
                     "The dominant container orchestrator. Complex but ubiquitous; managed offerings everywhere."),
            ArchTool("nomad", "HashiCorp Nomad",
                     "https://www.nomadproject.io/",
                     "arch:tool:nomad:home",
                     "Simpler orchestrator supporting more than containers. Pairs well with Consul and Vault."),
            ArchTool("docker-compose", "Docker Compose",
                     "https://docs.docker.com/compose/",
                     "arch:tool:docker-compose:home",
                     "Multi-container apps on a single host. Often enough for small deployments."),
        ),
    ),
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
                     "arch:tool:prometheus:home",
                     "Pull-based metrics collection. Deeply integrated with the Kubernetes ecosystem."),
            ArchTool("grafana", "Grafana",
                     "https://grafana.com/",
                     "arch:tool:grafana:home",
                     "Dashboard tool that fronts Prometheus, Loki, and most observability backends."),
            ArchTool("opentelemetry", "OpenTelemetry",
                     "https://opentelemetry.io/",
                     "arch:tool:opentelemetry:home",
                     "Vendor-neutral instrumentation standard for traces, metrics, and logs."),
            ArchTool("loki", "Grafana Loki",
                     "https://grafana.com/oss/loki/",
                     "arch:tool:loki:home",
                     "Log aggregation system designed to pair with Prometheus and Grafana."),
            ArchTool("logfire", "Pydantic Logfire",
                     "https://pydantic.dev/logfire",
                     "arch:tool:logfire:home",
                     "Observability platform from the Pydantic team. Structured tracing, OpenTelemetry-native."),
            ArchTool("honeycomb", "Honeycomb",
                     "https://www.honeycomb.io/",
                     "arch:tool:honeycomb:home",
                     "High-cardinality structured-event observability. Pioneered the \"observability 2.0\" frame."),
        ),
    ),
    ArchCategory(
        slug="vector",
        name="Vector & retrieval",
        intro=(
            "For most teams, pgvector inside Postgres is the right answer. "
            "Specialized vector databases buy scale (billions of vectors), "
            "advanced filtering, or hosted SLAs. The decision point is when "
            "retrieval workload starts to dominate normal load. Usually later "
            "than you think."
        ),
        tools=(
            ArchTool("pgvector", "pgvector",
                     "https://github.com/pgvector/pgvector",
                     "arch:tool:pgvector:home",
                     "Postgres extension that adds vector similarity search. The default for most teams."),
            ArchTool("qdrant", "Qdrant",
                     "https://qdrant.tech/",
                     "arch:tool:qdrant:home",
                     "Rust-based vector database with strong filtering. Open source; hosted offering available."),
            ArchTool("weaviate", "Weaviate",
                     "https://weaviate.io/",
                     "arch:tool:weaviate:home",
                     "Vector database with knowledge-graph features. Open source; hosted offering available."),
            ArchTool("pinecone", "Pinecone",
                     "https://www.pinecone.io/",
                     "arch:tool:pinecone:home",
                     "Managed vector database. The original commercial offering in the space."),
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
                     "arch:tool:vault:home",
                     "Secrets management: dynamic credentials, encryption-as-a-service, deep audit."),
            ArchTool("keycloak", "Keycloak",
                     "https://www.keycloak.org/",
                     "arch:tool:keycloak:home",
                     "Self-hostable identity and access management. Full OAuth, OIDC, SAML support."),
            ArchTool("ory", "Ory",
                     "https://www.ory.sh/",
                     "arch:tool:ory:home",
                     "Modern OSS identity stack split into composable services (Kratos, Hydra, Oathkeeper)."),
            ArchTool("auth0", "Auth0",
                     "https://auth0.com/",
                     "arch:tool:auth0:home",
                     "Managed identity-as-a-service. Fastest to integrate; expensive at scale."),
        ),
    ),
]


# ─── SPINE: cross-cutting resources ──────────────────────────────────────────

ARCH_RESOURCES: list[ArchResource] = [
    ArchResource(
        "Designing Data-Intensive Applications",
        "https://dataintensive.net/",
        "Martin Kleppmann",
        "arch:spine:ddia",
        learning_type="Best Practices",
        first_indexed_at=_INDEXED,
        blurb="The canonical map of databases, replication, consensus, and stream-processing tradeoffs.",
    ),
    ArchResource(
        "The Amazon Builders' Library",
        "https://aws.amazon.com/builders-library/",
        "AWS Builders' Library",
        "arch:spine:aws-builders-library",
        learning_type="Best Practices",
        first_indexed_at=_INDEXED,
        blurb="Principal engineers explaining how Amazon actually builds and operates production services.",
    ),
    ArchResource(
        "Marc Brooker's Blog",
        "https://brooker.co.za/blog/",
        "Marc Brooker (AWS)",
        "arch:spine:brooker-blog",
        learning_type="Discussion",
        first_indexed_at=_INDEXED,
        blurb="AWS distinguished engineer on databases, durability, queues, retries, and metastability.",
    ),
    ArchResource(
        "Jepsen Analyses",
        "https://jepsen.io/analyses",
        "Kyle Kingsbury (Aphyr)",
        "arch:spine:jepsen-analyses",
        learning_type="Best Practices",
        first_indexed_at=_INDEXED,
        blurb="Empirical safety tests of databases and queues under partitions, clock skew, and faults.",
    ),
    ArchResource(
        "Caches, Modes, and Unstable Systems",
        "https://brooker.co.za/blog/2021/08/27/caches.html",
        "Marc Brooker",
        "arch:spine:brooker-caches",
        learning_type="Best Practices",
        first_indexed_at=_INDEXED,
        published_at=date(2021, 8, 27),
        blurb="Why caches create metastable failure modes load tests miss until production explodes.",
    ),
    ArchResource(
        "TIGER_STYLE",
        "https://github.com/tigerbeetle/tigerbeetle/blob/main/docs/TIGER_STYLE.md",
        "TigerBeetle",
        "arch:spine:tiger-style",
        learning_type="Best Practices",
        first_indexed_at=_INDEXED,
        blurb="Safety-first engineering doctrine: assertions, static allocation, batching, zero dependencies.",
    ),
    ArchResource(
        "Distributed Systems lecture series",
        "https://www.youtube.com/playlist?list=PLeKd45zvjcDFUEv_ohr_HdUFe97RItdiB",
        "Martin Kleppmann (Cambridge)",
        "arch:spine:kleppmann-lectures",
        learning_type="Course",
        first_indexed_at=_INDEXED,
        blurb="Eight-lecture Cambridge series on clocks, replication, consensus, linearizability, and Spanner.",
    ),
    ArchResource(
        "Performance Analysis Methodology",
        "https://www.brendangregg.com/methodology.html",
        "Brendan Gregg",
        "arch:spine:gregg-methodology",
        learning_type="Best Practices",
        first_indexed_at=_INDEXED,
        blurb="USE Method and other systematic approaches to finding real performance bottlenecks fast.",
    ),
    ArchResource(
        "Hyrum's Law",
        "https://www.hyrumslaw.com/",
        "Hyrum Wright",
        "arch:spine:hyrums-law",
        learning_type="Discussion",
        first_indexed_at=_INDEXED,
        blurb="With enough users, every observable behavior of your API becomes a contract someone depends on.",
    ),
    ArchResource(
        "Workload Isolation Using Shuffle-Sharding",
        "https://aws.amazon.com/builders-library/workload-isolation-using-shuffle-sharding/",
        "Colm MacCárthaigh, AWS Builders' Library",
        "arch:spine:shuffle-sharding",
        learning_type="Best Practices",
        first_indexed_at=_INDEXED,
        blurb="Multi-tenant fault isolation pattern that limits blast radius without dedicated capacity.",
    ),
    ArchResource(
        "Static Stability Using Availability Zones",
        "https://aws.amazon.com/builders-library/static-stability-using-availability-zones/",
        "AWS Builders' Library",
        "arch:spine:static-stability",
        learning_type="Best Practices",
        first_indexed_at=_INDEXED,
        blurb="Design so a dependency's failure changes nothing. Pre-provision instead of reacting.",
    ),
    ArchResource(
        "The Morning Paper (archive)",
        "https://blog.acolyer.org/",
        "Adrian Colyer",
        "arch:spine:morning-paper",
        learning_type="Discussion",
        first_indexed_at=_INDEXED,
        blurb="Archive of daily computer-science paper summaries. Paused since 2021; back catalog is foundational.",
    ),
]


# ─── PER-TOOL RESOURCES ──────────────────────────────────────────────────────
# Each entry teaches a specific tool. The controller groups them by category
# at render time using each tool's parent ArchCategory.


def _r(title, url, source, key, tool_slugs, learning_type, blurb=""):
    """Compact builder for ArchToolResource. Defaults first_indexed_at."""
    return ArchToolResource(
        title=title, url=url, source=source, key=key,
        tool_slugs=tuple(tool_slugs), learning_type=learning_type,
        first_indexed_at=_INDEXED, blurb=blurb,
    )


ARCH_TOOL_RESOURCES: list[ArchToolResource] = [
    # Postgres
    _r("PostgreSQL Tutorial (official)",
       "https://www.postgresql.org/docs/current/tutorial.html",
       "PostgreSQL docs", "arch:res:postgres:docs-tutorial", ["postgres"], "Tutorial",
       "Official tutorial covering SQL basics, schemas, transactions, inheritance, and Postgres-specific features."),
    _r("PostgreSQL Tutorial (third-party)",
       "https://www.postgresqltutorial.com/",
       "PostgreSQL Tutorial", "arch:res:postgres:pgtutorial", ["postgres"], "Tutorial",
       "Free comprehensive tutorial covering psql, queries, joins, transactions, indexes, and performance."),
    _r("Use The Index, Luke!",
       "https://use-the-index-luke.com/",
       "Markus Winand", "arch:res:postgres:use-the-index-luke", ["postgres", "mysql"], "Best Practices",
       "Canonical guide to SQL indexing and query performance tuning for application developers."),

    # MySQL
    _r("MySQL Tutorial",
       "https://dev.mysql.com/doc/refman/8.0/en/tutorial.html",
       "MySQL docs", "arch:res:mysql:docs-tutorial", ["mysql"], "Tutorial",
       "Official walkthrough of the mysql client, creating databases, tables, and running queries."),
    _r("MySQL Course for Beginners",
       "https://www.youtube.com/watch?v=7S_tz1z_5bA",
       "YouTube · freeCodeCamp", "arch:res:mysql:freecodecamp", ["mysql"], "Course",
       "Three-hour video course covering installation, SQL syntax, joins, and database design."),

    # SQLite
    _r("SQLite Quickstart",
       "https://www.sqlite.org/quickstart.html",
       "SQLite docs", "arch:res:sqlite:quickstart", ["sqlite"], "Tutorial",
       "Official quickstart for the sqlite3 CLI: creating databases, schemas, and running queries."),
    _r("Appropriate Uses For SQLite",
       "https://www.sqlite.org/whentouse.html",
       "SQLite docs", "arch:res:sqlite:when-to-use", ["sqlite"], "Best Practices",
       "When SQLite is the right choice versus a client/server database, with concrete scenarios."),

    # MongoDB
    _r("MongoDB Getting Started",
       "https://www.mongodb.com/docs/manual/tutorial/getting-started/",
       "MongoDB docs", "arch:res:mongodb:getting-started", ["mongodb"], "Tutorial",
       "Official getting-started covering documents, collections, CRUD operations, and aggregation."),
    _r("MongoDB University",
       "https://learn.mongodb.com/",
       "MongoDB University", "arch:res:mongodb:university", ["mongodb"], "Course",
       "Free structured courses on data modeling, indexing, aggregation, and operational topics."),

    # DynamoDB
    _r("Getting Started with DynamoDB",
       "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GettingStartedDynamoDB.html",
       "AWS docs", "arch:res:dynamodb:getting-started", ["dynamodb"], "Tutorial",
       "Official tutorial walking through tables, items, queries, and the DynamoDB Local environment."),
    _r("The DynamoDB Guide",
       "https://www.dynamodbguide.com/",
       "Alex DeBrie", "arch:res:dynamodb:debrie-guide", ["dynamodb"], "Best Practices",
       "Opinionated guide to single-table design, access patterns, and DynamoDB modeling fundamentals."),

    # DuckDB
    _r("DuckDB Getting Started",
       "https://duckdb.org/docs/stable/",
       "DuckDB docs", "arch:res:duckdb:docs", ["duckdb"], "Tutorial",
       "Official quickstart for the embedded analytical database, with CLI, Python, and SQL examples."),
    _r("DuckDB Tutorial for Beginners",
       "https://motherduck.com/blog/duckdb-tutorial-for-beginners/",
       "MotherDuck blog", "arch:res:duckdb:motherduck-tutorial", ["duckdb"], "Tutorial",
       "Hands-on intro to querying CSV, Parquet, and JSON files directly with DuckDB."),

    # ClickHouse
    _r("ClickHouse Quick Start",
       "https://clickhouse.com/docs/getting-started/quick-start",
       "ClickHouse docs", "arch:res:clickhouse:quick-start", ["clickhouse"], "Tutorial",
       "Install, load data, and run analytical queries on the columnar OLAP database."),
    _r("ClickHouse Academy",
       "https://learn.clickhouse.com/",
       "ClickHouse Academy", "arch:res:clickhouse:academy", ["clickhouse"], "Course",
       "Free courses on data modeling, MergeTree engines, and production operations."),

    # Redis
    _r("Redis Quick Start",
       "https://redis.io/docs/latest/get-started/",
       "Redis docs", "arch:res:redis:quick-start", ["redis"], "Tutorial",
       "Official getting-started covering installation, redis-cli, key types, and common commands."),
    _r("Redis University",
       "https://university.redis.io/",
       "Redis University", "arch:res:redis:university", ["redis"], "Course",
       "Free structured courses on data structures, caching patterns, and Redis Stack modules."),

    # Valkey
    _r("Valkey: Introduction",
       "https://valkey.io/topics/introduction/",
       "Valkey docs", "arch:res:valkey:intro", ["valkey"], "Tutorial",
       "Introduction to the Linux Foundation Redis fork, including installation and command reference."),

    # Memcached
    _r("Memcached Wiki",
       "https://github.com/memcached/memcached/wiki",
       "GitHub · memcached", "arch:res:memcached:wiki", ["memcached"], "Tutorial",
       "Official wiki covering protocol, configuration, tuning, and common usage patterns."),

    # RabbitMQ
    _r("RabbitMQ Tutorials",
       "https://www.rabbitmq.com/tutorials",
       "RabbitMQ docs", "arch:res:rabbitmq:tutorials", ["rabbitmq"], "Tutorial",
       "Six canonical tutorials: work queues, pub/sub, routing, topics, RPC, and acknowledgements."),
    _r("RabbitMQ Documentation",
       "https://www.rabbitmq.com/docs",
       "RabbitMQ docs", "arch:res:rabbitmq:docs", ["rabbitmq"], "Best Practices",
       "Full documentation hub: clustering, persistence, flow control, monitoring, and production tuning."),

    # Kafka
    _r("Apache Kafka Quickstart",
       "https://kafka.apache.org/quickstart",
       "Kafka docs", "arch:res:kafka:quickstart", ["kafka"], "Tutorial",
       "Start a broker, create topics, produce and consume messages, and run Kafka Connect."),
    _r("Apache Kafka 101",
       "https://developer.confluent.io/courses/apache-kafka/events/",
       "Confluent Developer", "arch:res:kafka:101-course", ["kafka"], "Course",
       "Free video course on Kafka fundamentals: topics, partitions, producers, consumers, and brokers."),

    # NATS
    _r("NATS Concepts & Walkthrough",
       "https://docs.nats.io/nats-concepts/overview",
       "NATS docs", "arch:res:nats:overview", ["nats"], "Tutorial",
       "Concept overview and walkthroughs covering core NATS, JetStream, and key/value stores."),

    # SQS
    _r("Getting Started with Amazon SQS",
       "https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-getting-started.html",
       "AWS docs", "arch:res:sqs:getting-started", ["sqs"], "Tutorial",
       "Create queues, send and receive messages, and configure dead-letter queues via console or SDK."),
    _r("SQS Best Practices",
       "https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-best-practices.html",
       "AWS docs", "arch:res:sqs:best-practices", ["sqs"], "Best Practices",
       "Official guidance on visibility timeouts, polling, idempotency, and queue throughput tuning."),

    # Redis Streams
    _r("Introduction to Redis Streams",
       "https://redis.io/docs/latest/develop/data-types/streams/",
       "Redis docs", "arch:res:redis-streams:intro", ["redis-streams"], "Tutorial",
       "Official guide: XADD, consumer groups, XREADGROUP, acknowledgement, and stream trimming."),

    # BullMQ
    _r("BullMQ Guide",
       "https://docs.bullmq.io/guide/introduction",
       "BullMQ docs", "arch:res:bullmq:guide", ["bullmq"], "Tutorial",
       "Official guide for the Node.js queue library: producers, workers, flows, repeatable jobs."),

    # Sidekiq
    _r("Sidekiq Getting Started",
       "https://github.com/sidekiq/sidekiq/wiki/Getting-Started",
       "Sidekiq wiki", "arch:res:sidekiq:getting-started", ["sidekiq"], "Tutorial",
       "Official wiki: install, define workers, enqueue jobs, and run the Sidekiq process."),
    _r("Sidekiq Best Practices",
       "https://github.com/sidekiq/sidekiq/wiki/Best-Practices",
       "Sidekiq wiki", "arch:res:sidekiq:best-practices", ["sidekiq"], "Best Practices",
       "Job idempotency, small arguments, embracing concurrency, and operational guidance."),

    # Celery
    _r("First Steps with Celery",
       "https://docs.celeryq.dev/en/stable/getting-started/first-steps-with-celery.html",
       "Celery docs", "arch:res:celery:first-steps", ["celery"], "Tutorial",
       "Official tutorial: define tasks, configure brokers, run workers, and check results."),
    _r("Celery Best Practices",
       "https://denibertovic.com/posts/celery-best-practices/",
       "Deni Bertovic", "arch:res:celery:best-practices", ["celery"], "Best Practices",
       "Canonical post on idempotent tasks, retries, naming, and avoiding common Celery pitfalls."),

    # Nginx
    _r("Nginx Beginner's Guide",
       "https://nginx.org/en/docs/beginners_guide.html",
       "Nginx docs", "arch:res:nginx:beginners", ["nginx"], "Tutorial",
       "Official intro: serving static content, reverse proxy, FastCGI, and load balancing basics."),
    _r("Nginx Admin's Handbook",
       "https://github.com/trimstray/nginx-admins-handbook",
       "GitHub · trimstray", "arch:res:nginx:admins-handbook", ["nginx"], "Best Practices",
       "Operator guide covering configuration patterns, hardening, performance, and debugging."),

    # Traefik
    _r("Traefik Quick Start",
       "https://doc.traefik.io/traefik/getting-started/quick-start/",
       "Traefik docs", "arch:res:traefik:quick-start", ["traefik"], "Tutorial",
       "Run Traefik with Docker, discover services automatically, and route HTTP traffic."),

    # Caddy
    _r("Caddy Getting Started",
       "https://caddyserver.com/docs/getting-started",
       "Caddy docs", "arch:res:caddy:getting-started", ["caddy"], "Tutorial",
       "Run Caddy as a static file server, reverse proxy, and HTTPS terminator with automatic TLS."),
    _r("Caddyfile Concepts",
       "https://caddyserver.com/docs/caddyfile/concepts",
       "Caddy docs", "arch:res:caddy:caddyfile", ["caddy"], "Tutorial",
       "Caddyfile syntax, matchers, directives, and snippets for typical reverse-proxy setups."),

    # HAProxy
    _r("HAProxy Starter Guide",
       "https://docs.haproxy.org/3.0/intro.html",
       "HAProxy docs", "arch:res:haproxy:intro", ["haproxy"], "Tutorial",
       "Introduction to load balancing concepts, frontends, backends, and ACLs in HAProxy."),
    _r("HAProxy Configuration Manual",
       "https://docs.haproxy.org/3.0/configuration.html",
       "HAProxy docs", "arch:res:haproxy:config-manual", ["haproxy"], "Best Practices",
       "Canonical reference for every config directive: timeouts, health checks, stick tables, SSL."),

    # Envoy
    _r("Envoy Getting Started",
       "https://www.envoyproxy.io/docs/envoy/latest/start/start",
       "Envoy docs", "arch:res:envoy:start", ["envoy"], "Tutorial",
       "Run Envoy in Docker, configure listeners, clusters, and basic HTTP routing."),
    _r("Envoy Sandboxes",
       "https://www.envoyproxy.io/docs/envoy/latest/start/sandboxes/sandboxes",
       "Envoy docs", "arch:res:envoy:sandboxes", ["envoy"], "Tutorial",
       "Working Docker Compose examples for front proxy, gRPC bridge, JWT auth, and more."),

    # Elasticsearch
    _r("Elasticsearch Quick Start",
       "https://www.elastic.co/guide/en/elasticsearch/reference/current/getting-started.html",
       "Elastic docs", "arch:res:elasticsearch:quick-start", ["elasticsearch"], "Tutorial",
       "Run Elasticsearch locally, index documents, and run match, term, and aggregation queries."),
    _r("Elasticsearch: The Definitive Guide",
       "https://www.elastic.co/guide/en/elasticsearch/guide/current/index.html",
       "Elastic docs", "arch:res:elasticsearch:definitive-guide", ["elasticsearch"], "Best Practices",
       "Long-form guide to mapping, analyzers, relevance, aggregations, and cluster scaling."),

    # OpenSearch
    _r("OpenSearch Quickstart",
       "https://opensearch.org/docs/latest/getting-started/",
       "OpenSearch docs", "arch:res:opensearch:quickstart", ["opensearch"], "Tutorial",
       "Run OpenSearch and Dashboards, index data, and run search and aggregation queries."),

    # Meilisearch
    _r("Meilisearch Quick Start",
       "https://www.meilisearch.com/docs/learn/getting_started/quick_start",
       "Meilisearch docs", "arch:res:meilisearch:quick-start", ["meilisearch"], "Tutorial",
       "Install, add documents, and run typo-tolerant searches with filters and ranking rules."),

    # Typesense
    _r("Typesense Guide",
       "https://typesense.org/docs/guide/",
       "Typesense docs", "arch:res:typesense:guide", ["typesense"], "Tutorial",
       "Install, create collections, index documents, and tune ranking and faceting in Typesense."),

    # S3
    _r("Getting Started with Amazon S3",
       "https://docs.aws.amazon.com/AmazonS3/latest/userguide/GetStartedWithS3.html",
       "AWS docs", "arch:res:s3:getting-started", ["s3"], "Tutorial",
       "Create buckets, upload objects, manage access, and configure lifecycle and versioning."),
    _r("S3 Performance Best Practices",
       "https://docs.aws.amazon.com/AmazonS3/latest/userguide/optimizing-performance.html",
       "AWS docs", "arch:res:s3:performance", ["s3"], "Best Practices",
       "Official guidance on request rates, key naming, multipart uploads, and Transfer Acceleration."),

    # R2
    _r("Cloudflare R2 Get Started",
       "https://developers.cloudflare.com/r2/get-started/",
       "Cloudflare docs", "arch:res:r2:get-started", ["r2"], "Tutorial",
       "Create R2 buckets, upload objects via Wrangler or the S3-compatible API, and serve them."),

    # B2
    _r("Backblaze B2 Getting Started",
       "https://www.backblaze.com/docs/cloud-storage-getting-started-with-backblaze-b2",
       "Backblaze docs", "arch:res:b2:getting-started", ["b2"], "Tutorial",
       "Create buckets and application keys, upload files via web UI, CLI, and S3-compatible API."),

    # MinIO
    _r("MinIO Quickstart",
       "https://min.io/docs/minio/linux/index.html",
       "MinIO docs", "arch:res:minio:quickstart", ["minio"], "Tutorial",
       "Install MinIO single-node and distributed, use the mc client, and configure access policies."),

    # Kubernetes
    _r("Kubernetes Tutorials",
       "https://kubernetes.io/docs/tutorials/",
       "Kubernetes docs", "arch:res:kubernetes:tutorials", ["kubernetes"], "Tutorial",
       "Official tutorials: Kubernetes Basics, stateful apps, services, and ConfigMaps."),
    _r("Kubernetes The Hard Way",
       "https://github.com/kelseyhightower/kubernetes-the-hard-way",
       "GitHub · Kelsey Hightower", "arch:res:kubernetes:hard-way", ["kubernetes"], "Tutorial",
       "Bootstrap a cluster from scratch to understand every component without abstractions."),
    _r("Kubernetes Production Best Practices",
       "https://learnk8s.io/production-best-practices",
       "Learnk8s", "arch:res:kubernetes:learnk8s-prod", ["kubernetes"], "Best Practices",
       "Checklist covering app health, scalability, observability, security, and resource governance."),

    # Nomad
    _r("Nomad Tutorials",
       "https://developer.hashicorp.com/nomad/tutorials",
       "HashiCorp Developer", "arch:res:nomad:tutorials", ["nomad"], "Tutorial",
       "Official learning path: install, run jobs, schedule services, batch jobs, and integrate Consul."),

    # Docker Compose
    _r("Docker Compose Overview",
       "https://docs.docker.com/compose/",
       "Docker docs", "arch:res:docker-compose:overview", ["docker-compose"], "Tutorial",
       "Get started defining multi-container apps with compose.yaml, networks, volumes, and profiles."),
    _r("Awesome Compose",
       "https://github.com/docker/awesome-compose",
       "GitHub · Docker", "arch:res:docker-compose:awesome", ["docker-compose"], "Tutorial",
       "Official sample compose files: Django+Postgres, Flask+Redis, Nginx, and other common stacks."),

    # Prometheus
    _r("Prometheus Getting Started",
       "https://prometheus.io/docs/prometheus/latest/getting_started/",
       "Prometheus docs", "arch:res:prometheus:getting-started", ["prometheus"], "Tutorial",
       "Install Prometheus, scrape targets, run PromQL queries, and configure your first alert."),
    _r("PromQL for Mere Mortals",
       "https://grafana.com/blog/2020/02/04/introduction-to-promql-the-prometheus-query-language/",
       "Grafana Labs blog", "arch:res:prometheus:promql-intro", ["prometheus"], "Tutorial",
       "Approachable intro to PromQL data types, selectors, rate, and aggregation operators."),

    # Grafana
    _r("Grafana Getting Started",
       "https://grafana.com/docs/grafana/latest/getting-started/",
       "Grafana docs", "arch:res:grafana:getting-started", ["grafana"], "Tutorial",
       "Install Grafana, connect a data source, build dashboards, and configure alerting."),

    # OpenTelemetry
    _r("OpenTelemetry Getting Started",
       "https://opentelemetry.io/docs/getting-started/",
       "OpenTelemetry docs", "arch:res:opentelemetry:getting-started", ["opentelemetry"], "Tutorial",
       "Instrument an app with traces, metrics, and logs using the Collector and language SDKs."),
    _r("OpenTelemetry Demo",
       "https://opentelemetry.io/docs/demo/",
       "OpenTelemetry docs", "arch:res:opentelemetry:demo", ["opentelemetry"], "Tutorial",
       "Microservices reference app showing real instrumentation across many languages and signals."),

    # Loki
    _r("Grafana Loki Get Started",
       "https://grafana.com/docs/loki/latest/get-started/",
       "Grafana docs", "arch:res:loki:get-started", ["loki"], "Tutorial",
       "Install Loki, ship logs with Promtail or Alloy, and query them with LogQL in Grafana."),

    # Logfire
    _r("Pydantic Logfire Documentation",
       "https://logfire.pydantic.dev/docs/",
       "Pydantic docs", "arch:res:logfire:docs", ["logfire"], "Tutorial",
       "Install Logfire, instrument Python apps, and view structured traces and logs in the UI."),

    # Honeycomb
    _r("Honeycomb Get Started",
       "https://docs.honeycomb.io/get-started/",
       "Honeycomb docs", "arch:res:honeycomb:get-started", ["honeycomb"], "Tutorial",
       "Send events via OpenTelemetry, run BubbleUp queries, and investigate production issues."),
    _r("Observability Engineering",
       "https://www.honeycomb.io/wp-content/uploads/2022/05/observability-engineering-honeycomb.pdf",
       "Honeycomb · O'Reilly", "arch:res:honeycomb:observability-engineering", ["honeycomb"], "Best Practices",
       "Charity Majors et al. on high-cardinality events, SLOs, and modern observability practice."),

    # pgvector
    _r("pgvector README",
       "https://github.com/pgvector/pgvector",
       "GitHub · pgvector", "arch:res:pgvector:readme", ["pgvector"], "Tutorial",
       "Install the extension, create vector columns, build HNSW/IVFFlat indexes, and run kNN queries."),

    # Qdrant
    _r("Qdrant Quickstart",
       "https://qdrant.tech/documentation/quickstart/",
       "Qdrant docs", "arch:res:qdrant:quickstart", ["qdrant"], "Tutorial",
       "Run Qdrant in Docker, create collections, upsert vectors with payloads, and run filtered searches."),

    # Weaviate
    _r("Weaviate Quickstart",
       "https://weaviate.io/developers/weaviate/quickstart",
       "Weaviate docs", "arch:res:weaviate:quickstart", ["weaviate"], "Tutorial",
       "Spin up Weaviate Cloud, define collections, import data, and run vector and hybrid queries."),

    # Pinecone
    _r("Pinecone Quickstart",
       "https://docs.pinecone.io/guides/get-started/quickstart",
       "Pinecone docs", "arch:res:pinecone:quickstart", ["pinecone"], "Tutorial",
       "Create an index, upsert vectors with metadata, and run similarity queries via Python SDK."),

    # Vault
    _r("Vault Tutorials",
       "https://developer.hashicorp.com/vault/tutorials",
       "HashiCorp Developer", "arch:res:vault:tutorials", ["vault"], "Tutorial",
       "Hands-on tutorials for KV secrets, dynamic database creds, transit encryption, and auth methods."),
    _r("Vault Production Hardening",
       "https://developer.hashicorp.com/vault/tutorials/operations/production-hardening",
       "HashiCorp Developer", "arch:res:vault:production-hardening", ["vault"], "Best Practices",
       "Official checklist: end-to-end TLS, root token rotation, auditing, and least-privilege policies."),

    # Keycloak
    _r("Keycloak Getting Started (Docker)",
       "https://www.keycloak.org/getting-started/getting-started-docker",
       "Keycloak docs", "arch:res:keycloak:docker-start", ["keycloak"], "Tutorial",
       "Run Keycloak in Docker, create a realm, register a client, and secure a sample app."),
    _r("Keycloak Server Administration Guide",
       "https://www.keycloak.org/docs/latest/server_admin/",
       "Keycloak docs", "arch:res:keycloak:server-admin", ["keycloak"], "Best Practices",
       "Reference for realms, clients, identity brokering, user federation, and authentication flows."),

    # Ory
    _r("Ory Documentation",
       "https://www.ory.sh/docs/",
       "Ory docs", "arch:res:ory:docs", ["ory"], "Tutorial",
       "Get started with Ory Kratos identities, Hydra OAuth2/OIDC, Keto permissions, and Oathkeeper."),

    # Auth0
    _r("Auth0 Get Started",
       "https://auth0.com/docs/get-started",
       "Auth0 docs", "arch:res:auth0:get-started", ["auth0"], "Tutorial",
       "Set up a tenant, create applications, and integrate login via Universal Login and SDKs."),
    _r("Auth0 Architecture Scenarios",
       "https://auth0.com/docs/get-started/architecture-scenarios",
       "Auth0 docs", "arch:res:auth0:architecture-scenarios", ["auth0"], "Best Practices",
       "Reference architectures for SPA+API, mobile+API, and B2B/B2C identity scenarios."),
]


# ─── CREATORS ────────────────────────────────────────────────────────────────

ARCH_PEOPLE: list[Person] = [
    Person(
        "Martin Kleppmann", "martinkl", "blog", "https://martin.kleppmann.com/",
        "arch:person:blog:martinkl",
        "DDIA author and Cambridge researcher; writes on distributed systems and local-first software.",
    ),
    Person(
        "Marc Brooker", "MarcJBrooker", "blog", "https://brooker.co.za/blog/",
        "arch:person:blog:brooker",
        "AWS distinguished engineer publishing weekly on databases, queues, retries, and durability.",
    ),
    Person(
        "Kyle Kingsbury (Aphyr)", "aphyr", "blog", "https://aphyr.com/posts",
        "arch:person:blog:aphyr",
        "Jepsen author. The empirical voice on what distributed systems actually do under failure.",
    ),
    Person(
        "Brendan Gregg", "brendangregg", "blog", "https://www.brendangregg.com/",
        "arch:person:blog:brendangregg",
        "The reference for Linux performance, flame graphs, and BPF observability.",
    ),
    Person(
        "Will Larson", "lethain", "blog", "https://lethain.com/",
        "arch:person:blog:lethain",
        "Engineering leader writing concrete frameworks for strategy, architecture, and staff-engineer work.",
    ),
    Person(
        "Charity Majors", "mipsytipsy", "blog", "https://charity.wtf/",
        "arch:person:blog:charity",
        "Honeycomb co-founder. Defines what modern observability means and where it's going.",
    ),
    Person(
        "Hillel Wayne", "hillelogram", "blog", "https://www.hillelwayne.com/",
        "arch:person:blog:hillel",
        "TLA+ and formal-methods writer. Teaches how to reason about systems before shipping.",
    ),
    Person(
        "Camille Fournier", "skamille", "blog", "https://skamille.medium.com/",
        "arch:person:blog:camille",
        "Ex-ZooKeeper maintainer; pragmatic essays on technical strategy and engineering leadership.",
    ),
    Person(
        "Jessica Kerr", "jessitron", "blog", "https://jessitron.com/",
        "arch:person:blog:jessitron",
        "Honeycomb dev advocate; systems-thinking essays on observability and socio-technical design.",
    ),
    Person(
        "Alex Xu (ByteByteGo)", "bytebytego", "newsletter", "https://www.bytebytego.com/",
        "arch:person:newsletter:bytebytego",
        "High-volume system design explainers; great for surveying patterns and interview vocabulary.",
    ),
    Person(
        "Jepsen", "jepsen", "blog", "https://jepsen.io/",
        "arch:person:blog:jepsen",
        "Companion to Aphyr's blog: vendor-funded but uncompromising correctness analyses.",
    ),
]


# ─── FAQs ────────────────────────────────────────────────────────────────────

ARCH_FAQS: list[FAQ] = [
    FAQ(
        "What does system architecture actually mean for a small team?",
        "Forget the enterprise diagrams. For a small team, architecture is "
        "the set of recurring decisions you're making every few months: "
        "which database, which queue, which observability stack, which auth "
        "provider. Will Larson's framing is to write down five real "
        "architecture decisions you've made, then find the pattern. That "
        "pattern is your architecture. Documents that don't change anything "
        "aren't architecture; they're paperwork.",
        source_label="Will Larson: Good engineering strategy is boring",
        source_url="https://lethain.com/good-engineering-strategy-is-boring/",
        source_key="arch:faq:larson-strategy-is-boring",
    ),
    FAQ(
        "When should I use a queue vs. just a database table?",
        "If you need work to outlive the request that asked for it, you need "
        "persistence somewhere. A database table with a poll loop is the "
        "simplest version. A real queue (RabbitMQ, SQS) buys fair scheduling, "
        "backpressure, and retries handled properly. Marc Brooker's point: "
        "queues don't actually deliver exactly-once. They deliver at-least-once "
        "and you handle the duplicates. If your workload can't tolerate that, "
        "you need idempotency in the consumer regardless of which tool.",
        source_label="Marc Brooker: Exactly-Once Delivery May Not Be What You Want",
        source_url="https://brooker.co.za/blog/2014/11/15/exactly-once.html",
        source_key="arch:faq:brooker-exactly-once",
    ),
    FAQ(
        "Postgres vs. specialized stores: when does it stop being enough?",
        "Later than you think. Postgres handles relational workloads, "
        "full-text search, JSON, time-series (with extensions), geospatial "
        "(with PostGIS), and vector search (with pgvector). The hard line "
        "comes when one workload starts to dominate enough that operating "
        "Postgres for it becomes harder than running a specialized store: "
        "analytics over billions of rows (ClickHouse), key-value at flat "
        "scale (DynamoDB), columnar local-first (DuckDB). For most teams, "
        "the right answer is to wait for the actual pain before splitting "
        "it out.",
        source_label="Tiger Data: It's 2026, Just Use Postgres",
        source_url="https://www.tigerdata.com/blog/its-2026-just-use-postgres",
        source_key="arch:faq:tigerdata-just-use-postgres",
    ),
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
        source_key="arch:faq:charity-observability-2",
    ),
    FAQ(
        "What do I need to know about caching before I deploy a cache?",
        "Caches add a metastable failure mode that load tests usually miss. "
        "When your cache is warm, the database sees 10% of traffic. When "
        "the cache empties (eviction storm, network blip, restart), the "
        "database suddenly sees 100%. Usually more than it can handle, "
        "which keeps the cache from refilling, which keeps the database "
        "overloaded. Decide what cache-miss looks like at full hit-rate "
        "before you ship. The fix is usually request coalescing, "
        "stale-while-revalidate, or some kind of admission control.",
        source_label="Marc Brooker: Caches, Modes, and Unstable Systems",
        source_url="https://brooker.co.za/blog/2021/08/27/caches.html",
        source_key="arch:faq:brooker-caches",
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
        source_key="arch:faq:aws-static-stability",
    ),
]

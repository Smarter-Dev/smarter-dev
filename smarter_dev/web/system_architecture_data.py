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
        slug="apis",
        name="APIs & protocols",
        intro=(
            "REST is the default for most public-facing APIs. GraphQL pays "
            "off when clients have wildly varying data needs and you can "
            "absorb the resolver complexity. gRPC and Protobuf win for "
            "internal service-to-service traffic where latency and schema "
            "discipline matter. The protocol is a contract: pick once, "
            "change rarely."
        ),
        tools=(
            ArchTool("openapi", "OpenAPI (Swagger)",
                     "https://www.openapis.org/",
                     "arch:tool:openapi:home",
                     "REST spec format and tooling ecosystem; the lingua franca for documenting HTTP APIs."),
            ArchTool("json-schema", "JSON Schema",
                     "https://json-schema.org/",
                     "arch:tool:json-schema:home",
                     "Schema validation standard for JSON; backbone of OpenAPI, AsyncAPI, and a lot of tooling."),
            ArchTool("protobuf", "Protocol Buffers",
                     "https://protobuf.dev/",
                     "arch:tool:protobuf:home",
                     "Google's IDL and binary wire format; used by gRPC and many in-house RPC systems."),
            ArchTool("graphql", "GraphQL",
                     "https://graphql.org/",
                     "arch:tool:graphql:home",
                     "Query language and runtime spec for client-driven APIs with flexible field selection."),
            ArchTool("grpc", "gRPC",
                     "https://grpc.io/",
                     "arch:tool:grpc:home",
                     "Google's binary RPC framework over HTTP/2; the default for service-to-service traffic."),
            ArchTool("trpc", "tRPC",
                     "https://trpc.io/",
                     "arch:tool:trpc:home",
                     "TypeScript-native end-to-end typed RPC; no codegen, no schema, just function calls."),
            ArchTool("apollo-server", "Apollo Server",
                     "https://www.apollographql.com/docs/apollo-server",
                     "arch:tool:apollo-server:home",
                     "The most-used GraphQL server in the JS/TS ecosystem; the path of least resistance."),
            ArchTool("hasura", "Hasura",
                     "https://hasura.io/",
                     "arch:tool:hasura:home",
                     "Instant GraphQL over Postgres (and other DBs); subscriptions and authorization built in."),
            ArchTool("postgrest", "PostgREST",
                     "https://postgrest.org/",
                     "arch:tool:postgrest:home",
                     "Instant REST over Postgres; tables and views become endpoints with row-level security."),
            ArchTool("connect", "Connect (Buf)",
                     "https://connectrpc.com/",
                     "arch:tool:connect:home",
                     "gRPC-compatible RPC that also works in browsers; the friendly modern face of Protobuf."),
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

    # APIs & protocols
    _r("Getting Started with the OpenAPI Specification",
       "https://learn.openapis.org/",
       "OpenAPI Initiative", "arch:res:openapi:learn", ["openapi"], "Tutorial",
       "Official guided entry point that walks new authors through writing their first OpenAPI description."),

    _r("Creating your first JSON Schema",
       "https://json-schema.org/learn/getting-started-step-by-step",
       "JSON Schema docs", "arch:res:json-schema:first", ["json-schema"], "Tutorial",
       "Step-by-step product-catalog example covering types, required fields, constraints, and $ref."),
    _r("Understanding JSON Schema",
       "https://json-schema.org/understanding-json-schema",
       "JSON Schema docs", "arch:res:json-schema:understanding", ["json-schema"], "Best Practices",
       "Reference companion that explains keywords and idiomatic patterns for real-world schema design."),

    _r("Protocol Buffers Tutorials",
       "https://protobuf.dev/getting-started/",
       "protobuf.dev", "arch:res:protobuf:tutorials", ["protobuf"], "Tutorial",
       "Official per-language quickstarts (Go, Python, Java, C++) covering .proto files and codegen."),
    _r("Proto Best Practices",
       "https://protobuf.dev/best-practices/dos-donts/",
       "protobuf.dev", "arch:res:protobuf:best-practices", ["protobuf"], "Best Practices",
       "Google's vetted rules on tag numbers, enum zero values, and safe schema evolution."),

    _r("Learn GraphQL",
       "https://graphql.org/learn/",
       "GraphQL Foundation", "arch:res:graphql:learn", ["graphql"], "Tutorial",
       "Official tour through schemas, queries, mutations, subscriptions, HTTP transport, and authorization."),
    _r("Production Ready GraphQL",
       "https://book.productionreadygraphql.com/",
       "Marc-Andre Giroux", "arch:res:graphql:production-ready", ["graphql"], "Best Practices",
       "Shopify/GitHub veteran's book on schema design, performance, security, and migrating legacy APIs."),

    _r("Introduction to gRPC",
       "https://grpc.io/docs/what-is-grpc/introduction/",
       "grpc.io", "arch:res:grpc:intro", ["grpc"], "Tutorial",
       "Conceptual primer plus links to per-language quickstarts that build a working client and server."),

    _r("tRPC Quickstart",
       "https://trpc.io/docs/quickstart",
       "tRPC docs", "arch:res:trpc:quickstart", ["trpc"], "Tutorial",
       "Framework-agnostic walkthrough of routers, queries, mutations, and zod input validation."),
    _r("GraphQL, tRPC, REST and more: Pick Your Poison",
       "https://www.youtube.com/watch?v=ZfccwYUD8H0",
       "YouTube · Theo", "arch:res:trpc:theo-pick-your-poison", ["trpc", "graphql", "grpc"], "Talk",
       "Decision-framework video on when tRPC beats GraphQL or REST for typed full-stack TypeScript."),

    _r("Get Started with Apollo Server",
       "https://www.apollographql.com/docs/apollo-server/getting-started",
       "Apollo GraphQL docs", "arch:res:apollo-server:getting-started", ["apollo-server"], "Tutorial",
       "Eight-step TypeScript tutorial building a Books schema, resolvers, and Apollo Sandbox queries."),

    _r("Hasura Basics",
       "https://hasura.io/learn/graphql/hasura/introduction/",
       "Hasura Learn", "arch:res:hasura:basics", ["hasura"], "Course",
       "30-minute course building a realtime todo backend with queries, subscriptions, and authorization."),
    _r("Hasura Quickstart with Docker",
       "https://hasura.io/docs/2.0/getting-started/docker-simple/",
       "Hasura docs", "arch:res:hasura:docker-quickstart", ["hasura"], "Tutorial",
       "Spin up Hasura Engine plus Postgres locally and get an auto-generated GraphQL API in minutes."),

    _r("PostgREST Tutorial 0: Get it Running",
       "https://docs.postgrest.org/en/latest/tutorials/tut0.html",
       "PostgREST docs", "arch:res:postgrest:tut0", ["postgrest"], "Tutorial",
       "Install PostgREST and build a todo API backed by a schema with role-based access."),
    _r("PostgREST Tutorial 1: The Golden Key",
       "https://docs.postgrest.org/en/latest/tutorials/tut1.html",
       "PostgREST docs", "arch:res:postgrest:tut1", ["postgrest"], "Tutorial",
       "Layer JWT authentication and per-role authorization on top of the Tutorial 0 API."),

    _r("Connect Introduction",
       "https://connectrpc.com/docs/introduction/",
       "Connect RPC docs", "arch:res:connect:intro", ["connect"], "Tutorial",
       "Overview of Connect's browser- and gRPC-compatible protocol with codegen across Go, TS, and more."),
    _r("Connect Getting Started in Go",
       "https://connectrpc.com/docs/go/getting-started/",
       "Connect RPC docs", "arch:res:connect:go-getting-started", ["connect"], "Tutorial",
       "Fifteen-minute walkthrough writing a protobuf schema and serving it with connect-go."),
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
        "Will Larson", "lethain", "blog", "https://lethain.com/",
        "arch:person:blog:lethain",
        "Engineering leader writing concrete frameworks for strategy, architecture, and staff-engineer work.",
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
        "When should I reach for GraphQL or gRPC instead of REST?",
        "GraphQL pays off at client-server edges where the client wants to "
        "fetch exactly the fields it needs in one round trip, and where the "
        "complexity of writing resolvers is a price you can afford. gRPC "
        "wins for internal service-to-service traffic: binary framing, "
        "HTTP/2 streaming, generated clients, tight schema discipline. "
        "REST is still the right default for everything else, especially "
        "public APIs where caching, browser support, and developer "
        "familiarity matter more than the protocol's expressive power.",
        source_label="Stack Overflow Blog: When to use gRPC vs GraphQL",
        source_url="https://stackoverflow.blog/2022/11/28/when-to-use-grpc-vs-graphql/",
        source_key="arch:faq:so-blog-grpc-vs-graphql",
    ),
    FAQ(
        "Do I need a separate search index, or can Postgres full-text handle it?",
        "Postgres full-text search is more capable than most teams realize. "
        "For typical workloads (tens of millions of rows, basic ranking, "
        "single-language stemming) it's competitive with Elasticsearch and "
        "Meilisearch and skips the operational cost of running a second "
        "system. The line is when you need fuzzy/typo-tolerant matching, "
        "rich faceting, multi-language ranking, or real-time updates over "
        "very large indexes. Until then, the right move is staying in "
        "Postgres.",
        source_label="Supabase Engineering: Postgres FTS vs the rest",
        source_url="https://supabase.com/blog/postgres-full-text-search-vs-the-rest",
        source_key="arch:faq:supabase-postgres-fts",
    ),
    FAQ(
        "When does pgvector stop being enough?",
        "pgvector inside the Postgres you already run wins until the "
        "retrieval workload starts to dominate normal load. Specialized "
        "vector databases buy you horizontal sharding past tens of "
        "millions of vectors, faster index rebuilds, and best-in-class "
        "filtered approximate-nearest-neighbor search. If your "
        "collection fits comfortably alongside your transactional data "
        "and your query patterns are simple kNN with a few filters, "
        "stay in Postgres.",
        source_label="Tiger Data Engineering: pgvector vs Qdrant",
        source_url="https://www.tigerdata.com/blog/pgvector-vs-qdrant",
        source_key="arch:faq:tigerdata-pgvector-vs-qdrant",
    ),
]

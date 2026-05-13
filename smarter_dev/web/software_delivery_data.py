"""Curated content for /resources/software-delivery.

The Shipping layer of the resources index. Pairs with /resources/system-architecture
(the What), /resources/infrastructure-hosting (the Where), and
/resources/production-operations (the Keep-it-healthy).
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

DELIV_CATEGORIES: list[ArchCategory] = [
    ArchCategory(
        slug="version-control",
        name="Version control",
        intro=(
            "Git is the protocol. The host is what matters in practice. "
            "GitHub for default, GitLab for self-host-friendly, Codeberg "
            "and Forgejo for the ideological alternatives. The choice "
            "mostly determines your CI/CD options and how much of your "
            "workflow lives in the same UI as your code."
        ),
        tools=(
            ArchTool("git", "Git",
                     "https://git-scm.com/",
                     "deliv:tool:git:home",
                     "The distributed version control protocol everything else is built on."),
            ArchTool("github", "GitHub",
                     "https://github.com/",
                     "deliv:tool:github:home",
                     "The dominant code-hosting platform. Default if you want the biggest CI/CD ecosystem."),
            ArchTool("gitlab", "GitLab",
                     "https://about.gitlab.com/",
                     "deliv:tool:gitlab:home",
                     "Self-host-friendly Git host with first-party CI/CD, packages, and registries."),
            ArchTool("codeberg", "Codeberg",
                     "https://codeberg.org/",
                     "deliv:tool:codeberg:home",
                     "Non-profit, community-run Forgejo instance for OSS projects."),
            ArchTool("forgejo", "Forgejo",
                     "https://forgejo.org/",
                     "deliv:tool:forgejo:home",
                     "Self-hostable Gitea fork governed by a non-profit. The community-first alternative."),
            ArchTool("gitea", "Gitea",
                     "https://about.gitea.com/",
                     "deliv:tool:gitea:home",
                     "Lightweight self-hostable Git host. The smallest, fastest option to run yourself."),
            ArchTool("bitbucket", "Bitbucket",
                     "https://bitbucket.org/product",
                     "deliv:tool:bitbucket:home",
                     "Atlassian's Git host. Strongest when your team is already on Jira and Confluence."),
        ),
    ),
    ArchCategory(
        slug="cicd",
        name="CI/CD platforms",
        intro=(
            "CI runs on every push; CD ships what passed. The line between "
            "them blurs in modern platforms. GitHub Actions wins by default "
            "because most teams are already on GitHub. CircleCI and "
            "Buildkite remain strong standalone offerings. Dagger and "
            "Earthly are the new wave that runs the same pipeline locally "
            "and remotely, important if you've ever debugged a YAML file "
            "by force-pushing."
        ),
        tools=(
            ArchTool("github-actions", "GitHub Actions",
                     "https://github.com/features/actions",
                     "deliv:tool:github-actions:home",
                     "Default CI/CD for GitHub repos. Largest marketplace; YAML-driven workflows."),
            ArchTool("gitlab-ci", "GitLab CI",
                     "https://about.gitlab.com/solutions/continuous-integration/",
                     "deliv:tool:gitlab-ci:home",
                     "GitLab's first-party pipelines. Tight integration with the GitLab feature set."),
            ArchTool("circleci", "CircleCI",
                     "https://circleci.com/",
                     "deliv:tool:circleci:home",
                     "Standalone CI/CD with strong caching, orbs, and Docker-native execution."),
            ArchTool("buildkite", "Buildkite",
                     "https://buildkite.com/",
                     "deliv:tool:buildkite:home",
                     "Hybrid model: hosted control plane, self-hosted agents. Strong at scale."),
            ArchTool("jenkins", "Jenkins",
                     "https://www.jenkins.io/",
                     "deliv:tool:jenkins:home",
                     "The classic OSS CI server. Massive plugin ecosystem; operational cost is real."),
            ArchTool("drone", "Drone (Harness)",
                     "https://www.drone.io/",
                     "deliv:tool:drone:home",
                     "Container-native CI from Harness. Lightweight YAML pipelines, easy self-host."),
            ArchTool("earthly", "Earthly",
                     "https://earthly.dev/",
                     "deliv:tool:earthly:home",
                     "Earthfile-based builds that run identically locally and in any CI."),
            ArchTool("dagger", "Dagger",
                     "https://dagger.io/",
                     "deliv:tool:dagger:home",
                     "Programmable CI/CD: write pipelines in real languages, run anywhere with a container runtime."),
            ArchTool("teamcity", "TeamCity",
                     "https://www.jetbrains.com/teamcity/",
                     "deliv:tool:teamcity:home",
                     "JetBrains' CI/CD server. Polished UI, strong build-chain modeling, free for small teams."),
        ),
    ),
    ArchCategory(
        slug="iac",
        name="Infrastructure as Code (IaC)",
        intro=(
            "IaC lets you describe infrastructure as text and commit it. "
            "Terraform is the lingua franca; OpenTofu is its OSS fork "
            "after the license change. Pulumi gives you real programming "
            "languages. CDK is AWS's TypeScript/Python wrapper around "
            "CloudFormation. Crossplane brings IaC patterns into "
            "Kubernetes itself. Pick by how much programmability you "
            "want, and how much surface area your team can review."
        ),
        tools=(
            ArchTool("terraform", "Terraform",
                     "https://www.terraform.io/",
                     "deliv:tool:terraform:home",
                     "HashiCorp's IaC tool. The default declarative language for cloud resources."),
            ArchTool("opentofu", "OpenTofu",
                     "https://opentofu.org/",
                     "deliv:tool:opentofu:home",
                     "OSS fork of Terraform after the BSL license change. Drop-in compatible."),
            ArchTool("pulumi", "Pulumi",
                     "https://www.pulumi.com/",
                     "deliv:tool:pulumi:home",
                     "IaC in real programming languages (TypeScript, Python, Go, C#) instead of HCL."),
            ArchTool("aws-cdk", "AWS CDK",
                     "https://aws.amazon.com/cdk/",
                     "deliv:tool:aws-cdk:home",
                     "AWS's TypeScript/Python wrapper around CloudFormation. AWS-only, deeply integrated."),
            ArchTool("crossplane", "Crossplane",
                     "https://www.crossplane.io/",
                     "deliv:tool:crossplane:home",
                     "Manage cloud resources from inside Kubernetes via CRDs. The K8s-native IaC pattern."),
            ArchTool("ansible", "Ansible",
                     "https://www.ansible.com/",
                     "deliv:tool:ansible:home",
                     "Agentless config management and orchestration via SSH. Still the default for VM provisioning."),
        ),
    ),
    ArchCategory(
        slug="deployment-gitops",
        name="Deployment & GitOps",
        intro=(
            "GitOps means Git is the source of truth for what runs in "
            "production. ArgoCD and Flux are the Kubernetes-native "
            "canonical implementations. Helm is the package format "
            "underneath. For non-K8s deployments, the toolchain is more "
            "fragmented: Spinnaker for the heavyweight Netflix legacy, "
            "Tekton for the pipelines-as-code framework. The orchestration "
            "runtime itself is in "
            "<a href=\"/resources/infrastructure-hosting#orchestration\">Infrastructure &amp; Hosting</a>; "
            "this section is about how code gets there."
        ),
        tools=(
            ArchTool("argocd", "ArgoCD",
                     "https://argo-cd.readthedocs.io/",
                     "deliv:tool:argocd:home",
                     "CNCF GitOps controller for Kubernetes. The canonical declarative deploy tool."),
            ArchTool("flux", "Flux",
                     "https://fluxcd.io/",
                     "deliv:tool:flux:home",
                     "CNCF GitOps toolkit for Kubernetes. Composable controllers; the other canonical option."),
            ArchTool("helm", "Helm",
                     "https://helm.sh/",
                     "deliv:tool:helm:home",
                     "The Kubernetes package manager. Templates, releases, and chart repositories."),
            ArchTool("spinnaker", "Spinnaker",
                     "https://spinnaker.io/",
                     "deliv:tool:spinnaker:home",
                     "Netflix-born multi-cloud continuous delivery platform. Heavy but battle-tested."),
            ArchTool("tekton", "Tekton",
                     "https://tekton.dev/",
                     "deliv:tool:tekton:home",
                     "CNCF pipelines-as-code framework. Composable building blocks for CI/CD on K8s."),
            ArchTool("skaffold", "Skaffold",
                     "https://skaffold.dev/",
                     "deliv:tool:skaffold:home",
                     "Google's CLI for K8s dev loops. Watch, build, push, deploy on file change."),
        ),
    ),
    ArchCategory(
        slug="container-builds",
        name="Container build tools",
        intro=(
            "Building a container image is no longer just docker build. "
            "BuildKit gives you parallelism and better caching. Buildpacks "
            "(Cloud Native, Heroku) and Nixpacks auto-detect your stack. "
            "Ko builds Go binaries straight into images, no Dockerfile "
            "required. The right pick depends on whether you want "
            "Dockerfiles, declarative configs, or zero config at all. "
            "The container runtime itself is in "
            "<a href=\"/resources/infrastructure-hosting#containers\">Infrastructure &amp; Hosting</a>."
        ),
        tools=(
            ArchTool("buildkit", "BuildKit",
                     "https://github.com/moby/buildkit",
                     "deliv:tool:buildkit:home",
                     "Modern image builder for Docker/OCI. Parallel stages, better caching, secret mounts."),
            ArchTool("buildpacks", "Cloud Native Buildpacks",
                     "https://buildpacks.io/",
                     "deliv:tool:buildpacks:home",
                     "CNCF spec for building OCI images from source. Auto-detects language and dependencies."),
            ArchTool("nixpacks", "Nixpacks",
                     "https://nixpacks.com/",
                     "deliv:tool:nixpacks:home",
                     "Railway's Nix-backed alternative to buildpacks. Auto-detect with reproducible builds."),
            ArchTool("ko", "ko",
                     "https://ko.build/",
                     "deliv:tool:ko:home",
                     "Build Go binaries directly into OCI images. No Dockerfile required."),
            ArchTool("jib", "Jib",
                     "https://github.com/GoogleContainerTools/jib",
                     "deliv:tool:jib:home",
                     "Google's Java-to-container builder. Maven and Gradle plugins; no Docker daemon."),
            ArchTool("bazel", "Bazel",
                     "https://bazel.build/",
                     "deliv:tool:bazel:home",
                     "Google's hermetic build system. The heavyweight choice when you want fully reproducible builds."),
        ),
    ),
    ArchCategory(
        slug="local-dev",
        name="Local development environments",
        intro=(
            "Local dev is half the developer experience. Dev Containers "
            "run a containerized dev environment in any editor that "
            "speaks the spec. mise and asdf manage runtime versions. "
            "direnv handles per-directory env vars. Nix flakes give you "
            "fully reproducible environments at the cost of a steep "
            "learning curve. Devbox wraps Nix in friendlier abstractions. "
            "Tilt is the option for Kubernetes-aware local dev. Docker "
            "Compose is in "
            "<a href=\"/resources/infrastructure-hosting#orchestration\">Infrastructure &amp; Hosting</a>; "
            "most teams use it locally too."
        ),
        tools=(
            ArchTool("dev-containers", "Dev Containers",
                     "https://containers.dev/",
                     "deliv:tool:dev-containers:home",
                     "Open spec for editor-agnostic containerized dev environments. The default in VS Code."),
            ArchTool("mise", "mise",
                     "https://mise.jdx.dev/",
                     "deliv:tool:mise:home",
                     "Polyglot runtime version manager (formerly rtx). asdf-compatible, faster, with task running."),
            ArchTool("asdf", "asdf",
                     "https://asdf-vm.com/",
                     "deliv:tool:asdf:home",
                     "Multi-language runtime version manager with a wide plugin ecosystem."),
            ArchTool("direnv", "direnv",
                     "https://direnv.net/",
                     "deliv:tool:direnv:home",
                     "Per-directory environment variables. Auto-loaded when you cd into a project."),
            ArchTool("nix", "Nix / Nix Flakes",
                     "https://nixos.org/",
                     "deliv:tool:nix:home",
                     "Functional package manager and reproducible-environment system. Steep but powerful."),
            ArchTool("devbox", "Devbox",
                     "https://www.jetify.com/devbox",
                     "deliv:tool:devbox:home",
                     "Jetify's friendlier Nix wrapper. Per-project shells with simple JSON config."),
            ArchTool("tilt", "Tilt",
                     "https://tilt.dev/",
                     "deliv:tool:tilt:home",
                     "Live-update workflow for Kubernetes dev. Smart rebuilds and a unified status UI."),
        ),
    ),
    ArchCategory(
        slug="db-migrations",
        name="Database migrations",
        intro=(
            "Schema changes are deploys with no rollback. Migration tools "
            "force you to encode every change as a forward-only script. "
            "Atlas is the modern declarative entrant. Goose and "
            "golang-migrate are the language-agnostic mainstays. Alembic, "
            "Flyway, and Liquibase are the long-running standards in "
            "Python and JVM ecosystems. The database itself is a "
            "<a href=\"/resources/system-architecture#databases\">System Architecture decision</a>; "
            "this section is about safely changing its shape over time."
        ),
        tools=(
            ArchTool("atlas", "Atlas",
                     "https://atlasgo.io/",
                     "deliv:tool:atlas:home",
                     "Declarative schema-as-code migrations. Plans diffs, validates with linting and CI checks."),
            ArchTool("goose", "Goose",
                     "https://github.com/pressly/goose",
                     "deliv:tool:goose:home",
                     "Lightweight Go migration tool. SQL or Go-based migrations; ergonomic CLI."),
            ArchTool("golang-migrate", "golang-migrate",
                     "https://github.com/golang-migrate/migrate",
                     "deliv:tool:golang-migrate:home",
                     "Language-agnostic CLI + Go library for SQL migrations across most databases."),
            ArchTool("alembic", "Alembic",
                     "https://alembic.sqlalchemy.org/",
                     "deliv:tool:alembic:home",
                     "SQLAlchemy's migration tool. The Python standard for relational schemas."),
            ArchTool("flyway", "Flyway",
                     "https://flywaydb.org/",
                     "deliv:tool:flyway:home",
                     "JVM-era migration mainstay. SQL-first, broad DB support, strong baseline workflow."),
            ArchTool("liquibase", "Liquibase",
                     "https://www.liquibase.com/",
                     "deliv:tool:liquibase:home",
                     "Enterprise schema-change platform. Tracks change sets with rich rollback support."),
            ArchTool("prisma-migrate", "Prisma Migrate",
                     "https://www.prisma.io/migrate",
                     "deliv:tool:prisma-migrate:home",
                     "Migrations for Prisma ORM. Generated from schema changes; tight TypeScript story."),
            ArchTool("sqlx-migrate", "sqlx migrate",
                     "https://github.com/launchbadge/sqlx",
                     "deliv:tool:sqlx-migrate:home",
                     "The migration CLI shipped with Rust's sqlx. SQL-first with compile-time query checks."),
        ),
    ),
    ArchCategory(
        slug="feature-flags",
        name="Feature flags & progressive delivery",
        intro=(
            "Feature flags decouple deploy from release. You can ship "
            "code today and turn it on for 1% of users tomorrow. "
            "LaunchDarkly is the heavyweight commercial player. Statsig "
            "and PostHog combine flags with analytics. Unleash, "
            "Flagsmith, and GrowthBook are the OSS contenders. "
            "OpenFeature is the emerging standard for vendor-neutral "
            "SDKs. The decision is mostly whether you need experimentation "
            "(A/B testing) or just safe rollouts."
        ),
        tools=(
            ArchTool("launchdarkly", "LaunchDarkly",
                     "https://launchdarkly.com/",
                     "deliv:tool:launchdarkly:home",
                     "The heavyweight commercial feature-flag platform. Deepest experimentation features."),
            ArchTool("statsig", "Statsig",
                     "https://www.statsig.com/",
                     "deliv:tool:statsig:home",
                     "Flags + experimentation + analytics in one stack. Generous free tier."),
            ArchTool("posthog", "PostHog",
                     "https://posthog.com/",
                     "deliv:tool:posthog:home",
                     "Open-source product analytics with first-class feature flags and experiments."),
            ArchTool("unleash", "Unleash",
                     "https://www.getunleash.io/",
                     "deliv:tool:unleash:home",
                     "Open-source feature-flag service. Self-hostable, with a hosted offering."),
            ArchTool("flagsmith", "Flagsmith",
                     "https://www.flagsmith.com/",
                     "deliv:tool:flagsmith:home",
                     "OSS feature-flag and remote-config platform. Strong on multi-environment workflows."),
            ArchTool("growthbook", "GrowthBook",
                     "https://www.growthbook.io/",
                     "deliv:tool:growthbook:home",
                     "Open-source A/B testing and feature flags built around your existing data warehouse."),
            ArchTool("configcat", "ConfigCat",
                     "https://configcat.com/",
                     "deliv:tool:configcat:home",
                     "Lightweight feature-flag service with a generous free tier and simple SDKs."),
            ArchTool("openfeature", "OpenFeature",
                     "https://openfeature.dev/",
                     "deliv:tool:openfeature:home",
                     "CNCF-incubating standard for vendor-neutral feature-flag SDKs. Not a service; a spec."),
        ),
    ),
]


# ─── SPINE ───────────────────────────────────────────────────────────────────

DELIV_SPINE_RESOURCES: list[ArchResource] = [
    _s("Continuous Delivery",
       "https://continuousdelivery.com/",
       "Jez Humble & David Farley", "deliv:spine:continuous-delivery", "Tutorial",
       "The canonical text on building reliable, rapid, low-risk software releases through automation and discipline."),
    _s("Accelerate",
       "https://itrevolution.com/product/accelerate/",
       "Forsgren, Humble, Kim · IT Revolution", "deliv:spine:accelerate", "Tutorial",
       "Data-driven follow-up to Continuous Delivery; the research foundation behind the DORA metrics."),
    _s("The DevOps Handbook",
       "https://itrevolution.com/product/the-devops-handbook-second-edition/",
       "Kim, Humble, Debois, Willis · IT Revolution", "deliv:spine:devops-handbook", "Tutorial",
       "Comprehensive practitioner's guide to applying DevOps principles across the value stream."),
    _s("The Phoenix Project",
       "https://itrevolution.com/product/the-phoenix-project/",
       "Kim, Behr, Spafford · IT Revolution", "deliv:spine:phoenix-project", "Discussion",
       "The narrative DevOps text. A novel that introduced a generation of engineers to flow, feedback, and learning."),
    _s("Trunk-Based Development",
       "https://trunkbaseddevelopment.com/",
       "Paul Hammant", "deliv:spine:trunk-based-dev", "Best Practices",
       "Canonical reference on trunk-based development: short-lived branches, fast review, continuous integration."),
    _s("OpenGitOps Principles",
       "https://opengitops.dev/",
       "OpenGitOps · CNCF", "deliv:spine:opengitops", "Best Practices",
       "The CNCF-backed spec defining what GitOps is and what it isn't. Four principles, vendor-neutral."),
    _s("Conventional Commits",
       "https://www.conventionalcommits.org/",
       "Conventional Commits", "deliv:spine:conventional-commits", "Best Practices",
       "Lightweight convention for commit messages that machines can parse and humans can scan."),
    _s("Semantic Versioning",
       "https://semver.org/",
       "Tom Preston-Werner", "deliv:spine:semver", "Best Practices",
       "MAJOR.MINOR.PATCH and the contract a version number actually conveys. The default for shared libraries."),
    _s("Continuous Integration",
       "https://martinfowler.com/articles/continuousIntegration.html",
       "Martin Fowler", "deliv:spine:fowler-ci", "Best Practices",
       "Fowler's canonical 2006 essay defining CI: integrate often, automate the build, keep main releasable."),
    _s("Deploys: It's Not Actually About Friday",
       "https://charity.wtf/2019/05/01/friday-deploy-freezes-are-exactly-like-murdering-puppies/",
       "Charity Majors", "deliv:spine:charity-friday-deploys", "Discussion",
       "The case that deploy fear is technical debt; small, frequent, author-owned changes beat blanket freezes."),
    _s("Technology Radar",
       "https://www.thoughtworks.com/radar",
       "Thoughtworks", "deliv:spine:tw-radar", "Best Practices",
       "Quarterly opinionated assessment of tools, techniques, platforms, and languages by Thoughtworks practitioners."),
    _s("State of DevOps Report (DORA)",
       "https://dora.dev/research/",
       "DORA · Google Cloud", "deliv:spine:dora", "Best Practices",
       "Annual research linking delivery practices to org performance. The empirical backbone of modern delivery."),
]


# ─── PER-TOOL RESOURCES ─────────────────────────────────────────────────────

DELIV_TOOL_RESOURCES: list[ArchToolResource] = [
    # Version control
    _r("Pro Git Book",
       "https://git-scm.com/book/en/v2",
       "git-scm.com", "deliv:res:git:pro-git", ["git"], "Tutorial",
       "The free official Git book. The definitive learn-Git-from-scratch and Git-reference resource."),
    _r("GitHub Docs",
       "https://docs.github.com/",
       "GitHub docs", "deliv:res:github:docs", ["github"], "Best Practices",
       "Documentation root for GitHub features: repos, Actions, packages, security, and Codespaces."),
    _r("GitLab Documentation",
       "https://docs.gitlab.com/",
       "GitLab docs", "deliv:res:gitlab:docs", ["gitlab"], "Best Practices",
       "Reference for GitLab repos, CI/CD, registries, security, and self-managed installs."),
    _r("Getting Started with Codeberg",
       "https://docs.codeberg.org/getting-started/",
       "Codeberg docs", "deliv:res:codeberg:getting-started", ["codeberg"], "Tutorial",
       "Walks new users through account setup, repos, and Codeberg's community-driven Forgejo-based platform."),
    _r("Your First Codeberg Repository",
       "https://docs.codeberg.org/getting-started/first-repository/",
       "Codeberg docs", "deliv:res:codeberg:first-repo", ["codeberg"], "Tutorial",
       "Create a repo, connect your local environment, and push your first commit."),
    _r("Forgejo User Guide",
       "https://forgejo.org/docs/latest/user/",
       "Forgejo docs", "deliv:res:forgejo:user-guide", ["forgejo"], "Best Practices",
       "Official user-facing docs for repositories, pull requests, issues, and collaboration on a Forgejo instance."),
    _r("Forgejo Actions Quick Start",
       "https://forgejo.org/docs/latest/user/actions/quick-start/",
       "Forgejo docs", "deliv:res:forgejo:actions-quickstart", ["forgejo"], "Tutorial",
       "Run your first CI workflow with Forgejo Actions."),
    _r("Gitea Documentation",
       "https://docs.gitea.com/",
       "Gitea docs", "deliv:res:gitea:docs", ["gitea"], "Best Practices",
       "Installation, usage, and administration of the self-hosted Git forge."),
    _r("Gitea Install with Docker",
       "https://docs.gitea.com/installation/install-with-docker",
       "Gitea docs", "deliv:res:gitea:docker-install", ["gitea"], "Tutorial",
       "Pull the Gitea image, configure volumes, and complete the install wizard."),
    _r("Get Started with Bitbucket Cloud",
       "https://support.atlassian.com/bitbucket-cloud/docs/get-started-with-bitbucket-cloud/",
       "Atlassian docs", "deliv:res:bitbucket:get-started", ["bitbucket"], "Tutorial",
       "Atlassian's onboarding for repos, push/pull, issues, and wikis in Bitbucket Cloud."),
    _r("Get Started with Bitbucket Pipelines",
       "https://support.atlassian.com/bitbucket-cloud/docs/get-started-with-bitbucket-pipelines/",
       "Atlassian docs", "deliv:res:bitbucket:pipelines", ["bitbucket"], "Tutorial",
       "Bitbucket's built-in CI/CD with a first pipeline configured via bitbucket-pipelines.yml."),

    # CI/CD platforms
    _r("GitHub Actions Quickstart",
       "https://docs.github.com/en/actions/quickstart",
       "GitHub docs", "deliv:res:github-actions:quickstart", ["github-actions"], "Tutorial",
       "Create your first workflow, trigger it on push, and explore the marketplace of pre-built actions."),
    _r("GitLab CI/CD Getting Started",
       "https://docs.gitlab.com/ee/ci/quick_start/",
       "GitLab docs", "deliv:res:gitlab-ci:quickstart", ["gitlab-ci"], "Tutorial",
       "Add a .gitlab-ci.yml, define your first job, and run it on shared or self-hosted runners."),
    _r("CircleCI Quickstart",
       "https://circleci.com/docs/guides/getting-started/getting-started/",
       "CircleCI docs", "deliv:res:circleci:quickstart", ["circleci"], "Tutorial",
       "Connect a repo, commit a config.yml, and watch your first CircleCI build run."),
    _r("Getting Started with Buildkite Pipelines",
       "https://buildkite.com/docs/pipelines/getting-started",
       "Buildkite docs", "deliv:res:buildkite:getting-started", ["buildkite"], "Tutorial",
       "Hands-on tutorial creating a basic Buildkite pipeline from an example."),
    _r("Jenkins Guided Tour",
       "https://www.jenkins.io/doc/pipeline/tour/getting-started/",
       "Jenkins docs", "deliv:res:jenkins:guided-tour", ["jenkins"], "Tutorial",
       "Official guided tour: install Jenkins and create your first Pipeline."),
    _r("Jenkins User Handbook",
       "https://www.jenkins.io/doc/book/",
       "Jenkins docs", "deliv:res:jenkins:handbook", ["jenkins"], "Best Practices",
       "Comprehensive reference for installing, configuring, and operating Jenkins in production."),
    _r("Drone Quick Start",
       "https://docs.drone.io/quickstart/",
       "Drone docs", "deliv:res:drone:quickstart", ["drone"], "Tutorial",
       "Stand up a Drone server and run your first .drone.yml pipeline."),
    _r("Dagger Documentation",
       "https://docs.dagger.io/",
       "Dagger docs", "deliv:res:dagger:docs", ["dagger"], "Tutorial",
       "Write pipelines in Go, Python, or TypeScript and run them anywhere with a container runtime."),
    _r("Earthly Documentation",
       "https://docs.earthly.dev/",
       "Earthly docs", "deliv:res:earthly:docs", ["earthly"], "Tutorial",
       "Earthfile syntax, caching, and the local-equals-remote build model."),
    _r("Getting Started with TeamCity",
       "https://www.jetbrains.com/help/teamcity/getting-started-with-teamcity.html",
       "JetBrains docs", "deliv:res:teamcity:getting-started", ["teamcity"], "Tutorial",
       "Covers CI fundamentals, TeamCity concepts, and installing the server on Windows, Linux, or macOS."),
    _r("Run Your First TeamCity Build",
       "https://www.jetbrains.com/help/teamcity/configure-and-run-your-first-build.html",
       "JetBrains docs", "deliv:res:teamcity:first-build", ["teamcity"], "Tutorial",
       "Point TeamCity at a repo URL and let auto-detection scaffold your first build configuration."),

    # IaC
    _r("Terraform Tutorials",
       "https://developer.hashicorp.com/terraform/tutorials",
       "HashiCorp Developer", "deliv:res:terraform:tutorials", ["terraform"], "Tutorial",
       "Hands-on tutorials covering AWS, Azure, GCP, Kubernetes, and Terraform Cloud workflows."),
    _r("OpenTofu Documentation",
       "https://opentofu.org/docs/",
       "OpenTofu docs", "deliv:res:opentofu:docs", ["opentofu"], "Tutorial",
       "Install OpenTofu and run your first plan/apply against any supported provider."),
    _r("Pulumi Get Started",
       "https://www.pulumi.com/docs/get-started/",
       "Pulumi docs", "deliv:res:pulumi:get-started", ["pulumi"], "Tutorial",
       "Pick a language and cloud, stand up your first stack, and deploy it via the Pulumi CLI."),
    _r("Getting Started with AWS CDK",
       "https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html",
       "AWS docs", "deliv:res:aws-cdk:getting-started", ["aws-cdk"], "Tutorial",
       "Install the CDK CLI, bootstrap an environment, and deploy your first stack."),
    _r("AWS CDK Hello World Tutorial",
       "https://docs.aws.amazon.com/cdk/v2/guide/hello-world.html",
       "AWS docs", "deliv:res:aws-cdk:hello-world", ["aws-cdk"], "Tutorial",
       "End-to-end walkthrough scaffolding, synthing, and deploying a real CDK application."),
    _r("Get Started with Crossplane",
       "https://docs.crossplane.io/latest/get-started/",
       "Crossplane docs", "deliv:res:crossplane:get-started", ["crossplane"], "Tutorial",
       "Install Crossplane, manage cloud resources, and build compositions from one entry point."),
    _r("Getting Started with Ansible",
       "https://docs.ansible.com/projects/ansible/latest/getting_started/index.html",
       "Ansible docs", "deliv:res:ansible:getting-started", ["ansible"], "Tutorial",
       "Install Ansible, build an inventory, and write your first 'Hello World' playbook."),

    # Deployment & GitOps
    _r("ArgoCD Getting Started",
       "https://argo-cd.readthedocs.io/en/stable/getting_started/",
       "ArgoCD docs", "deliv:res:argocd:getting-started", ["argocd"], "Tutorial",
       "Install ArgoCD, register a Git repo as a source of truth, and deploy your first application."),
    _r("Flux Get Started",
       "https://fluxcd.io/flux/get-started/",
       "Flux docs", "deliv:res:flux:get-started", ["flux"], "Tutorial",
       "Bootstrap Flux into a cluster, point it at a Git repo, and reconcile your first deployment."),
    _r("Helm Quickstart",
       "https://helm.sh/docs/intro/quickstart/",
       "Helm docs", "deliv:res:helm:quickstart", ["helm"], "Tutorial",
       "Install Helm, search the chart repository, and release your first chart into a cluster."),
    _r("Get Started Using Spinnaker",
       "https://spinnaker.io/docs/guides/user/get-started/",
       "Spinnaker docs", "deliv:res:spinnaker:get-started", ["spinnaker"], "Tutorial",
       "Orientation for new Spinnaker users covering applications, pipelines, and deployment strategies."),
    _r("Spinnaker Hello Deployment Codelab",
       "https://spinnaker.io/docs/guides/tutorials/codelabs/hello-deployment/",
       "Spinnaker docs", "deliv:res:spinnaker:hello-deployment", ["spinnaker"], "Tutorial",
       "Hands-on codelab introducing Spinnaker by deploying a simple service end to end."),
    _r("Tekton: Getting Started with Pipelines",
       "https://tekton.dev/docs/getting-started/pipelines/",
       "Tekton docs", "deliv:res:tekton:pipelines", ["tekton"], "Tutorial",
       "Build two Tasks, chain them into a Pipeline, and run them with a PipelineRun on minikube."),
    _r("Tekton: Getting Started with Tasks",
       "https://tekton.dev/docs/getting-started/tasks/",
       "Tekton docs", "deliv:res:tekton:tasks", ["tekton"], "Tutorial",
       "Author and run your first standalone Tekton Task before moving up to full Pipelines."),
    _r("Skaffold Quickstart",
       "https://skaffold.dev/docs/quickstart/",
       "Skaffold docs", "deliv:res:skaffold:quickstart", ["skaffold"], "Tutorial",
       "Run skaffold dev against a sample Node app on minikube to see the build/deploy inner loop."),
    _r("Skaffold: Getting Started With Your Project",
       "https://skaffold.dev/docs/workflows/getting-started-with-your-project/",
       "Skaffold docs", "deliv:res:skaffold:your-project", ["skaffold"], "Tutorial",
       "Use skaffold init to scaffold a skaffold.yaml for an existing application."),

    # Container build tools
    _r("BuildKit on GitHub",
       "https://github.com/moby/buildkit",
       "GitHub", "deliv:res:buildkit:github", ["buildkit"], "Tutorial",
       "Project README covering frontends, caching, and modern Dockerfile features."),
    _r("Cloud Native Buildpacks: Get Started",
       "https://buildpacks.io/docs/for-app-developers/",
       "Buildpacks docs", "deliv:res:buildpacks:get-started", ["buildpacks"], "Tutorial",
       "Build OCI images from source with the pack CLI; no Dockerfile required."),
    _r("Nixpacks Documentation",
       "https://nixpacks.com/docs",
       "Nixpacks docs", "deliv:res:nixpacks:docs", ["nixpacks"], "Best Practices",
       "Official source-to-OCI-image builder docs. Note: the project is in maintenance mode; Railway directs new projects to Railpack."),
    _r("Get Started with ko",
       "https://ko.build/get-started/",
       "ko docs", "deliv:res:ko:get-started", ["ko"], "Tutorial",
       "Install ko, authenticate to a registry, and build a Go app into a container without a Dockerfile."),
    _r("Jib Maven Plugin",
       "https://github.com/GoogleContainerTools/jib/blob/master/jib-maven-plugin/README.md",
       "GitHub", "deliv:res:jib:maven", ["jib"], "Tutorial",
       "Quickstart, configuration, and goals for containerizing Java apps without Docker."),
    _r("Jib Gradle Plugin",
       "https://github.com/GoogleContainerTools/jib/blob/master/jib-gradle-plugin/README.md",
       "GitHub", "deliv:res:jib:gradle", ["jib"], "Tutorial",
       "Gradle plugin for building OCI images from Java projects directly from your build."),
    _r("Bazel: Getting Started",
       "https://bazel.build/start",
       "Bazel docs", "deliv:res:bazel:start", ["bazel"], "Tutorial",
       "Install Bazel and follow language-specific tutorials (C++, Java, Go, etc.) to set up your first workspace."),
    _r("Intro to Bazel",
       "https://bazel.build/about/intro",
       "Bazel docs", "deliv:res:bazel:intro", ["bazel"], "Best Practices",
       "Conceptual overview of how Bazel approaches builds, dependencies, and reproducibility."),

    # Local development
    _r("Dev Containers Specification",
       "https://containers.dev/implementors/spec/",
       "containers.dev", "deliv:res:dev-containers:spec", ["dev-containers"], "Best Practices",
       "The devcontainer.json spec that VS Code, JetBrains, GitHub Codespaces, and others implement."),
    _r("mise Documentation",
       "https://mise.jdx.dev/getting-started.html",
       "mise docs", "deliv:res:mise:getting-started", ["mise"], "Tutorial",
       "Install mise and start managing runtime versions, env vars, and tasks per project."),
    _r("asdf Getting Started",
       "https://asdf-vm.com/guide/getting-started.html",
       "asdf docs", "deliv:res:asdf:getting-started", ["asdf"], "Tutorial",
       "Install asdf, configure your shell, and add plugins for the language runtimes you need."),
    _r("direnv",
       "https://direnv.net/",
       "direnv docs", "deliv:res:direnv:home", ["direnv"], "Tutorial",
       "Project landing page with the canonical .envrc walkthrough and shell hook setup."),
    _r("direnv Installation",
       "https://direnv.net/docs/installation.html",
       "direnv docs", "deliv:res:direnv:install", ["direnv"], "Tutorial",
       "Install direnv and wire it into bash, zsh, fish, or your shell of choice."),
    _r("Learn Nix",
       "https://nixos.org/learn/",
       "NixOS.org", "deliv:res:nix:learn", ["nix"], "Best Practices",
       "Official learning hub pointing to nix.dev tutorials and the Nix reference manual."),
    _r("Devbox Quickstart",
       "https://www.jetify.com/docs/devbox/quickstart",
       "Jetify docs", "deliv:res:devbox:quickstart", ["devbox"], "Tutorial",
       "Initialize a devbox.json, add Nix-powered packages, and enter an isolated dev shell in minutes."),
    _r("Tilt Tutorial",
       "https://docs.tilt.dev/tutorial.html",
       "Tilt docs", "deliv:res:tilt:tutorial", ["tilt"], "Tutorial",
       "Build a Kubernetes dev loop with live updates, smart rebuilds, and a status UI."),

    # Database migrations
    _r("Atlas Documentation",
       "https://atlasgo.io/docs",
       "Atlas docs", "deliv:res:atlas:docs", ["atlas"], "Tutorial",
       "Declarative schema migrations: plan diffs, validate with linting, and ship via CI."),
    _r("pressly/goose",
       "https://github.com/pressly/goose",
       "GitHub", "deliv:res:goose:github", ["goose"], "Tutorial",
       "Canonical README for the Go migration tool: CLI usage, SQL and Go migration formats, embed examples."),
    _r("Goose Documentation",
       "https://pressly.github.io/goose/",
       "Goose docs", "deliv:res:goose:docs", ["goose"], "Best Practices",
       "Drivers, library usage, and recommended migration workflows."),
    _r("Getting Started with golang-migrate",
       "https://github.com/golang-migrate/migrate/blob/master/GETTING_STARTED.md",
       "GitHub", "deliv:res:golang-migrate:getting-started", ["golang-migrate"], "Tutorial",
       "Create up/down migration files, run them with the CLI, and embed migrations into a Go program."),
    _r("golang-migrate Postgres Tutorial",
       "https://github.com/golang-migrate/migrate/blob/master/database/postgres/TUTORIAL.md",
       "GitHub", "deliv:res:golang-migrate:postgres-tutorial", ["golang-migrate"], "Tutorial",
       "Database-specific walkthrough for running golang-migrate against Postgres."),
    _r("Alembic Tutorial",
       "https://alembic.sqlalchemy.org/en/latest/tutorial.html",
       "Alembic docs", "deliv:res:alembic:tutorial", ["alembic"], "Tutorial",
       "Set up Alembic with SQLAlchemy, generate your first revision, and apply it to a database."),
    _r("Flyway Documentation",
       "https://documentation.red-gate.com/fd",
       "Redgate / Flyway", "deliv:res:flyway:docs", ["flyway"], "Tutorial",
       "Install Flyway and version-control your database schema with SQL-first migrations."),
    _r("Get Started with Liquibase",
       "https://docs.liquibase.com/start/home.html",
       "Liquibase docs", "deliv:res:liquibase:start", ["liquibase"], "Tutorial",
       "Install Liquibase and run your first changelog using the bundled H2 sandbox database."),
    _r("Liquibase Documentation",
       "https://docs.liquibase.com/",
       "Liquibase docs", "deliv:res:liquibase:docs", ["liquibase"], "Best Practices",
       "Authoritative reference covering changelogs, contexts, rollbacks, and CI/CD integration patterns."),
    _r("Getting Started with Prisma Migrate",
       "https://www.prisma.io/docs/orm/prisma-migrate/getting-started",
       "Prisma docs", "deliv:res:prisma-migrate:getting-started", ["prisma-migrate"], "Tutorial",
       "Set up Prisma Migrate, create a baseline migration, and generate SQL from your Prisma schema."),
    _r("Prisma Migrate with TypeScript and PostgreSQL",
       "https://www.prisma.io/docs/getting-started/setup-prisma/start-from-scratch/relational-databases/using-prisma-migrate-typescript-postgresql",
       "Prisma docs", "deliv:res:prisma-migrate:typescript-postgres", ["prisma-migrate"], "Tutorial",
       "From-scratch tutorial wiring Prisma Migrate to a TypeScript + Postgres project."),
    _r("sqlx-cli",
       "https://crates.io/crates/sqlx-cli",
       "crates.io", "deliv:res:sqlx-migrate:cli", ["sqlx-migrate"], "Tutorial",
       "Install sqlx-cli and use sqlx migrate add/run to manage SQLx migrations in Rust projects."),
    _r("sqlx::migrate Migrator API",
       "https://docs.rs/sqlx/latest/sqlx/migrate/struct.Migrator.html",
       "docs.rs", "deliv:res:sqlx-migrate:api", ["sqlx-migrate"], "Best Practices",
       "API reference for embedding migrations into a Rust binary via the sqlx::migrate! macro."),

    # Feature flags
    _r("LaunchDarkly Get Started",
       "https://docs.launchdarkly.com/home/getting-started",
       "LaunchDarkly docs", "deliv:res:launchdarkly:get-started", ["launchdarkly"], "Tutorial",
       "Create a project, add an SDK, and ship your first flag-gated release."),
    _r("Get Started with the Statsig SDK",
       "https://docs.statsig.com/sdks/quickstart",
       "Statsig docs", "deliv:res:statsig:quickstart", ["statsig"], "Tutorial",
       "Install a Statsig SDK in your language of choice and start checking feature gates in a few lines."),
    _r("Statsig Feature Flags Overview",
       "https://docs.statsig.com/feature-flags/overview",
       "Statsig docs", "deliv:res:statsig:overview", ["statsig"], "Best Practices",
       "Concept guide explaining feature gates, targeting, rollouts, and overrides in Statsig."),
    _r("PostHog Feature Flags",
       "https://posthog.com/docs/feature-flags",
       "PostHog docs", "deliv:res:posthog:feature-flags", ["posthog"], "Tutorial",
       "Use PostHog's flags plus analytics plus experimentation in one stack."),
    _r("Unleash Documentation",
       "https://docs.getunleash.io/",
       "Unleash docs", "deliv:res:unleash:docs", ["unleash"], "Tutorial",
       "\"Get up and running with Unleash in less than 5 minutes\" with links to SDKs and tutorials."),
    _r("Unleash: Create and Configure a Feature Flag",
       "https://docs.getunleash.io/guides/how-to-create-feature-flags",
       "Unleash docs", "deliv:res:unleash:create-flag", ["unleash"], "Tutorial",
       "Create a flag, define strategies, and refine with constraints, segments, and variants."),
    _r("Flagsmith Quick Start",
       "https://docs.flagsmith.com/getting-started/quick-start",
       "Flagsmith docs", "deliv:res:flagsmith:quickstart", ["flagsmith"], "Tutorial",
       "Four-step tutorial: create a project and flag, import the JS SDK, fetch flags, and toggle behavior."),
    _r("GrowthBook Quick Start",
       "https://docs.growthbook.io/quick-start",
       "GrowthBook docs", "deliv:res:growthbook:quickstart", ["growthbook"], "Tutorial",
       "End-to-end walkthrough for feature flagging and experimentation with GrowthBook Cloud or self-host."),
    _r("GrowthBook SDK Quickstart",
       "https://docs.growthbook.io/lib/quickstart",
       "GrowthBook docs", "deliv:res:growthbook:sdk-quickstart", ["growthbook"], "Tutorial",
       "Wire a GrowthBook SDK into your app to start evaluating feature flags."),
    _r("ConfigCat Getting Started",
       "https://configcat.com/docs/getting-started/",
       "ConfigCat docs", "deliv:res:configcat:getting-started", ["configcat"], "Tutorial",
       "Add a flag in the ConfigCat dashboard and evaluate it from any of 20+ supported SDKs."),
    _r("OpenFeature Specification",
       "https://openfeature.dev/specification/",
       "OpenFeature docs", "deliv:res:openfeature:spec", ["openfeature"], "Best Practices",
       "The CNCF-incubating spec for vendor-neutral feature-flag SDKs."),
]


# ─── CREATORS ────────────────────────────────────────────────────────────────

DELIV_PEOPLE: list[Person] = [
    Person(
        "Jez Humble", "jezhumble", "x", "https://x.com/jezhumble",
        "deliv:person:x:jez-humble",
        "Continuous Delivery co-author, DORA co-founder, and the canonical voice on modern release practices.",
    ),
    Person(
        "Dave Farley", "davefarley77", "youtube", "https://www.youtube.com/@ContinuousDelivery",
        "deliv:person:youtube:dave-farley",
        "Continuous Delivery co-author. Runs the Continuous Delivery YouTube channel.",
    ),
    Person(
        "Gene Kim", "RealGeneKim", "x", "https://x.com/RealGeneKim",
        "deliv:person:x:gene-kim",
        "Phoenix Project and DevOps Handbook author; founder of IT Revolution and DevOps Enterprise Summit.",
    ),
    Person(
        "Nicole Forsgren", "nicolefv", "x", "https://x.com/nicolefv",
        "deliv:person:x:nicole-forsgren",
        "Accelerate co-author and the researcher behind the DORA metrics.",
    ),
    Person(
        "Mitchell Hashimoto", "mitchellh", "x", "https://x.com/mitchellh",
        "deliv:person:x:mitchell-hashimoto",
        "HashiCorp founder. Origin of Terraform, Vault, and Vagrant; deep writing on tooling and dev experience.",
    ),
    Person(
        "Adrian Cockcroft", "adrianco", "x", "https://x.com/adrianco",
        "deliv:person:x:adrian-cockcroft",
        "Ex-Netflix cloud architect; the canonical voice on microservices delivery patterns.",
    ),
    Person(
        "Daniel Bryant", "danielbryantuk", "x", "https://x.com/danielbryantuk",
        "deliv:person:x:daniel-bryant",
        "InfoQ news editor and prolific DevOps practitioner; runs deep technical interviews and analysis.",
    ),
    Person(
        "Bret Fisher", "BretFisher", "youtube", "https://www.youtube.com/@BretFisher",
        "deliv:person:youtube:bret-fisher",
        "Docker Captain with deep Docker, Kubernetes, and CI/CD content. Weekly live YouTube show.",
    ),
    Person(
        "Lars Wikman", "lawik", "blog", "https://underjord.io/",
        "deliv:person:blog:lars-wikman",
        "Elixir/Erlang deployment writing at underjord.io. Practical, opinionated, BEAM-focused.",
    ),
    Person(
        "Birgitta Böckeler", "BBoeckeler", "blog", "https://martinfowler.com/articles/exploring-gen-ai.html",
        "deliv:person:blog:birgitta-boeckeler",
        "Thoughtworks; lead contributor on Martin Fowler's \"Exploring Generative AI\" series on AI-assisted delivery.",
    ),
]


# ─── FAQs ────────────────────────────────────────────────────────────────────

DELIV_FAQS: list[FAQ] = [
    FAQ(
        "What's the minimum CI/CD setup for a small team?",
        "On-push: lint, test, build. On-merge-to-main: deploy to staging "
        "automatically. On-tag (or manual approval): deploy to production. "
        "Three pipelines, three triggers; that's the whole shape. Almost "
        "any platform (GitHub Actions, GitLab CI, CircleCI) can do this in "
        "well under a hundred lines of YAML. Resist the urge to add "
        "review apps, canaries, or environments-per-branch until the "
        "basic flow is reliable and fast for everyone on the team.",
        source_label="Martin Fowler: Continuous Integration",
        source_url="https://martinfowler.com/articles/continuousIntegration.html",
        source_key="deliv:faq:min-cicd",
    ),
    FAQ(
        "When should I add IaC to my project?",
        "Roughly when reproducing your infrastructure stops being a "
        "one-day task. If a new environment takes you a week of clicking "
        "in cloud consoles, IaC has already paid for itself. The other "
        "trigger is auditability: when you need to know who changed what "
        "in prod, plain SCM history beats a CloudTrail digest. Start "
        "with the resources that change most often (DNS, load balancers, "
        "IAM) and grow outward. You don't need to import everything on "
        "day one.",
        source_label="HashiCorp: What is Infrastructure as Code?",
        source_url="https://developer.hashicorp.com/terraform/intro",
        source_key="deliv:faq:when-iac",
    ),
    FAQ(
        "Should I use Terraform or Pulumi?",
        "If your team is fluent in TypeScript, Python, or Go and you "
        "want loops, conditionals, and real testing, Pulumi is the "
        "natural fit. If you want the largest ecosystem of modules, the "
        "most provider coverage, and the easiest hiring story, Terraform "
        "(or OpenTofu) is still the default. The license change pushed "
        "many OSS teams to OpenTofu; check whether your CI provider's "
        "Terraform Cloud integration matters before picking.",
        source_label="Thoughtworks Radar: OpenTofu (Adopt)",
        source_url="https://www.thoughtworks.com/radar/tools/opentofu",
        source_key="deliv:faq:terraform-vs-pulumi",
    ),
    FAQ(
        "Do I really need GitOps, or is kubectl apply enough?",
        "kubectl apply scales to small teams and short feedback loops. "
        "GitOps starts paying off when you have more than one cluster, "
        "more than a handful of services per cluster, or audit "
        "requirements that demand a clear answer to \"what's running and "
        "who put it there?\". The four OpenGitOps principles (declarative, "
        "versioned, pulled, continuously reconciled) describe what you "
        "gain. If you don't need the gains yet, the operational cost is "
        "real.",
        source_label="OpenGitOps Principles",
        source_url="https://opengitops.dev/",
        source_key="deliv:faq:need-gitops",
    ),
    FAQ(
        "How do I handle database migrations in CI/CD safely?",
        "Three rules: migrations are forward-only; every migration is "
        "backward-compatible with the previous app version; and migrations "
        "run before the app version that depends on them is deployed. "
        "That means expand/contract: add the new column or table first, "
        "ship the app that reads both old and new, backfill, then drop "
        "the old shape in a later release. Atlas, Alembic, Flyway, and "
        "Liquibase all support this pattern; the discipline comes from "
        "your team, not the tool.",
        source_label="Martin Fowler: Evolutionary Database Design",
        source_url="https://martinfowler.com/articles/evodb.html",
        source_key="deliv:faq:migrations-cicd",
    ),
    FAQ(
        "When does feature flagging actually pay off?",
        "Roughly when you start wanting to deploy more often than you "
        "want to release. Flags decouple the two: ship the code today, "
        "turn it on for 1% of users next week, ramp to 100% when the "
        "metrics hold. If your release cadence is monthly, you probably "
        "don't need a flag service yet. If it's daily and you have real "
        "users, the small operational cost pays for itself the first time "
        "you can roll back a bad release without rolling back a deploy.",
        source_label="LaunchDarkly: What are Feature Flags?",
        source_url="https://launchdarkly.com/blog/what-are-feature-flags/",
        source_key="deliv:faq:feature-flags-pay-off",
    ),
    FAQ(
        "How do I think about deploy frequency without breaking things?",
        "The DORA research is unambiguous: high-performing teams deploy "
        "more often AND have lower change-failure rates. The way they "
        "get there is small batch sizes, fast feedback, and automation "
        "of the boring parts. The path is not to deploy more by relaxing "
        "review; it's to make each deploy smaller and the rollback "
        "cheaper. Charity Majors's framing is the right one: deploys "
        "shouldn't be scary because each one carries less risk, not "
        "because you've removed risk by waiting.",
        source_label="DORA Research",
        source_url="https://dora.dev/research/",
        source_key="deliv:faq:deploy-frequency",
    ),
]

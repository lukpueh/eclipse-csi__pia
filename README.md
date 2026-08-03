# Project Identity Authority (PIA)
Authenticates Eclipse Foundation projects using OpenID Connect (OIDC) for the
purpose of uploading SBOMs to the Eclipse Foundation DependencyTrack instance.

> [!IMPORTANT]
> **Authorization is scoped to the Eclipse Foundation project — not to an
> individual workload or DependencyTrack project.** Workloads (GitHub repos or
> Jenkins instances) and DependencyTrack projects are both registered *under* an EF
> project. Any workload registered for an EF project may publish an SBOM to **any**
> DependencyTrack project registered for that **same** EF project; there is no
> per-workload → per-DependencyTrack-project binding. Which DependencyTrack project
> a given upload lands in is decided at upload time by the **`product_name`** field
> in the request payload: PIA resolves it to the DependencyTrack project. In
> other words, the workload authenticates and establishes the EF-project scope, and
> `product_name` selects the target within that scope.

See [Design Document](docs/DESIGN.md) for details.

## DependencyTrack Project Hierarchy

Projects on the Eclipse Foundation DependencyTrack instance form a three-level
hierarchy:

```
Eclipse Foo                 1. root — one per Eclipse Foundation project
└── foo-server              2. product — one per SBOM upload target
    └── foo-server 1.2.0    3. version — one per uploaded SBOM version
```

Levels 1 and 2 can be declared in a curated file and provisioned on
DependencyTrack using the PIA CLI, which also registers level 2 with PIA (see
[Managing Authorizations](#managing-authorizations) below). Level 3 is created
by DependencyTrack itself on each upload, using `product_name` and
`product_version` from the upload payload.

The PIA authentication runtime knows only part of this: per registered
DependencyTrack target it stores a **UUID** and a **name**. On upload, the
`product_name` in the payload must match a stored name, in order to resolve a
stored UUID as the upload target. This is also what makes a level-3 name match
its parent level-2 name: the matched name *is* the level-2 name, and it is
forwarded to DependencyTrack as the level-3 name.

The level-1 root name is not stored — it is only used by the CLI, to provision
the project hierarchy and to look up the level-2 UUID under the right root.

### Renaming Projects

A level-2 name lives in four places that must all agree: on DependencyTrack, as
a `products` entry in the curated file, as the name registered with PIA, and as
`product_name` in the upload payload.

Renaming it on DependencyTrack alone does not disrupt uploads, because the
upload target is routed via UUID. The registered name simply stays behind, so
uploads keep resolving under the old `product_name` and DependencyTrack keeps
creating level-3 projects under it — no longer matching their renamed parent.

It does, however, make the curated file stale, and catching up costs a
coordinated change: `pia sync` no longer resolves the level-2 project by its old
name, and updating the `products` entry to match re-registers the target under
the new name, so the publisher must switch `product_name` at the same time or
its uploads are rejected. Leaving the file stale is not a fix either — `pia
create-dt-projects` re-creates the old name as a *new*, empty level-2 project
alongside the renamed one, and the next `pia sync` points uploads at it.

A level-1 rename is cheap by comparison: the root name is not registered, so
only the curated file needs updating and no payload changes.

## Contributing

### Development Setup

PIA uses [uv](https://docs.astral.sh/uv/) for Python project management.

1. **Clone and change into repository:**
   ```bash
   git clone https://github.com/eclipse-csi/pia.git && cd pia
   ```

2. **Install dependencies:**
   ```bash
   uv sync --all-extras
   ```

### Running Tests

Run the full test suite with pytest:

```bash
uv run pytest                             # all tests
uv run pytest -v                          # verbose output
uv run pytest tests/test_main.py          # specific test
uv run pytest --cov=pia                   # with coverage
```

### Code Quality

Lint and check format

```bash
uv run ruff check && uv run ruff format --check
```

Auto-fix linting issues and auto-format

```bash
uv run ruff check --fix && uv run ruff format
```

### Database Migration

PIA uses [`alembic`](https://alembic.sqlalchemy.org/en/latest/) for database
migrations. Migration scripts live in `alembic/versions/`.

Migrations are applied as a dedicated step before the app rolls out, by running
`alembic upgrade head` with the application image — in production via a Helm
`pre-install`/`pre-upgrade` hook job (see the [helm
chart](https://github.com/eclipse-csi/helm-charts/tree/main/charts/pia)). The
app itself does not run migrations on startup, so its runtime database user only
needs read access. In local development, the `docker-compose` setup applies
migrations automatically before starting the app for convenience.

#### Creating Migration Scripts

To auto-generate a migration script, when adding, removing or changing PIA ORM
models (see `pia/models.py`), run below command and add the resulting script to
version control.
```shell
docker compose run --rm pia alembic revision --autogenerate --message "MESSAGE"
```

### Managing Authorizations

Project authorizations (workloads and DependencyTrack targets) live in the
database and are managed declaratively: reconcile the whole set from a curated
file with `pia sync`.

Use `pia create-dt-projects` to ensure existence of DependencyTrack targets
prior to syncing.

See [CLI section of the design doc](docs/DESIGN.md#55-cli-tool) or run with
`--help` for more info.

#### Local Testing

The `docker compose` stack includes a local DependencyTrack API server, so you
can exercise `pia sync` end to end.

1. **Start the stack.** This brings up Postgres, DependencyTrack, and the app
   (which applies migrations on startup):
   ```shell
   docker compose up -d
   ```

2. **Provision a DependencyTrack token.** DependencyTrack takes 1-2 minutes to
   become ready on first start; this command waits for it and creates an API
   token:
   ```shell
   uv run python scripts/dt_bootstrap.py
   ```
   The DependencyTrack UI/API is at http://localhost:8080 (admin login is
   printed by the command).

3. **Run a sync** against the local database and DependencyTrack using the
   command printed by `scripts/dt_bootstrap.py`.

   Drop `--dry-run` to apply, then re-run to see an empty (idempotent) plan.
   Edit `projects.local.yaml` and re-run to see updates and deletions in the plan.

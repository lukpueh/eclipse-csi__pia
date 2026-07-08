# Project Identity Authority (PIA)
Authenticates Eclipse Foundation projects using OpenID Connect (OIDC).

See [Design Document](docs/DESIGN.md) for details.

## Contributing

### Development Setup

PIA uses [uv](https://docs.astral.sh/uv/) for Python project management.

1. **Clone and changew into repository:**
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
database. Register them one at a time with `pia add-workload` / `pia add-dt-project`,
or reconcile the whole set declaratively from a curated file with `pia sync`
(see the [CLI section of the design doc](docs/DESIGN.md#55-cli-tool)):

```shell
# Validate a curated file's shape (no database or network access)
uv run pia sync projects.yaml --check

# Preview the reconcile plan against the database
PIA_DATABASE_URL=... PIA_DEPENDENCY_TRACK_API_KEY=... \
  uv run pia sync projects.yaml --dt-url https://sbom.eclipse.org --dry-run
```

Pass `--create-dt-projects` to have sync create missing DependencyTrack
parent/child projects instead of failing when they don't exist (requires a DT API
key with project-creation permission).

In production `pia sync` runs as an in-cluster Job; see the deployment repo.

#### Trying the sync CLI locally

The `docker compose` stack includes a local DependencyTrack API server, so you
can exercise `pia sync` end to end.

1. **Start the stack.** This brings up Postgres, DependencyTrack, and the app
   (which applies migrations on startup):
   ```shell
   docker compose up -d
   ```

2. **Provision a DependencyTrack token.** DependencyTrack takes 1-2 minutes to
   become ready on first start; this command waits for it, then creates an API
   token (and a demo project) and saves it to `.dt-api-key` and `.env`:
   ```shell
   make dt-token
   ```
   The DependencyTrack UI/API is at http://localhost:8080 (admin login is
   printed by the command).

3. **Run a sync** against the local database and DependencyTrack. The provided
   [`projects.local.yaml`](projects.local.yaml) matches the demo project:
   ```shell
   export PIA_DATABASE_URL=postgresql://pia:pia@localhost:5432/pia
   export PIA_DEPENDENCY_TRACK_API_KEY=$(cat .dt-api-key)
   uv run pia sync projects.local.yaml --dt-url http://localhost:8080 --dry-run
   ```
   Drop `--dry-run` to apply, then re-run to see an empty (idempotent) plan.
   Edit `projects.local.yaml` and re-run to see updates and deletions in the plan.

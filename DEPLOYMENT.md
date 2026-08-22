# Deployment

Production runs on the VDS at `91.98.233.170`, alongside four other
applications. Nothing here is specific to ShiftLush except the port and the
name — the shape is the one `/opt/vds-stack` already imposes, and departing
from it would mean one server with two conventions.

| | |
|---|---|
| API | `https://shiftlush-api.selamet.dev` → `127.0.0.1:8105` |
| Frontend | `https://shiftlush.selamet.dev` (Vercel) |
| Database | `database.selamet.dev`, isolated database `shiftlush` on the laboflush stack |

## How a deploy happens

1. A pull request merges to `main`.
2. CI runs. **Deploy waits for it** — the workflow triggers on CI's completion,
   not on the push, so a red build cannot ship.
3. GitHub connects as `selamet@91.98.233.170` with a key restricted to
   `/opt/vds-stack/deploy/gh-deploy.sh`. The key cannot open a shell; the only
   thing it can say is which application to deploy.
4. That script runs `deploy/shiftlush-api.sh`: `git pull --ff-only`, then
   `docker compose up -d --build`.
5. The container's entrypoint applies migrations, loads the address reference
   data if it is absent, then starts gunicorn.
6. The workflow polls `/health` for up to 150 seconds and **fails if it never
   answers**. A deploy that reports success while the container crash-loops is
   worse than no report.

`workflow_dispatch` re-runs step 3 onwards without a code change, which is what
you want after editing an environment variable on the server.

## Server layout

```
/opt/apps/shiftlush-api/          the repository, pulled by the deploy script
  .env                            secrets, 0600, never committed
  docker-compose.override.yml  →  /opt/vds-stack/overrides/shiftlush-api.override.yml
/opt/vds-stack/
  Caddyfile                       the site block for shiftlush-api.selamet.dev
  deploy/shiftlush-api.sh         what the forced command runs
  overrides/                      container name and the loopback port mapping
```

Deployment facts — the port, the container name — live in `vds-stack`, not in
this repository. The application does not need to know which port on which box
it happens to be behind.

## Environment

Every variable is read with no default. A missing secret stops the process at
boot rather than falling back to something insecure that nobody notices until
it is exploited. `.env` on the server is the only place they exist.

| Variable | Notes |
|---|---|
| `DJANGO_SECRET_KEY` | |
| `DJANGO_ALLOWED_HOSTS` | `shiftlush-api.selamet.dev` |
| `DATABASE_URL` | `sslmode=verify-full`; the image points libpq at the system CA bundle |
| `REDIS_URL` | the cache, `rediss://` on port 6379. Keys are namespaced with the `shiftlush:` prefix — the shared server's ACL refuses anything outside it |
| `REDIS_QUEUE_URL` | the queue broker, `rediss://` on port 6380 — a second instance because it never evicts. The only optional variable here: nothing reads it until background work exists |
| `CORS_ALLOWED_ORIGINS` | the frontend origin |
| `CSRF_TRUSTED_ORIGINS` | the same origin — Django checks it separately from CORS |
| `FIELD_ENCRYPTION_KEY` | **Rotating this without re-encrypting makes every stored national ID unreadable.** |
| `FRONTEND_URL` | every e-mail links into it |
| `EMAIL_URL` | SMTP. **Must use `smtp+tls://` or `smtp+ssl://`** — `?tls=True` is ignored and production refuses to boot without TLS, because SMTP AUTH would otherwise send the password in clear text. Invitations and password resets are the only way anyone but the founder gets in. |
| `R2_*` | object storage; `/ready` reports 503 while these are unset |

**The request limits** are the other exception, and the one that depends on the
deployment rather than on a credential. `ANON_RATE_LIMIT` (20/min per address)
and `USER_RATE_LIMIT` (300/min per user) default to what the specification asks
for and need not be set. `TRUSTED_PROXY_COUNT` defaults to **1** in production,
which is the Caddy in front of the container: the right-most `X-Forwarded-For`
entry is the address Caddy accepted the connection from, and everything to its
left is whatever the caller chose to send.

That number is the one to check if the limits ever behave strangely. Too low —
zero — and `REMOTE_ADDR` is the Docker gateway, so every anonymous caller in the
world shares a single twenty-a-minute bucket and the API looks broken under
mild load. Too high and a forged header buys a fresh bucket per request, which
removes the limit and puts an invented address in the audit log. Adding a second
proxy in front of Caddy means raising it, in the same change.

The counter itself lives in Redis, which is what makes the limit a limit: with
three gunicorn workers and a per-process cache, each worker enforces the number
separately and the real allowance is three times what is configured. Measured
rather than assumed — 60 of 120 requests through on the in-memory cache against
exactly 20 of 60 on Redis.

**Reverse geocoding** is the exception to the rule above: `GEOCODING_URL`,
`GEOCODING_USER_AGENT` and `GEOCODING_RATE_LIMIT` all have working defaults, so
nothing has to be set for the address picker to work. Two of them are still
worth setting deliberately. Nominatim's usage policy asks that the `User-Agent`
identify the operator and offer a way to reach them, and it caps traffic at one
request a second across the whole deployment — so the day this product has more
than a handful of firms on it, the answer is a self-hosted instance or a
commercial provider at `GEOCODING_URL`, not a larger rate limit. Responses are
cached in Redis for a month; that cache is what keeps the current traffic inside
the policy, and losing it costs quota rather than correctness.

**`PASSWORD_CHANGE_RATE_LIMIT`** also has a working default (`10/hour`, per
user). It bounds `POST /auth/password`, which reports whether a guess at the
current password was right and is therefore a guessing oracle for anybody
holding a stolen access token. The login lockout does not reach it — that counts
failures against a sign-in, and this caller is already signed in. Throttle state
lives in the default cache, so without `REDIS_URL` the count is per process and
several workers multiply the effective limit by their number.

## Bringing up a new environment

Everything here happens once, against a database and a box that have never run
this application. An existing deployment needs none of it.

**1. The database.** An empty database and a role that owns it, on the laboflush
stack at `database.selamet.dev`. Nothing else — no schema, no seed. `migrate`
creates the tables. The connection has to work with `sslmode=verify-full`
against the system CA bundle; if it does not, the container will not boot and
the reason will be a certificate rather than the application.

**2. The repository.**

```bash
ssh selamet@91.98.233.170
git clone https://github.com/selamet/shiftlush-api.git /opt/apps/shiftlush-api
```

**3. Secrets.** Every variable in the table above, none of them defaulted.

```bash
cd /opt/apps/shiftlush-api
cp .env.example .env
chmod 600 .env
"$EDITOR" .env
```

**4. The deployment facts, which live in `vds-stack` and not here.** For another
copy of *this* application they already exist and only the symlink is missing;
for a genuinely new application all four are new.

```bash
ln -s /opt/vds-stack/overrides/shiftlush-api.override.yml \
      /opt/apps/shiftlush-api/docker-compose.override.yml
```

- `/opt/vds-stack/overrides/shiftlush-api.override.yml` — container name, `.env`
  and the loopback port
- `/opt/vds-stack/deploy/shiftlush-api.sh` — what a deploy runs
- `/opt/vds-stack/deploy/gh-deploy.sh` — the name has to appear in its `case`, or
  the forced command refuses it
- `/opt/vds-stack/Caddyfile` — the site block and its `reverse_proxy` port

**5. First deploy.** The same script GitHub triggers, run by hand because there
is nothing to trigger it yet.

```bash
/opt/vds-stack/deploy/shiftlush-api.sh
```

The container's entrypoint applies migrations **and loads the Turkish address
data**, then starts gunicorn. Neither is a separate step, and neither is a
command anyone has to remember: an empty `province` / `district` /
`neighborhood` set is not a missing nicety but an API where creating a building,
a complex or a customer fails.

**6. Confirm the environment is usable.** `/ready` reports that the process can
reach its dependencies. It does not report whether the address tables hold
anything, and that is precisely the failure this section exists to prevent.

```bash
curl -s https://shiftlush-api.selamet.dev/ready

docker exec shiftlush-api python manage.py shell -c \
  "from apps.address.models import Province, District, Neighborhood; \
   print(Province.objects.count(), District.objects.count(), Neighborhood.objects.count())"
# 81 973 50437
```

**7. The first account.** `POST /auth/register` creates a company and its owner.
Everyone after that arrives by invitation.

### Address data, afterwards

Once the data is present the load is skipped on every restart: three existence
queries, no CSV parsing, no upserts, no write locks on a table the outgoing
container is still serving reads from. That is what `--if-missing` in
`entrypoint.sh` buys, and it is the reason the load can sit on the boot path at
all.

The consequence is that **the yearly refresh is a manual run.** New CSVs in the
image change nothing by themselves — a deploy carrying them looks like any other
deploy. After merging a data refresh:

```bash
ssh selamet@91.98.233.170
docker exec shiftlush-api python manage.py load_address_data
```

Without the flag the command upserts all three files by primary key. It is
idempotent — two consecutive runs leave the row counts identical, which the
suite asserts against the real dataset — so re-running it is never the wrong
thing to do. What it does not do is delete: a district that upstream has
dissolved stays until someone removes it deliberately, because the buildings
pointing at it would otherwise have nowhere to be.

Demo data (`seed_demo_data`) is for development and demonstration environments
only. It refuses to run twice, and it refuses to run at all before the address
data exists.

## Checking a deployment

```bash
curl https://shiftlush-api.selamet.dev/health   # liveness: is the process up
curl https://shiftlush-api.selamet.dev/ready    # readiness: can it serve
```

`/health` deliberately does not check the database. A liveness probe that fails
when a dependency is down gets the container killed and restarted, which fixes
nothing and removes the instance that could still have served cached reads.
`/ready` is the one that checks, and it is what a load balancer should ask.

It reports the cache too, but never fails on it: nothing depends on Redis
yet, so a broken cache is worth seeing and not worth pulling a working
instance out of rotation for. `"cache": "ok"` in the response is how you
confirm the Redis credential and key prefix are right after a deploy.

## Rolling back

```bash
ssh selamet@91.98.233.170
cd /opt/apps/shiftlush-api
git reset --hard <previous-commit>
/opt/vds-stack/deploy/shiftlush-api.sh
```

Note what this does *not* do: migrations are not reversed. A rollback across a
migration that dropped or rewrote a column needs a plan of its own, decided
before the migration ships rather than during the incident.

## Error reporting

Production reports uncaught exceptions and anything logged at `ERROR` to Sentry.
It is off unless `SENTRY_DSN` is set, so a deployment that does not want it
simply does not set one.

| Variable | |
|---|---|
| `SENTRY_DSN` | The project's DSN. A separate project from the frontend's — the platform decides how stack traces are read. |
| `SENTRY_ENVIRONMENT` | `production` by default. |
| `SENTRY_RELEASE` | The deployed commit, so a new issue points at the deploy that introduced it. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.0`. Tracing samples ordinary requests rather than errors, so it costs quota continuously. |

**What is not sent, and why.** The request body is never collected — not
scrubbed, not truncated. This application holds national identity numbers,
encrypted at rest; a validation error carrying one to a third party would undo
that encryption with none of the effort. Passwords, tokens and cookies are in
the same category. Sentry's own quickstart turns this collection on
(`send_default_pii=True`); this deployment does not, and `tests/test_observability.py`
fails if that is ever reversed.

What is sent instead: the company id, the user id, and the `request_id` the user
can read off their own error screen. Enough to find anyone in the admin, and
nothing that identifies them to whoever reads the report.

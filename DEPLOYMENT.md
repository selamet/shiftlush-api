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
5. The container's entrypoint applies migrations, then starts gunicorn.
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

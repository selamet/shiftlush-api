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
| `DATABASE_URL` | `sslmode=verify-full`; the image points libpq at the system CA bundle. **This is not end-to-end encryption to Postgres** — see "What `verify-full` actually covers" below |
| `REDIS_URL` | the cache, `rediss://` on port 6379. Keys are namespaced with the `shiftlush:` prefix — the shared server's ACL refuses anything outside it |
| `REDIS_QUEUE_URL` | the queue broker, `rediss://` on port 6380 — a second instance because it never evicts. The only optional variable here: nothing reads it until background work exists |
| `CORS_ALLOWED_ORIGINS` | the frontend origin |
| `CSRF_TRUSTED_ORIGINS` | the same origin — Django checks it separately from CORS |
| `FIELD_ENCRYPTION_KEY` | **Rotating this without re-encrypting makes every stored national ID unreadable.** |
| `FRONTEND_URL` | every e-mail links into it |
| `EMAIL_URL` | SMTP. **Must use `smtp+tls://` or `smtp+ssl://`** — `?tls=True` is ignored and production refuses to boot without TLS, because SMTP AUTH would otherwise send the password in clear text. Invitations and password resets are the only way anyone but the founder gets in. |
| `R2_*` | object storage; `/ready` reports 503 while these are unset |
| `ADDRESS_PROVINCES` | which provinces the address tables hold, as licence-plate codes — `25` for Erzurum. Optional, and the exception to the rule above: unset means all 81, which is what development and CI want. Changing it is a command rather than a restart — see "Address data, afterwards" |

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

How much of the country it loads is `ADDRESS_PROVINCES`, so a new environment
gets its scope right the first time rather than being narrowed afterwards.

**6. Confirm the environment is usable.** `/ready` reports that the process can
reach its dependencies. It does not report whether the address tables hold
anything, and that is precisely the failure this section exists to prevent.

```bash
curl -s https://shiftlush-api.selamet.dev/ready

docker exec shiftlush-api python manage.py shell -c \
  "from apps.address.models import Province, District, Neighborhood; \
   print(Province.objects.count(), District.objects.count(), Neighborhood.objects.count())"
# 81 973 50437   with ADDRESS_PROVINCES unset
# 1 20 1188      with ADDRESS_PROVINCES=25 (Erzurum), which is what production runs
```

**7. The first account.** `POST /auth/register` creates a company and its owner.
Everyone after that arrives by invitation.

**8. The demonstration account.** The one login that can be shown to somebody
without opening the firm's own records. It is a command rather than something
typed into a shell precisely so that this step exists: an account created by
hand is gone the next time a database is built, and nobody can say what it was.

```bash
docker exec shiftlush-api python manage.py create_demo_account --with-data
```

It creates the company `ShiftLush Demo` and `demo@selamet.dev` / `demo123123` as
its owner, with the address already marked verified — it is provisioned rather
than claimed, so no message is sent and nothing is left for the admin to tick.
Running it again creates no second company and resets no password, so it is safe
on the boot path of a rebuild.

`--with-data` fills the demo company with generated customers, buildings,
elevators and contracts, and a demonstration environment wants it: five empty
lists show nothing. It is not the default because this command is expected to be
run against databases holding real records, and it writes into the demo tenant
only — the tenant boundary is what guarantees that, not the names it picks. On a
tenant that already holds colleagues or customers it writes nothing and says so.

The password is checked against `AUTH_PASSWORD_VALIDATORS` before the account is
created, so `--password` cannot quietly install one below the policy the product
enforces on its customers.

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

Without the flag the command upserts all three files by primary key, within
`ADDRESS_PROVINCES`. It is idempotent — two consecutive runs leave the row counts
identical, which the suite asserts against the real dataset — so re-running it is
never the wrong thing to do. Inside the scope it still does not delete: a
district that upstream has dissolved stays until someone removes it deliberately,
because the buildings pointing at it would otherwise have nowhere to be.

### The scope, and changing it

`ADDRESS_PROVINCES` is a comma-separated list of licence-plate codes, and it is
read on **every** load path. Production runs `ADDRESS_PROVINCES=25`, so the
address tables hold Erzurum and the pickers offer Erzurum. Unset means all 81,
which is what development and CI use.

Reading it here rather than deleting the other eighty provinces by hand is the
whole point. A hand-written `DELETE` looks like it works — `--if-missing` only
asks whether the tables are non-empty, so the deletion survives every restart —
and then comes undone in silence at the refresh above, or on the next
environment built from nothing, both of which used to reload all 81. Because the
scope is a property of the deployment, **no run of this command can widen the
dataset past it**, and a second deployment for a firm in another province is one
variable rather than a code change.

Changing the scope is a command, not a restart:

```bash
"$EDITOR" /opt/apps/shiftlush-api/.env     # ADDRESS_PROVINCES=25
docker exec shiftlush-api python manage.py load_address_data
```

The container start does not do the second half. `--if-missing` never deletes —
fifty thousand rows and a possible refusal is not something to discover during a
boot — so a restart after an edit loads anything newly in scope, leaves what is
now out of it, and writes a line to the log saying how many provinces are still
there and what to run. The run above is the thing that removes them.

It removes them only if nothing points at them. Every foreign key into
`address.Neighborhood` is `on_delete=PROTECT`, so the database would refuse in
any case; the command asks first, before its first `DELETE`, and names the table
and the count instead of raising a `ProtectedError` about one row id:

```
CommandError: Refusing to remove provinces that records still point at:
properties.Building.neighborhood: 1. Move those records inside
ADDRESS_PROVINCES first — nothing has been deleted.
```

"Nothing has been deleted" is exact: the whole run is one transaction, so a
refusal takes the refreshed rows back with it. The check counts other tenants'
rows and soft-deleted ones too — they still hold the foreign key.

Demo data (`seed_demo_data`) is for development environments only. It registers
a company of its own and refuses to start once any company exists, so it is the
wrong command for anything already carrying real records — `create_demo_account`
is the one that provisions a demonstration tenant beside a live one. Both fill
the tenant from the same generator, and both refuse to run before the address
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

`"error_reporting"` is reported on the same terms, and for a second reason on
top of the first. Its two values are `"ok"` — a Sentry client exists and has a
transport, so an exception raised now would be transmitted — and `"disabled"`,
meaning no `SENTRY_DSN`, or one that is set but empty. Both are legitimate
states; the one that was not legitimate was being unable to tell them apart
without reading a 0600 file on the server. The deploy workflow's last step
prints `/ready`, so every deploy now records in its own log which of the two it
shipped.

It is asked of the SDK rather than of the setting, because those are different
questions. A DSN that is set but malformed, an `init` that never ran, and a
client that was started and closed all leave `SENTRY_DSN` looking correct while
nothing is sent.

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

**It also lasts only until the next deploy.** `git reset --hard` moves the
checkout back; it does not move `main`. The deploy script begins with
`git pull --ff-only`, which fast-forwards straight back to the commit you rolled
away from — and the next deploy need not be a deliberate one. Any merge to
`main` triggers it, and so does the `workflow_dispatch` button recommended above
for environment changes. A rollback is therefore a stopgap measured in minutes:
revert the commit on `main` and let CI ship the revert, or the server quietly
re-ships the build you rolled back.

**Never `git clean` in that directory.** It holds two untracked things the
deploy depends on and no commit can restore:

- `.env` — the only copy of every secret. Nothing else has them.
- `docker-compose.override.yml` — a symlink into `/opt/vds-stack/overrides/`
  carrying the port mapping and the container name. Without it the container
  comes up on no published port and Caddy proxies to nothing.

`git reset --hard` leaves both alone, which is why the sequence above is safe as
written. `git clean -fd` used to delete the symlink — it was untracked and
therefore looked like a stray file — which is why it is now in `.gitignore`.
`git clean -fdx` still removes both, `.env` included, and there is no recovering
from that without the owner.

## Error reporting

Production reports uncaught exceptions and anything logged at `ERROR` to Sentry.
It is off unless `SENTRY_DSN` is set, so a deployment that does not want it
simply does not set one.

Off is a deliberate state and stays one. It is not made required the way
`REDIS_URL` and `EMAIL_URL` are: an instance with no DSN serves every request
correctly, and refusing to boot over a missing DSN would take production down
over a feature that has no effect on a single customer. This repository has had
that outage once already — see `SL-60`, and note that `REDIS_URL` was actually
load-bearing. What is not acceptable is off and unknowable, so `/ready` answers
it; see "Checking a deployment" above.

Setting these on the server means adding them to **both** `.env` and the
`environment:` list in `docker-compose.prod.yml`. Compose substitutes the names
it is given and nothing else, so a variable in `.env` alone never reaches the
container — which is exactly how `SL-60` happened. All four are already listed;
a fifth would not be.

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

## Container limits

The host has 7.6 GiB and **no swap**, and runs 21 containers belonging to five
applications. With no limit, one runaway request in any of them takes the whole
box down and every other tenant with it. `docker-compose.prod.yml` therefore
caps this service:

| | |
|---|---|
| `mem_limit` | `768m` |
| `memswap_limit` | `768m` — equal, so adding swap later cannot turn a fast OOM kill into a slow thrash |
| `mem_reservation` | `384m` — soft floor; under host-wide pressure the label-render peak is reclaimed first |

The number was measured, not rounded. Running this image with its own
`--workers 3` and reading `/proc`:

| | |
|---|---|
| gunicorn master | 29 MB |
| each worker, warm | 110 MB — Django, DRF, psycopg, botocore |
| container, steady state | **293 MB** — summed PSS, so shared pages are counted once |
| a worker that has printed a QR label sheet | 166 MB |

WeasyPrint is imported lazily, inside `apps/elevators/labels.py`: +22 MB for the
import and +55 MB to render, and the allocator does not hand it back. So the
worst realistic case is all three workers having printed labels at least once —
293 + 3 × 77 ≈ **525 MB**. 768 MB clears that by about 45%: room for the peak
plus per-request allocation, and short of the 1 GiB that would spend a third of
the host's free memory on headroom nothing has ever used.

If the container starts being OOM-killed, raising this number is the wrong fix.
Steady state is 293 MB, so anything approaching 768 MB is a leak, and a higher
ceiling only buys the leak more time to reach the host. Fewer workers, or
`--max-requests` to recycle them, are the honest answers.

When the limit is hit the kernel kills the container, `restart: unless-stopped`
brings it back, and the outage is one application for a few seconds instead of
the whole box. `docker inspect --format '{{.State.OOMKilled}}' <container>` is
how you tell that apart from a crash.

## What `verify-full` actually covers

`DATABASE_URL` ends in `sslmode=verify-full`, which reads as end-to-end
encryption to Postgres. It is not, and the live server proves it:
`pg_stat_ssl.ssl` is **`false`** for this application's backends.

Both facts are true at once, because the connection has two hops:

```
container ──TLS, cert verified against database.selamet.dev──▶ proxy on the database host
                                                    proxy ──plaintext, Docker bridge──▶ postgres
```

So `verify-full` is real and doing its job on the hop that crosses the network:
the traffic is encrypted, and the certificate is checked against the hostname,
which is what distinguishes `verify-full` from `require` and is worth keeping.
The last hop — inside `database.selamet.dev`, from the TLS terminator to the
Postgres process across that host's Docker bridge — is in the clear, which is
why the backend reports no SSL.

**What that means in practice.** The trust boundary is the database host itself.
Anything with root on that box, or any container able to read that bridge, can
see queries and results in plaintext — including the national ID ciphertext and
everything else in the same statement. Between the two machines there is nothing
to see. Whether that is acceptable is the owner's decision and the arrangement
has not been changed; what must not happen is somebody reading `verify-full` in
`.env` and concluding the database session is encrypted the whole way.

Re-check it with, on the database host:

```sql
SELECT usename, ssl, client_addr FROM pg_stat_ssl JOIN pg_stat_activity USING (pid)
WHERE usename = 'shiftlush_app';
```

If that ever starts returning `ssl = true`, the terminator has moved and this
section is out of date. Do not "fix" it by weakening the setting: dropping to
`require` would silently give up the hostname check on the hop that actually
crosses the internet, which is the half of `verify-full` that is working.

## Mail authentication

Invitations and password resets are the only way anyone but the founder gets an
account, so mail landing in spam does not degrade the product — it stops
onboarding entirely. Mail goes out through Resend, which sends via Amazon SES,
from the `send.selamet.dev` envelope domain.

What is published today:

| Name | Type | Value | |
|---|---|---|---|
| `resend._domainkey.send.selamet.dev` | TXT | `p=MIGf…` | DKIM. Present, signs, and aligns |
| `send.selamet.dev` | TXT | `v=spf1 include:amazonses.com ~all` | SPF for the envelope sender |
| `send.selamet.dev` | MX | `10 feedback-smtp.eu-west-1.amazonses.com` | the custom `MAIL FROM`, which is why SPF is on this name |
| `_dmarc.selamet.dev` | TXT | `v=DMARC1; p=none;` | **no `rua=`** |
| `selamet.dev` | TXT | *(none at all)* | **no SPF on the apex** |

The gap that matters is `rua=`. `p=none` asks receivers to do nothing, and
without a reporting address nobody is told what receivers decided. If DKIM
signing broke — a key rotated at Resend, a provider change, a subdomain
retired — mail would start failing authentication and the first evidence would
be a customer saying they never got their invitation. Aggregate reports turn
that into a daily XML file.

### The records to add

Two, in this order. **Reporting first, enforcement second** — `p=quarantine`
switched on without reports first is a guess about which of your own mail is
authenticating, and the way you find out you were wrong is that nobody can be
onboarded.

**Step 1 — publish now.**

```dns
selamet.dev.          IN TXT "v=spf1 include:amazonses.com ~all"
_dmarc.selamet.dev.   IN TXT "v=DMARC1; p=none; rua=mailto:dmarc@selamet.dev; adkim=r; aspf=r;"
```

Neither changes how any mail is treated today, which is the point of doing them
first.

- The apex SPF closes a domain that currently publishes none. It matches the
  include already on `send.selamet.dev`, so anything that does send with an apex
  envelope passes rather than arriving unauthenticated. `~all` — softfail — not
  `-all`, until the reports say what else sends as `selamet.dev`.
- `rua=` starts the aggregate reports. Keep the mailbox on `selamet.dev`; the
  apex already has an MX. A reporting address on **any other domain** requires
  that domain to authorise it with
  `selamet.dev._report._dmarc.<their-domain> IN TXT "v=DMARC1"`, and without
  that record the reports are silently never sent — which would leave you
  believing you had monitoring when you had none.
- `adkim=r; aspf=r;` are the defaults, written out on purpose. Relaxed
  alignment is what makes this arrangement pass at all: DKIM signs as
  `d=send.selamet.dev` and the envelope is `@send.selamet.dev`, while the
  `From:` header is `selamet.dev`. Under strict alignment neither would align,
  and at `p=quarantine` that means every invitation goes to spam. Anyone
  "hardening" these to `s` later needs to move the DKIM selector and the
  envelope to the apex in the same change.

Lower the TTL on both names to 300 while you work, and put it back afterwards.

**Step 2 — after two to four weeks of clean reports.** Read them. You are
looking for one thing: 100% of legitimate mail passing DMARC with an aligned
DKIM signature, over a period long enough to include a real invitation cycle and
a password reset. Anything else that sends as this domain — a forgotten form, a
monitoring alert, a mailing list — shows up here and nowhere else.

Then tighten in two moves, not one:

```dns
_dmarc.selamet.dev.   IN TXT "v=DMARC1; p=quarantine; pct=25; rua=mailto:dmarc@selamet.dev; adkim=r; aspf=r;"
```

`pct=25` applies the policy to a quarter of failing mail. If the reports stay
clean for a week, go to `pct=100`, wait another week, and only then:

```dns
_dmarc.selamet.dev.   IN TXT "v=DMARC1; p=reject; rua=mailto:dmarc@selamet.dev; adkim=r; aspf=r;"
selamet.dev.          IN TXT "v=spf1 include:amazonses.com -all"
send.selamet.dev.     IN TXT "v=spf1 include:amazonses.com -all"
```

Subdomains inherit the apex policy unless `sp=` says otherwise, so
`send.selamet.dev` needs no DMARC record of its own.

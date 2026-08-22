"""The sign-in lockout of specification 7.4.

Five attempts per fifteen minutes for the same e-mail *and* address, then a
fifteen minute lock. Both halves of that key are in the specification and only
one of them was ever built.

**The address half is the one that matters.** A
lockout counted per account is not a weaker version of this; it points the other
way. Five wrong passwords cost an attacker nothing to send, so a lock that
follows the account alone lets anybody who knows a registered e-mail address
hold its owner out of the product indefinitely — from anywhere, renewed every
fifteen minutes. Against an owner, that also takes out the only role that can
manage users and company settings, and the customer experiences it as the
product being broken. Keyed on the pair, the same attacker locks only the bucket
they were failing in, and the person at their own desk never notices.

**What it costs to put the address in the key**, in both directions:

*An office behind one NAT address.* Twenty people share one address, and this
does not put them in one bucket, because the e-mail is in the key as well. A
colleague fumbling their own password spends their own allowance and nobody
else's. The per-address request throttle in `core.throttling` is the control
that an office genuinely shares, and it is set fifteen times larger for exactly
that reason.

*Somebody on a train.* An address that changes between attempts means the
failures land in different buckets, so a roaming caller effectively gets more
than five tries. The same is true of an attacker with a pool of addresses: five
guesses per address. That is the real cost of this key and it is worth paying.
Volume from one address is already the throttle's job (twenty a minute); what is
left for a distributed guesser is a handful of attempts against argon2id and the
common-password blacklist, which is a bad trade for them. The alternative — the
account-wide lock — buys nothing against that same distributed guesser, since
they are not trying to be let in so much as to be counted, and hands over a
free, reliable denial of service against any named customer. One of these
failure modes is speculative and one of them is a support call this afternoon.

**Where the counter lives: the Django cache, which is Redis in production.**
Not on the User row, and not in process memory. In memory it would not enforce
five attempts at all — it would enforce five *per gunicorn worker*, because each
worker only sees the attempts it was handed, and the deployment runs three. The
deviations table in the specification carries the measurement with real workers.
On the User row it would enforce the wrong thing, which is the bug this module
replaces, and it would also mean a table write on every wrong password typed by
anybody, which is a write amplifier pointed at the busiest unauthenticated
endpoint in the product.

The keys are hashed. The Redis instance is shared with other applications and
its keyspace is not a secret to whoever can run `KEYS` against it; an unhashed
key would turn "somebody once mistyped their password" into a browsable list of
customer e-mail addresses, which is a data-protection problem for a benefit of
nothing.
"""

from __future__ import annotations

import hashlib

from django.core.cache import cache

#: Specification 7.4: five attempts, a fifteen minute window, a fifteen minute
#: lock. The window and the lock are separate numbers even though they are equal
#: today, because they answer different questions — how long failures are
#: remembered for, and how long the door stays shut once it closes.
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60

_FAILURES = "login-failures:%s"
_LOCK = "login-lock:%s"


def _digest(email: str, ip: str | None) -> str:
    # "unknown" rather than None, matching core.throttling: a caller whose
    # address cannot be determined shares one bucket with the others instead of
    # dropping out of the count, which is what a null key would do.
    #
    # The separator cannot appear in either half, so no pair of (e-mail,
    # address) values can be made to collide with another by moving the boundary
    # — `a@b.com` from `1.2` and `a@b.com1` from `.2` are different keys.
    return hashlib.sha256(f"{email.lower()}\x00{ip or 'unknown'}".encode()).hexdigest()


def is_locked(*, email: str, ip: str | None) -> bool:
    """Whether this pair is inside a lock right now.

    Checked before the account is looked up, so a locked bucket costs no query
    and answers identically whether or not the address is registered.
    """
    return cache.get(_LOCK % _digest(email, ip)) is not None


def record_failure(*, email: str, ip: str | None) -> None:
    """Count one failed attempt, and close the door if that was the fifth.

    Called for unknown e-mail addresses as well as known ones. Counting only the
    ones that exist would leak exactly what `authenticate` refuses to say: the
    sixth attempt would start answering `ACCOUNT_LOCKED` for a registered
    address and go on answering `INVALID_CREDENTIALS` for an unregistered one,
    which is an account-enumeration oracle costing six requests per guess.
    """
    digest = _digest(email, ip)
    key = _FAILURES % digest

    # `add` then `incr` rather than a read-modify-write. Both halves are atomic
    # in Redis, so the count is exact across workers; a get/set pair would lose
    # attempts to the interleaving and the fifth failure would sometimes be the
    # seventh. `add` also leaves an existing key's expiry alone, so the window
    # runs from the first failure rather than restarting on every one — without
    # that, an attacker pacing themselves just under the limit would keep the
    # window open for ever and never be counted out of it.
    cache.add(key, 0, ATTEMPT_WINDOW_SECONDS)
    try:
        count = cache.incr(key)
    except ValueError:
        # The window expired between the two calls. This attempt opens the next
        # one rather than being dropped.
        cache.set(key, 1, ATTEMPT_WINDOW_SECONDS)
        count = 1

    if count >= MAX_ATTEMPTS:
        # The lock is its own key so that it lasts a full fifteen minutes from
        # the fifth failure. Reusing the counter's key would leave it whatever
        # remained of the attempt window — a second, if the five attempts took
        # fourteen minutes.
        cache.set(_LOCK % digest, True, LOCKOUT_SECONDS)
        cache.delete(key)


def clear(*, email: str, ip: str | None) -> None:
    """Forget this pair's failures. Called when the password was right."""
    digest = _digest(email, ip)
    cache.delete_many([_FAILURES % digest, _LOCK % digest])

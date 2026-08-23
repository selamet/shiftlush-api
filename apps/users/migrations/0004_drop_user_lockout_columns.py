"""Drop the two columns the sign-in lockout used to be kept in.

Specification 7.4 counts failed sign-ins per e-mail *and* address. A column on
the user row can only count per e-mail, and a lockout counted that way is not a
weaker control but an inverted one: five wrong passwords, which cost nothing to
send, hold the named owner of that address out of the product from anywhere. The
counter now lives in the cache keyed on the pair — see `apps.users.lockout`.

Nothing is migrated across. The columns held a counter inside a fifteen minute
window and an expiry no further than fifteen minutes away, so everything in them
was stale before the deploy finished, and the worst case of dropping them is
that somebody mid-lockout gets their next attempt. The best case is the same
thing for the person who was locked out by a stranger.

It reverses, and reversing it is not free: `RemoveField` puts the columns back
empty, which is exactly what code from before this release needs to run. So a
rollback of both together comes back up enforcing the account-wide lock again.
That is what rolling back means and it is left possible on purpose — but it is
a decision to take deliberately, not a step to run on the way past.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("users", "0003_refresh_session_chain")]

    operations = [
        migrations.RemoveField(model_name="user", name="failed_login_count"),
        migrations.RemoveField(model_name="user", name="locked_until"),
    ]

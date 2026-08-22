"""Free the elevators that a renewal left pinned to a finished contract.

`renew(carry_elevators=False)` moved the predecessor to `renewed` and left its
`contract_elevator` rows open. The partial unique index keys on
`removed_at IS NULL`, so each of those elevators was reserved for a contract the
user had already closed: every later attempt to cover one was refused with
ELEVATOR_ALREADY_CONTRACTED naming that finished contract, and nothing in the
API could release it. The service now closes those lines whatever the flag says;
rows written before that fix are still holding their elevators, and only a
migration can let go of them.

The footprint is exact. `renew` and `terminate` are the only ways a contract
reaches a closed status, both close every open line, so an open line on a
`renewed` contract is this bug and nothing else.

Each line is closed on the successor's `start_date` — the date the fix would
have used, and the date cover actually ended. Elevators are moved back to
`uncontracted` only where they are still `active`: that is the state the bug
left them in, whereas `suspended`, `sealed` and `out_of_service` are statements
somebody made about the machine afterwards, and a repair should not overwrite
them. The index is released either way; the status only decides which list the
elevator shows up on.
"""

from django.db import migrations


def release_stranded_lines(apps, schema_editor):
    contract_model = apps.get_model("contracts", "Contract")
    line_model = apps.get_model("contracts", "ContractElevator")
    elevator_model = apps.get_model("elevators", "Elevator")

    stranded = line_model.objects.filter(
        removed_at__isnull=True,
        is_deleted=False,
        contract__status="renewed",
        # Soft-deleted lines are excluded because the fixed service excludes
        # them too: they sit outside the index and outside every query, so they
        # strand nothing and closing them would only rewrite dead history.
    )

    for line_id, contract_id, elevator_id in stranded.values_list(
        "pk", "contract_id", "elevator_id"
    ).iterator():
        successor_start = (
            contract_model.objects.filter(previous_contract_id=contract_id)
            .order_by("start_date")
            .values_list("start_date", flat=True)
            .first()
        )
        if successor_start is None:
            # A `renewed` contract with no successor should not exist. Falling
            # back to its own end date still frees the elevator, and inventing a
            # date is better than leaving the row pinned forever.
            successor_start = (
                contract_model.objects.filter(pk=contract_id)
                .values_list("end_date", flat=True)
                .first()
            )

        line_model.objects.filter(pk=line_id).update(removed_at=successor_start)
        elevator_model.objects.filter(pk=elevator_id, status="active").update(status="uncontracted")


class Migration(migrations.Migration):
    dependencies = [
        ("contracts", "0003_contract_vat_rate_bounds"),
        ("elevators", "0003_initial"),
    ]

    operations = [
        migrations.RunPython(
            release_stranded_lines,
            # Not reversible in any useful sense: the rows this closes were only
            # ever open because of a bug, and reopening them would put the
            # elevators back in the trap.
            reverse_code=migrations.RunPython.noop,
            elidable=True,
        )
    ]

#!/usr/bin/env python3
"""Rebuild the address CSVs from the upstream dataset.

Run when the yearly refresh lands, not on deploy. The output is committed, so
the application never depends on a third party being reachable — and so a change
in the data is a reviewable diff rather than something that happens quietly.

    uv run python apps/address/data/build.py

Source: https://github.com/ubeydeozdmr/turkiye-api (MIT), which publishes the
national address data as JSON. 81 provinces, 973 districts, and about fifty
thousand neighbourhoods and villages.
"""

from __future__ import annotations

import csv
import json
import sys
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/ubeydeozdmr/turkiye-api/main/datasets/2025"
HERE = Path(__file__).resolve().parent

# Upstream numbers neighbourhoods and villages in separate id spaces, and 2,731
# ids appear in both. They share one table here, so villages are moved above
# every neighbourhood id. A constant rather than a renumbering: the offset is
# reproducible, so next year's refresh lands on the same rows instead of
# reshuffling every foreign key that points at them.
VILLAGE_ID_OFFSET = 1_000_000

EXPECTED = {"provinces": 81, "districts": 900, "neighborhoods": 30_000, "villages": 15_000}


def fetch(name: str) -> list[dict]:
    """Read one upstream file into memory.

    Deliberately not written to disk on the way past. An earlier version kept
    the downloads, and nine megabytes of intermediate JSON ended up committed
    because they landed in the working directory.
    """
    url = f"{BASE}/{name}.json"
    if not url.startswith("https://"):  # pragma: no cover - BASE is a constant
        raise SystemExit("refusing a non-https source")
    request = urllib.request.Request(url, headers={"User-Agent": "shiftlush"})
    with urllib.request.urlopen(request, timeout=120) as response:
        rows = json.load(response)

    # A source that quietly starts returning half the country would otherwise
    # be committed as a diff nobody reads closely enough.
    if len(rows) < EXPECTED[name]:
        raise SystemExit(f"{name}: got {len(rows)} rows, expected at least {EXPECTED[name]}")
    return rows


def write(name: str, header: list[str], rows: list[list]) -> None:
    path = HERE / f"{name}.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        # csv.writer defaults to CRLF. Set explicitly so the committed bytes
        # are the same whoever runs this, and a refresh diff shows the rows that
        # changed rather than every line.
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
    print(f"{path.name}: {len(rows)} rows")


def main() -> None:
    provinces = fetch("provinces")
    districts = fetch("districts")
    neighborhoods = fetch("neighborhoods")
    villages = fetch("villages")

    province_ids = {p["id"] for p in provinces}
    district_ids = {d["id"] for d in districts}

    # Checked rather than assumed: a row pointing at a parent that is not in the
    # file fails the load with an integrity error halfway through, which is a
    # worse place to find out.
    for district in districts:
        if district["provinceId"] not in province_ids:
            raise SystemExit(f"district {district['id']} points at unknown province")
    for row in (*neighborhoods, *villages):
        if row["districtId"] not in district_ids:
            raise SystemExit(f"settlement {row['id']} points at unknown district")

    write("province", ["id", "name"], [[p["id"], p["name"]] for p in provinces])
    write(
        "district",
        ["id", "province_id", "name"],
        [[d["id"], d["provinceId"], d["name"]] for d in districts],
    )

    settlements = [
        [n["id"], n["districtId"], n["name"], n.get("postalCode") or "", "neighborhood"]
        for n in neighborhoods
    ] + [
        [
            v["id"] + VILLAGE_ID_OFFSET,
            v["districtId"],
            v["name"],
            v.get("postalCode") or "",
            "village",
        ]
        for v in villages
    ]

    ids = [row[0] for row in settlements]
    if len(ids) != len(set(ids)):
        raise SystemExit("settlement ids collide — the village offset is no longer enough")

    write("neighborhood", ["id", "district_id", "name", "postal_code", "type"], settlements)


if __name__ == "__main__":
    sys.exit(main())

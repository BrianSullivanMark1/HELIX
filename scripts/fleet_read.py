"""Read one cell of the fleet - or all of them - for real, through your own gcloud login.

    python scripts/fleet_read.py               # MES dev
    python scripts/fleet_read.py WMS prod      # one cell
    python scripts/fleet_read.py --all         # every cell in the table (12), in parallel

Read-only by construction (helix/adapters/gcloud_fleet.py issues only `services describe`,
`revisions list` and `--version`). Prints ASCII only - this runs on a Windows console (rule 7).
Nothing here touches HELIX's container, settings or the face; it is the adapter, bare.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helix.adapters.gcloud_fleet import GcloudFleet  # noqa: E402
from helix.domain import fleet  # noqa: E402
from helix.domain.fleet import Env  # noqa: E402
from helix.ports.fleet import CellRead  # noqa: E402


def _yn(v: bool | None) -> str:
    return "?" if v is None else ("yes" if v else "no")


def show(r: CellRead) -> None:
    s = r.service
    print(f"== {s.key}   ({s.run_service or '-'})")
    if not r.ok:
        print(f"   READ FAILED: {r.problem}")
        if r.detail:
            print(f"   {r.detail}")
        return
    if r.api is None:
        print("   no API service in this environment (known absence)")
        return
    a = r.api
    print(f"   health     {a.health.value:<9} traffic {a.traffic_percent if a.traffic_percent is not None else '?'}%"
          f"{'  SPLIT' if a.is_split else ''}")
    print(f"   revision   {a.revision or '-'}")
    print(f"   commit     {a.commit or '(not recorded)'}{' -dirty' if a.dirty else ''}"
          f"{'   by ' + a.deployed_by if a.deployed_by else ''}")
    print(f"   deployed   {a.deployed_at.strftime('%Y-%m-%d %H:%M UTC') if a.deployed_at else '-'}")
    print(f"   db         {a.db or '-'}   readOnly {_yn(a.read_only)}   appCheck {_yn(a.flag('appCheckRequired'))}")
    n = len(r.served_revisions)
    targets = fleet.rollback_targets(r.served_revisions, a.revision)
    print(f"   revisions  {n} on the service; rollback could offer {len(targets)}: {', '.join(targets) or '-'}")
    if r.problem:
        print(f"   note       {r.problem}")
    print(f"   ({r.seconds:.1f}s)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("system", nargs="?", default="MES")
    ap.add_argument("env", nargs="?", default="dev", choices=[e.value for e in Env])
    ap.add_argument("--all", action="store_true", help="read every cell in the fleet table")
    args = ap.parse_args(argv)

    reader = GcloudFleet(fleet.GCP_PROJECT, fleet.GCP_REGION)
    ok, why = reader.available()
    print(f"gcloud: {reader._gcloud}")
    if not ok:
        print(why)
        return 2

    t0 = time.monotonic()
    if args.all:
        reads = reader.read_all(list(fleet.FLEET))
    else:
        svc = fleet.find(args.system.upper(), Env(args.env))
        if svc is None:
            print(f"no such cell: {args.system} {args.env}  (systems: {', '.join(fleet.SYSTEMS)})")
            return 2
        reads = [reader.read_cell(svc)]
    for r in reads:
        show(r)
    print(f"total {time.monotonic() - t0:.1f}s")
    return 0 if all(r.ok for r in reads) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

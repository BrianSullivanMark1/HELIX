"""RestFleet - the CLOUD profile's FleetReader. NOT BUILT YET, and says so.

HELIX_MARK1_PLAN.md §7.2 names two readers: gcloud_fleet for the DESKTOP profile (the user's own
login, spawning the CLI) and this one for the CLOUD profile, where there is no gcloud and no user
login - the reads come through a REST adapter on a service account with run.viewer only (Phase 3).

Until Phase 3 this file exists so the container can compose the CLOUD profile honestly: the board
shows every cell as UNKNOWN with this one sentence, rather than the CLOUD profile silently getting
the desktop reader (which would spawn a CLI that is not there and report NOT_INSTALLED - true, but
the wrong truth).

In PROTECTED_FILES (§10.1): when this is built it will hold a credential path. Fixed to the dream now
so that the protection is in place before the code that needs it.
"""
from __future__ import annotations

import time
from typing import Sequence

from helix.domain.fleet import Service
from helix.ports.fleet import CellRead

NOT_BUILT = "The cloud fleet reader is not built yet (Phase 3) - nothing is read on this profile."


class RestFleet:
    def available(self) -> tuple[bool, str | None]:
        return False, NOT_BUILT

    def read_cell(self, service: Service, *, timeout_s: float = 30.0) -> CellRead:
        t0 = time.monotonic()
        if service.run_service is None:
            return CellRead(service=service, ok=True, api=None, site=None,
                            seconds=time.monotonic() - t0)
        return CellRead(service=service, ok=False, problem=NOT_BUILT,
                        seconds=time.monotonic() - t0)

    def read_all(self, services: Sequence[Service], *, timeout_s: float = 90.0) -> list[CellRead]:
        return [self.read_cell(s, timeout_s=timeout_s) for s in services]

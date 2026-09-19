"""The fleet — the four systems HELIX takes responsibility for, and the laws that govern touching them.

Pure data + pure validators, exactly like `constitution.py`, and for the same reason: the *rules* live
here so they are trivial to read, unit-test and audit, while *enforcement* lives in services/fleet.py.
Nothing in this module performs I/O, spawns gcloud, or imports another HELIX layer.

WHY THIS FILE IS IN PROTECTED_FILES (HELIX_MARK1_PLAN.md §10.1). `helix/domain/` sits in the
Constitution's EDITABLE_PREFIXES, so by default the nightly dream may rewrite anything here. This module
holds the sentence "production is human-only" and the flags that have previously cost a service its
database. A rule an autonomous process can edit on a Tuesday night is not a rule, so this path is added
to PROTECTED_FILES by hand. HELIX can no longer improve its own fleet code overnight. That is the trade,
taken deliberately.

Phase 1 ships this module with tests and NO CALLERS. That is also deliberate: when Phase 2 writes its
first deploy button, the thing it must fail closed against already exists and is already proven, rather
than being written in the same hour as the code it is supposed to restrain.

Contract: HELIX_MARK1_PLAN.md §6 (the domain), §10 (safety boundaries), §11 (the rules that must
survive, each mapped to a constant or a validator here).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Sequence

# --------------------------------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------------------------------


class Env(str, Enum):
    """One deployment environment. `str` mixin so a value serializes as itself into Firestore/JSON."""

    DEV = "dev"
    QA = "qa"
    PROD = "prod"


class Health(str, Enum):
    """Whether a thing is answering.

    ABSENT and UNKNOWN are the two that matter and are easy to get wrong:

    - ABSENT means the service DOES NOT EXIST and we know that (ECHO qa/prod today). It is a known
      absence, never an error, and §11 rule 5 forbids creating it.
    - UNKNOWN means WE COULD NOT FIND OUT. A cell we failed to read must never render as healthy; a
      status board that shows green when it is actually blind is worse than one that shows nothing.
    """

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class Drift(str, Enum):
    """How far what is RUNNING has moved from what the REPO says.

    ROLLED_BACK exists because without it every rollback renders as BEHIND, which is exactly backwards:
    one is a deliberate act somebody took an hour ago, the other is ordinary un-deployed work. A board
    that cannot tell them apart teaches people to ignore the state that matters most.
    """

    CLEAN = "clean"              # serving commit == repo HEAD for that branch
    BEHIND = "behind"            # serving an ancestor of HEAD — normal, un-deployed work
    AHEAD = "ahead"              # serving something not in the repo's history — investigate
    DIVERGED = "diverged"        # neither is an ancestor of the other — investigate
    ROLLED_BACK = "rolled_back"  # deliberately serving an older revision (see check_rollback)
    UNKNOWN = "unknown"          # one of the two sides could not be determined


# The states a human should be pushed to look at. Everything else is a resting state.
ATTENTION: frozenset[Drift] = frozenset({Drift.AHEAD, Drift.DIVERGED})


# --------------------------------------------------------------------------------------------------
# What a cell is made of
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Company:
    """A company (or account): one GCP project, one region, one GitHub owner, one production allowlist.

    THE SCALING AXIS (HELIX_MARK1_PLAN.md §17.2). Everything else in the fleet hangs off a company, so a
    second company is a second Company and its apps - the board iterates companies, then apps, and
    nothing above the domain changes. Oats Overnight is the first and, today, only one.

    Plain names on purpose (Brian, 2026-09-17): company / app / environment. The biology lives in the
    art, not the nouns.
    """

    id: str                      # "oats-overnight" - stable slug, used in Firestore paths and keys
    label: str                   # "Oats Overnight"
    gcp_project: str
    region: str
    github_owner: str            # default owner for repos; a Service may name a different one
    prod_allowlist: frozenset[str] = frozenset()

    def may_touch_production(self, identity: str | None) -> bool:
        """§10.2 condition 2 of four, scoped to THIS company. Fail-closed."""
        if not identity:
            return False
        return identity.strip().lower() in self.prod_allowlist


@dataclass(frozen=True)
class Serving:
    """One deployed HALF of a cell, and what it is running right now.

    A cell has TWO of these (HELIX_MARK1_PLAN.md §6.1): the Cloud Run API service and the Firebase
    Hosting site. They version independently and roll back independently, so "what commit is serving
    MES prod?" has two answers, and modelling only the API would make the board quietly wrong the first
    time a frontend was deployed without its backend. Both halves are modelled; either may be None.

    `commit` is None whenever provenance was never recorded — which today is ALWAYS, because deploys
    run `gcloud run deploy --source .` from a working tree and no commit is attached to the resulting
    revision (§6.6). None means "not recorded", never "same as the repo".

    `dirty` marks a revision built from a working tree with uncommitted edits. It matters more than the
    commit: such a revision is not reproducible from the repo, and showing a clean-looking SHA for it
    would be a lie in the one place the board exists to be honest.
    """

    revision: str | None = None          # Cloud Run revision name, or Hosting version id
    commit: str | None = None            # short SHA, when it was recorded at deploy time
    dirty: bool = False                  # built from a working tree with uncommitted changes
    deployed_at: datetime | None = None
    deployed_by: str | None = None
    health: Health = Health.UNKNOWN
    traffic_percent: int | None = None   # None = unknown; < 100 means a split is in play
    # What the app says about ITSELF, from its /api/health (the MES convention, seen live 2026-09-17:
    # {"db":"BRMS_database_dev","readOnly":false,"flags":{"appCheckRequired":false,...},"version":"3dc6631"}).
    # `db` is the one fact that proves an environment points at the database it is supposed to, and
    # `flags` carries appCheckRequired - the per-environment switch rule 3 says is flipped one row at
    # a time. Both are None/empty when the app has no health endpoint or it was unreachable.
    db: str | None = None
    read_only: bool | None = None
    flags: tuple[tuple[str, bool], ...] = ()   # sorted (name, value) pairs; tuple so Serving stays hashable

    def flag(self, name: str) -> bool | None:
        """One health flag by name, or None when the app did not report it."""
        for k, v in self.flags:
            if k == name:
                return v
        return None

    @property
    def is_split(self) -> bool:
        """True when this revision is NOT taking all the traffic. The console assumes a single 100%
        revision (dev.ps1 line 721) and so does everyone's mental model, so a split is worth surfacing
        rather than silently reporting one of several answers as the answer."""
        return self.traffic_percent is not None and self.traffic_percent < 100


@dataclass(frozen=True)
class Service:
    """One cell of the grid: a system in an environment, and the two things that serve it.

    `run_service` / `hosting_site` are None when that half does not exist. ECHO has no qa or prod today,
    so three of its cells are entirely absent — the code must handle that rather than assume a uniform
    4 x 3, and `exists` is how it asks.
    """

    system: str
    env: Env
    run_service: str | None
    hosting_site: str | None
    repo: str
    branch: str = "main"   # which branch this cell is SUPPOSED to deploy from (see BRANCH note below)
    company: str = "oats-overnight"   # Company.id this cell belongs to

    @property
    def app(self) -> str:
        """The plain word for `system` (§17.2). `system` stays as the field name for history."""
        return self.system

    @property
    def key(self) -> str:
        """Stable id for Firestore documents, event payloads and log lines: 'oats-overnight/MES/prod'.
        Company-prefixed so two companies with an app called MES can never collide."""
        return f"{self.company}/{self.system}/{self.env.value}"

    @property
    def exists(self) -> bool:
        return self.run_service is not None or self.hosting_site is not None


@dataclass(frozen=True)
class Cell:
    """What THE STRAND renders for one service: both halves, the worst of their states, and one plain
    sentence when something is odd.

    `checked_at` is not decoration. A board that cannot say how stale it is will be trusted when it
    should not be, so every cell carries when it was last actually read.
    """

    service: Service
    api: Serving | None = None
    site: Serving | None = None
    repo_commit: str | None = None       # HEAD of service.branch, when we could read it
    drift: Drift = Drift.UNKNOWN
    behind_by: int | None = None         # commits behind HEAD, when countable
    checked_at: datetime | None = None
    note: str | None = None              # ONE plain sentence a human may read; ASCII, no paths
    repo_ok: bool | None = None          # did the repo answer for this cell's HEAD (None: not asked)

    @property
    def health(self) -> Health:
        """The worse of the two halves. A cell with a healthy API and a dead frontend is not healthy,
        and a cell we could not read is UNKNOWN rather than anything more comfortable."""
        if not self.service.exists:
            return Health.ABSENT
        halves = [h.health for h in (self.api, self.site) if h is not None]
        if not halves:
            return Health.UNKNOWN
        for worst in (Health.DOWN, Health.DEGRADED, Health.UNKNOWN, Health.ABSENT):
            if worst in halves:
                return worst
        return Health.OK

    @property
    def needs_attention(self) -> bool:
        return self.drift in ATTENTION or self.health in (Health.DOWN, Health.DEGRADED)


# --------------------------------------------------------------------------------------------------
# The fleet table — the single source of truth for the fleet's SHAPE
# --------------------------------------------------------------------------------------------------

MES_REPO = "BrendanSullivanMark1/BRMS_MES_WEB_VERSION"
WMS_REPO = "Alex-Mark1/WMS_V1"
MRP_REPO = "BrendanSullivanMark1/mrp_prod"
ECHO_REPO = "BrendanSullivanMark1/MES_OATS_DASHBOARD"

OATS_OVERNIGHT = Company(
    id="oats-overnight",
    label="Oats Overnight",
    gcp_project="windy-celerity-392822",
    region="us-west2",
    github_owner="BrendanSullivanMark1",
    prod_allowlist=frozenset({
        "brian_sullivan@mark1online.com",
        "brendan_sullivan@mark1online.com",
    }),
)

COMPANIES: tuple[Company, ...] = (OATS_OVERNIGHT,)


def company(company_id: str) -> Company | None:
    for c in COMPANIES:
        if c.id == company_id:
            return c
    return None


def apps(company_id: str) -> tuple[str, ...]:
    """The distinct app names in one company, in table order. The board's card list."""
    seen: list[str] = []
    for svc in FLEET:
        if svc.company == company_id and svc.system not in seen:
            seen.append(svc.system)
    return tuple(seen)


# BRANCH. There is no branch-per-environment convention to discover: dev.ps1's Git-Branch (line 1021)
# returns whatever branch is checked out and defaults to 'main', and deploys run from the working tree.
# So every cell compares against 'main' by default, and `branch` is per-cell so one environment can be
# pointed elsewhere later without touching any code but this table.
FLEET: tuple[Service, ...] = (
    # MES. Note prod drops the suffix entirely — there is no derivable naming rule for prod across the
    # fleet, which is exactly why this is a table and not a format string.
    Service("MES", Env.DEV, "brms-mes-api-dev", "oo-mes-dev", MES_REPO),
    Service("MES", Env.QA, "brms-mes-api-qa", "oo-mes-qa", MES_REPO),
    Service("MES", Env.PROD, "brms-mes-api", "oo-mes", MES_REPO),
    # WMS + MRP: every environment carries '-flask' (dev.ps1: ApiSvc="wms-$Env-flask"). The brief
    # had dev/qa as bare 'wms-dev' / 'mrp-dev'; the first live read (2026-09-17) came back ABSENT
    # for all four, and dev.ps1 settled it. The table is the truth; it was corrected, not patched
    # around.
    Service("WMS", Env.DEV, "wms-dev-flask", "oats-overnight-wms-dev", WMS_REPO),
    Service("WMS", Env.QA, "wms-qa-flask", "oats-overnight-wms-qa", WMS_REPO),
    Service("WMS", Env.PROD, "wms-prod-flask", "oats-overnight-wms-prod", WMS_REPO),
    Service("MRP", Env.DEV, "mrp-dev-flask", "oats-overnight-mrp-dev", MRP_REPO),
    Service("MRP", Env.QA, "mrp-qa-flask", "oats-overnight-mrp-qa", MRP_REPO),
    Service("MRP", Env.PROD, "mrp-prod-flask", "oats-overnight-mrp-prod", MRP_REPO),
    # ECHO is dev-only today. Its qa and prod cells DO NOT EXIST and are never auto-created (rule 5).
    Service("ECHO", Env.DEV, "brms-echo-api-dev",
            "manufacturing-execution-system-mes-dashboard-dev", ECHO_REPO),
    Service("ECHO", Env.QA, None, None, ECHO_REPO),
    Service("ECHO", Env.PROD, None, None, ECHO_REPO),
)

SYSTEMS: tuple[str, ...] = ("MES", "WMS", "MRP", "ECHO")

GCP_PROJECT = "windy-celerity-392822"
GCP_REGION = "us-west2"
SQL_INSTANCE = "oats-overnight-live"
VPC_CONNECTOR = "brms-egress"


def find(system: str, env: Env) -> Service | None:
    """One cell by system and environment. None when the pair is not in the fleet at all (as opposed to
    a cell that exists but is not deployed — that is a Service with run_service None)."""
    wanted = str(system).strip().upper()
    for svc in FLEET:
        if svc.system == wanted and svc.env is env:
            return svc
    return None


def services_for(system: str) -> tuple[Service, ...]:
    wanted = str(system).strip().upper()
    return tuple(s for s in FLEET if s.system == wanted)


def deployable() -> tuple[Service, ...]:
    """Only the cells that actually exist. What a deploy/rollback menu may offer."""
    return tuple(s for s in FLEET if s.exists)


# --------------------------------------------------------------------------------------------------
# The laws (HELIX_MARK1_PLAN.md §11) — each one a scar from the existing console
# --------------------------------------------------------------------------------------------------

# RULE 1. A WMS or MRP deploy swaps the image and NOTHING else. Passing any of these re-specifies the
# service's shape, and a deploy that omits one it previously had silently drops it — which is how a
# service loses its route to a private database. dev.ps1's source-deploy path passes none of them and
# says so in a comment ("account and scaling are NOT touched"). This encodes that comment as a rule.
FORBIDDEN_DEPLOY_FLAGS: dict[str, frozenset[str]] = {
    "WMS": frozenset({"--set-env-vars", "--vpc-connector", "--service-account"}),
    "MRP": frozenset({"--set-env-vars", "--vpc-connector", "--service-account"}),
}

# RULE 2. MES env vars have exactly ONE source of truth. MES deliberately DOES pass --set-env-vars, but
# only from this script — never assembled at a call site, or the two definitions drift and the drift is
# invisible until a container starts without a variable it needed.
MES_ENV_SOURCE = "backend/deploy.ps1"

# RULE 4 + §10.2. Production is human-only and is never a target for anything automated.
PROD_ENVS: frozenset[Env] = frozenset({Env.PROD})

# RULE 4, the two things prod refuses outright regardless of who is asking.
PROD_REFUSED_ACTIONS: frozenset[str] = frozenset({"local-run", "clear-records"})

# §10.3. Plant data is never written, in ANY environment, including dev. There is deliberately no
# parameter that turns this off, because a parameter that turns it off is a parameter somebody passes
# True at 2 a.m.
PLANT_DATA_IS_READ_ONLY = True

# §10.7. Who may act on production, day one. Adding someone is a deliberate act with a LINEAGE row;
# there is no PIN path to production (a PIN grants dev/QA only).
PROD_ALLOWLIST: frozenset[str] = OATS_OVERNIGHT.prod_allowlist   # the first company's; see Company

# §6.5. How far back a rollback can reach. NOT a display preference: dev.ps1's Run-CleanRevisions keeps
# the 5 newest revisions per service and DELETES the rest, so this is a hard floor set by what still
# exists in Cloud Run. Settings may raise it; the live revision list always wins.
DEFAULT_ROLLBACK_DEPTH = 5


def check_deploy(system: str, env: Env, flags: Sequence[str] = (), *,
                 action: str = "deploy", service_exists: bool = True) -> list[str]:
    """Gate one deploy. Empty list means allowed; any string is a plain-language refusal.

    Refusals are ASCII and readable aloud, because they are what the orb says back to a human. Every
    problem is collected rather than short-circuiting, so one call tells you everything wrong with a
    request instead of making you fix it one round-trip at a time.
    """
    problems: list[str] = []
    sys_name = str(system).strip().upper()

    # Rule 5 — a service that does not exist is refused, never created. Checked first: everything else
    # is moot if there is nothing to deploy to.
    svc = find(sys_name, env)
    if svc is None:
        problems.append(f"{sys_name} {env.value} is not part of the fleet.")
        return problems
    if not svc.exists or not service_exists:
        problems.append(
            f"{sys_name} {env.value} does not exist. I will not create it - "
            f"a production service is made deliberately, by a human, in the Cloud Console."
        )
        return problems

    # Rule 4 — prod refuses these outright, before any other consideration.
    if env in PROD_ENVS and action in PROD_REFUSED_ACTIONS:
        problems.append(f"{action} is refused on production, always.")

    # Rule 1 — the flags that cost a service its database.
    forbidden = FORBIDDEN_DEPLOY_FLAGS.get(sys_name, frozenset())
    for flag in flags:
        name = str(flag).split("=", 1)[0].strip()
        if name in forbidden:
            problems.append(
                f"{name} must not be passed on a {sys_name} deploy. A deploy swaps the image and "
                f"nothing else, or the service loses its database."
            )

    # Rule 2 — MES env vars come from one place.
    if sys_name == "MES":
        for flag in flags:
            if str(flag).split("=", 1)[0].strip() == "--set-env-vars":
                problems.append(
                    f"MES environment variables have one source of truth: {MES_ENV_SOURCE}. "
                    f"Change them there, not here."
                )

    return problems


def check_rollback(system: str, env: Env, to_revision: str,
                   served: Sequence[str], currently_serving: str | None = None) -> list[str]:
    """Gate one rollback. Empty list means allowed.

    A rollback is a traffic shift to a revision that ALREADY EXISTS and ALREADY RAN — no build, no new
    image, no flags, so rule 1 does not even apply to it. That is what makes it safer than the thing it
    undoes, and why it ships alongside the first deploy button rather than after it.

    The one precondition a deploy does not have: the target must have actually served traffic. Rolling
    back to a revision that was never live is a deploy wearing a safer word.
    """
    problems: list[str] = []
    sys_name = str(system).strip().upper()
    target = str(to_revision).strip()

    svc = find(sys_name, env)
    if svc is None or not svc.exists:
        problems.append(f"{sys_name} {env.value} does not exist, so there is nothing to roll back.")
        return problems
    if not target:
        problems.append("No revision was named to roll back to.")
        return problems
    if target not in set(served):
        problems.append(
            f"{target} has never served traffic on {sys_name} {env.value}, so it is not a rollback "
            f"target. Rolling back only ever returns to a version that was live."
        )
    if currently_serving is not None and target == currently_serving:
        problems.append(f"{target} is already serving {sys_name} {env.value}. Nothing to do.")
    return problems


def rollback_targets(served: Sequence[str], currently_serving: str | None = None,
                     depth: int = DEFAULT_ROLLBACK_DEPTH) -> tuple[str, ...]:
    """The revisions a rollback may offer, newest first, excluding the one already serving.

    `served` must be the LIVE revision list, newest first — never a cached one. Old revisions are
    deleted by the console's cleanup action, so a cached list will happily offer a target that no
    longer exists.
    """
    out = [r for r in served if r and r != currently_serving]
    return tuple(out[:max(0, int(depth))])


def may_touch_production(identity: str | None) -> bool:
    """§10.2 condition 2. Fail-closed: no identity, or one not on the list, is a no.

    This is ONE of four required conditions, never the whole gate. The others (the DESKTOP profile, a
    typed confirmation naming the system and environment, and an audit row written BEFORE the action)
    are enforced by the caller, and none of them is derivable from this one.
    """
    if not identity:
        return False
    return identity.strip().lower() in PROD_ALLOWLIST


# --------------------------------------------------------------------------------------------------
# Rule 7 — ASCII only
# --------------------------------------------------------------------------------------------------

def is_ascii(text: str) -> bool:
    return all(ord(ch) < 128 for ch in str(text))


def non_ascii(text: str) -> tuple[tuple[int, str], ...]:
    """Every offending character as (index, character). Returning positions rather than a bare bool is
    the difference between a test that says 'this fails' and one that says where to look — an em dash
    that a writing tool inserted is invisible until something points at its column."""
    return tuple((i, ch) for i, ch in enumerate(str(text)) if ord(ch) >= 128)


def assert_ascii(text: str, what: str = "output") -> None:
    """Raise if `text` is not pure ASCII. Applied to every generated script and every console line.

    Rule 7 exists because a non-ASCII character in a .ps1, or printed to a Windows console under a
    legacy code page, breaks the script on someone else's machine in a way that looks like a completely
    unrelated failure. It is the rule most likely to be broken by a well-meaning em dash, which is why
    the check runs against GENERATED OUTPUT rather than against source.
    """
    bad = non_ascii(text)
    if bad:
        index, ch = bad[0]
        raise ValueError(
            f"{what} must be ASCII only (rule 7): character {ch!r} at position {index}."
        )


# --------------------------------------------------------------------------------------------------
# Drift
# --------------------------------------------------------------------------------------------------

def drift_from(repo_commit: str | None, serving_commit: str | None, *,
               behind_by: int | None = None, rolled_back: bool = False,
               in_history: bool | None = None) -> Drift:
    """Classify one cell's drift from the two commits and what we know about their relationship.

    `in_history` answers "is the serving commit an ancestor of the repo's HEAD?" — True (ordinary
    un-deployed work), False (serving something the branch does not contain), or None (we could not
    tell). It is a parameter rather than something computed here because ancestry needs a repo, and this
    module does no I/O.

    UNKNOWN is returned generously and on purpose. Today `serving_commit` is None for every revision in
    the fleet (§6.6: deploys are --source from a working tree, so nothing records a commit), and the
    honest answer to "has it drifted?" is that we cannot say. Guessing CLEAN here would make the whole
    board a lie the day it shipped.
    """
    if rolled_back:
        return Drift.ROLLED_BACK
    if not repo_commit or not serving_commit:
        return Drift.UNKNOWN
    if repo_commit == serving_commit:
        return Drift.CLEAN
    if in_history is True:
        return Drift.BEHIND
    if in_history is False:
        return Drift.DIVERGED if behind_by else Drift.AHEAD
    return Drift.UNKNOWN

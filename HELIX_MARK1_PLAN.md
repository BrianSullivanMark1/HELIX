# HELIX — Mark1 Plan

**The plan for turning HELIX into the one place Mark1 builds, ships, watches and fixes every system the
company runs.**

```
Written 2026-09-07 · branch: mark1 (off main) · against HELIX v3.0.0
Status: DRAFT — nothing in this document has been built or run yet.
```

> **How to read this.** Sections are numbered because the source will cite them by number as contracts,
> exactly the way `helix/services/dream.py` cites `READ_ME/DREAM.md §3`. If you change a rule here,
> the code that cites it is wrong until someone fixes it.
>
> **Before writing Phase 1 code, read §4 (packs), §5 (profiles) and §11 (the rules that must survive).**
>
> Every file named below is marked **[NEW]**, **[EDIT]** or **[READ]**. Anything unmarked already exists
> and is untouched.

---

## §1 — What HELIX becomes

HELIX today is a personal presence: it converses, sees, remembers, makes things, and rewrites its own
source overnight. That stays. Nothing in this plan removes a faculty.

What we add is a second thing HELIX is responsible for: **the fleet**. Four systems, three environments
each, one GCP project. Today the truth about those twelve services lives in a 69,000-line PowerShell
console, in the GCP console's web UI, and in Brian's head. After this plan it lives in one place that
can be asked in plain language, watched without being asked, and eventually acted on — with the human
still holding every lever that matters.

The one-line version: **apps are the new files, and HELIX is the file manager.**

Three things this is not:

- **It is not a replacement for ECHO.** ECHO is a product Mark1 builds *for* Oats Overnight. HELIX is
  what builds and maintains ECHO. They are different objects with different users.
- **It is not a replacement for `dev.ps1`.** The MES console is wrapped, never rewritten. It keeps
  working through every phase, and it stays the break-glass path when HELIX is wrong or down (§10.6).
- **It is not a new deploy pipeline.** Deploys run on Cloud Build with scoped service accounts. HELIX
  asks Cloud Build to do things and reads what happened. It does not become the thing that deploys.

---

## §2 — The fleet

One GCP project, `windy-celerity-392822`, region `us-west2`. One private Cloud SQL instance,
`oats-overnight-live`. One VPC connector, `brms-egress`.

| System | Repo | Cloud Run services | Firebase Hosting sites |
|---|---|---|---|
| **MES** | `BrendanSullivanMark1/BRMS_MES_WEB_VERSION` | `brms-mes-api-dev` · `brms-mes-api-qa` · `brms-mes-api` | `oo-mes-dev` · `oo-mes-qa` · `oo-mes` |
| **WMS** | `Alex-Mark1/WMS_V1` | `wms-dev-flask` · `wms-qa-flask` · `wms-prod-flask` | `oats-overnight-wms-dev` · `-qa` · `-prod` |
| **MRP** | `BrendanSullivanMark1/mrp_prod` | `mrp-dev-flask` · `mrp-qa-flask` · `mrp-prod-flask` | `oats-overnight-mrp-dev` · `-qa` · `-prod` |
| **ECHO** | `BrendanSullivanMark1/MES_OATS_DASHBOARD` | `brms-echo-api-dev` | `manufacturing-execution-system-mes-dashboard-dev` |

Note the two irregularities the code must handle rather than assume away:

1. **Service names are not uniform.** MES prod drops the suffix (`brms-mes-api`); WMS and MRP carry
   `-flask` in *every* environment (`dev.ps1`: `ApiSvc="wms-$Env-flask"`). There is no derivable rule.
   The mapping is a table, not a format string (§6.2). *Corrected 2026-09-17: the brief had dev/qa as
   bare `wms-dev` / `mrp-dev`; the first live read came back ABSENT for all four and `dev.ps1` settled it.*
2. **ECHO is dev-only today.** Three of its twelve cells do not exist. A missing service is a *known
   absence*, rendered as "not deployed", never as an error and never auto-created (§11.5).

This table is the single source of truth for the fleet's shape and lives in `helix/domain/fleet.py`
(§6.2). Adding a fifth system is a change to that table and nothing else.

---

## §3 — Where the new code goes

HELIX is hexagonal and the dependency rule is absolute: `ui -> services -> ports <- adapters`, everything
may depend on `domain`, the domain depends on nothing. The fleet work obeys it exactly.

```
web/src/pages/Board.tsx           [NEW]  the Board — the read-only fleet page (cards + search)
   |
helix/api/server.py               [EDIT] +4 routes under /api/fleet/
   |
helix/services/fleet.py           [NEW]  the use case: poll, diff, cache, publish
   | depends on Protocols
helix/ports/fleet.py              [NEW]  FleetReader + FleetState contracts
   ^ implemented by
helix/adapters/gcloud_fleet.py    [NEW]  gcloud CLI  (DESKTOP)
helix/adapters/github_fleet.py    [NEW]  GitHub API  (repo HEAD, for drift)
helix/adapters/firestore_state.py [NEW]  shared fleet state
   |
helix/domain/fleet.py             [NEW]  pure: the fleet table, Cell, Drift, Health, the deploy laws
helix/domain/packs.py             [NEW]  pure: the pack system (§4)
helix/domain/runtime_profile.py   [NEW]  pure: DESKTOP / CLOUD and the tool allowlist (§5)
   |
helix/app/container.py            [EDIT] the only place an adapter is constructed
```

**Three consequences of the Constitution that shape all of the above.** (`helix/domain/constitution.py`
is the authority; this is what it means for us.)

1. `helix/ports/` is a `PROTECTED_PREFIX`. Putting the fleet contract in `ports/fleet.py` means **the
   nightly dream can never rewrite it**. That is the point. The seam HELIX reaches the fleet through is
   not part of the growable brain.
2. `helix/app/` is a `PROTECTED_PREFIX`. `container.py` is where the profile decides which adapter gets
   built, so **the dream cannot rewire which fleet adapter is live**. Also the point.
3. `helix/domain/` **is** editable. So a rule that merely sits in `domain/fleet.py` is a rule the dream
   may rewrite on a Tuesday night. Every safety-bearing constant in this plan therefore needs its file
   added to `PROTECTED_FILES` by hand. That is decision §10.1 and it is the single most important line
   in this document.

### §3.1 — A naming collision to avoid

`helix/services/profile.py` already exists: it is the *distilled user profile* (who is speaking, what
they like). It has nothing to do with DESKTOP vs CLOUD.

So the runtime profile does **not** go in a file called `profile.py`. It goes in
`helix/domain/runtime_profile.py`, and the enum is `HelixProfile`. Anyone who greps for "profile" will
find two unrelated things; the module names are what keep them apart.

### §3.2 — A stale contract worth knowing about

`helix/ports/cad.py`'s docstring describes OpenSCAD, `model.scad`, and an adapter
`helix/adapters/openscad_cli.py`. The code actually runs build123d, `model.py`, and
`helix/adapters/build123d_cad.py`. The docstring is stale.

It matters here only because `ports/` is protected: **the dream cannot fix it.** Only a human can. It is
listed in §15 as a one-line chore, not because it breaks anything today.

---

## §4 — The pack system

> **Read this before writing Phase 1 code.**

### §4.1 — What a pack is

A **pack** is a named bundle of capability that can be switched on or off as a unit: some tools, some
services it needs wired, some pages in the face, and some scheduled work. Core HELIX is not a pack —
core is always on and cannot be disabled.

| Pack | What it carries |
|---|---|
| *(core)* | Converse, remember, files, research, reminders, self-improvement, the dream. Always on. |
| `fleet` | The fleet reads and (from Phase 2) the deploy lane. the board and everything after it. |
| `maker` | Components, the enclosure generator, holograms, AR fit check, the printer. |
| `vision` | Screen capture, webcam, AR annotation, camera measurement. |
| `purchasing` | Amazon search, cart staging, parts lists. |

The split is not cosmetic. It is what lets the CLOUD profile (§5) be a genuinely small thing rather than
all of HELIX with some buttons hidden.

### §4.2 — Where it lives

`helix/domain/packs.py` **[NEW]** — pure data plus pure validators, the same shape as
`domain/constitution.py`. No I/O, no imports from any other HELIX layer.

```python
@dataclass(frozen=True)
class Pack:
    id: str                     # "fleet"
    label: str                  # "Fleet"
    setting: str                # "pack_fleet" — the settings key that switches it
    default_on: bool            # what a fresh install gets
    tools: frozenset[str]       # tool names this pack contributes
    pages: frozenset[str]       # face pages this pack contributes ("strand")

PACKS: tuple[Pack, ...] = (...)
CORE_TOOLS: frozenset[str] = ...        # never gated by any pack

def enabled(settings_get) -> frozenset[str]: ...          # which pack ids are on
def tools_for(pack_ids) -> frozenset[str]: ...            # CORE_TOOLS | each pack's tools
def pages_for(pack_ids) -> frozenset[str]: ...
def unknown_tools(all_registered) -> frozenset[str]: ...   # registry names no pack claims
```

`unknown_tools` is the fail-closed hinge and deserves a sentence. If someone adds a tool to
`services/tools.py` and forgets to put it in a pack, we must not silently allow it everywhere. A test
(§4.5) asserts `unknown_tools()` is empty, so forgetting is a red test rather than a quiet hole.

### §4.3 — Settings keys

Four booleans, written by the Settings card, read live:

```
pack_fleet       default: True   on DESKTOP
pack_maker       default: True
pack_vision      default: True
pack_purchasing  default: True
```

They join `_SETTING_KEYS` in `helix/api/server.py` so `GET/PUT /api/settings` carry them, and get a
card in `web/src/pages/Settings.tsx` **[EDIT]**. They are ordinary settings — *not* in `LOCKED_SETTINGS`
— because turning your own maker pack off is your business.

**But**: on the CLOUD profile the pack toggles are ignored entirely (§5.3). A setting a remote user can
write must never widen what that user can reach.

### §4.4 — Where the filter is applied

Tool filtering already happens in exactly one place — `helix/services/conversation.py`, which subtracts
`BUILD_TOOLS` from what an unattended agent may see. Pack filtering joins it there **[EDIT]**, composing
as a plain intersection:

```
visible = registered
          & packs.tools_for(enabled_packs)      # §4
          & profile.allowed_tools()             # §5 — CLOUD is a hard allowlist
          - (BUILD_TOOLS if unattended else ∅)  # the existing fence, unchanged
```

Order does not matter; they are all intersections and one subtraction. What matters is that **all four
are applied at the same point**, so there is one line to read when asking "why can't it see that tool?".

### §4.5 — Tests that pin this

`tests/test_packs.py` **[NEW]**

1. Every tool registered in `services/tools.py` is claimed by exactly one pack or by `CORE_TOOLS`
   (`unknown_tools()` is empty). Catches the forgotten-tool hole.
2. No tool is claimed by two packs.
3. Disabling every pack leaves exactly `CORE_TOOLS` visible — and HELIX still converses.
4. `BUILD_TOOLS` filtering still behaves exactly as it does today when all packs are on. **Pack work
   must not change the unattended-agent fence by accident**; this test is the proof.

---

## §5 — The two profiles

> **Read this before writing Phase 1 code.**

### §5.1 — Two profiles, one codebase

| | **DESKTOP** | **CLOUD** |
|---|---|---|
| Who | Brian, on his machine | the team, in a browser |
| Packs | all of them, by setting | `fleet` only, not settable |
| Self-modification | yes | **never** |
| The dream | yes | **never** |
| File tools | yes | **never** |
| Shell / desktop control | yes | **never** |
| Fleet writes | per phase | read-only until Phase 5 says otherwise |
| Identity | the machine | Firebase Auth |

One codebase, because two codebases drift and the drift is always in the safety direction you did not
want. One build, one test suite, one Constitution.

### §5.2 — Where it lives

`helix/domain/runtime_profile.py` **[NEW]** — pure.

```python
class HelixProfile(str, Enum):
    DESKTOP = "desktop"
    CLOUD = "cloud"

CLOUD_TOOLS: frozenset[str] = frozenset({...})   # the explicit, exhaustive list

def allowed_tools(profile: HelixProfile, pack_tools: frozenset[str]) -> frozenset[str]:
    if profile is HelixProfile.DESKTOP:
        return pack_tools
    return CLOUD_TOOLS & pack_tools              # fail-closed: intersection, never union
```

`CLOUD_TOOLS` is **written out by name**. Not "everything except", not a prefix rule, not derived from a
pack. A new tool is invisible on CLOUD until a human types its name into that frozenset. That is the
whole design: **the failure mode of forgetting is that a feature is missing, never that a door is open.**

### §5.3 — How the profile is decided

In `helix/app/container.py` **[EDIT]** — the composition root, and a protected file, so the dream cannot
move this decision:

```
HELIX_PROFILE env var, if set and valid          -> that profile
else                                             -> DESKTOP
```

Read once, at wiring time, into `self.profile`. Never read from settings (a settings file is writable by
anything with disk access) and never from a request header (trivially forged). A CLOUD deployment sets
`HELIX_PROFILE=cloud` in its Cloud Run service definition and nothing inside the process can change it.

An unrecognised value is not a warning — it **refuses to start**. A typo'd profile that silently falls
back to DESKTOP in the cloud is precisely the accident this plan exists to prevent.

### §5.4 — The second gate

The intersection in §4.4 happens in `services/conversation.py`, which is editable code. So there is a
second, independent check at dispatch time in the tool registry: **before a tool function is called**,
the registry re-asks `allowed_tools()`. A tool that is not allowed does not run, no matter what reached
the dispatcher.

Two gates, both fail-closed, in two layers, is the same belt-and-braces the Forge already uses (confirm
the spend, then scan for escapes). One gate is a bug away from nothing.

### §5.5 — What CLOUD does not get, restated as code

Even with `pack_fleet` on, the CLOUD profile's container **does not construct** the self-dev service, the
dream service, the file service's write path, the desktop service, or the coder. Not "constructs them and
hides them" — does not build them. An unbuilt object cannot be called by a bug.

### §5.6 — Tests that pin this

`tests/test_profile.py` **[NEW]**

1. `CLOUD_TOOLS` contains no tool in `BUILD_TOOLS` that writes, spends, builds or self-modifies.
   Enumerated explicitly so adding one is a red test.
2. `allowed_tools(CLOUD, everything)` never returns a tool absent from `CLOUD_TOOLS`.
3. A container built with `HELIX_PROFILE=cloud` has `selfdev`, `dream`, `coder` and `desktop` as `None`.
4. An invalid `HELIX_PROFILE` raises at construction.

---

## §6 — The fleet domain

`helix/domain/fleet.py` **[NEW]** — pure. No network, no gcloud, no imports outside the stdlib.

### §6.1 — The types

```python
class Env(str, Enum):      DEV = "dev"; QA = "qa"; PROD = "prod"
class Health(str, Enum):   OK; DEGRADED; DOWN; ABSENT; UNKNOWN

@dataclass(frozen=True)
class Service:                    # one cell of the 4x3 grid
    system: str                   # "MES"
    env: Env
    run_service: str | None       # None = this cell does not exist (ECHO qa/prod)
    hosting_site: str | None
    repo: str                     # "BrendanSullivanMark1/BRMS_MES_WEB_VERSION"

@dataclass(frozen=True)
class Cell:                       # what the board renders for one service
    service: Service
    serving_sha: str | None       # the commit actually running
    repo_sha: str | None          # the repo's HEAD for that branch
    drift: Drift
    health: Health
    revision: str | None          # the Cloud Run revision name
    deployed_at: datetime | None
    deployed_by: str | None
    checked_at: datetime
    note: str | None              # one plain sentence when something is odd
```

**One open modelling problem, flagged rather than buried (§15.11).** Every cell has *two* things serving:
a Cloud Run API service and a Firebase Hosting site, each with its own versions and its own rollback.
`Cell` above models only the Cloud Run half. Either `Cell` grows a second serving pair, or a cell is
declared to mean the API only and the frontend gets its own row. Phase 1 cannot ship honestly until this
is decided, because "what commit is serving MES prod?" currently has two answers.

`Health.ABSENT` is a first-class value, not an error. `Health.UNKNOWN` means *we could not find out* and
must never render as green. A cell we failed to read is not a healthy cell.

### §6.2 — The fleet table

The §2 table, as data, with the two irregularities encoded rather than derived:

```python
FLEET: tuple[Service, ...] = (
    Service("MES", Env.DEV,  "brms-mes-api-dev", "oo-mes-dev", MES_REPO),
    ...
    Service("MES", Env.PROD, "brms-mes-api",     "oo-mes",     MES_REPO),   # no suffix
    Service("WMS", Env.PROD, "wms-prod-flask",   "...-prod",   WMS_REPO),   # -prod-flask
    ...
    Service("ECHO", Env.QA,   None, None, ECHO_REPO),   # does not exist
    Service("ECHO", Env.PROD, None, None, ECHO_REPO),   # does not exist
)
```

### §6.3 — Drift

```python
class Drift(str, Enum):
    CLEAN    = "clean"      # serving_sha == repo_sha
    BEHIND   = "behind"     # serving_sha is an ancestor of repo_sha — normal, un-deployed work
    AHEAD    = "ahead"      # serving something not in the repo's history — investigate
    DIVERGED = "diverged"   # neither is an ancestor of the other — investigate
    UNKNOWN  = "unknown"    # we could not determine one of the two
```

`ROLLED_BACK` exists because without it **every rollback renders as `BEHIND`**, which is exactly backwards:
one is a deliberate act somebody took an hour ago, the other is ordinary un-deployed work. A grid that
cannot tell them apart teaches people to ignore the state that matters. `Cell` therefore also carries
`rolled_back_at`, `rolled_back_by` and `rolled_back_from` — set from the LINEAGE record, not inferred.

`BEHIND` with a count ("11 commits behind") is the normal working state of dev and is rendered calmly.
`AHEAD` and `DIVERGED` are the interesting ones and are what the board exists to surface: they mean
something was deployed from somewhere other than the branch we think we deploy from.

### §6.4 — The deploy laws, inert but present

The seven console rules (§11) become constants and pure validators here in Phase 1, **before there is
anything to validate**, so that Phase 2's first deploy button has something to fail closed against on
the day it is written rather than the week after:

```python
FORBIDDEN_DEPLOY_FLAGS: dict[str, frozenset[str]] = {
    "WMS": frozenset({"--set-env-vars", "--vpc-connector", "--service-account"}),
    "MRP": frozenset({"--set-env-vars", "--vpc-connector", "--service-account"}),
}
PROD_ENVS: frozenset[Env] = frozenset({Env.PROD})
PLANT_DATA_IS_READ_ONLY = True          # every environment, no exception (§11.8)

def check_deploy(system: str, env: Env, flags: Sequence[str]) -> list[str]:
    """Empty list = allowed. Any string = a plain-language refusal."""
```

Phase 1 ships these with tests and no callers. That is deliberate.

### §6.5 — Rollback

**Rollback is not a deploy, and the difference is load-bearing.**

Cloud Run revisions are immutable. Rolling back means shifting traffic to a revision that already exists
and already ran — no build, no new image, seconds rather than minutes. Three things follow:

1. **§11 rule 1 does not apply to it.** A rollback passes no flags at all, so there is no
   `--set-env-vars` to forbid. It cannot lose a service its database, because it changes nothing about
   the service except which revision receives traffic.
2. **It is strictly safer than the thing it undoes.** So it ships in **Phase 2, in the same commit as the
   first deploy button** — never after. "You can undo it" is the property that makes deploying safe, and
   shipping deploy without undo is shipping the dangerous half first.
3. **It has a hard precondition deploy does not**: the target must be a revision that **actually served
   traffic**. Rolling back to a revision that was never live is a deploy wearing a safer word, and
   `check_rollback()` refuses it.

**The model is the Forge's, copied deliberately.** `helix/ports/repo.py` already defines
`revert_to` as *"restore the tree to `sha` and commit it forward as a new version, so newer commits stay
in history and the revert is itself undoable"*, and `domain/models.py` calls a `Version` *"one entry in
the Archive — a git commit indexed for restore."* Commandment 6 is *"I keep every version; a bad change
rolls back in one step."*

Fleet rollback is that same idea against Cloud Run instead of git:

```python
@dataclass(frozen=True)
class Rollback:
    service: Service
    to_revision: str          # must appear in served_revisions(service)
    from_revision: str
    reason: str               # required, free text, goes in the LINEAGE row
    at: datetime
    by: str

def check_rollback(service, to_revision, served: Sequence[str]) -> list[str]:
    """Empty list = allowed. Refuses: a revision that never served, a service that does not exist,
    a target equal to what is already serving."""
```

- **Recorded forward, never rewritten.** A rollback appends a LINEAGE row like any other action. History
  is not edited. **A rollback can therefore be rolled back**, which is the same guarantee the Forge gives
  a built app.
- **Same gate, shortest path.** In production a rollback needs the same four conditions as a deploy
  (§10.2) — it still changes what customers touch. But it is **never queued behind another action and
  never rate-limited**, and its typed confirmation is the service name alone rather than
  system-plus-environment. A gate people route around at 2 a.m. is worse than no gate, and the whole
  value of LINEAGE is that the emergency path runs through it too.
- **`fleet_rollback` is a `BUILD_TOOLS` tool** from the day it is written, and is **not** in `CLOUD_TOOLS`.

**Rollback depth is five, and it is enforced by deletion, not by display.** `dev.ps1`'s
`Run-CleanRevisions` (menu item 15) keeps the five newest revisions per service and **deletes the rest**.
So the reach of a rollback is not a HELIX setting that shows more history — it is a hard floor set by
what still exists in Cloud Run.

Two requirements follow:

- `fleet_rollback_depth` (default 5, adjustable in Settings) governs what the board *offers*, and can
  never offer a revision that has been deleted. It reads the live revision list, never a cached one.
- **Cleaning revisions destroys rollback targets, and HELIX must say so before doing it.** If HELIX ever
  wraps that cleanup action, its confirmation names the oldest revision that will stop being reachable.
  Silently shortening how far back you can recover is the worst possible side effect of a tidy-up.

### §6.6 — The provenance problem (read this before building the board)

**the board's first promise — "what commit is serving?" — cannot be kept against the way deploys
actually happen today.** Found by reading `dev.ps1` line 846:

```powershell
gcloud run deploy $ApiSvcDev --source . --project $Project --region $Region --allow-unauthenticated
```

`--source .` builds an image from **the working directory**, not from a git ref. Cloud Build uploads a
tarball; no commit SHA is attached to the resulting revision, and a working tree with uncommitted edits
deploys those edits. So for every revision that exists right now, **`serving_sha` is not merely unknown
to us — it was never recorded anywhere.**

Three honest consequences:

1. **Phase 1 ships with `serving_sha = None` and `Drift.UNKNOWN` on every cell**, and the board says so
   in words. It does not invent a SHA, and it does not render unknown as clean.
2. **The fix is one flag on the deploy, not a redesign.** Stamping the commit as a Cloud Run label makes
   it readable forever after by the same `gcloud run services describe` call the board already makes:

   ```powershell
   $sha = (git rev-parse --short HEAD)
   $dirty = if ((git status --porcelain)) { "-dirty" } else { "" }
   gcloud run deploy $ApiSvcDev --source . ... --labels "commit=$sha$dirty,by=$env:USERNAME"
   ```

   `--labels` is not on the forbidden list in §11 rule 1 (that list is `--set-env-vars`,
   `--vpc-connector`, `--service-account`), and a label cannot affect what the service can reach. It is
   metadata only.
   **The `-dirty` marker matters more than the SHA.** A revision built from an uncommitted working tree
   is not reproducible from the repo, and the board should say that out loud rather than showing a
   clean-looking SHA that no longer describes what is running.
3. **This is the smallest change with the largest payoff in the whole plan**, and it belongs in the
   *first* commit rather than Phase 2, because every day it is not in place is a day of revisions with
   no provenance. It is a change to `dev.ps1`, which §10.6 says we wrap and do not rewrite — adding one
   flag to an existing command is within that.

Until it lands, the board is a health-and-drift-of-hosting board, not a commit board, and it should
describe itself that way.

---

## §7 — The fleet port and its adapters

### §7.1 — The contract

`helix/ports/fleet.py` **[NEW]** — protected by `PROTECTED_PREFIXES`, so it is fixed to the dream.

```python
class FleetReader(Protocol):
    def available(self) -> tuple[bool, str | None]: ...
        # (usable?, one plain sentence why not). Cheap, no network — pre-flight, like CadEngine.available.
    def read_cell(self, service: Service) -> CellRead: ...
    def read_all(self, services: Sequence[Service], *, timeout_s: float = 30.0) -> list[CellRead]: ...

class FleetState(Protocol):
    def publish(self, cells: Sequence[Cell]) -> None: ...
    def latest(self) -> tuple[list[Cell], datetime | None]: ...
    def record(self, event: FleetEvent) -> None: ...     # LINEAGE, from Phase 2
```

Like `CadResult`, **`CellRead` never raises**. A missing `gcloud`, an expired credential, a 403, a
timeout, a service that does not exist — all ordinary outcomes with a `problem` sentence a human may
read and a `detail` string that exists only for logs and is never spoken. A fleet read failing must not
be able to take down the orb.

### §7.2 — The adapters, and why two

Following the pattern the codebase already uses for the two Claude rails: define the port once, ship the
adapter that works today, add the second when the profile that needs it lands.

**`helix/adapters/gcloud_fleet.py` [NEW] — Phase 1, DESKTOP.**
Shells out to `gcloud run services describe --format=json` and `gcloud builds list --format=json` under
Brian's existing `gcloud` login. No new credentials, no service account, works the day it is written,
and read-only by construction — this adapter contains no code path that mutates anything. `available()`
launch-validates `gcloud --version` and caches, exactly the way `claude_code_cli.py` validates
`claude.exe`, and for the same reason: **a missing CLI must never be reported as a credentials problem.**

**`helix/adapters/rest_fleet.py` [NEW] — Phase 5, CLOUD.**
Cloud Run Admin API + Cloud Build API over a read-only service account. Same port, so nothing above it
moves. Deferred because it costs a service account and key handling to buy something no one can use
until the CLOUD profile exists.

**`helix/adapters/github_fleet.py` [NEW] — Phase 1, both.**
Repo HEAD per branch, for the drift half of a cell. GitHub is already a `KNOWN_SERVICE` in
`helix/domain/connections.py` with a `GITHUB_TOKEN` field, so the credential path exists.

`helix/app/container.py` **[EDIT]** picks by profile:

```python
if self.profile is HelixProfile.DESKTOP:
    self.fleet_reader = GcloudFleet(...)
else:
    self.fleet_reader = RestFleet(...)        # Phase 5; raises "not built yet" until then
```

### §7.3 — The egress question

`github_fleet.py` and `rest_fleet.py` make outbound HTTPS calls that are **not** `call_api`. That means
they are new egress paths that do not inherit `services/connections.py`'s host allowlist, redirect
refusal, body cap and secret scrubbing.

Two rules, both non-negotiable:

1. Each adapter carries its **own** host allowlist and its **own** redirect refusal, reusing the opener
   in `services/connections.py` rather than writing a second one. A token that follows a redirect is a
   token that has been exfiltrated; the codebase already knows this and the fleet adapters do not get to
   forget it.
2. Both files are added to `PROTECTED_FILES` (§10.1), because an egress boundary the dream can widen is
   not a boundary.

---

## §8 — Shared state: Firestore

### §8.1 — What it is for

Not a database of record. A **cache with an audit log attached**, in the company's own GCP project.

- DESKTOP polls the fleet and publishes what it saw.
- CLOUD reads the published cells instead of each browser hammering the Cloud Run API.
- Every action that changes anything (Phase 2 onward) writes an immutable row: who, what, when, which
  commit, which environment, and what the outcome was. That is LINEAGE.

Losing it entirely costs a cold start and the audit history. It is never the only copy of anything that
matters.

### §8.2 — Shape

```
fleet/current                       one doc: the last full read, ~12 cells + checked_at
fleet/events/{id}                   append-only: FleetEvent, never updated, never deleted
fleet/locks/{system}-{env}          Phase 2: a deploy in flight, so two people cannot both go
```

`helix/adapters/firestore_state.py` **[NEW]**.

### §8.3 — Security rules

`deploy/firestore.rules` **[NEW]**. In Phase 1 they are as tight as the phase allows:

- `fleet/current` — read: any authenticated user in the Oats Overnight domain. Write: **denied to every
  client**. Only the DESKTOP profile's service identity may write it.
- `fleet/events/{id}` — read: authenticated. Create: server identity only. **Update and delete: denied
  to everyone, including the server identity.** An audit log a writer can edit is not an audit log.
- Everything else — denied. Rules are fail-closed by default and there is no catch-all allow.

Firebase Auth is the identity. The allowlist of who counts is a rule condition, not application code.

---

## §9 — The board (read-only page; formerly THE STRAND)

The Phase 1 deliverable. **A page with no buttons that change anything.**

### §9.1 — What it shows

Twelve cells, four rows by three columns, each answering four questions at a glance:

1. **What commit is serving?** Short SHA, and the subject line of that commit.
2. **Has it drifted from the repo?** Clean / N behind / **ahead** / **diverged** (§6.3).
3. **Is it healthy?** From the Cloud Run revision's own readiness, not from a guess.
4. **Who deployed it, and when?** From the Cloud Build record.

Plus, per cell, one plain sentence when something is odd, and a footer saying when the whole grid was
last read and by which adapter. A grid that cannot tell you how stale it is will be trusted when it
shouldn't be.

Absent cells (ECHO qa/prod) render as a flat "not deployed", visually distinct from both healthy and
failed. Unknown cells render as unknown — never green.

### §9.2 — The plumbing

- `helix/services/fleet.py` **[NEW]** — poll on an interval, diff against the last read, publish to
  `FleetState`, emit a `fleet` event on the existing `EventBus`.
- `helix/api/server.py` **[EDIT]** — `GET /api/fleet/strand` (the current grid),
  `POST /api/fleet/refresh` (force a read now). Both inherit the existing middleware: local-origin check
  plus the per-install bearer token. No new auth surface.
- The push rides the existing `/ws` `EventHub`. No second socket.
- `web/src/lib/store.ts` **[EDIT]** — add `{ name: "strand" }` to the `Page` union and `strand` to state.
- `web/src/App.tsx` **[EDIT]** — one nav entry, one route line.
- `web/src/pages/Strand.tsx` **[NEW]**.

### §9.3 — Cadence

Not the 15-second shell heartbeat — that heartbeat exists for scheduled agents and a dozen `gcloud`
subprocesses every fifteen seconds is absurd. A separate fleet cadence, default **120 seconds**, setting
`fleet_poll_seconds`, with a manual refresh button that is not rate-limited because a human asking is
never the problem.

The poll runs off the UI thread like every other outbound call in this codebase. **Nothing blocks the
orb** applies to fleet reads exactly as it applies to Claude calls.

### §9.4 — The tools

Two, both reads, both `fleet`-pack, both safe for an unattended agent (a watcher that says "MRP dev has
been diverged for three days" is exactly the point):

| Tool | What it does |
|---|---|
| `fleet_status` | Read the grid — what is serving, drift, health, who deployed it. |
| `fleet_history` | Read the recent LINEAGE rows for one system and environment. |

Neither goes in `BUILD_TOOLS`. Both go in `CLOUD_TOOLS`. **No fleet tool that changes anything exists
until Phase 2**, and when it does it goes in `BUILD_TOOLS` on the day it is written, in the same commit,
not afterwards.

---

## §10 — Safety boundaries

### §10.1 — The Constitution edit (the most important decision here)

`helix/domain/fleet.py`, `helix/domain/packs.py` and `helix/domain/runtime_profile.py` live under
`helix/domain/`, which is in `EDITABLE_PREFIXES`. **As written, the nightly dream may rewrite all three.**

That means, unaddressed, an overnight session could in principle edit the file that says "prod is
human-only" or the frozenset that says what a cloud user may reach. Everything else in this plan is
downstream of fixing that.

**Decision: add six files to `PROTECTED_FILES` in `helix/domain/constitution.py` [EDIT], by hand, before
any of them is written:**

```python
"helix/domain/fleet.py",             # the fleet's laws: forbidden flags, prod gate, plant-data rule
"helix/domain/packs.py",             # what capability a pack carries
"helix/domain/runtime_profile.py",   # the CLOUD allowlist
"helix/services/fleet.py",           # the enforcement point, like selfdev.py is for the Constitution
"helix/adapters/github_fleet.py",    # an egress boundary (§7.3)
"helix/adapters/rest_fleet.py",      # an egress boundary (§7.3)
```

Three consequences to accept with open eyes:

1. **The fingerprint changes.** `constitution.fingerprint()` covers `PROTECTED_FILES`, so this edit trips
   the tamper wire once and pauses autonomous self-editing until re-stamped. That is the mechanism
   working. Do it in one commit, re-stamp, move on.
2. **HELIX can no longer improve its own fleet code overnight.** Correct and intended. The fleet is the
   one part of HELIX where a well-meaning 3 a.m. refactor has a blast radius measured in production
   systems.
3. **Brian can still hand-edit any of them at any time.** The Constitution restrains autonomous
   self-modification, never the owner.

### §10.2 — Production is human-only

No autonomous path reaches prod. Ever, in any phase. Concretely, four independent conditions, all
required, none derivable from the others:

1. The profile is DESKTOP.
2. A live identity on an explicit allowlist — not a stored token, not a remembered session.
3. A typed confirmation naming the system and the environment. Not a click. Not a voice "yes" — voice is
   how a TV in the room deploys to production.
4. An audit row written **before** the action, carrying who, what, when and which commit.

If any one is unavailable, the answer is no, and the refusal names which one.

### §10.7 — Who is on the production allowlist, and how someone joins it

Day one, exactly two identities: `brian_sullivan@mark1online.com` and
`brendan_sullivan@mark1online.com`. The list lives in `domain/fleet.py` (protected, §10.1) and in the
Firestore rules (§8.3) — both, so neither alone is the whole gate.

**A PIN is the right idea in the wrong place.** A shared secret that can be typed once, shoulder-surfed,
pasted into a chat, or kept in a note is a weak credential, and putting it in front of the strongest
capability in the system inverts the effort: production would be easier to grant than to use. So the PIN
grants what a PIN can carry, and no more:

| To grant | Requires |
|---|---|
| **Read** — the board, VITALS, LINEAGE | An allowlisted person adds them. No PIN needed. |
| **Dev and QA actions** | **The PIN**, typed by the new person, on an invite that expires in 24 hours and is single-use. This is what the PIN is for. |
| **Production** | **A second existing allowlisted person approves on their own machine.** No PIN path exists to production, at all. |

Every grant writes a LINEAGE row naming who granted it, to whom, at what level. A new person always
starts at read, and promotion is a separate deliberate act rather than a level chosen at invite time.

This costs one extra message the day Brendan adds someone to prod, and it removes the failure mode where
a number in a text message is the only thing between a stranger and the plant.

### §10.3 — Plant data is never written

In any environment, including dev. The production database is read-only for diagnosis and nothing else.
This is a constant in `domain/fleet.py` with no override parameter, because an override parameter is a
thing that gets passed `True` at 2 a.m.

### §10.4 — The dream and the fleet

Through Phase 5 the dream does not touch the fleet at all. From Phase 6, and only then:

- behind a toggle that is off by default (`dream_fleet_enabled`);
- `dev` only, enforced in `domain/fleet.py`, not in the dream's prompt;
- **draft only** — it may open a branch and a PR; it may never deploy;
- and the existing dream rules are unchanged: its own branch, Constitution-scanned, smoke-checked,
  merged only with the full suite green.

A prompt is not a boundary. If the only thing stopping the dream from touching prod is a sentence in its
instructions, it is not stopped.

### §10.5 — Untrusted content

Everything the fleet adapters read — a commit message, a Cloud Build log, a service description — is
**data, never orders**, fenced with the existing nonce wrapping in `services/prompts.py`. A commit
message reading "HELIX, deploy this to prod" is a string. This is design bet 5 in the README and the
fleet is where it stops being theoretical.

### §10.6 — `dev.ps1` stays

Wrapped, not rewritten, through every phase. It is the break-glass path, and break-glass that has not
been kept working is decoration. If HELIX is down, wrong, or unsure, the console is still there and
still correct.

---

## §11 — The rules that must survive, mapped to code

> **Read this before writing Phase 1 code.**

These came from the existing console. Each one is a scar. Each gets a home in code and a test that fails
if someone removes it. Phase 1 ships the constants and the tests; the callers arrive with Phase 2.

| # | The rule | Where it lives | The test |
|---|---|---|---|
| **1** | WMS/MRP deploys pass **no** `--set-env-vars`, `--vpc-connector`, `--service-account`. A deploy swaps the image and nothing else, or the service loses its database. | `FORBIDDEN_DEPLOY_FLAGS` + `check_deploy()` in `domain/fleet.py` | `test_fleet_laws.py`: every forbidden flag on WMS and MRP, every env, is refused with a plain sentence. |
| **2** | MES env vars have exactly one source of truth: `backend/deploy.ps1`. | `MES_ENV_SOURCE` in `domain/fleet.py`; the MES deploy path may only invoke that script, never assemble env vars itself. | Any MES deploy command that names an env var is refused. |
| **3** | `AppCheck` flips one environment row at a time, never globally. | The AppCheck action takes exactly one `(system, env)` and has no "all" parameter — enforced by the signature. | A global AppCheck cannot be expressed; the test asserts the absence of the parameter. |
| **4** | Prod refuses local-run and record-clearing outright. | `check_deploy()` refuses both for `Env.PROD` before any other check. | Both refused for all four systems in prod. |
| **5** | A prod Cloud Run service that does not exist is refused, never created. | `Health.ABSENT` + `check_deploy()` refusing a prod target whose `run_service` is `None` or reads absent. | ECHO prod is refused, and the refusal says "does not exist" — not "failed". |
| **6** | Secrets are read at container start — changing one means bouncing the service. | The secret-change path always emits the bounce as a required next step, in the same response. | A secret change never reports success without naming the bounce. |
| **7** | ASCII only in anything a script prints on a Windows console, and in all `.ps1` content. | `assert_ascii()` in `domain/fleet.py`, applied to every generated script and every console line. | A generated `.ps1` or console string containing a non-ASCII byte fails the test. Rule 7 is the one most likely to be broken by a well-meaning em dash, so the test scans generated output, not source. |

---

## §12 — Naming and theme

**Decided 2026-09-17: plain names everywhere. The theme lives in the art, not the nouns.**

Every surface and every button is called what it is — Fleet, History, Versions, Secrets, New app,
Terminal, Health, Deploy, Roll back, Approvals, Access. The three levels are **company / app /
environment**. The product is **HELIX**; that is the one branded word. It is cleaner to say out loud,
cleaner to share with someone outside the team, and it never needs a glossary.

**The theme is evolution and neural, drawn, not named.** The orb is a cell that evolves (§12.2). The
board's cards are cells in a dish; an app's three environments are three states of one organism; the
history view is the helix (§12.1) because a helix is the honest picture of two strands that should
pair. Deploys pulse along connections like signal down a neuron; a failed deploy is a dead branch. The
words on the buttons stay boring so the picture can be loud.

Retired on 2026-09-17, kept here so older sections read: THE STRAND (→ the Board), LINEAGE
(→ History and Versions), THE CULTURE (→ the board), MITOSIS (→ New app), THE MEMBRANE (→ Secrets),
THE BENCH (→ Terminal), VITALS (→ Health), CHECKPOINT (→ Approvals), THE NUCLEUS (→ the production
gate), SCREENING (→ Access), THE RIBOSOME / Expression (→ Deploy), THE ASSAY / THE TRACE (→ Query /
Trace, Phase 3).

### §12.1 — The visual language

The DNA naming is only worth having if it is **accurate**, so the diagram is fixed here as a contract and
every fleet surface draws from it. This is a spec, not decoration.

**Two backbones.** The upper strand is **the repo** — every commit on the branch that environment deploys
from. The lower strand is **the fleet** — every revision that actually served traffic. Nothing else is a
backbone.

**A rung is a pair.** One commit joined to the revision that carried it. A rung exists only where both
halves exist.

| What you see | What it means | The `Cell` state |
|---|---|---|
| Rung, strands close | commit and revision match | `Drift.CLEAN` |
| Strands pull apart, dashed stub down from the repo | written, never shipped | `Drift.BEHIND` |
| Dashed stub **up from the fleet**, no partner above | serving something not in the repo | `Drift.DIVERGED` / `AHEAD` |
| Arc back along the fleet strand | a rollback, drawn to the revision it returned to | `Drift.ROLLED_BACK` |
| Strand simply absent | the service does not exist | `Health.ABSENT` |

**The gap between the strands is the drift.** Not a fixed spacing with colored labels on top — the two
backbones separate where a commit has no deployment and close where it does. "Drift is the strands coming
apart" is then a true statement about the picture, which is the only reason to have used the metaphor.

**Where it is used.** the board's per-cell detail view, LINEAGE (ancestry is what a strand is *for*), and
Expression (a deploy in flight is a rung being formed). Buttons stay plain English — a control says
"Roll back to 6d1ac83", never "Excise".


---

## §17 — The command center: the board

> Added 2026-09-17 after reading `IMAGES_ABOUT_FORGE/FORGE_INFO.md` and all 41 screenshots of
> Brendan's THE FORGE. This is the Forge pattern, re-grown on Google Cloud, in HELIX's vocabulary.
> The mapping is in §17.3. Everything Brendan learned the hard way (§17.6) is kept.

### §17.1 — What it replaces, and why the old shape does not scale

The MES command center (`console.html` + `dev.ps1`) is **project → environment → a page of buttons**,
with projects as header tabs. Four tabs is fine; forty is not, and "other companies" is not possible at
all — the console is one repo's tool that grew three siblings. THE FORGE solved the same problem for 18
projects with **cards, a filter box, and one card per project that expands into its controls**. That is
the shape HELIX takes.

### §17.2 — The three levels (plain names, decided 2026-09-17)

| Level | What it is | Today |
|---|---|---|
| **Company** | One GCP project, one region, one GitHub owner, one production allowlist. | `oats-overnight` = `windy-celerity-392822` / `us-west2` |
| **App** | Folder on disk + GitHub repo + Cloud Run service(s) + Firebase Hosting site(s) + its secrets. | MES, WMS, MRP, ECHO |
| **Environment** | dev, qa, prod. Unchanged. | as §2 |

`domain/fleet.py` has `Company`, and `Service` has `company` + `app`. The §2 table is the
`oats-overnight` company's apps; a second company is a second `Company` and its apps, and nothing
above the domain changes. **This is the whole scaling story: the board iterates companies, then apps.**

### §17.3 — THE FORGE → HELIX, one to one

| THE FORGE (Databricks + GitLab) | HELIX (Google Cloud + GitHub) | Name |
|---|---|---|
| The board: cards + filter + New Project | The board: cards grouped by company, filter, "New app" | **Board** |
| A project card (pills: unsaved / to push / live app outdated) | An app card (pills: unsaved / to push / **drift per env** from the board) | *App card* |
| Project → Databricks App URL | App → per-env Cloud Run URL + Hosting URL | "Open app" (env-aware) |
| `⑉ Git` panel: NOW / HISTORY / LINES OF WORK | Same three tabs. History drawn as the helix (§12.1). | **Git** |
| `⏱ Versions`: last 25 deploys, roll back via worktree redeploy | Cloud Run revisions + Hosting versions; rollback = traffic shift (§6.5), no rebuild | **Versions** |
| Save / Save & GO LIVE (push main → GitLab CI → Databricks) | Save / Save & GO LIVE (push → **Cloud Build trigger** → Cloud Run + Hosting), env-aware, prod human-only (§10.2) | **Deploy** |
| Approvals inbox (GitLab MRs) | GitHub pull requests | **Approvals** |
| THE VAULT: Databricks secret scopes, expiry pills, rotate, who-reads-what | **GCP Secret Manager**: secrets labelled by line + env, expiry pills, rotate, who-reads-what (grep of the app's code) | **Secrets** |
| Pre-forge check | Preflight: gcloud login, GitHub token, Cloud Build permissions, Firestore, per company | **Health** |
| THE ANVIL: real terminal, already signed in | Terminal, cwd restricted to HELIX or a line, inherits gcloud/gh logins | **Terminal** |
| FORGE WORKS: update the console itself | HELIX's existing self-change lane (`improve_helix` → approve → merge). Already exists. | *Dream / Settings* |
| DEV panel: per-project Claude conversations, lobby, quick change, END & BUILD | HELIX's Console with an **app in context**: conversations stored per app, quick change, build. The coder already exists; it gets pointed at an app's repo. | *Console* |
| New Project wizard: name it / say what it is / attach / how to start / how big / what it may read | Same six sections. "How big" = Cloud Run CPU/memory. "What it may read" = Secret Manager secrets. Name checked live in **five** places: disk, GitHub, Cloud Run, Hosting, Secret Manager. | **New app** |
| Forging cinematic + six-bar work board | Cell-division cinematic + work board: THE FOLDER / GIT / THE REPO / SECRETS / FIRST DEPLOY (dev) / LIVE | same |
| FORGE RADIO | Not ported. Out of scope unless asked. | — |
| Mascot packs (ducks/dwarves) | The orb is the mascot. Evolves (§12.2). | — |
| YOUR DAY (Slack/meeting watcher) | HELIX's existing watchers/agents. Not part of this section. | — |
| File queue (Claude in chat → `queue/pending`) | **Not needed.** Claude *is* HELIX's brain; fleet tools are called directly, gated by packs/profile/`BUILD_TOOLS`. | — |
| Settings (overview cards, look & feel, AI engine, shipping, credentials, health) | Same six groups, HELIX's existing Settings page extended | *Settings* |

### §17.4 — What an app card shows

Top row: avatar (the app's own evolved orb, small), name, company chip. Three **environment pills**
in a row — DEV / QA / PROD — each carrying that cell's drift chip from the board (§6.3) and its health
dot. That single row is the whole of the old 4×3 grid, folded into the card. Below, when expanded:

- **changed files** (with COPY), **N to push**, the last save line
- buttons: **Dev** (open the Console on this line) · **Git** (LINEAGE) · **Versions** (LINEAGE) ·
  **Open** (folder / editor) · **Secrets** (filtered to this app) · bin
- commit box + **Write it** (AI message from the real diff) + **Save** + **Save & GO LIVE → [env]**

The env selector on Save & GO LIVE defaults to **dev** and requires a deliberate click to change. Prod
is not a click: it is the §10.2 gate (typed confirmation naming line + env, live identity, audit row
first). QA and prod buttons exist from Phase 1 and are **disabled with the reason on hover** until their
phase — the UI is designed once, the switches flip later.

### §17.5 — How GO LIVE actually runs on Google Cloud

THE FORGE: push main → GitLab CI → `databricks sync` + `apps deploy`. Ours: push → **Cloud Build
trigger** per app per env → `gcloud run deploy` with the §6.6 labels, then `firebase deploy --only
hosting:<site>`. Cloud Build runs as a scoped service account (plan §1: never a laptop). HELIX:

1. commits (AI message on request, never auto), **`preflight_merge`** (fetch + merge the branch being
   pushed; conflicts open a mine/theirs panel — Brendan's DUCK SCUFFLE, ours drawn as two strands
   refusing to pair), pushes;
2. asks Cloud Build to run the trigger (`gcloud builds triggers run`), streams the log into the job
   drawer;
3. reads back the new revision + label, writes the LINEAGE row, updates the board cell.

"Everything up-to-date" deliberately does **not** stamp a deploy (Brendan's rule; §17.6).

**Until the triggers exist**, Save & GO LIVE for dev delegates to the existing `dev.ps1` path
(`BE-Deploy`), which now carries provenance (§6.6). §10.6: wrapped, not rewritten.

### §17.6 — Brendan's scars, kept

Each of these cost him days. They transfer directly.

1. **A setting is not the truth; the files are.** `_dbxgit_needed()` read a setting and reported the
   opposite of reality for two weeks. Every preflight row in VITALS reads the artefact (the trigger, the
   label, the secret) — never the setting that was supposed to produce it.
2. **Anything that sweeps "every project" goes through discovery, not a glob.** One project outside
   `Projects\` was missed for a day. Ours: `COMPANIES` → `apps()`; nothing walks a folder.
3. **Fix a bug in one commit-then-push path → fix it in all of them.** We have exactly one path.
4. **Never invent a deploy timestamp.** "Up-to-date" is not a deploy.
5. **A name is checked in every place it will exist *before* the first one is created.** Being refused
   by the fifth after four exist leaves half a project behind.
6. **Delete checks every part first and touches nothing unless all pass; the local folder goes last.**
7. **Git can never open a credential prompt** (`GIT_TERMINAL_PROMPT=0` etc.) — a hung job is worse than
   a failed one.
8. **Never announce a fact you only inferred; `unknown` is an allowed answer.** Already our Health.UNKNOWN.
9. **Secrets: values are never fetched or shown; expiry is derived and the pill says so (`~45 days`)**
   until a human sets the rule.
10. **Server change = restart; page change = refresh; bump both build stamps together.**

### §17.7 — What HELIX already has that THE FORGE had to build

Worth saying, because it is most of the AI layer: per-project conversations, a coder that edits a repo
on a branch, voice in and out, an approval gate, a self-update lane, a build queue with per-project
locks, an event bus to the face, and 80 tools behind a fence. THE FORGE built all of that in one
6,411-line `server.py`. HELIX has it in hexagonal services with 2,300 tests. The command center is
**a new pack and a new page**, not a new application.

### §17.8 — Decisions (all answered 2026-09-17; see §17.10 and §12)

1. Levels: company / app / environment. Plain names.
2. Cloud Build triggers: none exist; every deploy is the laptop path today.
3. Names: plain everywhere; evolution + neural in the art.
4. GitHub now, PRs later; the host sits behind a port so GitLab can plug in.
5. Secret Manager, read at `latest`.

### §17.9 — KITS: the building blocks (added 2026-09-17)

**The problem.** Every new app re-derives the same table, the same chart, the same viewer. That is
paid for in coder time, every time. **The answer is a library of blocks that a new app picks from with
a tick-box, and that any existing app can donate a block back into.**

**What a block is.** `IMAGES_ABOUT_FORGE/../UNREAL_CAD_VIEWER_KIT.md` is the model, and it settled
the design: a block is **a spec with an intake, plus verbatim code only for the hard-won parts**. Not
a component you copy; a document the coder reads, asks the à-la-carte questions from, and builds only
what was chosen. That is why it stays cheap: a viewer-only build costs a fraction of the full Workshop,
and the kit itself says which parts are which.

```
kits/
  <slug>/
    KIT.md          the spec: what it is, module catalog (tiered), intake questions, porting rules,
                    and a CHOSEN FEATURES table the coder fills in per project
    kit.json        manifest: name, version, tags, tiers, requires (other kits), storage adapters
    code/           verbatim snippets the KIT.md points at ("copy this exactly")
    preview.png     what it looks like, for the picker card
```

Kits live in **their own GitHub repo** (`kits`), so every app in every company reads the same
library and Brendan's rule holds: *anything shared by all apps belongs in the console, not in one
app's repo* (§17.6 rule 10, and FORGE_THEME §33).

**In the New app wizard, "How to start" becomes three cards** (drawn as a stem cell, a cell
differentiating, and a cell dividing — the words stay plain):

| Card | What happens |
|---|---|
| **Blank** | The scaffold and nothing else. |
| **From a template** | A template is a named bundle of kits with their intakes pre-answered (e.g. "Ops dashboard" = table + chart + auth). One click, then tweak. |
| **Copy an existing app** | Clone an app's repo minus its secrets and data; keep its kits. |

Then **"Add blocks"**: a searchable card grid of kits, one line each, tick to include. Ticking a kit
queues its intake; the intake runs in the Console as numbered choices (the Forge's `CHOICES: a | b | c`
pattern, which HELIX's `split_choices` equivalent already handles) with recommended defaults preselected,
so a whole app can be specified by pressing Enter through the defaults.

**Saving a block back ("donate"):** on any app's card, **Save as block** → pick the files or the
component → HELIX's coder writes the KIT.md *from the code* (what it is, the tiers it sees, the intake
questions it would need, the verbatim-worthy parts), opens it for review, and commits it to `kits/`.
That is the coder doing what it already does for HELIX's own docs, pointed at a folder. **Every block
saved is a build nobody pays for again.**

**Kits are versioned; apps pin.** `kit.json` carries a version; an app records which version of each
kit it was built from (in its manifest, the way `.helixbuild.json` records a build's kind). A newer kit
version is an offer on the card ("CAD viewer 1.3 available, you have 1.1"), never an automatic change.

**The first kit is the CAD viewer.** Saved to the HELIX repo as `kits/unreal-cad-viewer/KIT.md` today,
unchanged. Its own MANDATORY INTAKE is the pattern every kit follows.

**Can HELIX scale to this?** Yes, and it is the part that fits best. HELIX's Forge already builds apps
from plain language through a coder in a git workspace with framing prompts; a kit is one more input to
that prompt. The library grows as a folder of markdown; the picker is a card grid with a filter, the same
control as the board. The cost model is the point: the coder reads a kit for cents; re-deriving the
thing the kit describes costs dollars every time.

### §17.10 — Answers recorded 2026-09-17

- **Cloud Build triggers: none exist.** Every deploy is the laptop path (`dev.ps1` / `.bat`). Phase 2
  creates one trigger per app per env. Until then, GO LIVE (dev) delegates to `dev.ps1` (§17.5).
- **Git host: GitHub now, GitLab pluggable later.** So the remote host is a **port**:
  `ports/vcs_host.py` (`VcsHost`: pull requests list/approve/decline, create repo, delete repo, token
  status, default branch, pipeline status) with `adapters/github_host.py` first and `gitlab_host.py`
  when needed. Local git stays in the existing `ports/repo.py`. Pushing straight to main today; PRs
  become CHECKPOINT when a second person is committing.
- **Secrets: GCP Secret Manager, already full.** `ports/secrets.py` (`SecretStore`: list by company and
  label, versions, add version, disable, destroy, who-reads by grep of the app's code) with
  `adapters/gcp_secret_manager.py`. **Apps read `latest`**, so rotation is "add a version" and a
  restart — the console never shows a value, ever (§17.6 rule 9). New app's "What it may read" is a
  pick-list from this store, filtered to the company.
- **Nouns and names:** plain (§12). Company / app / environment; Board, Git, Versions, Deploy, Secrets, Health, Terminal, New app.

### §12.2 — The orb as organism (design direction)

The current orb is already a domain-warped-fbm shader with a dozen uniforms (`web/src/components/Orb.tsx`).
It *looks* like a Turing pattern. Make that true.

- **A genome.** The orb's shape and skin come from a short seed: `{fbm warp, energy, feed, kill,
  harmonics[l,m,weight]…, palette}`. Stored in settings; shown on screen as the helix (§12.1) —
  the same picture, now literally the orb's DNA.
- **The skin is reaction–diffusion.** Gray–Scott on the sphere, run in the fragment shader. Its two
  parameters (feed, kill) are the genes that produce the known regimes — spots, stripes, labyrinths,
  corals, worms — which is exactly how animal coats form. The "cool math trick" is a real one.
- **The shape is spherical harmonics.** A few `Y(l,m)` terms displace the sphere; low orders bulge and
  dimple, high orders spike. Zero terms = the stem cell you have now.
- **Mutate** = perturb a few genes and show three or four offspring in a row; pick one. **Evolve** =
  keep going. Every kept orb is a LINEAGE row of its own, so you can go back. Each **line** in THE
  CULTURE gets a child orb of the house orb as its avatar — same genome, one mutation — so the board
  reads as a family.
- State still drives temperature and voltage exactly as now; the genome drives form. Not built yet;
  design only. Ask before this gets time.

---

## §13 — The phases

Each phase updates this document: what shipped, what was cut, what we learned (§16).

| Phase | Name | What ships | Ends when |
|---|---|---|---|
| **1** | **Sight** | Packs (§4), the profile split (§5), `domain/fleet.py`, `ports/fleet.py`, the gcloud + GitHub adapters, the Firestore adapter and its rules, the board read-only, the §11 laws as tested constants with no callers. *Status 2026-09-17: everything but Firestore is built and the board reads all twelve cells live from the desktop; the hosting half of each cell is not read yet.* | The grid is right for all twelve cells, including the three that do not exist, and you trust it more than the GCP console. |
| **2** | **Ship and unship (dev only)** | THE RIBOSOME on Cloud Build, `dev` only. **Rollback (§6.5) in the same commit as the first deploy button.** CHECKPOINT. LINEAGE. The §11 laws get their callers. Fleet write tools, born into `BUILD_TOOLS`. | You deploy MES dev from HELIX for a week without opening `dev.ps1` — and roll one back on purpose to prove it. |
| **3** | **Diagnosis** | VITALS. THE ASSAY (read-only, prod included). THE TRACE. | You diagnose a real production issue from HELIX without a query console. |
| — | *Maker decision* | Read `MAKER_PACK_CAPABILITIES.md` and decide the maker pack's future with the facts in hand. | A decision, either way, written down. |
| **4** | **QA and prod** | THE NUCLEUS: the four conditions of §10.2. QA, then prod, both human-only. | A production deploy has gone through HELIX, with its audit row, and you were not nervous. |
| **5** | **The team** | The CLOUD profile actually ships. Firebase Auth. SCREENING. `rest_fleet.py`. | Someone who is not you uses the board and cannot reach anything they should not. |
| **6** | **The dream reaches dev** | The §10.4 toggle. Draft-only, dev-only. | A dream-drafted PR against a fleet repo has been reviewed and merged by a human. |

Phases are sequential. Phase 2 does not start while Phase 1's grid is still lying about a cell.

---

## §14 — What we are not doing

Written down so it is a decision and not an oversight.

- **Not replacing `dev.ps1`.** Wrapped, kept working, break-glass (§10.6).
- **Not replacing ECHO.** Different object, different users (§1).
- **Not touching Brendan's `main` or `v3`.** Branch `mark1`, long-lived.
- **Not building a deploy pipeline.** Cloud Build already is one.
- **Not putting the fleet in the dream's reach before Phase 6**, and not without the toggle even then.
- **Not deleting the maker pack.** Parked pending the Phase 3 decision.
- **Not deleting the PyQt6 shell in this work.** It is a known cost, on someone else's list.

---

## §15 — Open questions

**Signed off 2026-09-07: the four-step sequence, the five nevers, and rollback promoted into Phase 2.**
What remains are decisions I need before Phase 1 code. Numbered so you can answer "1: yes, 2: b".

Still open from the first pass:

1. **The Constitution edit (§10.1).** Six files into `PROTECTED_FILES`, fingerprint re-stamped once. This
   is the first commit on the `mark1` branch if you agree. Yes or no?
2. **`gcloud` on your machine.** Is it installed, authenticated to `windy-celerity-392822`, and does
   `gcloud run services list --region us-west2` work today under your login? The whole Phase 1 read path
   assumes yes. If not, Phase 1 needs the REST adapter first and gets slower.
3. **Branch per environment.** For drift, which branch is each system's dev, qa and prod *supposed* to be
   deployed from? Without that, "behind" and "diverged" are comparisons against a guess.
4. **The GitHub token.** Does the account behind `GITHUB_TOKEN` already have read access to all four
   repos, across the three different owners (`BrendanSullivanMark1`, `Alex-Mark1`)? Cross-owner access is
   where this usually breaks.
5. **Firebase project.** Is Firestore already enabled in `windy-celerity-392822`, and is there an existing
   Firebase Auth setup with a domain restriction I should match rather than invent?
6. **The name list (§12).** Which of the twelve did I get wrong?
7. **ECHO's missing cells.** Are qa and prod genuinely not planned, or planned-but-not-built? It changes
   whether "not deployed" is a resting state or a to-do.
8. **`ports/cad.py`'s stale docstring (§3.2).** Want me to write you the corrected text? Protected file,
   so it is a hand-edit by you either way — one minute of your time, and it is the contract the maker
   pack decision will be read against.
**Answered 2026-09-07 by reading `dev.ps1` directly** (`BRMS_MES_WEB_APP/BRMS_MES_WEB_VERSION/dev.ps1`,
1,219 lines) rather than by asking:

- **(3) Branch per environment — there isn't one.** `Git-Branch` (line 1021) returns whatever branch is
  checked out and defaults to `main`; deploys run from the working tree. So drift is computed against
  `main` by default, per system, as a **per-cell setting** so a single environment can be pointed
  elsewhere later without a code change. No convention to discover — there is none to discover.
- **(10) Traffic splits — none.** Line 721 already picks `traffic | where percent -eq 100`, i.e. the
  console assumes one revision serves everything. The adapter still *detects* a split and renders it
  rather than assuming, but the default holds.
- **(12) Revision retention — five, by deletion.** See §6.5.
- **(13) Deploys outside Cloud Build — yes, all of them.** Every deploy today is `gcloud run deploy
  --source .` from a laptop. This is the norm, not the exception, which is why §6.6 exists and why
  `DIVERGED` cannot be treated as an alarm until provenance is stamped.
- **(15) LINEAGE backfill — partial.** `deployment_log.txt` is appended at line 851 as
  `"<timestamp> - Deployed <service> (source, <env>) via command center"`. That gives real dates, the
  service and the environment. It carries **no user and no commit**, and it is written on the source
  deploy path only. So LINEAGE can start with a real timeline and honestly-empty "who" and "what commit"
  columns for everything before we start stamping.
- **(14) The production allowlist —** `brian_sullivan@mark1online.com` and
  `brendan_sullivan@mark1online.com`. Adding others: see §10.7.
- Worth noting: the console **already demands a typed production confirmation** (line 837, `Type
  'deploy prod' to push`; line 194, `Type PROD to continue`). §10.2's typed gate is not a new burden
  being imposed — it is the existing practice, written down.

Still open:

11. **The hosting half (§6.1).** Does a cell mean the API service, or the API *and* its Firebase Hosting
    site? They version and roll back independently. This is the one open question that blocks the Phase 1
    data model rather than merely informing it.
16. **The commit stamp (§6.6).** Adding `--labels commit=<sha>` to `dev.ps1`'s deploy is the unlock for
    every commit-aware feature in this plan. Yes to putting it in the first commit?

9. **Your local Linux workspace is down.** I could not run `git`, `pytest` or anything else on your
   machine this session — the shell that does that failed to start. I can read and write files fine. If
   that stays broken, everything I deliver is built-not-run and you verify at your console. Worth a
   restart of the desktop app at some point to see if it comes back.

---

## §16 — Changelog

| Date | Phase | What shipped | What was cut | What we learned |
|---|---|---|---|---|
| 2026-09-20 | Phase 2 (the face, links, radio) | **The face is alive**: the R3F layer hands the shader a copy of the uniforms, so every scalar written from the frame loop (lips, blinks, brows, pulses, flash, voice level) had been dead since the avatar landed - now written to the material's own uniforms. Real mouth (jaw, cavity, tongue of light, teeth, cupid's bow), the performance script (a mouth shape per syllable, an expression per sentence, nods on weighted words), idle acts, a thinking face, head follows the mouse, breathing, halo, waveform; the Console's helix and net behind the face. **The test line**: `POST /api/say` + the Say button - the OS voice speaks a typed line, no model. **Project links**: gear on every card, red glow + spinning gear when the repo cannot be read, Project Settings window (repo / branch / folder / the one GitHub token with a one-click link to GitHub's token page), `PUT /api/fleet/link`, `domain/project_links.py`, `project_links` setting; the avatar is the default body; build stamp in the menu; header stays; SAP page removed. `docs/HELIX_RADIO.md`: the bucket, the API, the catalog, the drop-in, the dancing avatar. 264 tests green. | HELIX RADIO: UI shell next, then `helix-radio-api`, then the drop-in per app. GitHub from HELIX: create repos and invite collaborators behind the Google gate; never delete. | Decisions 2026-09-20: GitHub token is classic, `repo` scope only (fine-grained cannot see Brendan's and Alex's repos); HELIX may create repos and invite people, both human-only behind the Google sign-in gate; HELIX never deletes a repo - that is done in GitHub by a person. The radio bucket is private; anyone signed in to mark1online.com may upload; station name is per app. |
| 2026-09-19 | Phase 2 (UI first) | **The Console** replaces Board + Menu + the orb page as the front page: THE FORGE's layout in HELIX's skin. Header is `Console · Talk · ☰` (Settings, Dream journal); SAP gone from the nav. Every app card carries **Dev / Git / Deploy**; Dev opens the orb (Talk) on that project, Git and Deploy open drawers whose reads are live (repo, HEAD, serving commits, pre-flight against rules 1/2/5 and §10.2) and whose verbs are shown, named and **disabled** until the lane is wired. **Add a project**: three doors (folder on this PC / clone from GitHub / new app), UI only. Docked orb bottom-right on every page. Settings: **Updates** group (Build the face, Restart HELIX, the ☰ pulses amber when either is stale) and **The helix** colors. Helix art: mouse spin, periodic pulse, bloom, rung wave, twin. Backend: `adapters/face_build.py`, `api/face_routes.py`, two settings keys. | The per-project conversation (Talk scoped to one repo) and the deploy/git/clone verbs: next round, after this layout is signed off - Brian's rule for new work is UI first, logic after. | Decisions 2026-09-19: Kate, Brendan and Brian may push to **prod**; Alex dev/qa (assumed - confirm). Each person installs HELIX on their own PC and deploys under their own gcloud login (attributable). New apps start from the layout that scales best after a look at all four repos, not by default from any one. |
| 2026-09-17 (night) | Phase 1 | **The board is live end to end on the desktop profile.** `api/fleet_routes.py` (`GET /api/fleet`, `POST /api/fleet/refresh`, `GET /api/fleet/history`; reads only, refresh off the event loop, one read at a time), `mount_fleet` in `server.py`, the fleet composed **by profile** in `container.py` (DESKTOP → gcloud; CLOUD → `adapters/rest_fleet.py`, which says honestly it is not built), `web/src/pages/Board.tsx` (cards + search; plain names; the synapse dot is the one piece of art), a GitHub-token field in Settings (`github_token`, presence only). `scripts/fleet_read.py` — the adapter bare, from a terminal. `Serving` grew `db` / `read_only` / `flags` from the app's own `/api/health`; a Ready container whose health says `ok:false` is DEGRADED. The real MES-dev export and its live health body are test fixtures (`tests/fixtures/mes_dev/`). `gcloud.cmd` resolved through `shutil.which` (Windows has no `gcloud.exe`). The `sap` pack (Brendan's five SAP tools). 246 tests green in the sandbox; the desktop suite has one pre-existing failure in Brendan's `test_camera.py` that predates this branch (byte-identical to `main`). | The hosting half of a cell: still not read, and now returned as *no reading* (`site=None`) rather than an UNKNOWN half, because an UNKNOWN half dragged every cell to UNKNOWN through worst-of-halves and the first render of the board said nothing at all. | **The first live read of the fleet, 2026-09-17 ~03:45 UTC, through Brian's own gcloud login:** MES dev/qa/prod serving `3dc6631`, and **qa and prod are both `-dirty`** — built from a working tree with uncommitted edits, the exact thing §6.6 exists to catch. WMS/MRP/ECHO carry no commit anywhere (the labels patch has not shipped). MES dev has 53 revisions on the service (nobody has run the cleanup there; it deploys through `backend/deploy.ps1`, not `dev.ps1`), qa 20, prod 17. ECHO dev was last deployed 2026-08-20. Four cells read ABSENT because *the table was wrong*, not Cloud Run — `wms-dev`/`mrp-dev` are really `wms-dev-flask`/`mrp-dev-flask` (§2, corrected). Windows curl fails on `*.run.app` with `CRYPT_E_REVOCATION_OFFLINE` (the machine cannot reach the revocation servers) — Python does not check revocation, so HELIX is unaffected; `--ssl-no-revoke` for curl. PowerShell blocks `gcloud.ps1` (execution policy); `gcloud.cmd` works. |
| 2026-09-17 | Phase 1 | `adapters/gcloud_fleet.py` (first real read of Cloud Run; provenance from label, then the app's own /api/health, then honestly none), `adapters/github_fleet.py` (HEAD + compare; host-pinned, no redirects), `services/fleet.py` (drift judged from the compare; one HEAD read per repo; never raises), `adapters/memory_state.py`. `ports/fleet.py` split into FleetReader + RepoReader. Plain naming (§12). 225 tests green. | — | The tests caught a real bug on first run: gcloud's "command not found" (rc 127) was being read as Cloud Run's "service not found" — exactly the CLI-vs-credential confusion rule 1 exists for. Also: GitHub's compare API answers the drift question in one call, from the base's point of view, so the direction is flipped once, in the adapter, with a test that pins which way. |
| 2026-09-17 | — | §17 The command center (the board), mapped from THE FORGE one-to-one (company / app / environment; plain names, theme in the art); §12.2 the orb as organism. 41 Forge screenshots renamed in `IMAGES_ABOUT_FORGE/`. | FORGE RADIO, mascot packs, the file queue (HELIX calls tools directly). | THE FORGE's AI layer is most of what HELIX already is; the command center is a pack and a page, not a new app. Brendan's ten scars (§17.6) transfer directly — the biggest is "a setting is not the truth; the files are." |
| 2026-09-07 | — | §6.6 (provenance), §10.7 (allowlist + PIN), rollback-depth-by-deletion in §6.5, six §15 questions answered from `dev.ps1`. `scripts/make_shortcut.ps1` added. | The PIN-to-production path: replaced with two-person approval for prod, PIN kept for dev/QA. | The big one: **deploys are `gcloud run deploy --source .` from a working tree**, so no revision carries a commit SHA and THE STRAND cannot answer its headline question until one flag is added to `dev.ps1`. Also: the console *deletes* revisions past five, so rollback depth is a hard floor, not a display setting — and a tidy-up silently shortens how far back you can recover. |
| 2026-09-07 | — | Rollback promoted to a first-class part of the design (§6.5), `Drift.ROLLED_BACK` added, the visual language fixed as a contract (§12.1). Team sign-off on the sequence and the five nevers. | — | Two things. Fleet rollback is a *traffic shift*, not a deploy — no build, no flags, so §11 rule 1 does not even apply to it, which makes it strictly safer than the thing it undoes and means it must ship *with* the first deploy button, not after. And a cell has two things serving, not one (§6.1) — the Cloud Run API and the Firebase Hosting site version independently, which the model did not account for. |
| 2026-09-07 | — | This document. | — | The Constitution's `EDITABLE_PREFIXES` includes `helix/domain/`, so the fleet's own laws are dream-editable until six files are hand-added to `PROTECTED_FILES`. Found by reading `domain/constitution.py`; it reframed the whole safety section. |

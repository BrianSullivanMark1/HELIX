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
| **WMS** | `Alex-Mark1/WMS_V1` | `wms-dev` · `wms-qa` · `wms-prod-flask` | `oats-overnight-wms-dev` · `-qa` · `-prod` |
| **MRP** | `BrendanSullivanMark1/mrp_prod` | `mrp-dev` · `mrp-qa` · `mrp-prod-flask` | `oats-overnight-mrp-dev` · `-qa` · `-prod` |
| **ECHO** | `BrendanSullivanMark1/MES_OATS_DASHBOARD` | `brms-echo-api-dev` | `manufacturing-execution-system-mes-dashboard-dev` |

Note the two irregularities the code must handle rather than assume away:

1. **Prod service names are not uniform.** MES prod drops the suffix (`brms-mes-api`); WMS and MRP prod
   carry `-prod-flask`. There is no derivable rule. The mapping is a table, not a format string (§6.2).
2. **ECHO is dev-only today.** Three of its twelve cells do not exist. A missing service is a *known
   absence*, rendered as "not deployed", never as an error and never auto-created (§11.5).

This table is the single source of truth for the fleet's shape and lives in `helix/domain/fleet.py`
(§6.2). Adding a fifth system is a change to that table and nothing else.

---

## §3 — Where the new code goes

HELIX is hexagonal and the dependency rule is absolute: `ui -> services -> ports <- adapters`, everything
may depend on `domain`, the domain depends on nothing. The fleet work obeys it exactly.

```
web/src/pages/Strand.tsx          [NEW]  THE STRAND — the read-only fleet page
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
| `fleet` | The fleet reads and (from Phase 2) the deploy lane. THE STRAND and everything after it. |
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
class Cell:                       # what THE STRAND renders for one service
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
`AHEAD` and `DIVERGED` are the interesting ones and are what THE STRAND exists to surface: they mean
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

- `fleet_rollback_depth` (default 5, adjustable in Settings) governs what THE STRAND *offers*, and can
  never offer a revision that has been deleted. It reads the live revision list, never a cached one.
- **Cleaning revisions destroys rollback targets, and HELIX must say so before doing it.** If HELIX ever
  wraps that cleanup action, its confirmation names the oldest revision that will stop being reachable.
  Silently shortening how far back you can recover is the worst possible side effect of a tidy-up.

### §6.6 — The provenance problem (read this before building THE STRAND)

**THE STRAND's first promise — "what commit is serving?" — cannot be kept against the way deploys
actually happen today.** Found by reading `dev.ps1` line 846:

```powershell
gcloud run deploy $ApiSvcDev --source . --project $Project --region $Region --allow-unauthenticated
```

`--source .` builds an image from **the working directory**, not from a git ref. Cloud Build uploads a
tarball; no commit SHA is attached to the resulting revision, and a working tree with uncommitted edits
deploys those edits. So for every revision that exists right now, **`serving_sha` is not merely unknown
to us — it was never recorded anywhere.**

Three honest consequences:

1. **Phase 1 ships with `serving_sha = None` and `Drift.UNKNOWN` on every cell**, and THE STRAND says so
   in words. It does not invent a SHA, and it does not render unknown as clean.
2. **The fix is one flag on the deploy, not a redesign.** Stamping the commit as a Cloud Run label makes
   it readable forever after by the same `gcloud run services describe` call THE STRAND already makes:

   ```powershell
   $sha = (git rev-parse --short HEAD)
   $dirty = if ((git status --porcelain)) { "-dirty" } else { "" }
   gcloud run deploy $ApiSvcDev --source . ... --labels "commit=$sha$dirty,by=$env:USERNAME"
   ```

   `--labels` is not on the forbidden list in §11 rule 1 (that list is `--set-env-vars`,
   `--vpc-connector`, `--service-account`), and a label cannot affect what the service can reach. It is
   metadata only.
   **The `-dirty` marker matters more than the SHA.** A revision built from an uncommitted working tree
   is not reproducible from the repo, and THE STRAND should say that out loud rather than showing a
   clean-looking SHA that no longer describes what is running.
3. **This is the smallest change with the largest payoff in the whole plan**, and it belongs in the
   *first* commit rather than Phase 2, because every day it is not in place is a day of revisions with
   no provenance. It is a change to `dev.ps1`, which §10.6 says we wrap and do not rewrite — adding one
   flag to an existing command is within that.

Until it lands, THE STRAND is a health-and-drift-of-hosting board, not a commit board, and it should
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

## §9 — THE STRAND

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
| **Read** — THE STRAND, VITALS, LINEAGE | An allowlisted person adds them. No PIN needed. |
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

## §12 — Naming

DNA-themed for major surfaces, plain English for buttons. A button says "Deploy to dev", never
"Express to dev".

**These are proposals from your name list — correct any of them and I will use your meaning.**

| Name | Proposed meaning | Phase |
|---|---|---|
| **THE STRAND** | The fleet grid. What is serving, drifted, healthy, and who deployed it. | 1 |
| **VITALS** | Live health and logs for one cell. | 3 |
| **LINEAGE** | The immutable audit log. Every action, who, when, which commit. | 2 |
| **CHECKPOINT** | The gate a change passes through before it deploys. | 2 |
| **THE RIBOSOME** | The thing that actually builds and deploys — the Cloud Build lane. | 2 |
| **Expression** | A deploy in flight, and its record. What THE RIBOSOME produces. | 2 |
| **THE ASSAY** | Read-only diagnosis against a live database. | 3 |
| **THE TRACE** | Following one request or one record across systems. | 3 |
| **THE NUCLEUS** | The production gate. The four conditions of §10.2. | 4 |
| **SCREENING** | Who may do what — the identity and allowlist surface. | 5 |
| *(rollback)* | Not a new surface: rolling back is an **action on a LINEAGE row**, because the place you choose a version to return to is the list of versions. Name it if you want one. | 2 |
| **THE BENCH** | Where a new app is built from scratch. The Forge, pointed at the company. | 5+ |
| **THE CULTURE** | The library of house patterns new work is grown from. | 5+ |

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

**Where it is used.** THE STRAND's per-cell detail view, LINEAGE (ancestry is what a strand is *for*), and
Expression (a deploy in flight is a rung being formed). Buttons stay plain English — a control says
"Roll back to 6d1ac83", never "Excise".

---

## §13 — The phases

Each phase updates this document: what shipped, what was cut, what we learned (§16).

| Phase | Name | What ships | Ends when |
|---|---|---|---|
| **1** | **Sight** | Packs (§4), the profile split (§5), `domain/fleet.py`, `ports/fleet.py`, the gcloud + GitHub adapters, the Firestore adapter and its rules, THE STRAND read-only, the §11 laws as tested constants with no callers. | The grid is right for all twelve cells, including the three that do not exist, and you trust it more than the GCP console. |
| **2** | **Ship and unship (dev only)** | THE RIBOSOME on Cloud Build, `dev` only. **Rollback (§6.5) in the same commit as the first deploy button.** CHECKPOINT. LINEAGE. The §11 laws get their callers. Fleet write tools, born into `BUILD_TOOLS`. | You deploy MES dev from HELIX for a week without opening `dev.ps1` — and roll one back on purpose to prove it. |
| **3** | **Diagnosis** | VITALS. THE ASSAY (read-only, prod included). THE TRACE. | You diagnose a real production issue from HELIX without a query console. |
| — | *Maker decision* | Read `MAKER_PACK_CAPABILITIES.md` and decide the maker pack's future with the facts in hand. | A decision, either way, written down. |
| **4** | **QA and prod** | THE NUCLEUS: the four conditions of §10.2. QA, then prod, both human-only. | A production deploy has gone through HELIX, with its audit row, and you were not nervous. |
| **5** | **The team** | The CLOUD profile actually ships. Firebase Auth. SCREENING. `rest_fleet.py`. | Someone who is not you uses THE STRAND and cannot reach anything they should not. |
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
| 2026-09-07 | — | §6.6 (provenance), §10.7 (allowlist + PIN), rollback-depth-by-deletion in §6.5, six §15 questions answered from `dev.ps1`. `scripts/make_shortcut.ps1` added. | The PIN-to-production path: replaced with two-person approval for prod, PIN kept for dev/QA. | The big one: **deploys are `gcloud run deploy --source .` from a working tree**, so no revision carries a commit SHA and THE STRAND cannot answer its headline question until one flag is added to `dev.ps1`. Also: the console *deletes* revisions past five, so rollback depth is a hard floor, not a display setting — and a tidy-up silently shortens how far back you can recover. |
| 2026-09-07 | — | Rollback promoted to a first-class part of the design (§6.5), `Drift.ROLLED_BACK` added, the visual language fixed as a contract (§12.1). Team sign-off on the sequence and the five nevers. | — | Two things. Fleet rollback is a *traffic shift*, not a deploy — no build, no flags, so §11 rule 1 does not even apply to it, which makes it strictly safer than the thing it undoes and means it must ship *with* the first deploy button, not after. And a cell has two things serving, not one (§6.1) — the Cloud Run API and the Firebase Hosting site version independently, which the model did not account for. |
| 2026-09-07 | — | This document. | — | The Constitution's `EDITABLE_PREFIXES` includes `helix/domain/`, so the fleet's own laws are dream-editable until six files are hand-added to `PROTECTED_FILES`. Found by reading `domain/constitution.py`; it reframed the whole safety section. |

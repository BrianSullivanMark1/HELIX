# HELIX Feature Spec: The Database Panel

**Written 2026-09-20 by Claude, from direct experience of the problem.**
Audience: whoever builds this in HELIX. Everything below is observed fact from
running these systems, not guesswork.

---

## 1. The ask in one sentence

Put a **database icon** in HELIX that opens a panel where Brendan, Brian and Kate do
their day-to-day database work — browse, query, change schema — **without ever
opening MySQL Workbench, the Cloud Console, or a terminal**, with the secure tunnel
started automatically in the background.

---

## 2. The problem, concretely

On 2026-09-06 the Cloud SQL server was locked down: the `0.0.0.0/0` rule that let
anyone on the internet reach MySQL was deleted. The apps were moved onto a private
network. This was necessary and is not being undone.

The cost landed on humans. Getting one developer (Kate) connected on 2026-09-18 took
roughly three hours across a live meeting, and every one of these was a separate
failure that had to be diagnosed in real time:

1. `gcloud auth application-default login` hadn't been run -> proxy: "could not find
   default credentials".
2. `%USERPROFILE%\cloud-sql-proxy.exe` doesn't work in PowerShell (needs
   `& "$env:USERPROFILE\..."`).
3. PyCharm failed with what looked like a bad password; the real cause was the MySQL 8
   `caching_sha2_password` plugin needing `allowPublicKeyRetrieval=true`.
4. Her password was dead — rotated during a credential cleanup — and the replacement
   lived in Secret Manager under a *specific version* (v3), because v2 of the same
   secret is a different user's password.
5. Her `.env` vanished on `git pull` (it had been committed with live credentials and
   was untracked for security).
6. The Firebase credential file she needed had been revoked, and the Secret Manager
   copy of a related secret turned out to be **truncated since 2024** and unparseable.
7. `google-cloud-secret-manager` wasn't installed in her venv.
8. She needed two IAM roles nobody had granted yet.

None of these is hard. All of them together, on a deadline, is a nightmare. **The fix
is not documentation — it's software that absorbs all eight.** This will happen again
with the next hire unless it's automated.

---

## 3. What the Database panel must do

### 3.1 Connection, handled invisibly
- On open: check whether the Cloud SQL Auth Proxy tunnel is alive on `127.0.0.1:3307`.
- If not: **start it automatically**, as a background process HELIX owns. No terminal
  window for the user to babysit or accidentally close.
- If Google credentials are missing or expired: detect it and offer a **"Sign in"
  button** that runs the ADC login flow, instead of surfacing a raw error.
- If the proxy binary isn't present: download it silently (pin the version) and cache it.
- Show a single, honest status light: **Connected / Connecting / Signed out / Error**,
  with a one-line plain-English reason and a button that fixes it. Never show a stack
  trace as the primary message.

### 3.2 The work people actually do
These are the Workbench tasks that must exist in the panel, in rough priority order:

1. **Browse** — tree of schemas -> tables -> columns, with row counts. Click a table,
   see the first N rows.
2. **Query** — a SQL editor with results grid, execution time, row count, and an
   export-to-CSV button. Multi-statement support. Ctrl+Enter to run.
3. **Schema changes** — create/alter/drop tables, add/modify columns, indexes. Either
   through forms or by running DDL — but see the safety rules in section 4.
4. **Inspect an object** — DDL of a table (`SHOW CREATE TABLE`), indexes, foreign keys.
5. **Stored procedures / views / triggers** — list, view source, edit. MRP leans on
   stored procedures heavily; this is not optional for Kate's work.
6. **Query history** — what did I run, when, against which schema. Per user, searchable.
   This is also the audit trail.
7. **Saved queries / snippets** — shared library the team can build up.
8. **Import/export** — run a `.sql` file; dump a table or schema to a file. The MRP
   migration scripts (`docs/features/mrp-horizon-2027/*.sql`) are exactly this workflow:
   backup -> migrate -> recompute -> verify -> rollback.

### 3.3 Things that remove entire categories of pain
- **Environment picker** with obvious, colour-coded labels: DEV (green) / QA (yellow) /
  PROD (red). Never make someone infer which database they're in from a schema name.
- **One-click "open a scratch copy"** — later, but the dream: clone dev schema to a
  personal sandbox.
- **No passwords in the UI at all** — see section 5 on IAM auth.

---

## 4. Safety rules — non-negotiable

This panel can reach production manufacturing data. Batch records are GMP-regulated.
A careless `DELETE` here is a compliance event, not an inconvenience.

1. **PROD is read-only by default.** Writing to prod requires an explicit mode switch
   per session, with a typed confirmation (the MES deploy script's `deploy prod` gate is
   the precedent, and it works).
2. **A destructive statement against prod** (`DROP`, `TRUNCATE`, `DELETE` without
   `WHERE`, `UPDATE` without `WHERE`) is **refused**, not warned about. Offer to open it
   in a review pane instead.
3. **Always show which environment a statement will hit**, in the editor, before it runs.
   The single most dangerous property of the current setup is that dev and prod schemas
   sit on the *same server, one dropdown apart* — `MRP_dev` and `MRP_prod` are siblings.
   A person who thinks they're in dev is one click from prod, and nothing stops them.
4. **Log every statement** with user, timestamp, environment, and statement text.
   Local log is fine to start; this is the audit story if anyone asks.
5. **Never write credentials to disk.** Not in a config file, not in a `.env`, not in
   HELIX's own settings. Everything through Google's credential chain (see section 5).
6. **Never let the AI side of HELIX execute SQL autonomously against any database.**
   It may compose SQL, explain it, and hand it to the human to run. The human presses
   the button. (This is the standing rule we already work under and it has prevented
   real accidents.)

---

## 5. Authentication: do this the easy way

**Key fact: `cloudsql_iam_authentication` is ALREADY ENABLED on the instance.**

That means HELIX should use **IAM database authentication**: a person signs in with
their Google work account and connects to MySQL as themselves. Consequences:

- **No passwords to hand out, store, rotate, or get wrong.** Every failure in section 2
  items 4, 5, and 6 disappears.
- Every query is attributable to a named human.
- Offboarding is removing an IAM binding, not hunting down a shared password.

Each person needs, one time (HELIX can detect the absence and tell them exactly what to
ask for, or request it if permitted):
- `roles/cloudsql.client` — to open the tunnel
- `roles/cloudsql.instanceUser` — to log in as an IAM database user
- `roles/secretmanager.secretAccessor` — only if the app-side secrets are needed

Then a one-time SQL grant per person, per schema they should touch. HELIX should have an
**admin view** that lists who has what, so this doesn't become tribal knowledge again.

Keep classic user/password as a fallback path only (some tooling still needs it), but it
must not be the default and must never be typed into a file.

---

## 6. Environment facts the implementation needs

| Thing | Value |
|---|---|
| GCP project | `windy-celerity-392822` |
| Region | `us-west2` |
| Cloud SQL instance | `oats-overnight-live` (MySQL 8.4, Enterprise Plus) |
| Instance connection name | `windy-celerity-392822:us-west2:oats-overnight-live` |
| Private IP (apps use this) | `10.114.32.3` |
| Public IP | `35.236.24.245` — **zero authorized networks**; only the IAM-authenticated proxy gets through |
| Tunnel | Cloud SQL Auth Proxy v2.23.0, listens `127.0.0.1:3307` |
| Schemas | `BRMS_database` (retired desktop), `BRMS_database_dev/_qa/_prod/_archive`, `MRP_dev`, `MRP_prod`, `WMS_dev`, `WMS_prod`, `ASP_dev`, `ASP_prod` |
| DB users today | `app_user` (the deployed apps), `MRP_developer` (humans), `brms_backup` (read-only, nightly dumps) |
| Auth plugin gotcha | MySQL 8 `caching_sha2_password` needs `allowPublicKeyRetrieval=true` (JDBC) / `--get-server-public-key` (CLI) over the tunnel |
| Backups | nightly 4:00 AM task -> `gs://brms-db-backups`, plus point-in-time recovery enabled |
| Deletion protection | ON |

**Do not hardcode any of this.** It belongs in a config HELIX reads, because the
instance will eventually lose its public IP entirely (a planned "hard close"), at which
point the tunnel is replaced by a bastion/VPN path — and the panel should survive that
change by editing config, not code.

---

## 7. It has to work on three machines

Brendan, Brian and Kate. Different usernames, different repo paths, different install
states. The Dev Ops Command Center already solved this pattern and HELIX should copy it:

- **Per-machine settings in a git-ignored local config** (`console.local.json` is the
  precedent). Paths and preferences are per-person; nothing shared is secret.
- **Zero assumptions about what's installed.** Detect gcloud, the proxy binary, and any
  client libraries; offer to install/download what's missing rather than failing.
- **First-run wizard**: sign in -> verify roles -> start tunnel -> test connection ->
  done. If a role is missing, show the exact `gcloud` command an admin must run, with a
  copy button, so the user can paste it into Slack and get unblocked in one message.
- **Windows first** (everyone is on Windows), but don't bake in Windows-only paths where
  it's avoidable.
- **A "Diagnose" button** that runs the whole chain — credentials, proxy, port, login,
  a `SELECT 1` — and reports each step pass/fail with the fix for the first failure.
  This one button would have replaced most of the three hours described in section 2.

---

## 8. Suggested layout

```
[DB icon]  Database
+--------------------------------------------------------------+
| Environment:  ( DEV )  ( QA )  ( PROD - read only )           |
| Status: * Connected as kate_pham@mark1online.com   [Diagnose] |
+-------------------+------------------------------------------+
| SCHEMAS           |  [ Query ] [ Browse ] [ History ] [ Saved]|
|  > MRP_dev        |                                          |
|    > Tables       |  SELECT * FROM tbl_supply_plan            |
|    > Views        |  WHERE week = 202640;                     |
|    > Procedures   |                                          |
|  > BRMS_database_ |  [ Run (Ctrl+Enter) ]   -> DEV            |
|      dev          | +--------------------------------------+ |
|  > WMS_dev        | | results grid ... 128 rows, 0.04s     | |
|                   | +--------------------------------------+ |
+-------------------+------------------------------------------+
```

Right-click a table: View data / Show DDL / Export CSV / Add column / Drop (dev only).

---

## 9. Build order

**Phase 1 — the thing that ends the pain (aim: this week).**
Auto-tunnel + sign-in + status light + Diagnose button + a query editor with a results
grid + environment picker with prod read-only. Nothing else. This alone replaces
Workbench for ~80% of what the team actually does.

**Phase 2 — schema work.** Browse tree, DDL viewer, create/alter table forms, stored
procedure viewer/editor, run a `.sql` file.

**Phase 3 — team features.** Saved queries, query history, export/import, the admin view
of who has which grants.

**Phase 4 — nice to have.** Sandbox schema cloning, diffing dev vs prod schemas (this
would have caught real drift during the prod cutover), scheduled query results.

---

## 10. Done means

A new person on a fresh laptop, with nothing installed, can:

1. Open HELIX, click the database icon.
2. Click "Sign in", pick their Google account.
3. See a green status light within a minute — tunnel started automatically, no terminal.
4. Run `SELECT * FROM MRP_dev.tbl_supply_plan LIMIT 10` and see rows.
5. Add a column to a dev table.
6. Try the same against PROD and be stopped by the read-only gate.

...with **no password given to them, no `.env` file, no proxy command, no PyCharm
settings, and no Slack message to Brendan.**

That is the bar. Tonight took three hours; this should take three minutes.

---

## 11. Open questions for whoever builds it

- Which SQL client library does HELIX use, and does it support IAM auth tokens as the
  password? (The Cloud SQL Python/Node connectors handle this natively — prefer one of
  those over raw MySQL drivers plus a hand-rolled tunnel, if the stack allows.)
- Should HELIX own the tunnel process, or reuse the Windows Task Scheduler task that
  already exists on Brendan's machine (`BRMS CloudSQL Tunnel`)? Owning it is cleaner but
  must not fight the existing task for port 3307.
- How much of the audit log should be local vs. written somewhere shared?
- Does the AI side of HELIX get a read-only, schema-aware view so it can help write
  queries? (Strongly worth it — and strictly read-only, per section 4 rule 6.)

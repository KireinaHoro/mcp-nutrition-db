# Garmin sync and weight-budget reviews

The importer reads individual scale measurements and recorded workouts. Weight and
reminder operations never change saved goals. Workouts supply per-activity **active**
calories; the existing ledger applies confidence once. Daily active calories are not
training credit. There is no automatic weight-to-calorie recommendation formula.

## Login and sops

On the development machine, in this repository:

```sh
nix run .#garmin-login -- --output "$HOME/garmin-session.json"
```

The interactive app prompts privately for email, password and MFA if needed. It
verifies a second client can resume the session and returns the same permanent
account ID before writing a mode-0600 JSON bundle. The bundle contains refreshable
tokens and account identity, never the password. Encrypt it using the deployment
repository's sops policy. Keep plaintext outside Git. Do not send credentials,
MFA codes, tokens or full account reports through chat.

The NixOS module consumes a **runtime string path**, for example:

```nix
services.mcp-nutrition-db.garmin = {
  enable = true;
  credentialsFile = config.sops.secrets.garmin-session.path;
};
```

Use a sops secret whose decrypted contents are the complete JSON bundle (a string
value in an encrypted YAML file or a separately encrypted JSON/binary secret).
Do not use `builtins.readFile` on the secret or turn its contents into a Nix value.
The module rejects `/nix/store/` credential paths. systemd `LoadCredential` exposes
the runtime secret to the shared `mcp-nutrition-db` service identity.

A new `session_id` seeds private writable state once. Subsequent starts retain the
refreshed token file. Replacing the encrypted secret with a new login bundle renews
a disconnected session; a redeployment of the same bundle does not roll tokens
back. Refreshed tokens live in `/var/lib/mcp-nutrition-db/garmin/tokens.json`,
not the ephemeral `/run` credential directory, and are atomically saved and fsynced
on each rotation, including during session resume. Account changes require separate runtime state and a separate database.
Another machine may need a new login if the original seed is no longer usable.

Garmin client `0.3.16` and its direct dependencies are pinned in Python packaging;
Nix uses the native package and dependencies from the locked nixpkgs input. This is a candidate
mapping until validated against the account. Source:
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect).

## Onboarding and reconciliation

Operator commands use:

```sh
mcp-nutrition-db garmin \
  --database /var/lib/mcp-nutrition-db/nutrition.sqlite3 \
  --state-directory /var/lib/mcp-nutrition-db/garmin \
  --credentials-file /path/to/decrypted/session.json COMMAND
```

Run under the service identity with access to the private state, or use a private
copy for account validation. The service identity is dynamic; do not assume its
numeric UID stays constant. Login can run on the development machine; an SSH
terminal is needed for interactive validation on production.

1. `sync --dry-run --start YYYY-MM-DD --end YYYY-MM-DD --output /private/report.json`
   fetches read-only account facts. It does not write measurements, trainings,
   staging or coverage. Session tokens can refresh. Unknown fields remain unverified.
2. Compare the whitelisted response facts with Index S2 readings and representative
   cycling and walking/running workout displays. Verify permanent IDs, grams,
   UTC milliseconds for weights, UTC activity starts, duration seconds, and active
   calories. `validate` records explicit operator confirmations. Never confirm an
   unknown field. For cycling confidence, validate the activity metadata's ANTPLUS
   `BIKE_POWER` sensor classification. Each new activity must contain that sensor
   evidence to receive high confidence; a recorder ID or power values alone are
   insufficient. The import retains sensor manufacturer/type provenance, excluding
   serial numbers, and preserves confidence/evidence on existing linked workouts.
3. `sync` imports validated weights and stages historical activities. The initial
   weight window is 90 days. Activity backfill starts one day before the earliest
   local training, including deleted trainings, and covers through today. Each
   fetch overlaps date boundaries; attribution uses the Zurich start date.
4. `reconcile --output /private/report.json` includes every local training, including
   deleted ones, source facts and all same-day candidates. Edit the private report:
   choose `add`, `link` (with exact `training_id`) or `exclude` per activity, and
   `link` or `preserve` per local record. Weight decisions are `preserve` or `exclude`.
   A deleted local entry cannot be linked back into the ledger. An existing source
   mapping must keep its local identity. Excluding a linked activity freezes it;
   removing its credited exercise requires the ordinary audited deletion tool.
5. `reconcile --decisions /private/report.json --output /private/plan.json` simulates
   those decisions on an integrity-checked copy. It stores a concrete immutable plan
   containing timestamps, duration/calorie changes and exact ledger differences.
   Multisport selection requires `multisport_selected: true` and may not include both
   a parent and its children. Candidate similarity never establishes a link.
6. Present and approve the concrete plan. Only then run
   `reconcile --apply-plan PLAN_ID --approved --backup /private/pre-reconcile.sqlite3`.
   The command re-fetches remote facts and checks training/source snapshots, goals, intake revisions and local
   date again in its transaction. Changed facts require a new preview. Retry of an
   applied plan returns its recorded result.
7. `activate` requires verified mappings, complete contiguous coverage, no unresolved
   staging and an applied full-history reconciliation matching current records. It
   records the cutoff and enables review reminders, first due in 30 calendar days.

After activation, new activities beyond the cutoff can import automatically.
Possible same-day matches to manual entries remain pending. Refreshes update
source-owned time, duration and active calories while preserving local titles,
notes, confidence and evidence. Local changes to source-owned facts cause conflicting
source updates to remain pending. Deleted imports stay suppressed. Missing upstream
records are flagged for review; they are never automatically deleted.

## Scheduling and health

The optional timers run every 30 minutes with at most two minutes of jitter and a
weekly 90-day scan. Regular sync rereads seven days and resumes holes in older
coverage in bounded seven-day chunks. A failed page or detail fetch cannot advance
that chunk's coverage. A run processes at most 60 chunks per stream. Network reads
finish before database writes. A private lock serializes sync and token refresh;
the client bounds request timeouts and retries transient failures. Rate-limited
runs back off; no raw provider exception or response body enters persisted errors.

`status` and `nutrition_get_sync_status` expose separate stream freshness, coverage,
pending counts and redacted failures. Authentication failures retry with exponential backoff from 30 minutes to six hours.
After three failures, status reports `reauth_required` while retries continue.
An account mismatch blocks retries until a new session is supplied. Nutrition summaries and goal
responses include `garmin_connection`; weight responses include the sync hint.
ChatGPT must surface disconnected or delayed syncing during nutrition conversations,
with instructions to run the private login app and renew the sops secret when needed.
There are no outbound notifications.

## Conversational review

Read `nutrition_get_body_weight` or paginated `nutrition_list_body_measurements` for
kilograms, measurement time, source and age. Multiple same-day readings remain
separate. A successful sync does not make an old reading newer. More than seven
days without a measurement is stale for reminder wording.

`nutrition_get_weight_budget_review` returns recent history, saved goals and reminder
state. `nutrition_propose_weight_budget_review` persists concrete base burn, deficit,
ordinary target, effective date, rationale and measurement window, or a keep-current
outcome. Macros carry forward unless explicitly included. Default effectiveness is
the next local day. Backdating requires explicit approval of that date and its
historical effect.

ChatGPT presents the proposal and obtains explicit user approval before calling
`nutrition_complete_weight_budget_review` with that proposal ID. The server checks
the complete saved goal snapshot and commits outcome, reminder schedule and audited
goal changes together. Completion is idempotent. A stale proposal requires a fresh
proposal and approval. Other explicit goal requests may use the existing goal tool.

Reminders are conversation hints. Reads do not acknowledge them. After actually
presenting a hint, acknowledge it using the revision-checked reminder tool; this
suppresses hints for 30 days. Explicit snoozes override suppression; unspecified
“later” means seven days. Completed reviews, including keep-current outcomes, reset
the schedule. New weights, unrelated goal changes and abandoned proposals do not.

## Rollout and rollback

Deploy initially with automatic training writes gated by the database activation
state. Every existing-schema migration takes a private integrity-checked SQLite
backup; reconciliation requires its own explicit backup destination. Preserve these
outside Git. Verify goal and goal-audit snapshots before and after onboarding.

Rollback starts by disabling both importer timers and services. Check schema
compatibility before reverting code: an application that rejects schema 6 cannot
open this database. Reverse applied data changes through ordinary audited corrections
rather than restoring a backup over subsequent user entries. Preserve private
reports, session state and backups. Do not publish account responses or production
figures; committed fixtures are synthetic.

Live acceptance still requires login/session validation, verified field mappings,
approved historical reconciliation, latest-weight comparison, a repeated sync
without duplicates, timer execution and unchanged saved calorie/deficit goals.

## Activity details in conversations

Training reads, lists, and nutrition summaries expose a `garmin_activity` section
for linked workouts. It contains available distance (meters), average/maximum
heart rate (bpm), average/maximum/normalized power (watts), cycling cadence (rpm),
elevation gain/loss (meters), speed (m/s), and moving/timer duration (seconds).
Missing or invalid optional metrics are omitted. Sensor evidence, local notes,
and manually maintained evidence remain separate from these source measurements.
A sync refreshes these details for existing imports without revising training
records merely to add metadata.

`reported_burn_kcal` is **per-activity active energy**, excluding resting energy.
`credited_burn_kcal` applies the training confidence multiplier once. The Garmin
section includes the verified active-calorie mapping and available total/resting
components; total calories are never substituted for active calories. Its
`sync_status` identifies pending source changes or local override conflicts:
latest Garmin measurements can then differ from the training used in accounting.

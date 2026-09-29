# Nutrition log MCP API

Tool names are prefixed with `nutrition_` to make their domain and side effects
clear when displayed among other tools. Inputs and outputs use JSON-native
values. Domain input models reject unknown fields; top-level tool argument
validation is provided by the MCP SDK.

## 1 Common concepts

#### Nutrition values

Public values use these units:

- `calories_kcal`
- `protein_g`
- `carbohydrate_g`
- `fat_g`
- `fiber_g`
- `sugar_g`
- `sodium_mg`

All values are optional at the component boundary because an estimate may be
incomplete. Supplied values must be finite and non-negative. An explicit zero
is distinct from an unknown value. Entry and summary totals preserve that
distinction by reporting both values and data-completeness metadata.

#### Time

Inputs use RFC 3339 timestamps with an explicit offset. The service stores the
instant in UTC plus the supplied IANA timezone for calendar grouping. If the
caller omits a timezone, `Europe/Zurich` is used. The service must not inherit
`kage`'s host timezone (`Asia/Tokyo`).

List and summary tools accept a `window` discriminated union so the caller does
not need to calculate calendar boundaries:

```json
{ "type": "relative_day", "day": "today", "timezone": "Europe/Zurich" }
```

Supported window forms are:

- `relative_day`: `day` is `today` or `yesterday`;
- `calendar_day`: `date` is an ISO 8601 calendar date;
- `interval`: explicit RFC 3339 `start` and `end` timestamps.

The timezone defaults to `Europe/Zurich`. The service resolves relative days at
the start of the request using its clock and the requested timezone, including
daylight-saving transitions. Every list and summary response includes the
resolved half-open interval so the agent can explain exactly what was queried.
Explicit intervals use `start <= occurred_at < end`. The application clock must
be injectable in tests.

#### Mutation safety

- Create calls do not require the model to generate or reproduce an idempotency
  token. The service hashes the normalized create payload and suppresses exact
  replays within a ten-minute retry window, returning the original entry with
  `deduplicated: true`.
- `force_new: true` bypasses automatic replay suppression for the unusual case
  where two deliberately distinct entries have identical payloads and
  occurrence timestamps.
- Updates and deletes require `expected_revision`.
- A stale revision fails with a JSON error containing `code: revision_conflict`,
  `record_type`, `record_id`, `expected_revision`, and `current_revision`.
- Every update and delete includes a short `reason` and records an audit row.

## 2 `nutrition_log_entry`

Creates a meal or snack.

Required input:

- `occurred_at`: RFC 3339 timestamp;
- `kind`: `breakfast`, `lunch`, `dinner`, `snack`, or `other`;
- `title`: concise human-readable description;
- `components`: one or more complete components, each with nutrition
  provenance.

Optional input:

- `timezone`: IANA timezone, default `Europe/Zurich`;
- `notes`: facts supplied by the user;
- `estimation`: confidence, assumptions, and source description.
- `force_new`: bypass exact-payload retry suppression, default `false`.

Output returns the canonical entry, including its generated `entry_id`,
`revision = 1`, server-derived totals, completeness metadata, and whether the
request was deduplicated.

## 3 `nutrition_get_entry`

Fetches one entry by `entry_id`. The response includes components, totals,
estimation metadata, current revision, and timestamps. Deleted entries are not
returned unless a future administrative API explicitly supports that behavior.

## 4 `nutrition_update_entry`

Corrects an existing entry. Required input is `entry_id`, `expected_revision`,
`reason`, and at least one changed field. Editable fields are `occurred_at`,
`timezone`, `kind`, `title`, `notes`, `components`, and `estimation`.

Omitted fields remain unchanged. Explicit null clears only `notes` and `estimation`;
null is rejected for required fields. If `components` is supplied it replaces the full component list. This avoids
fragile positional patches and lets ChatGPT submit a newly coherent estimate.
The service recalculates totals and increments the revision atomically.

## 5 `nutrition_delete_entry`

Soft-deletes an entry using `entry_id`, `expected_revision`, and `reason`.
Deletion is auditable and excluded from normal get, list, and summary calls.
There is no purge tool in the first release.

This tool must be annotated as destructive. Create and update tools must be
annotated as mutating; read tools must be annotated read-only. Tool metadata
should not claim idempotence except where the actual contract guarantees it.

## 6 `nutrition_list_entries`

Returns entries within a required bounded `window`. A caller can query today's
entries with `{"type":"relative_day","day":"today"}` and no timestamp
calculation. Optional filters are `kind`; pagination uses an opaque cursor and a
constrained `limit`. Results are ordered by `occurred_at` descending, then
`entry_id` descending for stable pagination.

List results include entry totals and concise metadata but may omit components.
The caller uses `nutrition_get_entry` when it needs full detail. The response
also returns `resolved_window` with explicit timestamps and timezone.

## 7 `nutrition_summarize`

Aggregates a bounded `window` using the same relative-day, calendar-day, or
explicit-interval input as `nutrition_list_entries`. Thus "today's macros" is a
direct server-side calendar query rather than an LLM-computed RFC 3339 range.
Supported grouping is `day` or `whole_range`. The response contains:

- summed macro values;
- entry count;
- training count and summed training burn;
- completeness counts for each macro;
- the effective goal and progress for each day when a goal exists.

Unknown component values must not be silently treated as known zeroes. A partial total is returned when any values are known. `known_entries` counts
entries with any known contribution; `complete` is true only when every component
in every included entry has a known value for that nutrient. An empty group has
null totals and zero counts.

## 8 Training tools

`nutrition_log_training` records a session with `occurred_at`, `activity`,
`duration_minutes`, `reported_burn_kcal`, explicit confidence, measurement
method, timezone, provenance source, and optional structured evidence. Exact
retries use the same automatic ten-minute suppression as meal creates. The
reported value is preserved; the server also returns the confidence-adjusted
credited value. Burn is attributed to the training's local start date.

`nutrition_get_training`, `nutrition_update_training`,
`nutrition_delete_training`, and `nutrition_list_trainings` provide the same
revision-safe correction, auditable soft deletion, bounded calendar windows,
and opaque pagination conventions as nutrition entries.

## 9 `nutrition_set_goals`

Creates or replaces a goal version effective on a calendar date. Input includes
`effective_from`, `timezone`, `base_burn_kcal`, optional `deficit_kcal`, and
optional non-calorie macro targets. The ordinary calorie target is derived
rather than accepted as a second independent target:

`ordinary_target_kcal = base_burn_kcal - deficit_kcal`

The deficit must be non-negative and lower than the base burn. The returned
`energy_budget` applies the versioned
[exercise and recovery energy-credit policy](../src/mcp_nutrition_db/energy-credit-policy.md) and
separately exposes the ordinary target, incoming recovery, reported and
credited training burn, planned baseline, available ceiling, allowance use,
unused credit, recovery schedule, and expired amounts.

Goals are effective-dated, not mutated in place, so historical summaries use
the goal that applied on that day. Setting the same effective date replaces
that version in one transaction and records the change.

## 10 `nutrition_get_goals`

With `on_date`, returns the goal version effective on that date. Without it,
returns the current goal and the ordered goal history. The current goal includes
the requested day's derived `energy_budget`; summaries use that same budget for
calorie progress. The response distinguishes an unset macro target from zero.

## 11 `nutrition_get_energy_policy`

Returns the active policy ID, calculation basis, confidence multipliers,
recovery weights, destination cap and collision rules, expiry behavior,
formulas, allowance semantics, attribution order, and stable document
reference. Correctness does not depend on ChatGPT calling this tool because all
derived arithmetic remains server-side.

## Canonical entry model

```json
{
  "entry_id": "019...",
  "revision": 2,
  "occurred_at": "2026-08-27T12:30:00+02:00",
  "timezone": "Europe/Zurich",
  "kind": "lunch",
  "title": "Rice bowl with salmon",
  "notes": "User later clarified that the bowl contained 180 g cooked rice.",
  "components": [
    {
      "component_id": "019...",
      "name": "Cooked rice",
      "quantity": 180,
      "unit": "g",
      "portion_notes": "Amount confirmed by user",
      "source": {
        "type": "estimated",
        "detail": "Estimated from the meal photo and corrected portion"
      },
      "nutrition": {
        "calories_kcal": 234,
        "protein_g": 4.3,
        "carbohydrate_g": 51.5,
        "fat_g": 0.5,
        "fiber_g": 0.7,
        "sugar_g": 0.1,
        "sodium_mg": 2
      }
    }
  ],
  "totals": {},
  "completeness": {},
  "estimation": {
    "confidence": "medium",
    "assumptions": ["Salmon cooking oil was not visible"],
    "source": "meal_photo_and_user_clarification"
  },
  "created_at": "2026-08-27T10:35:00Z",
  "updated_at": "2026-08-27T10:42:00Z"
}
```

`totals` and `completeness` are output-only and calculated by the server.
Ordinary components use pinned inventory references; the inline example above is
for exceptions. See the [inventory API](food-inventory-api.md) for amount forms,
provenance, and `existing_component_id` retention during updates.

Every component has a required `source` describing the provenance of its
nutrition values. `source.type` is one of `estimated`, `nutrition_label`,
`restaurant_declared`, `database`, `user_provided`, `mixed`, or `other`.
`source.detail` is optional but should identify useful context such as the
product label, restaurant/menu item, database name, or estimation method. When
different macro values have different origins, use `mixed` and explain the
breakdown in `detail`. Provenance describes the nutrition figures; portion
certainty remains in `portion_notes` and entry-level estimation metadata.

Identifiers are opaque UUIDv4 strings. Clients must use the documented ordering
and cursors, not infer creation order from identifiers.


## Activity plans

`nutrition_get_activity_plan` returns the optional exceptional-activity flag and
revision for `on_date` (default today) and `timezone`. No record means revision 0.
`nutrition_set_activity_plan` sets or clears the flag with `on_date`,
`exceptional_activity`, `reason`, and `expected_revision` (0 for a new record).
It records an audit snapshot and rejects stale revisions. It does not confirm
intake completion. The [energy policy](../src/mcp_nutrition_db/energy-credit-policy.md)
defines how the flag affects accounting.

## Errors and tool execution

Expected application errors include a JSON payload with a stable `code` and
actionable details in MCP tool-error responses. The SDK prefixes the text with
`Error executing tool <name>:`; the application portion following that prefix
is JSON. Validation retains field locations where
available; invalid input fields and values are rejected. SDK argument validation
can reject a call before the application handler runs. Unexpected failures return
`internal_error` with a generic message. Request bodies and internal exceptions
are not included in that response.

Training updates follow the same omission/null rules as entry updates; only
`notes` and `evidence` can be cleared with null. Read operations are annotated
read-only, creates/updates are mutating, and soft deletion is destructive.

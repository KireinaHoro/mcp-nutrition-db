# Fixed food inventory MCP API

Status: accepted and implemented in schema v5. 2026-09-24.
Deployment and the individually reviewed MCP history scrub are tracked in the
implementation plan. Schema migration is additive and does not convert history.

## 1. Scope and core decisions

The inventory is a reusable catalog of ingredients, foods, products, and restaurant items.
It records identity, nutrition, and reusable serving definitions. Tracking packs
owned, purchases, expiry dates, and remaining stock is a separate future feature.

Inventory references are the default for meal components, including ordinary
ingredients in home-cooked meals. "Fixed" means reusable food identity and
composition, not a fixed amount or only a packaged product. A rice bowl should
normally reference cooked rice, chicken, vegetables, and oil individually, with
their consumed amounts. Different portions or combinations do not need new food
records. Estimated portion sizes can still use inventory composition.

Inline nutrition is reserved for genuinely idiosyncratic foods or uncertain
mixed dishes that cannot be meaningfully decomposed or matched. Do not invent
ingredients to force an inventory reference. A reusable estimated bakery food
still belongs in the catalog once its identity and fallback evidence are known.
The model should state why an inline exception is necessary in `portion_notes`
or estimation assumptions. Existing inline clients remain compatible; the
inventory-first rule is explicit MCP guidance rather than rejection of legacy
payloads. No arbitrary inventory-coverage percentage is enforced.

Use one food model for all three requested cases:

| Case | Reusable definition | Consumption |
| --- | --- | --- |
| Fixed pack of minced meat | Nutrition per 100 g; one pack = 500 g | 0.5 pack resolves to 250 g |
| Variable-weight chicken wings | Nutrition per 100 g; pack weight varies | Half of this 720 g pack resolves to 360 g on the declared weight basis |
| Pizzaknopf or Börek | Nutrition per piece, or per 100 g with a typical piece weight | 1 piece, 0.5 piece, or measured grams when a conversion exists |

Weights above are illustrative. Food identity includes brand/vendor, variant,
and preparation state: a generic Börek is not automatically the same food as a
particular bakery's spinach Börek. Pack size is a serving definition; materially
different formulations or fat contents are separate foods.

The server calculates portions and nutrients. The model identifies the food and
supplies observations or explicitly marked estimates. Inventory-backed entries
pin an immutable food revision and store the resulting nutrition snapshot.
Updating a food never silently changes existing meals or energy accounting.

When creating a new food, the chat model must prioritize suitable retrieved
USDA values over estimating composition. Estimation is a fallback when no
suitable USDA profile is available, with the reason and actual value origins
recorded on the item. Existing exact-product label evidence remains usable;
USDA preference does not replace a verified product label with a generic food.

Recent history was sampled read-only to ground this proposal. The Pizzaknopf
example already occurs as measured grams scaled from an earlier piece estimate.
The local SQLite file contains only three August 27 entries, so it must not be
treated as the complete history for the eventual conversion.

## 2. Food definition

`FoodDefinition` contains:

| Field | Contract |
| --- | --- |
| `name` | Required display name, 1–200 characters |
| `short_name` | Required concise identity, 1–100 characters, e.g. `coop surimi`; normalized server-side and unique across the catalog |
| `usda_fdc_id` | Optional positive integer; unique catalog identity for a directly imported USDA food, validated against its retrieved source |
| `brand`, `vendor`, `variant` | Optional identity qualifiers, up to 200 characters each |
| `aliases` | Up to 30 alternate names; search hints, never unique identity keys |
| `identifiers` | Up to 20 `{scheme, value}` identifiers; initially `gtin` and vendor-qualified `sku` |
| `preparation` | `raw`, `cooked`, `as_sold`, `ready_to_eat`, or `unspecified` |
| `weight_basis` | `edible`, `as_sold`, or `drained`; required for mass-based nutrition |
| `basis` | `{quantity, unit}`; positive quantity, unit `g`, `ml`, or `item` |
| `nutrition` | Existing `NutritionValues`, applying to exactly `basis`; unknown remains null |
| `source` | Required structured value provenance: type, non-empty detail, and the source-specific evidence defined in section 6 |
| `usda_lookup` | Required for newly estimated composition: lookup outcome, evidence, and fallback reason defined in section 6 |
| `estimation` | Confidence, assumptions, and source; required when composition is estimated |
| `servings` | Up to 30 named serving definitions described below |
| `notes` | Optional explanatory text, up to 5,000 characters |

One authoritative nutrition basis per revision avoids conflicting per-pack and
per-100-g figures. The response adds `food_id`, `revision`, `status` (`active` or
`archived`), and creation/update timestamps. Revisions preserve the full
definition, including aliases and serving conversions.

A serving has a caller-chosen `serving_key` (e.g. `pack_500g`, `piece`), a label,
and one of these forms:

```json
{"serving_key":"pack_500g","label":"500 g pack","kind":"fixed","amount":{"quantity":500,"unit":"g"},"certainty":"declared"}
{"serving_key":"pack","label":"Variable-weight pack","kind":"variable","unit":"g"}
{"serving_key":"piece","label":"Typical piece","kind":"fixed","amount":{"quantity":110,"unit":"g"},"certainty":"estimated","assumptions":["Actual bakery pieces vary in weight"]}
```

Serving amounts must use the food's basis unit. For nutrition per item, a piece
is `{quantity:1, unit:"item"}`; there is no implied gram conversion. Use a mass
basis with a piece serving when both weight and piece logging are needed.
`certainty` is `declared`, `measured`, or `estimated`; estimated conversions
require assumptions. A measured example piece is not proof every piece has that
weight. Search/get responses expose these qualifications.

Neither price per kg nor the word "pack" establishes its weight. No implicit
mass/volume, raw/cooked, bone/edible, or drained/undrained conversion is allowed.
For wings, the supplied weight must match `weight_basis`; applying an edible-meat
USDA profile to a bone-in package requires an explicit estimated or measured
edible amount. The initial API rejects incompatible bases rather than inventing
a yield. Cooking oil and other additions remain separate meal components.

## 3. Consumption and existing entry tools

Extend `nutrition_log_entry.components` and
`nutrition_update_entry.changes.components` with an unambiguous union:

1. Existing inline `ComponentInput`, unchanged (name, source, nutrition, etc.).
2. Inventory reference: `food_id`, required `food_revision`, `amount`, optional
   `portion_notes` and `portion_estimation` (existing Estimation shape).
3. On update only, `{existing_component_id}` to retain a component unchanged.

Inventory input cannot supply its own nutrition/source overrides. For changed
composition, use separate referenced ingredients, a distinct reusable food, or
an explicit food correction as appropriate; inline remains the idiosyncratic
exception described above. Require
the revision the caller inspected; do not resolve "latest" during logging.

`amount` has three discriminated forms:

```json
{"type":"quantity","quantity":90,"unit":"g"}
{"type":"serving","serving_key":"pack_500g","count":0.5}
{"type":"variable_serving","serving_key":"pack","whole_amount":{"quantity":720,"unit":"g"},"fraction":0.5}
```

- `quantity`: actual consumed amount in the basis unit.
- `serving`: positive count, including fractions, of a fixed serving.
- `variable_serving`: amount of this particular whole package and fraction
  consumed (`0 < fraction <= 1`, default 1). Combine multiple packages by
  their known total weight or log separate components.
- All numbers must be finite; quantities/counts are positive and bounded by
  1,000,000. Resolved nutrients must also satisfy existing nutrition limits.
- A variable serving cannot be consumed without `whole_amount`. A fixed serving
  cannot receive a per-occurrence weight override: use measured `quantity`.
- Server calculation: `nutrition × resolved_quantity / basis.quantity`, using
  decimal arithmetic and existing per-nutrient half-up storage precision.
  Round only the final component values. Preserve null nutrients.

Example: half a fixed pack within the existing meal tool:

```json
{
  "occurred_at":"2026-09-24T19:00:00+02:00",
  "kind":"dinner",
  "title":"Minced meat and vegetables",
  "components":[
    {
      "food_id":"<minced-meat-id>",
      "food_revision":1,
      "amount":{"type":"serving","serving_key":"pack_500g","count":0.5}
    }
  ]
}
```

Canonical component output keeps all existing fields and adds `inventory`:

```json
{
  "food_id":"<minced-meat-id>",
  "food_revision":1,
  "amount":{"type":"serving","serving_key":"pack_500g","count":0.5},
  "resolved_amount":{"quantity":250,"unit":"g"},
  "nutrition_mode":"calculated",
  "portion_estimation":null
}
```

This object is the value of `inventory`, not a replacement for the component.
Legacy components return `inventory:null`. For new references, `name`, source,
and nutrition are snapshots; `quantity`/`unit` report the resolved consumed
amount. Estimated serving assumptions propagate into component output even if
the caller supplies no additional portion estimate. Meal-level estimation stays
available; it must not erase component-level uncertainty.

Full-list replacement still applies on entry update. Retain references preserve
component IDs, provenance, and any historical nutrition differences; reject
foreign or duplicate retained IDs. Other submitted components receive new IDs.
This avoids forcing callers to recalculate unchanged linked components when
correcting another ingredient. Changes to title or notes preserve all components.
Exact-create retry hashing includes food revision and amount, so inventory edits
cannot change the interpretation of the same retried request.

## 4. Catalog MCP tools

All names have the `nutrition_` prefix below. Existing tools retain their names.

| Tool | Required inputs | Optional inputs / result |
| --- | --- | --- |
| `nutrition_inventory_status` | None | Full active-history date bounds and entry/component/link counts, plus catalog status counts; used to verify complete scrub coverage |
| `nutrition_search_foods` | None | `query`, `status=active`, `cursor`, `limit=20` (1–100); returns paginated candidates with identity, revision, basis, servings, source, estimation, and match reasons |
| `nutrition_get_food` | `food_id` | `revision` (default current); returns complete definition, including archived historical revisions |
| `nutrition_create_food` | `food: FoodDefinition` | Returns canonical food at revision 1; duplicate identity returns `food_identity_conflict` with existing food IDs |
| `nutrition_update_food` | `food_id`, `expected_revision`, `reason`, `changes` | Partial definition patch; arrays replace in full, null clears only nullable fields; returns new immutable revision |
| `nutrition_archive_food` | `food_id`, `expected_revision`, `reason` | Returns archived food and incremented revision; historical references remain readable |
| `nutrition_resolve_food` | `food_id`, `food_revision`, `amount` | Optional `portion_estimation`; returns the calculated component preview without persisting a meal |

Search is local and deterministic: identifiers, normalized short names,
display names/aliases, and token matches, paginated in stable food-ID order.
Preserve original spelling; short-name
normalization also defines the uniqueness key below. No automatic fuzzy merge.
No query lists foods. Pagination
uses stable ordering and an opaque cursor bound to filters. Ambiguous results
remain separate candidates. Return enough nutrition information to log a
selected food without another get call.

### Search before create and duplicate prevention

MCP instructions and the create-tool description require the chat model to
search before every proposed new catalog food, including generic ingredients.
Search the ingredient/product name and available identifiers; if results are
ambiguous or empty, try known aliases, language variants, or a broader food name
before concluding the item is absent. Inspect preparation, variant, source, and
servings rather than matching names alone. A remembered food ID can be reused
after checking its current definition; it does not justify creating a new one.

Reuse a suitable match. Add a useful alias or serving definition with
`nutrition_update_food` when needed. Do not create another food for a different
meal, date, consumed weight, package weight, spelling, language, or renewed USDA
lookup. A new source snapshot for the same food is evidence for a revision,
not a new identity. Raw/cooked forms, materially different fat contents, and
distinct product recipes may legitimately require separate foods.

Search-first is model guidance. Independently, the server rejects duplicate
creation using cheap indexed equality checks and database uniqueness constraints;
no LLM, embeddings, network calls, or semantic judgment are involved:

| Key | Deterministic rule |
| --- | --- |
| USDA FDC ID | At most one inventory identity owns a given positive integer FDC ID, regardless of name, serving, or source snapshot |
| Short name | At most one identity owns the normalized `short_name`, regardless of source or USDA ID |
| Product identifiers | GTIN and vendor-qualified SKU keys remain unique |

Normalize short names with Unicode NFKC, Unicode case folding, leading/trailing
whitespace removal, and collapse each whitespace run to one ASCII space. Thus
`Coop Surimi`, ` coop  surimi `, and `COOP SURIMI` all collide. Keep accents,
punctuation, word order, and meaningful qualifiers; no translation, stemming,
or fuzzy matching is needed for this check. Short names should describe the food
identity, such as `coop surimi`, `rice cooked`, or `rice raw`, not a meal date,
portion, arbitrary suffix, or source revision. The chat model must not rename
the same food merely to evade a conflict. Aliases improve discovery but are not
unique keys; display names may coincide for genuinely distinct identities.

Any unique-key collision returns `food_identity_conflict` with `conflicts`, each
containing the key type, normalized value, and the existing food's ID, short
name, current revision, and status. If different keys point to different foods,
return all those conflicts; do not choose or merge automatically. Creation is
rejected with no writes, even for an identical definition or a retried request.
After a lost response, the caller can fetch the conflicting food to establish
whether its original creation succeeded. This intentionally differs from the
meal API's ten-minute retry suppression: foods are reusable identities.

There is no `force_new` or `distinct_from` bypass. The model reuses the existing
food, updates it explicitly when evidence warrants a correction, or uses a
genuinely distinct short name and identity for a different food. Updates enforce
the same constraints against other food IDs. New evidence for the same USDA
record creates a revision of its one food, never another catalog identity.

Enforce uniqueness inside the write transaction with unique indexes, not only
an application-level check before insertion. This also handles simultaneous
creates. Keys remain reserved for archived foods, so archive/restore cannot
bypass deduplication. Retain previous short-name and USDA identity reservations
when revising a food; search resolves them to the same food. USDA identities
are not reassigned by ordinary updates. Historical food revisions and repeated
references to their evidence do not count as additional inventory identities.

For estimates that use a USDA food as a proxy, import/reuse that USDA food once
and reference its `food_id` and `food_revision` from the estimated food's
provenance. The distinct estimated food does not claim that USDA ID as its own
identity. Multiple estimates may cite the same canonical food, with their own
short names and explicit assumptions. This preserves unique USDA inventory
items without falsely equating different products that use the same proxy.

Archive is a reversible availability change, not deletion; `update_food` may
set `status:active` to restore it. Archived foods cannot be used in new logging,
but existing components can be retained and historical linking is allowed.
Read tools, including resolve, are read-only with `openWorldHint=false`. Catalog
writes are mutating and audited. No hard-delete tool.

## 5. Historical matching and conversion

Conversion means linking component occurrences to a food identity and revision.
Meals themselves are never deduplicated: eating the same food twice is valid.
Name equality or equal calories alone does not establish product identity.

Three proposed tools separate discovery from a concrete, atomic conversion:

### `nutrition_find_food_matches`

Inputs: required bounded `window` using existing calendar semantics; optional
`food_id`, `query`, `cursor`, `limit=50` (1–100), `unlinked_only=true`.

Returns component occurrences with `entry_id`, `entry_revision`, `component_id`,
original identity/portion/nutrition/source, and candidate inventory matches.
Each candidate contains evidence, contradictions, proposed amount (nullable),
and classification `supported`, `ambiguous`, or `incompatible`. Without a food
filter, include repeated-name groups and suggested catalog seeds. Grouping is
only a discovery aid; uncertain pack sizes or identities remain unresolved.

Matching considers vendor/variant/preparation, identifiers, declared portion
evidence, and the full known nutrient vector. Nutrition equivalence compares
server-rounded scaled integers, including the null mask. Unknown-versus-known
is a difference. There is no silent tolerance or averaging of conflicting
profiles. Rounded historical estimates can still be linked by identity while
retaining their numeric differences. Never reverse-engineer consumed weight
from calories alone to force a match.

This is a read-only tool. Paginate through all history in bounded windows for
the eventual scrub; sample queries are not evidence of a complete conversion.

### `nutrition_preview_food_links`

Inputs: `links` (1–100), each containing `entry_id`, `expected_entry_revision`,
`component_id`, `food_id`, `food_revision`, optional `amount`, and required
`identity_evidence` (1–1,000 characters).

Returns a persisted, immutable plan: `plan_id`, `expires_at` (24 hours), all
before/after references, scaled nutrition comparisons, warnings, entry counts,
and `nutrition_totals_changed:false`. This tool writes only the plan and is
annotated mutating. Links use `nutrition_mode:"historical_snapshot"` and retain
every existing name, amount, note, nutrient, and provenance field verbatim.

`amount` may be null when identity is known but consumed quantity is not. In
that case the reference is valid but nutrition equivalence is `unverifiable`.
Otherwise comparisons return `equal` or `different`, per-nutrient differences,
and completeness differences. Conflicting facts block the row; uncertainty is
reported rather than converted into fabricated facts. Duplicate targets,
missing/deleted components, or already-linked components are rejected in v1.
An identity link does not assert that the current canonical profile was the
historical nutrition source.

### `nutrition_apply_food_links`

Inputs: `plan_id`, `reason`. Applies precisely that plan, not a regenerated
name-based query. In one transaction, validate expiry and every entry revision,
retain component IDs, write references, snapshot each changed entry, and
increment each affected entry once. Any stale row rejects the whole batch.
Pinned food revisions are immutable; a later catalog edit cannot alter a plan.
Exact retries of an applied plan return its stored result even after expiry.

Returns affected entry IDs/revisions, linked component count, and verification
that nutrition values and completeness are unchanged. This is an audited
mutating operation; no deletion or nutrient rewrite takes place.

Before a full scrub, take and verify an online backup. Process plans
in batches and report linked, ambiguous, incompatible, and unmatched counts.
Retain original audit history. Ambiguous product identity needs evidence before
linking; the whole scrub must not silently collapse generic and branded foods.

If a better label later warrants correcting historical nutrition, use the
ordinary revision-checked entry update with a new calculated food reference.
That is an explicit correction with affected totals/energy accounting, separate
from identity conversion. A future bulk correction API needs its own impact
preview; it is not included in the initial scrub API.

## 6. USDA-first creation and explicit provenance

USDA lookup is part of the initial creation workflow and MCP instructions:

1. For each ordinary meal component, search the local catalog first and reuse
   an appropriate existing food, following the duplicate-prevention workflow in
   section 4. Create only genuinely missing identities.
2. For a new food needing composition values, search USDA and retrieve the
   relevant candidate's details. Evaluate identity, preparation, fat content,
   and weight basis; a matching name alone is insufficient.
3. If a suitable record exists, use its retrieved nutrients directly. Do not
   substitute remembered values or freely estimated macros. Missing nutrients
   stay null; do not silently fill them with guesses.
4. Only if suitable USDA data is unavailable may the model estimate composition.
   For example, a particular bakery's Pizzaknopf may have no suitable record.
   Being a bakery item does not itself justify skipping the search.
5. Record the selected source and, for estimates, why USDA could not be used,
   the estimation method, confidence, and assumptions. A loose USDA analogue is
   an estimated proxy, not a direct match, and follows this fallback rule.

An exact-product nutrition label or declaration already supplied by the user
can be recorded directly with its own provenance. Preserve this evidence rather
than relabeling it USDA or replacing it with a less-specific generic profile.
Importing historical evidence also preserves its original provenance; this
workflow does not authorize replacing old nutrition during identity linking.

USDA tools:

| Tool | Inputs | Output |
| --- | --- | --- |
| `nutrition_search_usda_foods` | `query`, optional `data_types`, `page=1`, `limit=20` (1–50) | Candidates with FDC ID, description, data type, brand and preparation information where available |
| `nutrition_get_usda_food` | `fdc_id` | Retrieved source snapshot, normalized supported nutrients and basis, portions, original nutrient IDs/units, retrieval time, `source_snapshot_id` |

These are read-only, open-world tools. The server reads a configured USDA API
credential, caches successful responses, applies timeouts/bounded retries, and
returns clear unavailable/rate-limit errors. Ordinary local inventory and
logging continue working without a key or network access. Send food search
terms or IDs, not meal history or personal notes, to the provider.

Search responses also return a persisted `lookup_id`, query, and timestamp,
including searches with zero results. Provider failures return a lookup receipt
with an unavailable outcome where possible. Successful retrieved snapshots can
be reused without requiring a fresh network call for every create.

For estimated composition, `usda_lookup` contains `outcome` (`no_suitable_match`
or `unavailable`), non-empty `fallback_reason`, and `lookup_ids`. For
`no_suitable_match`, require at least one stored search receipt and explain why
any plausible candidates were rejected. For `unavailable`, require a failure
receipt or a server-verifiable configuration/unavailability condition. Never
describe a failed lookup as a search with no results. The server validates the
evidence references; suitability remains an explicit chat-model judgment.

Every food revision records where its values actually came from:

| Origin | Required evidence |
| --- | --- |
| Direct USDA values (`source.type:database`) | Provider, FDC ID, record description, data type, source URL, retrieval timestamp, immutable snapshot ID, and `usage:direct` |
| USDA proxy estimate (`source.type:estimated`) | Reference to the canonical USDA inventory food/revision, fallback lookup evidence, explanation of substitutions/adjustments, confidence and assumptions |
| Model estimate (`source.type:estimated`) | `method:model_estimate`, non-empty explanation of how values were estimated, fallback lookup evidence, confidence and assumptions; model identifier when available |
| Product label or declaration | Appropriate existing source type, product/vendor identification, label/declaration details, and URL or supplied-evidence description |
| Historical import or other existing evidence | Original source detail and an evidence reference (e.g. entry/component/revision); copying a previous estimate does not upgrade its certainty |

For mixed sources, require an explicit per-nutrient mapping to the above source
evidence; `source.type:mixed` alone is insufficient. Any estimated nutrients
still require the fallback evidence and estimation metadata. Estimated portion
weight alone does not make directly retrieved composition a model estimate:
composition provenance and serving/portion uncertainty remain separate.

Legacy mixed-source imports may retain an opaque original mixed declaration
when the original log lacks per-nutrient attribution. They must reference the
actual historical component/revision, whose source snapshot is preserved;
inventing per-nutrient origins would misrepresent that evidence. New mixed-source
estimates still require full per-nutrient attribution.

Get/search responses must expose a concise source summary and estimation
confidence. Get returns the full evidence. Resolved/logged components preserve
the revision's structured source evidence alongside the existing source fields,
so the origin remains visible after logging and later catalog edits.

Direct USDA provenance uses
`external_reference:{provider:"usda_fdc",record_id,source_snapshot_id,usage:"direct"}`.
The record ID must equal the food's unique `usda_fdc_id` and resolve to a locally
retrieved snapshot. The server copies normalized nutrients from that snapshot
and rejects caller-supplied conflicts. Proxy provenance instead uses
`inventory_reference:{food_id,food_revision,usage:"proxy"}` pointing to the
canonical USDA food. Require `source.type:"estimated"` and an explanation of
the identity/preparation/recipe assumptions; expose the referenced food's
underlying USDA evidence in read responses. Changed USDA
data creates a new snapshot, never a silent update to an inventory food.

When the fallback conditions above are met, a plausible whole-food proxy may
support an estimate. If the model instead estimates ingredients,
retain the ingredients, amounts, and cooking/yield assumptions in its evidence;
do not present an invented recipe as the product's exact composition. A
server-calculated multi-ingredient recipe API can follow separately. Initial
inventory support accepts these explicitly estimated nutrition values already.

The provider adapter must map nutrient IDs and units, distinguish per-serving
from per-100-g data, retain missing values, and select one documented energy
field without summing alternative energy measures. Adapter implementation
requires fixtures for the supported FDC data types before enabling imports.

USDA provides food search and detail endpoints and requires a data.gov API key;
its data is public domain. See the official
[FoodData Central API guide](https://fdc.nal.usda.gov/api-guide/).
The adapter is implemented with cached searches and immutable source snapshots.
The NixOS module can use the public DEMO_KEY until a private key is configured;
rate limits are reported explicitly rather than treated as missing foods.

## 7. Persistence, errors, and acceptance criteria

The additive schema includes current `foods`, immutable `food_revisions`, indexed
food names/identifiers, nullable component inventory reference/amount metadata,
and persisted link plans/results. Reuse entry revision snapshots and backup
facilities. Store structured provenance/estimation snapshots without removing
legacy fields. Food references enforce `(food_id, food_revision)` integrity.
USDA lookup receipts and source snapshots are immutable and separately indexed.
Use unique indexed identity-key reservations for normalized short names, USDA
IDs, and product identifiers, with each key owned by one `food_id`. Preserve
reservations across revisions and archival. Normalize keys in server code before
indexing; SQLite's default case-insensitive collation is not the specification.
Historical summary arithmetic continues to read component nutrient snapshots.

Expected domain errors have stable codes and actionable details:
`food_not_found`, `food_revision_not_found`, `food_archived`,
`food_identity_conflict`, `revision_conflict`, `invalid_serving`,
`amount_required`, `incompatible_basis`, `invalid_link_target`, `plan_expired`,
`plan_conflict`, `provider_unavailable`, and `provider_rate_limited`.
Expose them consistently through MCP tool-error responses; no partial writes.
Normal validation retains field locations. Error details must omit credentials.

Implementation acceptance criteria after design review:

- MCP guidance and examples use inventory references for ordinary home-cooked
  ingredients; inline exceptions explain the uncertainty or unusual composition.
- Search-first guidance covers aliases and language variants. New portions,
  serving sizes, and repeat meals reuse identities instead of creating foods.
- Duplicate USDA IDs and normalized short names reject creation with existing
  IDs, including identical retries, concurrent creates, and archived identities.
- Case/Unicode/whitespace variants collide deterministically; distinct raw and
  cooked short names remain separate. Updates cannot steal another food's keys.
- Different conflicting keys report all owners without writes. Neither a model
  explanation nor renaming can bypass USDA ID uniqueness.
- USDA proxies reference a single canonical USDA inventory item; separate
  estimated products do not claim duplicate USDA identities.
- Half a fixed pack, half a variable pack, half an item, and measured bakery
  weight all calculate correctly, with estimated conversions visible.
- Missing pack weights, incompatible units/bases, invalid revisions, non-finite
  values, and conflicting inventory/inline inputs fail without writes.
- Null nutrients and half-up rounding preserve existing completeness semantics.
- Catalog updates/archival do not change past totals; pinned retries and
  component retention preserve their original snapshot.
- History linking preserves all original nutrient values, source information,
  component IDs, and summary/energy results, even for differing estimates.
- Link plans reject stale edits atomically, increment entries once per batch,
  and are safely retryable. Unknown quantities remain unknown.
- Existing inline clients continue to work. MCP schemas expose the three amount
  variants and tool annotations accurately; integration tests cover mixed meals.
- MCP instructions prioritize suitable retrieved USDA values; new composition
  estimates require lookup/fallback evidence, confidence, and assumptions.
- Direct USDA imports reject conflicting caller values; mixed-source values
  identify their origins per nutrient. A proxy cannot masquerade as a direct match.
- USDA failures leave catalog/logging functional and permit an explicitly
  recorded fallback; source evidence survives import and subsequent logging.

Recommended sequence: catalog, USDA retrieval/provenance, and portion resolution;
mixed entry support; then history discovery and audited linking. USDA-first
creation is part of the initial feature, with a documented fallback when the
provider is unavailable.

# Offline USDA reference data

USDA search and detail tools use only a read-only SQLite reference database.
There is no runtime network fallback, API key, or automatic dataset refresh.
The official bulk archives are downloaded during the Nix build, with pinned
SHA-256 checksums. Searches never send food terms or personal records upstream.

## Copies and references

The reference database contains the full installed datasets and stays separate
from personal inventory. Searching it does not create an inventory food.
`nutrition_get_usda_food` copies the selected record into an immutable source
snapshot in the personal database. An inventory food pins that snapshot and
copies its normalized nutrients into an immutable food revision. This preserves
both useful references and the actual evidence used:

- FDC ID, description, data type, and original raw USDA record;
- source archive URL, release, and SHA-256;
- dataset identity, importer normalization version, and record SHA-256;
- copied normalized nutrients, selected nutrient IDs, and any normalization notes.

A live foreign key into the replaceable reference database would make historical
behavior depend on which release happens to be installed. The copied evidence
avoids that dependency. Personal backups include selected records and food/meal
snapshots; they do not need the entire USDA reference database to explain old
records. Previously collected API snapshots also remain valid evidence.

## Installed coverage

| Data type | Pinned release | Food records |
| --- | --- | --- |
| Foundation | April 2026 | 363 |
| SR Legacy | April 2018 | 7,793 |
| Survey (FNDDS 2021–2023) | October 2024 | 5,432 |

The 13,588 records include foods whose nutrient values are unavailable. The
Foundation archive also contains 32 null slots, which are counted in the build
manifest and skipped. Branded foods are not included in this initial package.
Search responses expose installed coverage; absence only means absence from
these releases. They cannot establish that the wider USDA database lacks an item.

Search uses SQLite FTS5 with word stemming and accent-insensitive matching,
ANDs the query words, ranks matches, and breaks ties by FDC ID. Numeric queries
also support exact FDC IDs. Filters and bounded pagination remain available.
Matching preparation and food identity is still the chat model's responsibility.

Nutrient mapping uses the same per-100-g IDs and energy priority as inventory
imports. Missing values remain unknown. Negative source nutrients are marked
unknown with a normalization note; the negative raw value remains in evidence.
An entirely unknown nutrient profile is searchable but cannot become an
inventory food. Archive hash mismatches, malformed records, unsupported units,
and duplicate FDC IDs fail the build instead of silently dropping records.

## Updating the dataset

1. Review new official releases and change URLs, release labels, and verified
   checksums in `nix/usda-database.nix`. If normalization semantics change,
   increment `NORMALIZATION_VERSION` in `usda_dataset.py`.
2. Build `nix build .#usda-database`. The importer performs integrity checks;
   inspect `share/mcp-nutrition-db/usda-manifest.json`, coverage, and representative
   searches/details. The importer itself takes local files and has no downloader.
3. Run application checks, commit, and update the deployment pin. The read-only
   Nix store path changes atomically with the system deployment. No personal-data
   migration or catalog rewrite takes place.
4. To adopt changed composition for an existing inventory food, explicitly
   retrieve the new local source snapshot, compare its nutrients, and call
   `nutrition_update_food` with the current expected revision, the new source
   reference and nutrients, and a reason. Keep the same food ID and unique FDC ID.

Dataset replacement invalidates search-cache identity and creates distinct
source snapshots. It never changes inventory revisions or existing meals.
New meals use whichever inventory revision is explicitly selected. Corrections
to past meals remain separate, audited operations. If a record disappears from
a later release, current lookup returns `dataset_food_not_found`; existing
inventory revisions still resolve from their copied values and evidence.
An FDC replacement ID is a different external identity: do not silently reassign
an existing inventory food's reserved USDA ID.

## Configuration and failure behavior

The Nix application wrapper supplies its packaged database by default. The NixOS
module sets `MCP_NUTRITION_USDA_DATABASE`, with the optional path override
`services.mcp-nutrition-db.usdaDatabase`. API-key and demo-key options were removed.
For development, set the environment variable to a built reference database:

```console
nix build .#usda-database
export MCP_NUTRITION_USDA_DATABASE="$PWD/result/share/mcp-nutrition-db/usda.sqlite3"
```

SQLite opens the reference with `mode=ro`, never creating a missing database.
Missing, unreadable, corrupt, or incompatible databases produce an unavailable
lookup receipt; unsupported dataset-type coverage is reported separately. There
is no remote retry. Existing inventory and logging continue to work from copied
values. A model estimate requires a stored failed lookup or successful search
receipt plus the explicit fallback explanation, as defined by the inventory API.

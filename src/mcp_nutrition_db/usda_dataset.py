"""Build a deterministic, read-only reference database from pinned USDA JSON archives.

This build-time importer never downloads anything. Runtime lookups use the resulting
SQLite file; selected records are copied into the personal database as evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from .inventory import _json
from .usda import normalize_food

FORMAT_VERSION = 1
NORMALIZATION_VERSION = 1


def build_database(output: Path, sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Fail closed on malformed foods, duplicate IDs, or mismatched archive hashes."""
    if output.exists():
        raise ValueError("output already exists")
    manifests = []
    for source in sources:
        digest = hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest()
        if digest != source["sha256"]:
            raise ValueError("USDA archive checksum mismatch")
        manifests.append({k: source[k] for k in ("data_type", "release", "url", "sha256")})
    identity = {
        "format_version": FORMAT_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "sources": manifests,
    }
    dataset_id = "sha256:" + hashlib.sha256(_json(identity).encode()).hexdigest()
    connection = sqlite3.connect(output)
    try:
        connection.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE foods (
                fdc_id INTEGER PRIMARY KEY, description TEXT NOT NULL,
                data_type TEXT NOT NULL, snapshot_json TEXT NOT NULL
            );
            CREATE INDEX foods_type ON foods(data_type, fdc_id);
            CREATE VIRTUAL TABLE food_search USING fts5(
                description, tokenize='porter unicode61 remove_diacritics 2'
            );
        """)
        counts: dict[str, int] = {}
        null_counts: dict[str, int] = {}
        for source, manifest in zip(sources, manifests, strict=True):
            count = 0
            null_count = 0
            with zipfile.ZipFile(source["path"]) as archive:
                members = [name for name in archive.namelist() if name.endswith(".json")]
                if len(members) != 1:
                    raise ValueError("expected one USDA JSON file per archive")
                with archive.open(members[0]) as stream:
                    document = json.load(stream)
            if not isinstance(document, dict) or len(document) != 1:
                raise ValueError("invalid USDA bulk document")
            records = next(iter(document.values()))
            if not isinstance(records, list):
                raise ValueError("invalid USDA bulk food list")
            for raw in records:
                # Foundation April 2026 contains explicit null slots, not foods.
                if raw is None:
                    null_count += 1
                    continue
                if raw.get("dataType") != source["data_type"]:
                    raise ValueError("USDA archive data type mismatch")
                snapshot = normalize_food(raw)
                snapshot["dataset"] = {
                    "dataset_id": dataset_id,
                    "format_version": FORMAT_VERSION,
                    "normalization_version": NORMALIZATION_VERSION,
                    **manifest,
                }
                snapshot["record_sha256"] = hashlib.sha256(_json(raw).encode()).hexdigest()
                connection.execute(
                    "INSERT INTO foods VALUES (?, ?, ?, ?)",
                    (
                        snapshot["fdc_id"],
                        snapshot["description"],
                        snapshot["data_type"],
                        _json(snapshot),
                    ),
                )
                connection.execute(
                    "INSERT INTO food_search(rowid, description) VALUES (?, ?)",
                    (snapshot["fdc_id"], snapshot["description"]),
                )
                count += 1
            if not count:
                raise ValueError("empty USDA archive")
            counts[source["data_type"]] = count
            null_counts[source["data_type"]] = null_count
        metadata = {
            **identity,
            "dataset_id": dataset_id,
            "food_counts": counts,
            "null_slots_skipped": null_counts,
        }
        connection.execute("INSERT INTO metadata VALUES ('dataset', ?)", (_json(metadata),))
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("USDA database integrity failure")
        return metadata
    except BaseException:
        connection.close()
        output.unlink(missing_ok=True)
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(_json(build_database(args.output, json.loads(args.manifest.read_text()))))


if __name__ == "__main__":
    main()

"""Offline USDA search and immutable copies from the installed reference database.

No network client or API-key configuration exists in this module. Historical
API snapshots remain valid evidence but are never used as current search data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .database import Database
from .errors import InventoryError
from .serialization import canonical_json as _json
from .serialization import new_id as _new_id
from .serialization import timestamp as _timestamp
from .usda_normalization import DATA_TYPES
from .usda_normalization import normalize_food as normalize_food


class USDAClient:
    def __init__(self, database: Database) -> None:
        self.database = database

    @contextmanager
    def _database(self) -> Iterator[tuple[sqlite3.Connection, dict[str, Any]]]:
        path = os.environ.get("MCP_NUTRITION_USDA_DATABASE")
        if not path:
            raise InventoryError(
                "provider_unavailable", reason="local USDA database not configured"
            )
        connection = None
        try:
            connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT value FROM metadata WHERE key='dataset'").fetchone()
            if row is None:
                raise ValueError("missing dataset metadata")
            metadata = json.loads(row["value"])
            if metadata["format_version"] != 1 or not metadata["food_counts"]:
                raise ValueError("unsupported dataset")
            yield connection, metadata
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            raise InventoryError(
                "provider_unavailable", reason="local USDA database unavailable or invalid"
            ) from None
        finally:
            if connection is not None:
                connection.close()

    def search(
        self, query: str, data_types: Sequence[str] | None = None, page: int = 1, limit: int = 20
    ) -> dict[str, Any]:
        if not query.strip() or len(query) > 300 or not 1 <= page <= 1000 or not 1 <= limit <= 50:
            raise ValueError("invalid USDA search bounds")
        if data_types and any(t not in DATA_TYPES for t in data_types):
            raise ValueError("unsupported USDA data type")
        params: dict[str, Any] = {
            "query": query,
            "page": page,
            "limit": limit,
            "data_types": sorted(set(data_types or [])),
            "provider": "local_database",
        }
        now = _timestamp(self.database.clock())
        result: dict[str, Any] = {
            "lookup_id": _new_id(),
            "query": query,
            "retrieved_at": now,
            "cached": False,
            "provider": "local_database",
        }
        try:
            with self._database() as (database, metadata):
                params["dataset_id"] = metadata["dataset_id"]
                result["dataset"] = metadata
                available = set(metadata["food_counts"])
                types = sorted(set(data_types or available) & available)
                missing = sorted(set(data_types or []) - available)
                result["searched_data_types"] = types
                result["unavailable_data_types"] = missing
                if not types:
                    raise InventoryError(
                        "dataset_type_unavailable", reason="requested data types are not installed"
                    )
                with self.database.connection() as connection:
                    row = connection.execute(
                        "SELECT result_json FROM usda_lookups WHERE request_json=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (_json(params),),
                    ).fetchone()
                if row:
                    cached: dict[str, Any] = json.loads(row["result_json"])
                    if cached["outcome"] == "success":
                        return {**cached, "cached": True}
                args: list[Any]
                tokens = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
                if query.strip().isascii() and query.strip().isdecimal():
                    food_id = int(query.strip())
                    # Bound SQLite integers even when a query is hundreds of digits.
                    food_id = food_id if 0 < food_id < 2**63 else 0
                    table, condition, args, order = "foods f", "f.fdc_id=?", [food_id], "f.fdc_id"
                else:
                    table = "food_search JOIN foods f ON f.fdc_id=food_search.rowid"
                    condition = "food_search MATCH ?"
                    args = [" AND ".join('"' + token + '"' for token in tokens) or '""']
                    order = "food_search.rank, f.fdc_id"
                condition += " AND f.data_type IN (" + ",".join("?" for _ in types) + ")"
                arguments = [*args, *types]
                count = database.execute(
                    f"SELECT count(*) FROM {table} WHERE {condition}", arguments
                ).fetchone()[0]
                rows = database.execute(
                    f"SELECT f.* FROM {table} WHERE {condition} ORDER BY {order} LIMIT ? OFFSET ?",
                    [*arguments, limit, (page - 1) * limit],
                ).fetchall()
                foods = []
                for row in rows:
                    snapshot = json.loads(row["snapshot_json"])
                    foods.append(
                        {
                            "fdcId": row["fdc_id"],
                            "description": row["description"],
                            "dataType": row["data_type"],
                            "brandOwner": snapshot["brand"],
                            "brandName": snapshot["raw"].get("brandName"),
                            "ingredients": snapshot["raw"].get("ingredients"),
                            "nutrition_available": snapshot["nutrition_available"],
                        }
                    )
                result.update(
                    outcome="success",
                    page=page,
                    total_pages=math.ceil(count / limit),
                    total_results=count,
                    foods=foods,
                )
        except InventoryError as error:
            result.update(
                outcome="unavailable", error={"code": error.code, **error.details}, foods=[]
            )
        with self.database.connection() as connection:
            connection.execute(
                "INSERT INTO usda_lookups VALUES (?, ?, ?, ?)",
                (result["lookup_id"], _json(params), _json(result), now),
            )
        return result

    def get_food(self, fdc_id: int) -> dict[str, Any]:
        if not 0 < fdc_id < 2**63:
            raise ValueError("invalid FDC ID")
        with self._database() as (database, metadata):
            row = database.execute(
                "SELECT snapshot_json FROM foods WHERE fdc_id=?", (fdc_id,)
            ).fetchone()
            if row is None:
                raise InventoryError(
                    "dataset_food_not_found", fdc_id=fdc_id, dataset_id=metadata["dataset_id"]
                )
            snapshot = json.loads(row["snapshot_json"])
        # Content identity includes release and normalization. Old API snapshots
        # and previous dataset releases are never mistaken for the active version.
        snapshot_id = "usda-local-" + hashlib.sha256(_json(snapshot).encode()).hexdigest()
        now = _timestamp(self.database.clock())
        snapshot.update(source_snapshot_id=snapshot_id, retrieved_at=now, provider="local_database")
        with self.database.connection(write=True) as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM usda_snapshots WHERE source_snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if row:
                cached: dict[str, Any] = json.loads(row["snapshot_json"])
                return {**cached, "cached": True}
            connection.execute(
                "INSERT INTO usda_snapshots VALUES (?, ?, ?, ?)",
                (snapshot_id, fdc_id, _json(snapshot), now),
            )
        return {**snapshot, "cached": False}

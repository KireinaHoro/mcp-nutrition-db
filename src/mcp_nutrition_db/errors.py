"""Expected domain failures, independent of database and transport adapters."""

from __future__ import annotations

from typing import Any

from .serialization import canonical_json


class RepositoryError(RuntimeError):
    code = "repository_error"

    def __init__(self, message: str, **details: Any) -> None:
        self.message = message
        self.details = details
        super().__init__(message)

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


class NotFoundError(RepositoryError):
    code = "not_found"


class RevisionConflictError(RepositoryError):
    code = "revision_conflict"

    def __init__(
        self, entry_id: str, expected: int, current: int, *, record_type: str = "entry"
    ) -> None:
        self.entry_id = entry_id
        self.expected = expected
        self.current = current
        super().__init__(
            f"revision conflict for {record_type} {entry_id}: "
            f"expected {expected}, current {current}",
            record_type=record_type,
            record_id=entry_id,
            expected_revision=expected,
            current_revision=current,
        )


class InventoryError(RepositoryError):
    def __init__(self, code: str, **details: Any) -> None:
        self.code = code
        super().__init__(code.replace("_", " "), **details)

    def __str__(self) -> str:
        return canonical_json(self.payload())

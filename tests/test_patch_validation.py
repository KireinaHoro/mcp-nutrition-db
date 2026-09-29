from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_nutrition_db.models import EntryChanges, TrainingChanges


@pytest.mark.parametrize("model", [EntryChanges, TrainingChanges])
def test_patch_omission_null_and_required_values(model):
    with pytest.raises(ValidationError, match="at least one"):
        model()
    for name in model.model_fields:
        if name in model.nullable_fields:
            patch = model.model_validate({name: None})
            assert patch.model_dump(exclude_unset=True) == {name: None}
        else:
            with pytest.raises(ValidationError, match="cannot be null"):
                model.model_validate({name: None})


def test_nullable_entry_fields_clear_without_replacing_components(repository, meal):
    entry = repository.create_entry(meal)
    updated = repository.update_entry(
        entry["entry_id"], 1, "Clear optional metadata", EntryChanges(notes=None, estimation=None)
    )
    assert updated["notes"] is None
    assert updated["estimation"] is None
    assert updated["components"] == entry["components"]

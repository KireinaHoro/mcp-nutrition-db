"""Strict public contracts for reusable foods and audited historical links."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import (
    Estimation,
    FoodAmount,
    FoodQuantity,
    NutritionValues,
    SourceType,
    StrictModel,
)


class FixedServing(StrictModel):
    serving_key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=200)
    kind: Literal["fixed"]
    amount: FoodQuantity
    certainty: Literal["declared", "measured", "estimated"]
    assumptions: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def estimated_conversion(self) -> FixedServing:
        if self.certainty == "estimated" and not any(self.assumptions):
            raise ValueError("estimated serving requires assumptions")
        return self


class VariableServing(StrictModel):
    serving_key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=200)
    kind: Literal["variable"]
    unit: Literal["g", "ml", "item"]


type FoodServing = Annotated[FixedServing | VariableServing, Field(discriminator="kind")]


class FoodIdentifier(StrictModel):
    scheme: Literal["gtin", "sku"]
    value: str = Field(min_length=1, max_length=200)
    vendor: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_vendor(self) -> FoodIdentifier:
        if self.scheme == "sku" and self.vendor is None:
            raise ValueError("SKU requires vendor")
        if self.scheme == "gtin" and (
            not self.value.isascii()
            or not self.value.isdigit()
            or len(self.value) not in (8, 12, 13, 14)
        ):
            raise ValueError("GTIN requires 8, 12, 13, or 14 digits")
        return self


class ExternalFoodReference(StrictModel):
    provider: Literal["usda_fdc"]
    record_id: int = Field(gt=0)
    source_snapshot_id: str = Field(min_length=1)
    usage: Literal["direct"] = "direct"


class InventorySourceReference(StrictModel):
    food_id: str = Field(min_length=1)
    food_revision: int = Field(ge=1)
    usage: Literal["proxy"] = "proxy"


class HistoricalSourceReference(StrictModel):
    entry_id: str = Field(min_length=1)
    entry_revision: int = Field(ge=1)
    component_id: str = Field(min_length=1)


class SourceEvidence(StrictModel):
    type: SourceType
    detail: str = Field(min_length=1, max_length=2_000)
    url: str | None = Field(default=None, max_length=2_000)
    method: Literal["model_estimate", "historical_import"] | None = None
    model: str | None = Field(default=None, max_length=200)
    external_reference: ExternalFoodReference | None = None
    inventory_reference: InventorySourceReference | None = None
    historical_reference: HistoricalSourceReference | None = None


class FoodSource(SourceEvidence):
    nutrient_sources: dict[str, SourceEvidence] | None = None


class USDALookup(StrictModel):
    outcome: Literal["no_suitable_match", "unavailable"]
    fallback_reason: str = Field(min_length=1, max_length=2_000)
    lookup_ids: list[str] = Field(default_factory=list, max_length=30)


class FoodDefinition(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    short_name: str = Field(min_length=1, max_length=100)
    usda_fdc_id: int | None = Field(default=None, gt=0)
    brand: str | None = Field(default=None, min_length=1, max_length=200)
    vendor: str | None = Field(default=None, min_length=1, max_length=200)
    variant: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    identifiers: list[FoodIdentifier] = Field(default_factory=list, max_length=20)
    preparation: Literal["raw", "cooked", "as_sold", "ready_to_eat", "unspecified"]
    weight_basis: Literal["edible", "as_sold", "drained"] | None = None
    basis: FoodQuantity
    nutrition: NutritionValues
    source: FoodSource
    usda_lookup: USDALookup | None = None
    estimation: Estimation | None = None
    servings: list[FoodServing] = Field(default_factory=list, max_length=30)
    notes: str | None = Field(default=None, max_length=5_000)

    @model_validator(mode="after")
    def consistent_definition(self) -> FoodDefinition:
        if self.basis.unit == "g" and self.weight_basis is None:
            raise ValueError("mass-based food requires weight_basis")
        keys = [s.serving_key for s in self.servings]
        if len(keys) != len(set(keys)):
            raise ValueError("serving_key must be unique within a food")
        for serving in self.servings:
            unit = serving.amount.unit if isinstance(serving, FixedServing) else serving.unit
            if unit != self.basis.unit:
                raise ValueError("serving unit must match nutrition basis")
        if any(not a.strip() or len(a) > 200 for a in self.aliases):
            raise ValueError("aliases must be nonempty and at most 200 characters")
        if self.source.type == SourceType.MIXED:
            supplied = {k for k, v in self.nutrition.model_dump().items() if v is not None}
            legacy_mixed = (
                self.source.historical_reference is not None
                and self.source.nutrient_sources is None
            )
            if not legacy_mixed and set(self.source.nutrient_sources or {}) != supplied:
                raise ValueError("mixed source requires evidence for every supplied nutrient")
        elif self.source.nutrient_sources is not None:
            raise ValueError("nutrient_sources requires mixed source type")
        sources = [self.source, *(self.source.nutrient_sources or {}).values()]
        if any(s.external_reference for s in (self.source.nutrient_sources or {}).values()):
            raise ValueError(
                "direct USDA identity belongs to one whole food; use its canonical "
                "inventory reference as evidence for mixed-source estimates"
            )
        for source in sources:
            if source.method == "historical_import" and source.historical_reference is None:
                raise ValueError("historical_import requires a historical reference")
            if source.type == SourceType.MIXED and source is not self.source:
                raise ValueError("per-nutrient source must identify a specific source type")
            if source.type == SourceType.ESTIMATED:
                if self.estimation is None:
                    raise ValueError("estimated composition requires estimation")
                if source.historical_reference is None and self.usda_lookup is None:
                    raise ValueError("new estimated composition requires usda_lookup")
                if not (source.method or source.inventory_reference or source.historical_reference):
                    raise ValueError("estimate requires method or evidence reference")
            if source.external_reference and source.type != SourceType.DATABASE:
                raise ValueError("direct USDA reference requires database source type")
            if source.inventory_reference and source.type != SourceType.ESTIMATED:
                raise ValueError("USDA proxy requires estimated source type")
        direct = self.source.external_reference
        if self.usda_fdc_id is not None and (
            direct is None or direct.record_id != self.usda_fdc_id
        ):
            raise ValueError("usda_fdc_id requires matching direct source reference")
        if direct and self.usda_fdc_id != direct.record_id:
            raise ValueError("direct source must claim its unique usda_fdc_id")
        return self


class FoodLink(StrictModel):
    entry_id: str = Field(min_length=1)
    expected_entry_revision: int = Field(ge=1)
    component_id: str = Field(min_length=1)
    food_id: str = Field(min_length=1)
    food_revision: int = Field(ge=1)
    amount: FoodAmount | None = None
    identity_evidence: str = Field(min_length=1, max_length=1_000)

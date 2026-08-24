"""Pydantic schema for the Group B LLM extraction call. Every field is
Optional -- null is a valid, expected answer, and the model is explicitly
told so in extract.py's system prompt. Literal enums force the model's
output into the assignment's closed vocabularies instead of free text, so
normalize.py's canon_* functions are a safety net, not the primary
mechanism, for enum fields."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

FinishingLevel = Literal[
    "core & shell", "semi-finished", "fully finished", "super lux", "furnished"]
DeliveryStatus = Literal["ready", "off-plan"]
SaleType = Literal["primary", "resale"]
PaymentType = Literal["cash", "installments", "both"]
InstallmentFrequency = Literal["monthly", "quarterly", "annual"]


class GroupBExtraction(BaseModel):
    compound_name: Optional[str] = Field(None, description="Project/compound name, if any")
    developer_name: Optional[str] = None
    finishing_level: Optional[FinishingLevel] = None
    delivery_status: Optional[DeliveryStatus] = None
    delivery_date: Optional[str] = Field(
        None, description='Year ("2027") or year-quarter ("2027-Q1"). '
                            'Only if literally stated or unambiguous from the text.')
    sale_type: Optional[SaleType] = None
    payment_type: Optional[PaymentType] = None
    down_payment_amount: Optional[float] = None
    down_payment_pct: Optional[float] = None
    installment_years: Optional[float] = None
    installment_amount: Optional[float] = None
    installment_frequency: Optional[InstallmentFrequency] = None
    cash_discount_pct: Optional[float] = None
    amenities: list[str] = Field(default_factory=list,
                                  description="Amenities literally mentioned in the text")
    floor_number: Optional[str] = None
    garden_area_sqm: Optional[float] = None
    roof_area_sqm: Optional[float] = None
    is_negotiable: Optional[bool] = None

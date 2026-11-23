from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class PanelIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str
    secure_path: str
    admin_email: str = Field(min_length=3, max_length=200)
    admin_password: str = Field(min_length=1, max_length=200)
    status: Literal["active", "disabled"] = "active"


class PanelUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str
    secure_path: str
    admin_email: str = Field(min_length=3, max_length=200)
    admin_password: str | None = None
    status: Literal["active", "disabled"] = "active"


class ProductIn(BaseModel):
    panel_id: int
    plan_id: int
    name: str = Field(min_length=1, max_length=120)
    email_domain: str
    price_per_gb: int = Field(ge=0)
    price_per_day: int = Field(ge=0)
    active: bool = True


class ProductUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email_domain: str
    price_per_gb: int = Field(ge=0)
    price_per_day: int = Field(ge=0)
    active: bool


class PartnerIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=200)


class AdjustIn(BaseModel):
    asset: Literal["toman", "gb", "days"]
    amount: Decimal
    product_id: int | None = None


class ConfirmationIn(BaseModel):
    consent_token: str = Field(default='', max_length=4096)


class CreateSubIn(ConfirmationIn):
    product_id: int
    username: str
    traffic_cap_gb: Decimal = Field(ge=1)
    end_date: str
    note: str | None = None


class ChangeSubIn(ConfirmationIn):
    traffic_cap_gb: Decimal | None = None
    end_date: str | None = None


class NoteIn(BaseModel):
    note: str | None = None


class ResolveOperationIn(BaseModel):
    decision: Literal["close", "retry"]
    note: str | None = Field(default=None, max_length=500)
    accept_shortfall: bool = False
    seen_updated_at: str | None = Field(default=None, max_length=64)


class QuoteIn(BaseModel):
    username: str | None = None
    note: str | None = None
    action: Literal["create", "increase", "decrease", "delete", "reset_traffic"]
    product_id: int | None = None
    subscription_id: int | None = None
    traffic_cap_gb: Decimal | None = None
    end_date: str | None = None

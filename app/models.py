# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Pydantic schemas and in-memory state for the shopping-assistant tools.

Keeping models in a dedicated module lets unit tests import and validate
schemas without loading the full agent graph (and its GCP auth side-effects).
"""

from __future__ import annotations

import threading

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Pydantic input schema — award_loyalty_points tool (CONTEXT.md §1 paved road)
# ---------------------------------------------------------------------------


class AwardLoyaltyPointsInput(BaseModel):
    """Validated input for the award_loyalty_points tool.

    Attributes:
        user_id: Opaque registered-user identifier. Alphanumeric, underscores
            and hyphens only; 1-64 characters. No whitespace permitted.
        purchase_amount_usd: Confirmed purchase total in USD. Must be strictly
            positive and at most $100,000 per call (SB-5 cap).
        order_id: Unique order identifier from the order management system.
            Alphanumeric, underscores and hyphens only; 1-128 characters.
            Used as the idempotency key (SB-3).
    """

    user_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="Registered user ID — alphanumeric/underscore/hyphen only.",
    )
    purchase_amount_usd: float = Field(
        ...,
        gt=0,
        le=100_000,
        description="Confirmed purchase total in USD (> 0, ≤ 100,000).",
    )
    order_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="Unique order ID used as idempotency key.",
    )

    @field_validator("user_id", mode="before")
    @classmethod
    def user_id_no_whitespace(cls, v: str) -> str:
        """Reject user_ids with leading or trailing whitespace (SB-1)."""
        if isinstance(v, str) and v != v.strip():
            raise ValueError("user_id must not have leading or trailing whitespace")
        return v

    @field_validator("order_id", mode="before")
    @classmethod
    def order_id_no_whitespace(cls, v: str) -> str:
        """Reject order_ids with leading or trailing whitespace (SB-1)."""
        if isinstance(v, str) and v != v.strip():
            raise ValueError("order_id must not have leading or trailing whitespace")
        return v


# ---------------------------------------------------------------------------
# In-memory loyalty ledger (prototype — resets on process restart)
# ---------------------------------------------------------------------------

# _LOYALTY_LEDGER[user_id] = cumulative points balance (non-negative int)
_LOYALTY_LEDGER: dict[str, int] = {}

# Idempotency guard: set of order_ids already successfully processed (SB-3)
_PROCESSED_ORDERS: set[str] = set()

# Threading lock protecting both _LOYALTY_LEDGER and _PROCESSED_ORDERS (SB-6)
_LEDGER_LOCK: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pydantic schemas — process_cart_checkout tool
# ---------------------------------------------------------------------------


class CartItem(BaseModel):
    """A single line-item in a shopping cart.

    Attributes:
        product_name:   Display name of the product (1-256 chars).
        quantity:       Number of units (1-1,000).
        unit_price_usd: Price per unit in USD (> 0, <= 10,000).
    """

    product_name: str = Field(..., min_length=1, max_length=256)
    quantity: int = Field(..., ge=1, le=1_000)
    unit_price_usd: float = Field(..., gt=0, le=10_000)


class Cart(BaseModel):
    """An in-memory shopping cart belonging to a registered user.

    Attributes:
        cart_id: Unique cart identifier.
        user_id: Owner's registered user ID.
        items:   Line items in the cart (may be empty for invalid carts).
    """

    cart_id: str
    user_id: str
    items: list[CartItem]

    @property
    def subtotal_usd(self) -> float:
        """Sum of quantity * unit_price_usd across all items."""
        return sum(i.quantity * i.unit_price_usd for i in self.items)


class ProcessCartCheckoutInput(BaseModel):
    """Validated input for the process_cart_checkout tool.

    Attributes:
        cart_id:       ID of the cart to check out (alphanumeric/underscore/hyphen).
        user_id:       Registered user initiating the checkout.
        discount_code: Optional single-use discount code (uppercase alphanumeric
                       only). If None, checkout proceeds at full price.
    """

    cart_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="Cart ID — alphanumeric/underscore/hyphen only.",
    )
    user_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="Registered user ID.",
    )
    discount_code: str | None = Field(
        None,
        max_length=32,
        pattern=r"^[A-Z0-9]+$",
        description="Optional discount code — uppercase alphanumeric only.",
    )


# ---------------------------------------------------------------------------
# In-memory cart store and checkout state (prototype — resets on restart)
# ---------------------------------------------------------------------------

# _CART_STORE[cart_id] = Cart — populated by tests / a future "add_to_cart" tool
_CART_STORE: dict[str, Cart] = {}

# Idempotency guard: set of cart_ids that have been successfully checked out (SB-6)
_CHECKED_OUT_CARTS: set[str] = set()

# Threading lock protecting cart checkout state transitions (SB-6)
_CHECKOUT_LOCK: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pydantic schemas — update_discount_status tool
# ---------------------------------------------------------------------------


class UpdateDiscountStatusInput(BaseModel):
    """Validated input for the update_discount_status tool.

    Attributes:
        admin_user_id: ID of the administrator performing the action.
        discount_code: The discount code to create or update (uppercase alphanumeric).
        active:        Whether the code should be active and redeemable.
        discount_percent: Required when creating a new code. The percentage off (1-100).
    """

    admin_user_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="ID of the administrator performing the action.",
    )
    discount_code: str = Field(
        ...,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Z0-9]+$",
        description="The discount code to create or update (uppercase alphanumeric).",
    )
    active: bool = Field(
        ...,
        description="Whether the code should be active and redeemable.",
    )
    discount_percent: int | None = Field(
        None,
        ge=1,
        le=100,
        description="Required when creating a new code. The percentage off (1-100).",
    )

    @field_validator("admin_user_id", mode="before")
    @classmethod
    def admin_user_id_no_whitespace(cls, v: str) -> str:
        """Reject admin_user_ids with leading or trailing whitespace."""
        if isinstance(v, str) and v != v.strip():
            raise ValueError(
                "admin_user_id must not have leading or trailing whitespace"
            )
        return v

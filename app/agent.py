# ruff: noqa
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

import logging
import math
import os
import threading
from typing import Optional

# pyrefly: ignore [missing-import]
import google.auth

# pyrefly: ignore [missing-import]
from google.adk.agents import Agent

# pyrefly: ignore [missing-import]
from google.adk.apps import App

# pyrefly: ignore [missing-import]
from google.adk.models import Gemini

from app.models import (
    AwardLoyaltyPointsInput,
    UpdateDiscountStatusInput,
    _LEDGER_LOCK,
    _LOYALTY_LEDGER,
    _PROCESSED_ORDERS,
)

_audit_log = logging.getLogger("shopping_assistant.audit")


_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"


# ---------------------------------------------------------------------------
# In-memory discount code store
# ---------------------------------------------------------------------------
# Each entry: {"discount": <percent_off>, "redeemed": <bool>, "active": <bool>}
_DISCOUNT_CODES: dict[str, dict] = {
    "WELCOME50": {"discount": 50, "redeemed": False, "active": True},
    "SUMMER20": {"discount": 20, "redeemed": False, "active": True},
}

_DISCOUNT_LOCK: threading.Lock = threading.Lock()

# Simulated set of registered user IDs.
_REGISTERED_USERS: set[str] = {"user_001", "user_002", "user_003"}

# Simulated set of administrator user IDs (SB-2).
_ADMIN_USERS: set[str] = {"admin_001", "super_admin"}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def redeem_discount_code(code: str, user_id: str) -> dict:
    """Attempt to redeem a single-use discount code for a registered user.

    Each code can only be redeemed once across all users.  The caller must
    supply a valid registered user ID — anonymous redemptions are rejected.

    Args:
        code:    The discount code string (e.g. "WELCOME50").
        user_id: The unique ID of the registered customer requesting redemption.

    Returns:
        A dict with keys:
          - "success" (bool): Whether the redemption succeeded.
          - "discount_percent" (int | None): The percentage discount applied,
            or None on failure.
          - "message" (str): Human-readable outcome description.
    """
    code = code.strip().upper()

    # Validate user
    if user_id not in _REGISTERED_USERS:
        return {
            "success": False,
            "discount_percent": None,
            "message": f"User '{user_id}' is not a registered account. "
            "Please sign in or create an account before redeeming codes.",
        }

    with _DISCOUNT_LOCK:
        # Validate code existence
        if code not in _DISCOUNT_CODES:
            return {
                "success": False,
                "discount_percent": None,
                "message": f"Code '{code}' is not recognised. Please check and try again.",
            }

        entry = _DISCOUNT_CODES[code]

        # Check active status
        if not entry.get("active", True):
            return {
                "success": False,
                "discount_percent": None,
                "message": f"Code '{code}' is currently inactive.",
            }

        # Check single-use constraint
        if entry["redeemed"]:
            return {
                "success": False,
                "discount_percent": None,
                "message": f"Code '{code}' has already been redeemed and cannot be used again.",
            }

        # Redeem
        entry["redeemed"] = True
        pct = entry["discount"]
    return {
        "success": True,
        "discount_percent": pct,
        "message": f"Code '{code}' redeemed successfully! You receive {pct}% off your order.",
    }


def browse_products(category: Optional[str] = None) -> list[dict]:
    """Return a sample list of products, optionally filtered by category.

    Args:
        category: Optional product category to filter by (e.g. "electronics",
                  "clothing", "home").  Pass None to return all products.

    Returns:
        A list of product dicts, each with keys "name", "category", and "price_usd".
    """
    catalogue = [
        {
            "name": "Wireless Noise-Cancelling Headphones",
            "category": "electronics",
            "price_usd": 79.99,
        },
        {"name": "Ergonomic Desk Chair", "category": "home", "price_usd": 249.99},
        {"name": "Merino Wool Sweater", "category": "clothing", "price_usd": 89.99},
        {
            "name": "Stainless Steel Water Bottle",
            "category": "home",
            "price_usd": 24.99,
        },
        {"name": "Running Shoes", "category": "clothing", "price_usd": 119.99},
        {"name": "Bluetooth Speaker", "category": "electronics", "price_usd": 49.99},
    ]
    if category:
        catalogue = [p for p in catalogue if p["category"].lower() == category.lower()]
    return catalogue


# ---------------------------------------------------------------------------
# Tool: award_loyalty_points
# ---------------------------------------------------------------------------


def award_loyalty_points(
    user_id: str,
    purchase_amount_usd: float,
    order_id: str,
) -> dict:
    """Award loyalty points to a registered user after a confirmed purchase.

    Points are calculated at a rate of 1 point per $1 USD spent (floor division).
    Each order can only be processed once — duplicate order_ids are rejected to
    prevent double-crediting (idempotency key).

    Args:
        user_id:              Registered user identifier (alphanumeric/underscore/hyphen).
        purchase_amount_usd:  Confirmed purchase total in USD (must be > 0, <= 100,000).
        order_id:             Unique order ID from the order management system.

    Returns:
        A dict with keys:
          - "success" (bool): Whether points were awarded.
          - "points_awarded" (int | None): Points credited this call, or None on failure.
          - "new_balance" (int | None): Updated total balance, or None on failure.
          - "message" (str): Human-readable outcome.
    """
    # --- Stage 1: Pydantic validation (CONTEXT.md §1 — rejects injections, bad amounts) ---
    try:
        params = AwardLoyaltyPointsInput(
            user_id=user_id,
            purchase_amount_usd=purchase_amount_usd,
            order_id=order_id,
        )
    except Exception as exc:
        return {
            "success": False,
            "points_awarded": None,
            "new_balance": None,
            "message": f"Invalid input: {exc}",
        }

    # --- Stage 2: Identity check (SB-4 — reject unregistered users) ---
    if params.user_id not in _REGISTERED_USERS:
        _audit_log.warning(
            "award_loyalty_points: rejected unregistered user",
            extra={"user_id": params.user_id, "order_id": params.order_id},
        )
        return {
            "success": False,
            "points_awarded": None,
            "new_balance": None,
            "message": "Unable to award points. Please verify your account and try again.",
        }

    # --- Stage 3 & 4: Idempotency guard + ledger mutation under lock (SB-3, SB-6) ---
    points = math.floor(params.purchase_amount_usd)

    with _LEDGER_LOCK:
        if params.order_id in _PROCESSED_ORDERS:
            _audit_log.warning(
                "award_loyalty_points: duplicate order rejected",
                extra={"user_id": params.user_id, "order_id": params.order_id},
            )
            return {
                "success": False,
                "points_awarded": None,
                "new_balance": _LOYALTY_LEDGER.get(params.user_id, 0),
                "message": f"Order '{params.order_id}' has already been processed. Points were not re-awarded.",
            }

        _PROCESSED_ORDERS.add(params.order_id)
        current = _LOYALTY_LEDGER.get(params.user_id, 0)
        new_balance = current + points
        _LOYALTY_LEDGER[params.user_id] = new_balance

    # --- Stage 5: Audit log (STRIDE R-1 fix) ---
    _audit_log.info(
        "award_loyalty_points: success",
        extra={
            "user_id": params.user_id,
            "order_id": params.order_id,
            "purchase_amount_usd": params.purchase_amount_usd,
            "points_awarded": points,
            "new_balance": new_balance,
        },
    )

    return {
        "success": True,
        "points_awarded": points,
        "new_balance": new_balance,
        "message": f"Awarded {points} loyalty points for order '{params.order_id}'. New balance: {new_balance} pts.",
    }


# ---------------------------------------------------------------------------
# Tool: update_discount_status
# ---------------------------------------------------------------------------


def update_discount_status(
    admin_user_id: str,
    discount_code: str,
    active: bool,
    discount_percent: Optional[int] = None,
) -> dict:
    """Administrator tool to create, activate, or deactivate discount codes.

    Args:
        admin_user_id: ID of the administrator performing the action.
        discount_code: The discount code to create or update (uppercase alphanumeric).
        active:        Whether the code should be active and redeemable.
        discount_percent: Required when creating a new code. The percentage off (1-100).

    Returns:
        A dict with the outcome of the action.
    """
    # Stage 1: Pydantic validation (SB-1, SB-4)
    try:
        params = UpdateDiscountStatusInput(
            admin_user_id=admin_user_id,
            discount_code=discount_code,
            active=active,
            discount_percent=discount_percent,
        )
    except Exception as exc:
        return {
            "success": False,
            "message": f"Invalid input: {exc}",
        }

    # Stage 2: Authorization check (SB-2)
    if params.admin_user_id not in _ADMIN_USERS:
        _audit_log.warning(
            "update_discount_status: unauthorized attempt",
            extra={
                "user_id": params.admin_user_id,
                "discount_code": params.discount_code,
            },
        )
        return {
            "success": False,
            "message": "Unauthorized: Only administrators can update discount codes.",
        }

    # Stage 3: Lock and update (SB-5)
    with _DISCOUNT_LOCK:
        if params.discount_code in _DISCOUNT_CODES:
            entry = _DISCOUNT_CODES[params.discount_code]
            entry["active"] = params.active
            if params.discount_percent is not None:
                entry["discount"] = params.discount_percent

            _audit_log.info(
                "update_discount_status: code updated",
                extra={
                    "admin": params.admin_user_id,
                    "discount_code": params.discount_code,
                    "active": params.active,
                    "discount": entry["discount"],
                },
            )
            return {
                "success": True,
                "message": f"Discount code '{params.discount_code}' updated successfully.",
            }
        else:
            if params.discount_percent is None:
                return {
                    "success": False,
                    "message": "discount_percent is required when creating a new discount code.",
                }

            _DISCOUNT_CODES[params.discount_code] = {
                "discount": params.discount_percent,
                "redeemed": False,
                "active": params.active,
            }

            _audit_log.info(
                "update_discount_status: new code created",
                extra={
                    "admin": params.admin_user_id,
                    "discount_code": params.discount_code,
                    "active": params.active,
                    "discount": params.discount_percent,
                },
            )
            return {
                "success": True,
                "message": f"New discount code '{params.discount_code}' created successfully.",
            }


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

# The GOOGLE_API_KEY should be set in the environment or .env file.
# We will no longer hardcode credentials.
# Ensure os is imported if not already, but we assume it's imported at the top.
# os.environ["GOOGLE_API_KEY"] is already populated by the environment.

from functools import cached_property
from google.adk.models import Gemini
from google.genai import Client
import os

class CustomGemini(Gemini):
    @cached_property
    def api_client(self) -> Client:
        from google.genai import types
        api_key = os.environ.get("GOOGLE_API_KEY")
        return Client(api_key=api_key, vertexai=False)

    @cached_property
    def _live_api_client(self) -> Client:
        from google.genai import types
        api_key = os.environ.get("GOOGLE_API_KEY")
        kwargs = {
            'http_options': types.HttpOptions(
                headers=self._tracking_headers(),
                api_version=self._live_api_version,
            )
        }
        return Client(api_key=api_key, vertexai=False, **kwargs)

root_agent = Agent(
    name="shopping_assistant",
    model=CustomGemini(model="gemini-3-flash-preview"),
    instruction=(
        "You are a friendly and knowledgeable AI shopping assistant for a retail store. "
        "Help customers discover products, answer questions about items in the catalogue, "
        "and redeem single-use discount codes. "
        "When a customer wants to redeem a discount code, always ask for their registered "
        "user ID before calling the redeem_discount_code tool. "
        "If redemption fails, explain why clearly and suggest next steps. "
        "After a customer completes a confirmed purchase, call award_loyalty_points with "
        "the confirmed order_id and purchase total to credit their loyalty points. "
        "NEVER call award_loyalty_points speculatively before a purchase is fully confirmed "
        "and a real order_id exists. "
        "When an administrator wants to manage discount codes, use the update_discount_status "
        "tool. Always ask for their administrative user ID before proceeding. Do NOT attempt to "
        "use this tool for regular shoppers. "
        "Be concise, helpful, and enthusiastic about helping customers find great deals."
    ),
    tools=[
        redeem_discount_code,
        browse_products,
        award_loyalty_points,
        update_discount_status,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)

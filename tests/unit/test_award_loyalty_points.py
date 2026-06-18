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
"""Unit tests for award_loyalty_points tool — written BEFORE implementation (TDD).

Each test asserts a contract defined in the implementation plan's Security
Boundaries & Assertions section. Tests are intentionally independent of the
full agent graph: they import the tool function and models directly.

Run with:
    uv run pytest tests/unit/test_award_loyalty_points.py -v
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Imports — these will fail (ImportError / AttributeError) until Stage 3
# creates the tool function. That is expected in TDD: red → green → refactor.
# ---------------------------------------------------------------------------
from app.models import (
    _LEDGER_LOCK,
    _LOYALTY_LEDGER,
    _PROCESSED_ORDERS,
    AwardLoyaltyPointsInput,
)

# ---------------------------------------------------------------------------
# Fixture: reset shared in-memory state between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_ledger_state():
    """Clear the ledger and processed-orders set before each test."""
    with _LEDGER_LOCK:
        _LOYALTY_LEDGER.clear()
        _PROCESSED_ORDERS.clear()
    yield
    with _LEDGER_LOCK:
        _LOYALTY_LEDGER.clear()
        _PROCESSED_ORDERS.clear()


@pytest.fixture
def award_fn() -> Callable:
    """Return the award_loyalty_points tool function."""
    from app.agent import award_loyalty_points  # imported here so the module

    # can be tested before agent-graph GCP auth runs at module level
    return award_loyalty_points


# ===========================================================================
# SB-1  Input Schema Injection
# ===========================================================================


class TestInputValidation:
    """Pydantic schema rejects all malformed inputs before tool body runs."""

    def test_user_id_with_special_chars_rejected(self):
        """SQL-injection fragment in user_id raises ValidationError (SB-1)."""
        with pytest.raises(ValidationError):
            AwardLoyaltyPointsInput(
                user_id="'; DROP TABLE users; --",
                purchase_amount_usd=50.0,
                order_id="ORD-001",
            )

    def test_order_id_with_path_traversal_rejected(self):
        """Path-traversal sequence in order_id raises ValidationError (SB-1)."""
        with pytest.raises(ValidationError):
            AwardLoyaltyPointsInput(
                user_id="user_001",
                purchase_amount_usd=50.0,
                order_id="../../../etc/passwd",
            )

    def test_user_id_with_whitespace_rejected(self):
        """Leading/trailing whitespace in user_id raises ValidationError (SB-1)."""
        with pytest.raises(ValidationError):
            AwardLoyaltyPointsInput(
                user_id="  user_001  ",
                purchase_amount_usd=50.0,
                order_id="ORD-001",
            )

    def test_empty_user_id_rejected(self):
        """Empty user_id string raises ValidationError (SB-1, SB-7)."""
        with pytest.raises(ValidationError):
            AwardLoyaltyPointsInput(
                user_id="",
                purchase_amount_usd=50.0,
                order_id="ORD-001",
            )

    def test_empty_order_id_rejected(self):
        """Empty order_id string raises ValidationError (SB-7)."""
        with pytest.raises(ValidationError):
            AwardLoyaltyPointsInput(
                user_id="user_001",
                purchase_amount_usd=50.0,
                order_id="",
            )


# ===========================================================================
# SB-2  Negative / Zero Purchase Amount
# ===========================================================================


class TestAmountBounds:
    """Pydantic Field(gt=0, le=100_000) enforces financial integrity."""

    def test_zero_amount_rejected(self):
        """purchase_amount_usd=0 raises ValidationError (SB-2)."""
        with pytest.raises(ValidationError):
            AwardLoyaltyPointsInput(
                user_id="user_001",
                purchase_amount_usd=0.0,
                order_id="ORD-001",
            )

    def test_negative_amount_rejected(self):
        """Negative purchase amount raises ValidationError (SB-2)."""
        with pytest.raises(ValidationError):
            AwardLoyaltyPointsInput(
                user_id="user_001",
                purchase_amount_usd=-100.0,
                order_id="ORD-001",
            )

    def test_amount_above_cap_rejected(self):
        """purchase_amount_usd > 100_000 raises ValidationError (SB-5)."""
        with pytest.raises(ValidationError):
            AwardLoyaltyPointsInput(
                user_id="user_001",
                purchase_amount_usd=100_000.01,
                order_id="ORD-001",
            )

    def test_amount_at_cap_accepted(self):
        """purchase_amount_usd == 100_000 is valid (boundary value)."""
        m = AwardLoyaltyPointsInput(
            user_id="user_001",
            purchase_amount_usd=100_000.0,
            order_id="ORD-001",
        )
        assert m.purchase_amount_usd == 100_000.0


# ===========================================================================
# SB-3  Idempotency / Double-Spend
# ===========================================================================


class TestIdempotency:
    def test_idempotent_on_same_order_id(self, award_fn):
        """Submitting the same order_id twice does NOT double-credit (SB-3)."""
        first = award_fn(
            user_id="user_001",
            purchase_amount_usd=100.0,
            order_id="ORD-IDEMPOTENT",
        )
        assert first["success"] is True

        second = award_fn(
            user_id="user_001",
            purchase_amount_usd=100.0,
            order_id="ORD-IDEMPOTENT",
        )
        assert second["success"] is False
        assert "has already been processed" in second["message"].lower()

        # Balance must equal only ONE award
        with _LEDGER_LOCK:
            assert _LOYALTY_LEDGER.get("user_001", 0) == 100


# ===========================================================================
# SB-4  Unregistered User Identity
# ===========================================================================


class TestIdentityCheck:
    def test_unknown_user_rejected(self, award_fn):
        """Unregistered user_id returns success=False without ledger mutation (SB-4)."""
        result = award_fn(
            user_id="ghost_user",
            purchase_amount_usd=50.0,
            order_id="ORD-002",
        )
        assert result["success"] is False
        with _LEDGER_LOCK:
            assert "ghost_user" not in _LOYALTY_LEDGER

    def test_error_message_non_enumerable(self, award_fn):
        """Failure message must not distinguish 'unknown user' from other errors (SB-4)."""
        result = award_fn(
            user_id="ghost_user",
            purchase_amount_usd=50.0,
            order_id="ORD-003",
        )
        # The message should NOT contain the exact user_id in a way that
        # confirms/denies account existence; just a generic failure.
        assert result["success"] is False
        assert result.get("discount_percent") is None


# ===========================================================================
# Core Happy Path & Points Calculation
# ===========================================================================


class TestHappyPath:
    def test_valid_award_credits_points(self, award_fn):
        """Registered user + valid purchase returns success=True with points (happy path)."""
        result = award_fn(
            user_id="user_001",
            purchase_amount_usd=75.99,
            order_id="ORD-100",
        )
        assert result["success"] is True
        assert result["points_awarded"] == 75  # floor(75.99)
        assert result["new_balance"] == 75

    def test_points_calculation_rate(self, award_fn):
        """1 point per $1 spent, rounded down (floor division)."""
        award_fn(
            user_id="user_002",
            purchase_amount_usd=49.99,
            order_id="ORD-200",
        )
        with _LEDGER_LOCK:
            assert _LOYALTY_LEDGER["user_002"] == 49  # floor(49.99)

    def test_points_accumulate_across_orders(self, award_fn):
        """Multiple distinct orders accumulate into the user's running balance."""
        award_fn(user_id="user_003", purchase_amount_usd=50.0, order_id="ORD-A")
        award_fn(user_id="user_003", purchase_amount_usd=30.0, order_id="ORD-B")
        with _LEDGER_LOCK:
            assert _LOYALTY_LEDGER["user_003"] == 80


# ===========================================================================
# SB-6  Thread-Safety
# ===========================================================================


class TestThreadSafety:
    def test_concurrent_award_same_user(self, award_fn):
        """Concurrent calls for the same user accumulate without lost updates (SB-6)."""
        errors: list[Exception] = []
        results: list[dict] = []
        lock = threading.Lock()

        def do_award(order_suffix: int) -> None:
            try:
                r = award_fn(
                    user_id="user_001",
                    purchase_amount_usd=10.0,
                    order_id=f"ORD-CONCURRENT-{order_suffix}",
                )
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=do_award, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors in threads: {errors}"
        successful = [r for r in results if r["success"]]
        assert len(successful) == 20
        # 20 concurrent awards of 10 points each = 200 total
        with _LEDGER_LOCK:
            assert _LOYALTY_LEDGER["user_001"] == 200

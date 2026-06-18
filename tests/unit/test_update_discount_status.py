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
"""Unit tests for update_discount_status tool — written BEFORE implementation (TDD).

Each test asserts a contract defined in the implementation plan's Security
Boundaries & Assertions section.

Run with:
    uv run pytest tests/unit/test_update_discount_status.py -v
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from app.models import UpdateDiscountStatusInput

# ---------------------------------------------------------------------------
# Fixture: reset shared in-memory state between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_discount_state():
    """Clear and re-seed the discount codes before each test."""
    from app.agent import _DISCOUNT_CODES, _DISCOUNT_LOCK

    with _DISCOUNT_LOCK:
        _DISCOUNT_CODES.clear()
        _DISCOUNT_CODES["WELCOME50"] = {
            "discount": 50,
            "redeemed": False,
            "active": True,
        }
        _DISCOUNT_CODES["SUMMER20"] = {
            "discount": 20,
            "redeemed": False,
            "active": True,
        }
    yield
    with _DISCOUNT_LOCK:
        _DISCOUNT_CODES.clear()
        _DISCOUNT_CODES["WELCOME50"] = {
            "discount": 50,
            "redeemed": False,
            "active": True,
        }
        _DISCOUNT_CODES["SUMMER20"] = {
            "discount": 20,
            "redeemed": False,
            "active": True,
        }


@pytest.fixture
def update_fn() -> Callable:
    """Return the update_discount_status tool function."""
    from app.agent import update_discount_status

    return update_discount_status


@pytest.fixture
def redeem_fn() -> Callable:
    """Return the redeem_discount_code tool function for integration tests."""
    from app.agent import redeem_discount_code

    return redeem_discount_code


# ===========================================================================
# SB-1 Input Injection
# ===========================================================================


class TestInputValidation:
    """Pydantic schema rejects all malformed inputs before tool body runs."""

    def test_injection_in_discount_code(self):
        """SQL-injection fragment in discount_code raises ValidationError (SB-1)."""
        with pytest.raises(ValidationError):
            UpdateDiscountStatusInput(
                admin_user_id="admin_001",
                discount_code="'; DROP TABLE users; --",
                active=True,
                discount_percent=20,
            )

    def test_lowercase_discount_code_rejected(self):
        """Lowercase discount_code raises ValidationError (SB-1)."""
        with pytest.raises(ValidationError):
            UpdateDiscountStatusInput(
                admin_user_id="admin_001",
                discount_code="welcome50",
                active=True,
                discount_percent=50,
            )

    def test_admin_user_id_with_whitespace_rejected(self):
        with pytest.raises(ValidationError):
            UpdateDiscountStatusInput(
                admin_user_id=" admin_001 ",
                discount_code="NEWCODE10",
                active=True,
                discount_percent=10,
            )


# ===========================================================================
# SB-4 Financial Integrity (Percentage Bounds)
# ===========================================================================


class TestFinancialIntegrity:
    def test_percent_out_of_bounds_high(self):
        with pytest.raises(ValidationError):
            UpdateDiscountStatusInput(
                admin_user_id="admin_001",
                discount_code="FREE101",
                active=True,
                discount_percent=101,
            )

    def test_percent_out_of_bounds_low(self):
        with pytest.raises(ValidationError):
            UpdateDiscountStatusInput(
                admin_user_id="admin_001",
                discount_code="FREE0",
                active=True,
                discount_percent=0,
            )


# ===========================================================================
# SB-2 Elevation of Privilege
# ===========================================================================


class TestElevationOfPrivilege:
    def test_non_admin_rejected(self, update_fn):
        """Normal user attempting admin action is rejected (SB-2)."""
        result = update_fn(
            admin_user_id="user_001",  # A regular user
            discount_code="NEWCODE",
            active=True,
            discount_percent=50,
        )
        assert result["success"] is False
        assert "Unauthorized" in result["message"]

        from app.agent import _DISCOUNT_CODES

        assert "NEWCODE" not in _DISCOUNT_CODES


# ===========================================================================
# Happy Path & Logic
# ===========================================================================


class TestAdminHappyPath:
    def test_admin_can_create_new_code(self, update_fn):
        result = update_fn(
            admin_user_id="admin_001",
            discount_code="WINTER30",
            active=True,
            discount_percent=30,
        )
        assert result["success"] is True

        from app.agent import _DISCOUNT_CODES

        assert "WINTER30" in _DISCOUNT_CODES
        assert _DISCOUNT_CODES["WINTER30"]["discount"] == 30
        assert _DISCOUNT_CODES["WINTER30"]["active"] is True
        assert _DISCOUNT_CODES["WINTER30"]["redeemed"] is False

    def test_missing_percent_for_new_code(self, update_fn):
        result = update_fn(
            admin_user_id="admin_001",
            discount_code="FALL15",
            active=True,
            discount_percent=None,
        )
        assert result["success"] is False
        assert "discount_percent is required" in result["message"]

    def test_admin_can_deactivate_code(self, update_fn):
        result = update_fn(
            admin_user_id="admin_001", discount_code="WELCOME50", active=False
        )
        assert result["success"] is True

        from app.agent import _DISCOUNT_CODES

        assert _DISCOUNT_CODES["WELCOME50"]["active"] is False

    def test_admin_can_update_existing_code_percent(self, update_fn):
        result = update_fn(
            admin_user_id="admin_001",
            discount_code="WELCOME50",
            active=True,
            discount_percent=60,
        )
        assert result["success"] is True

        from app.agent import _DISCOUNT_CODES

        assert _DISCOUNT_CODES["WELCOME50"]["discount"] == 60


# ===========================================================================
# Integration with redeem_discount_code
# ===========================================================================


class TestIntegration:
    def test_deactivated_code_cannot_be_redeemed(self, update_fn, redeem_fn):
        # Admin deactivates
        update_fn(admin_user_id="admin_001", discount_code="WELCOME50", active=False)

        # User tries to redeem
        result = redeem_fn(code="WELCOME50", user_id="user_001")
        assert result["success"] is False
        assert "inactive" in result["message"].lower()

        # Re-activate
        update_fn(admin_user_id="admin_001", discount_code="WELCOME50", active=True)

        # User tries to redeem again
        result = redeem_fn(code="WELCOME50", user_id="user_001")
        assert result["success"] is True


# ===========================================================================
# SB-5 Thread-Safety
# ===========================================================================


class TestThreadSafety:
    def test_concurrent_discount_updates(self, update_fn):
        """Ensure updates are serialized properly (SB-5)."""
        errors: list[Exception] = []
        lock = threading.Lock()

        def do_update(pct: int) -> None:
            try:
                update_fn(
                    admin_user_id="admin_001",
                    discount_code="CONCUR10",
                    active=True,
                    discount_percent=pct,
                )
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=do_update, args=(i,)) for i in range(1, 21)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        from app.agent import _DISCOUNT_CODES

        assert "CONCUR10" in _DISCOUNT_CODES
        assert 1 <= _DISCOUNT_CODES["CONCUR10"]["discount"] <= 20

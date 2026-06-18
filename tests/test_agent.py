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
"""Outcome-based security test suite for the agent using agents-cli.

This integration suite treats the agent as a black box and asserts that its
natural-language outputs respect the required security guardrails for the
redeem_discount_code tool.
"""

import subprocess
import uuid

import pytest


def run_agent(prompt: str, session_id: str | None = None) -> str:
    """Run the agent and return its text output."""
    cmd = ["agents-cli", "run", prompt]
    if session_id:
        cmd.extend(["--session-id", session_id])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        pytest.fail(f"Agent failed to run:\n{result.stderr}")
    return result.stdout


@pytest.fixture(autouse=True)
def clean_agent_server():
    """Ensure a fresh server for each test so in-memory state is reset."""
    # Stop any existing server just in case
    subprocess.run(["agents-cli", "run", "--stop-server"], capture_output=True)

    # Start a persistent server so that multi-turn memory and in-memory dicts
    # are preserved across `run_agent` calls within the same test.
    subprocess.run(["agents-cli", "run", "--start-server", "ping"], capture_output=True)

    yield

    # Stop the server after the test to tear down state
    subprocess.run(["agents-cli", "run", "--stop-server"], capture_output=True)


@pytest.fixture
def session_id() -> str:
    """Unique session ID for multi-turn tests."""
    return str(uuid.uuid4())


def test_anonymous_identity_protection():
    """Test that the agent refuses to redeem without a user ID."""
    response = run_agent("I want to redeem WELCOME50.")
    assert "user" in response.lower() or "id" in response.lower()
    assert "50%" not in response  # Should not successfully redeem


def test_unregistered_user_block():
    """Test that an unregistered user ID is blocked."""
    response = run_agent("I want to redeem WELCOME50. My user ID is unknown_999.")
    assert "not a registered account" in response.lower() or "not registered" in response.lower() or "sign in" in response.lower()
    assert "50%" not in response


def test_invalid_code_handling():
    """Test that an invalid code is reported correctly."""
    response = run_agent("Redeem FAKECODE for user_001.")
    assert "not recognised" in response.lower() or "check and try again" in response.lower() or "invalid" in response.lower()


def test_inactive_code_protection(session_id):
    """Test that an inactive code cannot be redeemed."""
    # First, deactivate WELCOME50 as an admin
    response_admin = run_agent("I am admin_001. Deactivate the code WELCOME50.", session_id=session_id)
    assert "successfully" in response_admin.lower() or "deactivated" in response_admin.lower()

    # Then try to redeem as a user
    response_user = run_agent("I am user_001. Redeem WELCOME50.", session_id=session_id)
    assert "inactive" in response_user.lower()
    assert "50%" not in response_user


def test_single_use_constraint(session_id):
    """Test that a code cannot be used twice."""
    # First redemption succeeds
    response_1 = run_agent("Redeem SUMMER20 for user_001.", session_id=session_id)
    assert "successfully" in response_1.lower() or "20%" in response_1.lower()

    # Second redemption fails
    response_2 = run_agent("Redeem SUMMER20 for user_002.", session_id=session_id)
    assert "already been redeemed" in response_2.lower() or "cannot be used again" in response_2.lower()

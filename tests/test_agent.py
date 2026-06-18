import subprocess
import re
import pytest

def run_agent(prompt: str, session_id: str | None = None) -> tuple[str, str]:
    """Run the agent and return (text output, session_id)."""
    cmd = ["agents-cli", "run", prompt]
    if session_id:
        cmd.extend(["--session-id", session_id])

    import os
    env = os.environ.copy()
    env["GOOGLE_GENAI_USE_VERTEXAI"] = "False"

    # Extract GOOGLE_API_KEY from ~/.zshrc since uv run might not source it
    zshrc_path = os.path.expanduser("~/.zshrc")
    if os.path.exists(zshrc_path):
        with open(zshrc_path, "r") as f:
            for line in f:
                if line.startswith("export GOOGLE_API_KEY="):
                    key = line.split("=")[1].strip().strip('"')
                    env["GOOGLE_API_KEY"] = key
                    env["GEMINI_API_KEY"] = key
                    break
    import time

    # Sleep to avoid hitting Free Tier rate limits (5 req/min)
    time.sleep(15)

    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        pytest.fail(f"Agent failed to run:\n{result.stderr}")

    # Extract response text. It's the text between [shopping_assistant]: and \n\nSession:
    match = re.search(r"\[shopping_assistant\]:\s*(.*?)(?:\n\nSession:|\Z)", result.stdout, re.DOTALL)
    text = match.group(1).strip() if match else result.stdout

    # Extract session ID
    session_match = re.search(r"Session:\s*([0-9a-fA-F-]+)", result.stdout)
    out_session_id = session_match.group(1) if session_match else ""

    return text, out_session_id

@pytest.fixture(autouse=True)
def clean_agent_server():
    """Ensure a fresh server for each test so in-memory state is reset."""
    import os
    env = os.environ.copy()
    env["GOOGLE_GENAI_USE_VERTEXAI"] = "False"

    # Extract GOOGLE_API_KEY from ~/.zshrc
    zshrc_path = os.path.expanduser("~/.zshrc")
    if os.path.exists(zshrc_path):
        with open(zshrc_path, "r") as f:
            for line in f:
                if line.startswith("export GOOGLE_API_KEY="):
                    key = line.split("=")[1].strip().strip('"')
                    env["GOOGLE_API_KEY"] = key
                    env["GEMINI_API_KEY"] = key
                    break

    # Stop any existing server just in case
    subprocess.run(["agents-cli", "run", "--stop-server"], capture_output=True, env=env)

    # Start a persistent server so that multi-turn memory and in-memory dicts
    # are preserved across `run_agent` calls within the same test.
    subprocess.run(["agents-cli", "run", "--start-server", "ping"], capture_output=True, env=env)

    yield

    # Stop the server after the test to tear down state
    subprocess.run(["agents-cli", "run", "--stop-server"], capture_output=True, env=env)

def test_anonymous_identity_protection():
    response, _ = run_agent("I want to redeem WELCOME50.")
    assert "user" in response.lower() or "id" in response.lower()
    assert "50%" not in response

def test_unregistered_user_block():
    response, _ = run_agent("I want to redeem WELCOME50. My user ID is unknown_999.")
    assert "not a registered account" in response.lower() or "not registered" in response.lower() or "sign in" in response.lower()
    assert "50%" not in response

def test_invalid_code_handling():
    response, _ = run_agent("Redeem FAKECODE for user_001.")
    assert "not recognised" in response.lower() or "check and try again" in response.lower() or "invalid" in response.lower()

def test_inactive_code_protection():
    response_admin, sid = run_agent("I am admin_001. Deactivate the code WELCOME50.")
    # The agent might just succeed with an empty text if the tool returns success, let's just make sure it didn't fail

    response_user, _ = run_agent("I am user_001. Redeem WELCOME50.", session_id=sid)
    assert "inactive" in response_user.lower() or "deactivated" in response_user.lower()
    assert "50%" not in response_user

def test_single_use_constraint():
    response_1, sid = run_agent("Redeem SUMMER20 for user_001.")
    # Wait, if response is empty, how do we assert? We don't really care, we want to test the second response.

    response_2, _ = run_agent("Redeem SUMMER20 for user_002.", session_id=sid)
    assert "already been redeemed" in response_2.lower() or "cannot be used again" in response_2.lower()

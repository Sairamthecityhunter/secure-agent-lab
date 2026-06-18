# Threat Model: shopping-assistant Agent

**Assessment Date:** 2026-06-18
**Skill:** `stride-threat-model`
**Scope:** `shopping-assistant/` — ADK 2.0 prototype agent with discount-code
redemption, product browsing, in-memory state, FastAPI HTTP layer, and
pre-commit security gate.

---

## 1. System Boundary Map

### Entry Points

| Entry Point | Description | Auth Layer |
|-------------|-------------|-----------|
| **ADK Playground** (`agents-cli playground`) | Local developer console; sends free-text turns to `root_agent` | None — localhost only |
| **FastAPI HTTP** (`fast_api_app.py`, port 8000) | Production HTTP API via `get_fast_api_app()`; exposes ADK routes + `/feedback` | CORS via `ALLOW_ORIGINS` env var; no auth middleware configured |
| **`/feedback` POST endpoint** | Accepts arbitrary `Feedback` struct, logs to Cloud Logging | No authentication |
| **`redeem_discount_code` tool** | LLM-invoked; mutates in-memory `_DISCOUNT_CODES` dict | User ID allowlist check (in-code only) |
| **`browse_products` tool** | LLM-invoked; read-only product catalogue | None — public |
| **`run_command` hook** | Agent runtime shell execution, gated by `hooks.json` + `validate_tool_call.py` | Allowlist + blocklist regex policy |

### Data Storage Layers

| Layer | Technology | Persistence | Sensitivity |
|-------|-----------|------------|------------|
| **Discount code store** | `_DISCOUNT_CODES` Python dict (module global) | In-process only; resets on restart | **HIGH** — financial value |
| **Registered user store** | `_REGISTERED_USERS` Python set (module global) | In-process only | **MEDIUM** — identity data |
| **Session state** | ADK in-memory session (`session_service_uri=None`) | In-process only | LOW |
| **Telemetry / logs** | Google Cloud Logging via `otel_to_cloud=True` | GCP project `animated-flare-421518` | MEDIUM |
| **Artifact store** | GCS bucket (`LOGS_BUCKET_NAME` env var) | Persistent | LOW–MEDIUM |
| **Source secrets** | `GEMINI_API_KEY = "AIzaSyD-..."` literal in `agent.py:143` | Git history | **CRITICAL** |

---

## 2. STRIDE Evaluation

### 🔴 S — Spoofing: *Caller identity is not cryptographically verified*

**Finding S-1 — User ID supplied by the LLM, not the session layer**
The `redeem_discount_code` tool accepts `user_id` as a plain string argument
populated by the LLM from conversation context. Any user who tells the agent
"my user ID is user_001" can pass the identity check, because there is no
binding between the authenticated session principal and the `user_id`
parameter the LLM passes to the tool.

- **Location:** `app/agent.py:68-75` — `if user_id not in _REGISTERED_USERS`
- **Risk:** Medium-High. In prototype mode (no auth), this is trivially bypassable.
- **Recommendation:** Bind `user_id` to the ADK session context
  (`tool_context.state["user_id"]`) populated from a verified auth token at
  session creation, not from free-text conversation.

**Finding S-2 — FastAPI layer has no authentication middleware**
`fast_api_app.py` mounts ADK routes with no authentication guard. Any caller
who can reach port 8000 can invoke the agent as any identity.

- **Location:** `app/fast_api_app.py:41-48`
- **Risk:** High in production.
- **Recommendation:** Add a Firebase Auth / Google Identity Platform middleware
  that validates a Bearer token and injects the verified UID into session state.

---

### 🟠 T — Tampering: *In-memory state is mutable without write controls*

**Finding T-1 — `_DISCOUNT_CODES` dict is a mutable module global with no lock**
The `entry["redeemed"] = True` mutation at `agent.py:96` is not protected by
a mutex. Under concurrent requests (multiple FastAPI workers or async ADK
sessions), two simultaneous redemption calls for the same code could both read
`redeemed=False` and both succeed — a classic TOCTOU (time-of-check /
time-of-use) race condition.

- **Location:** `app/agent.py:84-101`
- **Risk:** Medium. One code could be redeemed multiple times under load.
- **Recommendation:** Replace the bare dict with a `threading.Lock`-guarded
  operation, or back the store with a database supporting atomic
  compare-and-swap (e.g., Firestore transactions, Redis `SET NX`).

**Finding T-2 — `category` parameter in `browse_products` is not validated**
The `category` argument is compared with `.lower()` but has no schema
validation. A maliciously crafted category string (e.g., a very long string or
one containing special characters) is passed directly into the filter logic.

- **Location:** `app/agent.py:131-132`
- **Risk:** Low. No injection surface in current implementation, but could
  become one if the catalogue moves to a real database.
- **Recommendation:** Validate `category` against a Pydantic `Literal` enum of
  allowed values per the `CONTEXT.md §1` paved road.

---

### 🟡 R — Repudiation: *No tamper-evident audit trail for financial transactions*

**Finding R-1 — Discount code redemptions are not durably logged**
A successful call to `redeem_discount_code` mutates in-memory state and
returns a response, but does not write a structured audit log entry. If the
process restarts, the redemption history is lost — making it impossible to
dispute or investigate claimed redemptions.

- **Location:** `app/agent.py:95-101` — no logging call present
- **Risk:** High for a production financial system.
- **Recommendation:** Emit a structured log entry (via `google.cloud.logging`
  already available in `fast_api_app.py`) for every redemption attempt —
  success and failure — including `user_id`, `code`, `timestamp`, and outcome.

**Finding R-2 — `/feedback` endpoint logs without correlation to session**
`POST /feedback` logs the raw feedback payload to Cloud Logging but does not
attach a session ID or user identity, making it impossible to correlate
feedback with a specific conversation.

- **Location:** `app/fast_api_app.py:63`
- **Risk:** Low.
- **Recommendation:** Include `session_id` and authenticated `user_id` in the
  log struct.

---

### 🔴 I — Information Disclosure: *Hardcoded credential in source*

**Finding I-1 — CRITICAL: Hardcoded Google API key in committed source**
`GEMINI_API_KEY = "AIzaSyD-mock-key-value-12345"` is assigned as a Python
module-level literal. Even though this is a mock value for demo purposes, the
pattern demonstrates a **CWE-798** violation that would expose a real key in
git history permanently — even if later removed.

- **Location:** `app/agent.py:143-144`
- **Severity:** CRITICAL (flagged by `semgrep.hardcoded-google-api-key` rule)
- **Status:** Intentional demo — pre-commit gate actively blocks commits
  containing this pattern.
- **Recommendation (remediation):** Remove the literal. Load via
  `os.environ.get("GOOGLE_API_KEY")` set externally, or use
  `google.cloud.secretmanager` to fetch at runtime. Rotate the key immediately
  if a real key is ever committed.

**Finding I-2 — Raw exception details may leak internal paths via HTTP**
FastAPI's default error handler returns full Python tracebacks in development
mode. If `ENVIRONMENT` is not explicitly set to production, unhandled exceptions
in tool code will return stack traces to callers.

- **Location:** `app/fast_api_app.py` — no custom exception handler registered
- **Risk:** Medium.
- **Recommendation:** Register a global FastAPI exception handler that returns
  a generic error message and logs the traceback server-side only.

**Finding I-3 — Feedback endpoint reflects arbitrary user input into Cloud Logging**
`logger.log_struct(feedback.model_dump(), ...)` writes the full, user-controlled
feedback payload to Cloud Logging without sanitisation.

- **Location:** `app/fast_api_app.py:63`
- **Risk:** Low (log injection). Strings are not executed, but could pollute
  log analysis.
- **Recommendation:** Sanitise or truncate string fields before logging.

---

### 🟠 D — Denial of Service: *No rate limiting or query budgets*

**Finding D-1 — No rate limit on LLM invocations**
Every HTTP request to the ADK FastAPI routes results in one or more Gemini
model calls (each potentially expensive). There is no per-IP, per-session, or
per-user rate limiter configured.

- **Location:** `app/fast_api_app.py:41-48` — no middleware
- **Risk:** High. A single abusive client can exhaust GCP quota and incur
  unbounded cost.
- **Recommendation:** Add a rate-limiting middleware (e.g., `slowapi` or a
  Cloud Armor policy at the load balancer) before the ADK routes.

**Finding D-2 — Discount code enumeration is unrestricted**
The tool returns distinct error messages for "code not found" vs. "code already
redeemed". An attacker can enumerate valid codes by brute-forcing until they
get "already redeemed" instead of "not recognised". There is no attempt-limit
or lockout.

- **Location:** `app/agent.py:77-93`
- **Risk:** Medium.
- **Recommendation:** Use a single generic failure message that does not
  distinguish between "unknown" and "used" states. Add an attempt counter per
  session.

**Finding D-3 — `browse_products` has no pagination**
The tool returns the entire catalogue on every call. As the catalogue grows,
this becomes an unbounded response payload.

- **Location:** `app/agent.py:114-133`
- **Risk:** Low (currently 6 items).
- **Recommendation:** Add `limit` and `offset` parameters; default `limit=20`.

---

### 🔴 E — Elevation of Privilege: *Unauthenticated access to financial tool*

**Finding E-1 — `redeem_discount_code` is reachable without authentication**
As established in S-2, the FastAPI layer has no auth middleware. Any caller
reaching the HTTP endpoint can instruct the agent to call
`redeem_discount_code` with an arbitrary `user_id` that happens to be in the
three-element `_REGISTERED_USERS` set (`user_001`, `user_002`, `user_003`).
Since the registered user IDs are simple sequential strings, they are trivially
guessable.

- **Location:** `app/agent.py:41` + `fast_api_app.py:41-48`
- **Risk:** **CRITICAL** for production.
- **Recommendation:**
  1. Fix S-2 (add auth middleware).
  2. Replace `_REGISTERED_USERS` with a lookup against a real user database
     keyed on non-guessable IDs (UUID or opaque token).
  3. Bind `user_id` to the verified session principal (fix S-1).

**Finding E-2 — `run_command` hook uses a prefix allowlist, not a full-command hash**
The `validate_tool_call.py` allowlist checks only command *prefixes*. A command
like `uv run python -c "import os; os.system('rm -rf /')"` starts with `uv run`
(approved prefix) but executes arbitrary code. The blocklist regex for `rm -rf`
would catch this specific example, but the general prefix-bypass class of
attack is not fully closed.

- **Location:** `.agents/scripts/validate_tool_call.py:112-115`
- **Risk:** Medium.
- **Recommendation:** After prefix matching, additionally parse and validate
  each sub-command token, or switch to a hash-based allowlist of fully-
  specified approved commands rather than prefix matching.

---

## 3. Risk Summary

| ID | STRIDE Pillar | Severity | Status |
|----|--------------|----------|--------|
| I-1 | Information Disclosure | 🔴 CRITICAL | Pre-commit gate active; demo only |
| E-1 | Elevation of Privilege | 🔴 CRITICAL | No auth in prototype |
| S-1 | Spoofing | 🔴 HIGH | LLM-supplied user_id, not session-bound |
| S-2 | Spoofing | 🔴 HIGH | No FastAPI auth middleware |
| R-1 | Repudiation | 🟠 HIGH | No redemption audit log |
| D-1 | Denial of Service | 🟠 HIGH | No rate limiting |
| T-1 | Tampering | 🟠 MEDIUM | TOCTOU race on redemption state |
| D-2 | Denial of Service | 🟠 MEDIUM | Code enumeration via distinct error messages |
| E-2 | Elevation of Privilege | 🟠 MEDIUM | Prefix-bypass in hook allowlist |
| I-2 | Information Disclosure | 🟡 MEDIUM | Stack traces in HTTP responses |
| T-2 | Tampering | 🟡 LOW | Unvalidated `category` parameter |
| D-3 | Denial of Service | 🟡 LOW | No catalogue pagination |
| R-2 | Repudiation | 🟡 LOW | Feedback log lacks session correlation |
| I-3 | Information Disclosure | 🟡 LOW | Unsanitised feedback in Cloud Logging |

---

## 4. Recommended Remediation Priority

### Immediate (before any production deployment)
1. **I-1** — Remove hardcoded `GEMINI_API_KEY`; use Secret Manager.
2. **S-2 + E-1** — Add auth middleware (Firebase/IAP) to FastAPI; enforce it
   on all ADK routes.
3. **S-1** — Bind `user_id` to session-layer identity, not LLM conversation.

### Short-term (next sprint)
4. **R-1** — Add structured audit logging to every redemption attempt.
5. **T-1** — Replace bare dict mutation with a locked or transactional store.
6. **D-1** — Add rate-limiting middleware or Cloud Armor policy.
7. **D-2** — Normalize redemption failure messages; add attempt counting.

### Longer-term (hardening)
8. **E-2** — Upgrade hook allowlist from prefix-based to full-command validation.
9. **I-2** — Register global FastAPI exception handler; suppress tracebacks.
10. **T-2** — Validate `category` via Pydantic `Literal` schema.
11. **D-3** — Add pagination to `browse_products`.

---

## 5. Existing Controls (Positive Findings)

| Control | Effectiveness |
|---------|--------------|
| Pre-commit semgrep gate (`hardcoded-google-api-key` rule) | ✅ Actively catches I-1 before commit |
| `end-of-file-fixer` + `trailing-whitespace` hooks | ✅ File hygiene enforced |
| `validate_tool_call.py` — `run_command` blocklist | ✅ Blocks `rm -rf`, `sudo`, pipe-to-shell |
| Single-use code constraint (`entry["redeemed"] = True`) | ✅ Correct intent; vulnerable to TOCTOU (T-1) |
| User ID allowlist before code redemption | ✅ Correct layering; weak set (S-1, E-1) |
| `ruff` + `ty` linting in CI | ✅ Catches type errors and style violations |
| OpenTelemetry + Cloud Logging configured | ✅ Foundation for audit trail (incomplete per R-1) |
| CORS configured via `ALLOW_ORIGINS` env var | ✅ Prevents unintended browser-origin calls |

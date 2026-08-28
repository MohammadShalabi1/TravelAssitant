# Rihla / TravelAssitant — Production-Readiness Tasks

> Goal: turn the current TravelAssitant repository into a deployable, portfolio-quality AI travel assistant that demonstrates **junior+ to mid-level engineering** across AI engineering, backend reliability, security, system design, testing, observability, and deployment.
>
> Repository analyzed: `MohammadShalabi1/TravelAssitant`
>
> Priority legend:
> - **P0** = security/correctness blocker before public deployment
> - **P1** = required for a credible production-ready release
> - **P2** = strong portfolio / mid-level improvement
> - **J+** = junior+ implementation depth
> - **MID** = mid-level design/engineering depth

---

## 0. Current Architecture Snapshot

Current project already contains useful production-oriented pieces:

- React + Vite frontend.
- FastAPI backend.
- PostgreSQL conversation persistence.
- JWT authentication.
- Gemini agent with explicit tool/function calling.
- Weather, geocoding, and nearby-place tools.
- Redis-backed semantic cache with in-memory fallback.
- Per-session and per-IP rate limiting.
- Tool retry with exponential backoff.
- Bounded agent tool loop.
- Health and metrics endpoints.
- Request timing and security-header middleware.
- Vercel configuration for the frontend.

The roadmap below focuses on the gaps that still prevent this from being safely called production-ready.

---

# Milestone 1 — Fix Production Blockers

## 1.1 P0 — Enforce Session Ownership and Prevent BOLA / IDOR
**Level: J+ → MID**  
**System design terms:** object-level authorization, tenant isolation, authorization boundary, least privilege.

The API authenticates the caller, but operations that receive `session_id` must also prove that the session belongs to `current_user.user_id`.

- [x] Add a repository function such as `get_owned_conversation(session_id, user_id)`.
- [x] Make ownership validation part of every session-scoped query, preferably directly in SQL:
  - `WHERE session_id = %s AND user_id = %s AND deleted_at IS NULL`
- [x] Enforce ownership in:
  - [x] `POST /api/chat`
  - [x] `GET /api/sessions/{session_id}/history`
  - [x] `PATCH /api/sessions/{session_id}/rename`
  - [x] `DELETE /api/sessions/{session_id}`
  - [x] `GET /api/sessions/{session_id}/export`
- [x] Return `404` for inaccessible sessions instead of revealing whether another user's session exists.
- [x] Never expose another user's messages through cache, export, history, or agent context.
- [x] Add integration tests with **User A** and **User B** proving that User B cannot read, mutate, delete, export, or chat against User A's session.

### Acceptance criteria
- [x] A valid JWT is not enough to access an arbitrary `session_id`.
- [x] Cross-user access tests consistently return `404`/forbidden behavior.
- [x] No repository/service function can load private session data without a user ownership scope.

---

## 1.2 P0 — Fix Semantic Cache Isolation and Context Leakage
**Level: MID**  
**AI engineering terms:** semantic cache, cache key design, context sensitivity, tenant isolation, stale response risk.

The current semantic cache is based mainly on user text similarity. A personalized answer can be unsafe to reuse across users or across conversations with different context.

- [x] Classify cacheable requests into:
  - [x] **Global/stateless:** deterministic public data where reuse is safe.
  - [x] **User-scoped:** may depend on user/session preferences.
  - [x] **Non-cacheable:** conversation-sensitive or security-sensitive prompts.
- [x] Prefer caching **tool results** (weather/geocoding/place lookups) rather than arbitrary final assistant messages.
- [x] If final-answer caching remains, include these in the cache identity:
  - normalized query
  - model/version
  - prompt version
  - relevant conversation-context hash
  - user/session scope when personalized
- [x] Do not cache authentication failures, rate-limit responses, errors, or blocked security prompts.
- [x] Add cache invalidation/versioning when the system prompt or model changes.
- [x] Add cache-hit metadata to traces without exposing cached content.
- [x] Test two users asking semantically similar prompts with different preferences.

### Acceptance criteria
- [x] A cached answer from User A can never leak User A-specific context to User B.
- [x] Dynamic travel data respects explicit TTLs.
- [x] Cache keys are documented and testable.

---

## 1.3 P0 — Remove Development Artifacts from the Repository
**Level: J+**

- [x] Remove `frontend/node_modules` from Git history/current tracking.
- [x] Add to `.gitignore`:
  - [x] `node_modules/`
  - [x] `dist/`
  - [x] `.venv/`
  - [x] `venv/`
  - [x] `.pytest_cache/`
  - [x] `.coverage`
  - [x] local DB/artifact files
  - [x] IDE-specific files if not intentionally shared
- [x] Remove or move unrelated `excelagent.py` into another repository/examples folder if it is not part of Rihla.
- [x] Confirm no API keys, tokens, credentials, generated logs, or secrets exist in Git history.
- [x] Add secret scanning to CI.

---

## 1.4 P0 — Create Reproducible Backend Dependency Management
**Level: J+**

- [x] Add `pyproject.toml` or a pinned `requirements.txt`.
- [x] Pin runtime dependencies to known-compatible versions.
- [x] Separate development dependencies from runtime dependencies.
- [x] Document supported Python version.
- [x] Add dependency vulnerability scanning (`pip-audit` or equivalent).
- [x] Verify a clean machine/container can install and boot the API without undeclared packages.

---

# Milestone 2 — Flagship AI Security Feature

# 2.1 P0/P1 — Prompt Injection + Tool-Abuse Defense Layer
**Attack addressed: direct/indirect prompt injection and unsafe tool manipulation**  
**Level: MID**  
**AI security terms:** prompt injection, instruction hierarchy, tool authorization, data/instruction separation, policy enforcement point, defense in depth.

Implement this as a visible feature of the project, not only as a longer system prompt.

### Target architecture

`User Input → Input Guard → Agent → Tool Policy Gateway → Tool → Untrusted Tool Data Wrapper → Agent → Output Guard → Response`

## A. Input Guard
- [x] Create `backend/security/prompt_guard.py`.
- [x] Define a `PromptRiskResult` model:
  - `risk_score`
  - `risk_level`
  - `signals`
  - `action`
- [x] Detect common suspicious instruction patterns such as:
  - attempts to override system/developer instructions
  - requests to reveal hidden prompts or secrets
  - requests to disable safety/tool policies
  - encoded or obfuscated override attempts
- [x] Start with deterministic heuristics and a score.
- [x] Optionally add a lightweight classifier later, but do not make an LLM classifier the only security control.
- [x] Define clear actions:
  - `allow`
  - `allow_with_restrictions`
  - `block`
- [x] Log only security metadata, not raw sensitive prompts.

## B. Tool Policy Gateway
- [x] Route every agent-requested tool call through one function such as:
  `authorize_and_execute_tool(tool_name, args, context)`.
- [x] Keep an explicit tool allowlist.
- [x] Validate all arguments with Pydantic before execution.
- [x] Add strict bounds:
  - [x] latitude `[-90, 90]`
  - [x] longitude `[-180, 180]`
  - [x] radius with a safe maximum
  - [x] location string maximum length
  - [x] allowed place categories/tags
- [x] Do not allow the model to choose arbitrary URLs, hosts, files, commands, or Python code.
- [x] Treat tool results as **untrusted data**, not as new instructions.
- [x] Escape/structure external text before returning it to the model.
- [x] Add a maximum number of tool calls and per-tool timeout budget.
- [x] Add per-turn total tool budget.

## C. Protect Against Tool-Argument Injection
The Overpass query currently uses model-controlled `tag_filter` to build query text.

- [x] Replace free-form `tag_filter` with an enum/allowlist of supported categories.
- [x] Map user concepts such as `restaurant`, `cafe`, `museum`, `hotel`, `attraction` to server-owned query fragments.
- [x] Never concatenate arbitrary model-provided query syntax into Overpass QL.
- [x] Add tests containing quotes, brackets, query operators, and malicious-looking tag strings.

## D. Prompt/Secret Exfiltration Protection
- [x] Never include environment variables, JWT secrets, database URLs, or API keys in model context.
- [x] Add explicit handling for requests asking for system prompts, credentials, or hidden configuration.
- [x] Ensure exceptions sent to the model/client do not include secrets.
- [x] Sanitize tool error messages before they enter conversation history.

## E. Security Evaluation
Create `tests/security/test_prompt_injection.py`.

- [x] Build an attack corpus with at least:
  - [x] direct instruction override
  - [x] system-prompt exfiltration request
  - [x] encoded override attempt
  - [x] tool-argument injection
  - [x] malicious text embedded in a mocked tool result
  - [x] benign travel requests that resemble attack keywords
- [x] Track:
  - **Attack Success Rate (ASR)**
  - false-positive rate
  - blocked tool calls
  - guard latency
- [x] Add a CI security test that fails if known attack cases succeed.

### Acceptance criteria
- [x] The agent cannot execute a non-allowlisted tool.
- [x] Malformed/out-of-range tool arguments are rejected before network calls.
- [x] Known injection test cases cannot reveal secrets or change system/tool policy.
- [x] Benign travel queries continue working.
- [x] Security decisions are observable through structured metrics.

---

# Milestone 3 — Authentication and API Security

## 3.1 P1 — Production Authentication Lifecycle
**Level: MID**  
**Terms:** short-lived access token, refresh token rotation, token revocation, HttpOnly cookie, SameSite, CSRF.

- [x] Stop storing long-lived bearer tokens in `localStorage`.
- [x] Preferred design:
  - short-lived access token held in memory
  - refresh token in `HttpOnly`, `Secure`, `SameSite` cookie
- [x] Add refresh-token rotation.
- [x] Store refresh-token hashes/server-side session records so sessions can be revoked.
- [x] Add logout/revoke endpoint.
- [x] Add password minimum length and reasonable strength rules.
- [x] Normalize emails before storage/comparison.
- [x] Add login rate limiting.
- [x] Add generic auth failures to reduce account enumeration.
- [x] If cookies authenticate state-changing requests cross-site, add CSRF protection.
- [x] Add expiration/revocation tests.

---

## 3.2 P1 — Tighten CORS and Proxy Trust
**Level: J+ → MID**

- [x] Remove production fallback `allow_origins="*"`.
- [x] Set explicit production frontend origins.
- [x] Restrict methods/headers to what the API actually uses.
- [x] Do not blindly trust arbitrary `X-Forwarded-For`.
- [x] Configure trusted proxy behavior for the deployment platform.
- [x] Derive client IP only from a trusted reverse proxy.
- [x] Test direct requests that spoof forwarding headers.

---

## 3.3 P1 — Improve Security Headers
**Level: J+**

- [ ] Add an explicit Content Security Policy appropriate for the frontend.
- [ ] Add HSTS at the HTTPS edge/reverse proxy.
- [ ] Add `Permissions-Policy`.
- [ ] Remove obsolete `X-XSS-Protection` reliance.
- [ ] Verify headers with automated tests.

---

# Milestone 4 — AI Agent Engineering

## 4.1 P1 — Create an Agent Orchestration Boundary
**Level: MID**  
**Terms:** orchestration layer, tool registry, policy gateway, state machine, bounded execution.

Refactor `run_single_turn` into clear stages:

- [ ] `load_context`
- [ ] `select_model`
- [ ] `apply_input_policy`
- [ ] `call_model`
- [ ] `validate_tool_call`
- [ ] `execute_tool`
- [ ] `record_tool_result`
- [ ] `finalize_response`
- [ ] `persist_turn`
- [ ] `record_trace`

Keep the custom loop if desired. The goal is clarity and testability, not adopting a framework just for a résumé keyword.

---

## 4.2 P1 — Real Model Routing
**Level: MID**  
The current router always returns one model.

- [ ] Define routing categories:
  - simple conversation
  - tool-heavy factual request
  - itinerary planning/reasoning
  - safety-sensitive request
- [ ] Implement deterministic routing first.
- [ ] Store the selected model in trace metadata.
- [ ] Add fallback model/provider behavior.
- [ ] Test routing decisions with a labeled dataset.
- [ ] Measure cost/latency/quality before adding complexity.

---

## 4.3 P1 — Context Window and Memory Management
**Level: MID**  
**Terms:** context budget, sliding window, summarization memory, token budget, conversation state.

- [ ] Fix history retrieval so the agent receives the **most recent** N messages, not simply the first N records.
- [ ] Add a token-based context budget instead of only a message-count limit.
- [ ] Exclude internal tool traces that do not help the next turn.
- [ ] Add optional rolling conversation summary for long sessions.
- [ ] Persist summary version and timestamp.
- [ ] Keep user-scoped memory separate from global knowledge.
- [ ] Add tests for long conversations and context truncation.

---

## 4.4 P1 — Structured Agent Outputs
**Level: J+ → MID**

For itinerary-style requests, return a Pydantic schema internally before rendering natural language.

Example structure:

- trip summary
- destination
- dates
- daily itinerary
- weather notes
- places
- warnings/limitations
- source/tool metadata

Tasks:

- [ ] Add `TripPlan` / `ItineraryDay` schemas.
- [ ] Validate model output.
- [ ] Retry once with schema-repair instructions if invalid.
- [ ] Fall back safely if structured generation fails.
- [ ] Keep the public response readable.

---

## 4.5 P1 — AI Evaluation Harness
**Level: MID**  
**Terms:** golden dataset, regression evaluation, tool-routing accuracy, groundedness, hallucination rate, ASR, latency, cost.

Create `evals/`.

- [ ] Build at least 50 representative prompts across:
  - [ ] weather
  - [ ] nearby restaurants/cafes
  - [ ] ambiguous locations
  - [ ] normal travel questions
  - [ ] multi-turn context
  - [ ] unavailable tool/provider
  - [ ] adversarial/prompt-injection cases
- [ ] Store expected behavior, not only exact expected text.
- [ ] Measure:
  - [ ] tool-selection accuracy
  - [ ] tool-argument validity
  - [ ] groundedness against tool output
  - [ ] answer completeness
  - [ ] prompt-injection ASR
  - [ ] P50/P95 latency
  - [ ] tokens/request
  - [ ] estimated model cost/request
  - [ ] cache hit rate
- [ ] Add a regression threshold so quality cannot silently fall after prompt/model changes.
- [ ] Version system prompts and evaluation results.

---

## 4.6 P2 — Response Streaming
**Level: MID**  
**Terms:** SSE, time-to-first-token, cancellation, backpressure.

- [ ] Add a streaming chat endpoint using Server-Sent Events.
- [ ] Stream token/text deltas.
- [ ] Emit structured lifecycle events:
  - `turn_started`
  - `tool_started`
  - `tool_completed`
  - `message_delta`
  - `turn_completed`
  - `error`
- [ ] Do not expose private tool internals or chain-of-thought.
- [ ] Support client cancellation.
- [ ] Record time-to-first-token and total latency.
- [ ] Update React UI to render streamed responses.

---

# Milestone 5 — Backend and System Design

## 5.1 P1 — Separate API, Service, Repository, and Integration Layers
**Level: MID**

Target structure:

```text
backend/
  api/
    routes/
    dependencies/
    schemas/
  application/
    chat_service.py
    session_service.py
  agent/
    orchestrator.py
    model_router.py
    policies.py
  repositories/
    conversation_repository.py
    user_repository.py
  integrations/
    gemini.py
    weather.py
    geocoding.py
    places.py
  security/
    prompt_guard.py
    tool_policy.py
  core/
    config.py
    logging.py
    metrics.py
```

- [ ] Keep HTTP concerns out of repositories.
- [ ] Keep SQL out of route handlers.
- [ ] Keep external API calls behind integration adapters.
- [ ] Inject dependencies where practical for testability.

---

## 5.2 P1 — Database Migrations and Connection Pooling
**Level: MID**  
**Terms:** schema migration, connection pool, transaction boundary, rollback.

- [ ] Stop modifying schema during normal request paths.
- [ ] Move schema creation/changes to Alembic migrations.
- [ ] Create an initial migration for users, conversations, messages, and indexes.
- [ ] Add the session `name` column through a migration, not inside rename requests.
- [ ] Use a PostgreSQL connection pool.
- [ ] Define transaction boundaries for multi-step writes.
- [ ] Add rollback behavior on partial failures.
- [ ] Add DB readiness check.
- [ ] Test migrations against a fresh database.

---

## 5.3 P1 — Distributed Rate Limiting
**Level: MID**  
Current in-memory limits are process-local and break under multiple workers/instances.

- [ ] Move IP/user/session rate-limit state to Redis.
- [ ] Prefer atomic Redis operations/Lua script or a proven limiter.
- [ ] Rate-limit by:
  - authenticated user
  - trusted client IP
  - expensive AI endpoint
- [ ] Return standard `Retry-After`.
- [ ] Add separate limits for login/register.
- [ ] Define behavior when Redis is down:
  - safe local fallback, or
  - fail closed for highly sensitive endpoints
- [ ] Load test rate limits across multiple workers.

---

## 5.4 P1 — Idempotent Chat Requests
**Level: MID**  
**Terms:** idempotency key, retry safety, duplicate suppression, at-least-once delivery.

Network/client retries should not create duplicate messages or duplicate LLM charges.

- [ ] Accept `Idempotency-Key` on `POST /api/chat`.
- [ ] Store request hash + result keyed by user + idempotency key.
- [ ] Return the original result for a repeated identical request.
- [ ] Reject reuse of the same key with a different request body.
- [ ] Set an expiry window.
- [ ] Test concurrent duplicate requests.

---

## 5.5 P1 — Per-Session Concurrency Control
**Level: MID**

Prevent two simultaneous messages from corrupting conversation ordering.

- [ ] Add a Redis distributed lock or optimistic sequence/version per session.
- [ ] Guarantee deterministic message order.
- [ ] Reject or queue overlapping turns for the same conversation.
- [ ] Do not globally block unrelated users/sessions.
- [ ] Add concurrency tests.

---

## 5.6 P1 — External API Resilience
**Level: MID**  
**Terms:** timeout budget, retry policy, exponential backoff, jitter, circuit breaker, bulkhead.

- [ ] Use a shared HTTP client with connection pooling.
- [ ] Set connect/read/total timeouts per provider.
- [ ] Retry only safe/transient failures.
- [ ] Add jitter to exponential backoff.
- [ ] Respect upstream `429` and `Retry-After`.
- [ ] Add circuit breakers for weather/geocoding/places/Gemini.
- [ ] Add provider-specific error classes.
- [ ] Do not use bare `except:` blocks.
- [ ] Return graceful degraded responses when one provider fails.
- [ ] Track provider latency/error rate separately.

---

## 5.7 P2 — API Versioning
**Level: J+**

- [ ] Move public endpoints under `/api/v1`.
- [ ] Keep `/health/live` and `/health/ready` outside API versioning.
- [ ] Add OpenAPI examples and error schemas.
- [ ] Document backwards compatibility rules.

---

# Milestone 6 — Observability and Operations

## 6.1 P1 — Structured Request and Agent Tracing
**Level: MID**  
**Terms:** correlation ID, trace ID, span, structured logging, RED metrics.

- [ ] Generate/propagate a request ID.
- [ ] Generate an agent `trace_id` per chat turn.
- [ ] Record:
  - user ID as a non-sensitive internal identifier
  - session ID
  - model
  - prompt version
  - tools requested/executed/blocked
  - tool latency
  - model latency
  - total latency
  - cache hit/miss
  - retry count
  - token usage when available
  - final status
- [ ] Never log JWTs, passwords, API keys, full secrets, or unnecessary message text.
- [ ] Prefer JSON logs in production.
- [ ] Add OpenTelemetry or equivalent tracing as a P2 extension.

---

## 6.2 P1 — Production Metrics
**Level: MID**

- [ ] Track counters/histograms for:
  - request count
  - HTTP errors
  - P50/P95 latency
  - LLM calls/errors/latency
  - tool calls/errors/latency
  - rate-limit blocks
  - prompt-injection blocks
  - cache hit ratio
  - DB errors
- [ ] Avoid process-local metrics as the only source when using multiple workers.
- [ ] Protect `/metrics` from arbitrary public internet access.
- [ ] Add dashboard-ready metric names.

---

## 6.3 P1 — Liveness and Readiness
**Level: J+ → MID**

- [ ] `/health/live`: process is alive, no external dependency calls.
- [ ] `/health/ready`: required dependencies are ready.
- [ ] Readiness should check DB and critical configuration.
- [ ] Do not perform an expensive Gemini generation on every probe.
- [ ] Return useful status codes for orchestration/deployment platforms.

---

## 6.4 P2 — Define Service Objectives
**Level: MID**

Document initial SLO targets, for example:

- [ ] API availability target.
- [ ] P95 non-AI endpoint latency.
- [ ] P95 AI-turn latency.
- [ ] maximum acceptable tool error rate.
- [ ] prompt-injection ASR target from the security eval suite.
- [ ] alert conditions for sustained provider/DB failures.

The exact numbers should be based on measured behavior, not invented résumé claims.

---

# Milestone 7 — Testing and Quality Gates

## 7.1 P1 — Backend Unit Tests
**Level: J+**

Use `pytest`.

- [ ] auth hashing/token tests
- [ ] ownership authorization tests
- [ ] rate-limit tests
- [ ] prompt-guard tests
- [ ] tool-policy tests
- [ ] tool schema validation tests
- [ ] cache isolation tests
- [ ] model-router tests
- [ ] retry/circuit-breaker tests
- [ ] context-window tests

---

## 7.2 P1 — API Integration Tests
**Level: J+ → MID**

- [ ] register/login
- [ ] create/list/rename/delete session
- [ ] chat lifecycle
- [ ] history pagination
- [ ] export
- [ ] expired/invalid auth
- [ ] BOLA/IDOR attempts
- [ ] duplicate idempotency key
- [ ] provider timeout
- [ ] Redis unavailable
- [ ] DB unavailable
- [ ] prompt-injection/tool-abuse cases

Use a temporary test database and Redis instance/container.

---

## 7.3 P1 — Mock External Providers
**Level: J+**

- [ ] Never call real paid Gemini/weather APIs in normal unit tests.
- [ ] Create provider interfaces and fakes/mocks.
- [ ] Add deterministic tool fixtures.
- [ ] Keep a small optional end-to-end provider smoke test outside the default test suite.

---

## 7.4 P1 — Frontend Tests
**Level: J+**

Add Vitest + React Testing Library.

- [ ] auth state
- [ ] login/register errors
- [ ] session switching
- [ ] loading/error states
- [ ] 401 logout/refresh behavior
- [ ] 429 retry UI
- [ ] streaming response rendering
- [ ] cancellation

Add Playwright later for one full user journey.

---

## 7.5 P1 — Static Analysis and Dependency Checks
**Level: J+**

Backend:
- [ ] Ruff
- [ ] formatter
- [ ] mypy/pyright on important modules
- [ ] Bandit or equivalent
- [ ] `pip-audit`

Frontend:
- [ ] ESLint
- [ ] Prettier
- [ ] `npm audit`
- [ ] remove unused/dead modules

---

# Milestone 8 — Frontend Production Cleanup

## 8.1 P1 — Use One API Client
**Level: J+**

The frontend currently has overlapping API client modules.

- [ ] Consolidate API calls into one client.
- [ ] Centralize:
  - base URL
  - auth
  - token refresh
  - error normalization
  - timeout/cancellation
- [ ] Ensure components/hooks never duplicate raw `fetch` behavior.

---

## 8.2 P1 — Better Failure UX
**Level: J+**

- [ ] Handle offline/network failure.
- [ ] Display provider degradation without exposing internal exceptions.
- [ ] Add retry action where retry is safe.
- [ ] Add cancel button while generating.
- [ ] Preserve unsent input when a request fails.
- [ ] Handle auth expiry cleanly.
- [ ] Keep rate-limit feedback consistent with server `Retry-After`.

---

## 8.3 P2 — Accessibility and UI Quality
**Level: J+**

- [ ] keyboard navigation
- [ ] semantic form labels
- [ ] focus states
- [ ] ARIA for dynamic chat updates
- [ ] responsive layouts
- [ ] loading skeleton/status
- [ ] accessible error messages
- [ ] color contrast check

---

# Milestone 9 — Deployment

## 9.1 P1 — Containerize the Backend
**Level: J+ → MID**

- [ ] Add a backend `Dockerfile`.
- [ ] Use a small supported Python base image.
- [ ] Run as a non-root user.
- [ ] Use `.dockerignore`.
- [ ] Install only runtime dependencies in the final image.
- [ ] Expose/read the platform `PORT`.
- [ ] Run without `reload=True`.
- [ ] Add graceful shutdown.
- [ ] Validate container startup with required env vars.

Suggested production process shape:

```text
uvicorn backend.api:app --host 0.0.0.0 --port $PORT
```

Add multiple workers only after all shared state is moved out of process memory.

---

## 9.2 P1 — Local Production-Like Compose Stack
**Level: MID**

- [ ] Add `docker-compose.yml` for:
  - API
  - PostgreSQL
  - Redis
- [ ] Use healthchecks.
- [ ] Keep secrets in env files not committed to Git.
- [ ] Add one command that starts the full local stack.

---

## 9.3 P1 — Production Configuration
**Level: J+**

Create `.env.example` with names only, never real values:

- `DATABASE_URL`
- `REDIS_URL`
- `GEMINI_API_KEY`
- `OPENWEATHER_API_KEY`
- `JWT_SECRET` or token signing configuration
- `ALLOWED_ORIGINS`
- `FRONTEND_URL`
- environment name
- logging level

- [ ] Fail fast on missing required configuration.
- [ ] Validate configuration through a typed Settings object.
- [ ] Use the deployment platform's secret manager/environment variables.

---

## 9.4 P1 — CI Pipeline with GitHub Actions
**Level: J+ → MID**

On every PR/push:

- [ ] install backend/frontend dependencies
- [ ] lint
- [ ] type-check
- [ ] run backend tests
- [ ] run frontend tests
- [ ] run security tests
- [ ] build frontend
- [ ] build backend Docker image
- [ ] dependency/security scan
- [ ] fail the pipeline on test/security regression

Optional deployment job:

- [ ] deploy only from `main`
- [ ] run DB migrations before traffic is switched
- [ ] perform readiness smoke test after deployment
- [ ] stop/rollback deployment if readiness fails

---

## 9.5 P1 — Deployment Topology
**Level: MID**

A sensible first production topology:

```text
Browser
  |
  v
Vercel / CDN (React)
  |
 HTTPS
  v
FastAPI Service
  |          \
  v           v
PostgreSQL   Redis
  |
External Providers:
Gemini / OpenWeather / Nominatim / Overpass
```

- [ ] Keep this as a **modular monolith**.
- [ ] Do **not** split into microservices yet.
- [ ] Do **not** add Kubernetes just to look more advanced.
- [ ] Scale the API horizontally only when shared state, rate limits, cache, and locks are externalized.

This is stronger system-design judgment than unnecessary infrastructure.

---

# Milestone 10 — Documentation That Helps in Interviews

## 10.1 P1 — Write a Real README
**Level: J+**

- [ ] project purpose
- [ ] screenshots/demo
- [ ] architecture diagram
- [ ] request flow
- [ ] agent/tool flow
- [ ] security model
- [ ] local setup
- [ ] environment variables
- [ ] tests
- [ ] deployment
- [ ] evaluation metrics
- [ ] known limitations
- [ ] tradeoffs

---

## 10.2 P1 — Add an Architecture Diagram
**Level: J+ → MID**

Document:

`React → FastAPI → Auth/Policy → Agent Orchestrator → Gemini → Tool Gateway → Providers`

plus:

`FastAPI → PostgreSQL`  
`FastAPI/Agent → Redis`

Include trust boundaries and which components hold sensitive data.

---

## 10.3 P1 — Add a Threat Model
**Level: MID**

Create `docs/THREAT_MODEL.md`.

Cover at least:

- BOLA/IDOR
- stolen access token
- brute-force login
- prompt injection
- tool-argument injection
- cross-user cache leakage
- abuse/cost exhaustion
- spoofed proxy/IP headers
- dependency compromise
- secret leakage through logs/errors
- external provider failure

For each:
- asset
- attacker goal
- attack path
- mitigation
- residual risk
- test/monitoring signal

---

## 10.4 P2 — Add Architecture Decision Records
**Level: MID**

Create small ADRs for:

- [ ] Why modular monolith instead of microservices.
- [ ] Why Redis is used for distributed state.
- [ ] Why tool calls go through a policy gateway.
- [ ] Why cache is tool-result/context scoped.
- [ ] Why SSE was selected for one-way streaming.
- [ ] Why Postgres stores conversation state.

---

# Milestone 11 — Production Definition of Done

The project can be described as **production-ready for a portfolio/demo deployment** only when all of these are true:

## Security
- [ ] Session ownership enforced everywhere.
- [ ] Prompt-injection/tool-abuse tests pass.
- [ ] No secrets committed.
- [ ] Tokens are not long-lived in `localStorage`.
- [ ] CORS is explicit.
- [ ] Rate limiting works across workers.
- [ ] Dependency/security scans pass.

## Reliability
- [ ] External calls have timeout/retry/circuit-breaker policies.
- [ ] Duplicate chat requests are idempotent.
- [ ] Same-session concurrency is controlled.
- [ ] DB uses migrations and pooling.
- [ ] Graceful degradation exists for provider outages.

## AI quality
- [ ] Tool-selection evaluation exists.
- [ ] Groundedness/regression eval exists.
- [ ] Prompt-injection ASR is measured.
- [ ] Prompt/model versions are tracked.
- [ ] Context memory is bounded by tokens.

## Operations
- [ ] Docker image builds.
- [ ] CI is green.
- [ ] Liveness/readiness work.
- [ ] Structured logs + request/trace IDs exist.
- [ ] P95 latency and error rate are measurable.
- [ ] Metrics endpoint is not unnecessarily public.

## Frontend
- [ ] Single API client.
- [ ] Auth expiry is handled.
- [ ] Retry/rate-limit/network states are handled.
- [ ] Production API URL is configured correctly.
- [ ] Frontend build is clean.

## Documentation
- [ ] README.
- [ ] architecture diagram.
- [ ] threat model.
- [ ] setup/deployment instructions.
- [ ] evaluation methodology.
- [ ] known limitations/tradeoffs.

---

# Recommended Implementation Order

Work in this order so each step makes the next one safer:

1. [ ] BOLA/IDOR ownership fix.
2. [ ] Cache privacy/isolation fix.
3. [ ] Remove tracked `node_modules` and unrelated artifacts.
4. [ ] Dependency lock/pinning + `.env.example`.
5. [ ] DB migrations + connection pooling.
6. [ ] Redis distributed rate limiting.
7. [ ] Prompt Injection + Tool Policy Gateway.
8. [ ] Backend unit/integration/security tests.
9. [ ] Agent orchestration refactor.
10. [ ] Context-window/memory correction.
11. [ ] AI evaluation harness.
12. [ ] Auth token lifecycle hardening.
13. [ ] Idempotency + same-session concurrency control.
14. [ ] Provider resilience/circuit breakers.
15. [ ] Structured traces + production metrics.
16. [ ] Consolidate frontend API client.
17. [ ] Docker + Compose.
18. [ ] CI pipeline.
19. [ ] Deploy backend + frontend.
20. [ ] Run smoke, load, security, and AI regression tests against the deployed environment.
21. [ ] Finish README, threat model, architecture diagram, and ADRs.
22. [ ] Add SSE streaming as the strongest P2 UX/system-design improvement.

---

# Interview Vocabulary You Should Be Able to Explain After Building This

Do not memorize these as buzzwords. You should be able to point to the exact code and explain the tradeoff.

- **BOLA / IDOR:** authentication proves who the caller is; object-level authorization proves they own the requested resource.
- **Tenant isolation:** one user's data cannot be read, cached, mutated, or inferred by another user.
- **Prompt injection:** untrusted text attempts to change the agent's trusted instruction hierarchy or tool behavior.
- **Tool policy gateway:** centralized authorization/validation before any model-requested tool executes.
- **Defense in depth:** prompt rules + typed schemas + tool allowlist + argument validation + output filtering + tests.
- **Semantic cache:** reuse based on meaning, with special care for context, freshness, and user isolation.
- **Idempotency:** retries return the same operation result instead of duplicating side effects or LLM cost.
- **Circuit breaker:** temporarily stop calling a failing provider to reduce cascading failures.
- **Bulkhead:** isolate provider/workload failures so one dependency cannot consume every worker/resource.
- **P95 latency:** 95% of requests are faster than this value; more useful than average latency alone.
- **Backpressure:** prevent producers/clients from overwhelming a slower model/tool pipeline.
- **Correlation/trace ID:** connect HTTP logs, agent calls, tools, and errors for one request.
- **Connection pooling:** reuse DB/network connections instead of opening a new one for every query.
- **Readiness vs liveness:** alive means process is running; ready means it can safely receive traffic.
- **Horizontal scaling:** multiple API instances share state through Postgres/Redis instead of process memory.
- **Modular monolith:** one deployable service with clean internal boundaries; simpler and more appropriate here than premature microservices.
- **Golden dataset:** fixed representative AI cases used to detect quality regressions.
- **Attack Success Rate:** percentage of adversarial evals where an AI security attack achieves its goal.
- **Groundedness:** whether the final response is supported by authoritative tool/retrieval results.
- **Token/context budget:** intentionally choose which conversation state reaches the model instead of growing history without bounds.

---

# Strong Portfolio Story After Completion

You should be able to explain the project in interviews in concrete terms:

> Built and deployed a multi-user AI travel assistant using React, FastAPI, PostgreSQL, Redis, and Gemini function calling. Designed a bounded agent orchestration loop with typed travel tools, user-scoped conversation memory, distributed rate limiting, semantic/tool caching, idempotent chat requests, and provider resilience. Added object-level authorization against BOLA/IDOR and an AI security policy layer for prompt-injection and tool-abuse defense. Evaluated tool routing, groundedness, latency, cache behavior, and adversarial attack success through a regression suite, then shipped the service through Docker and CI with production health checks, metrics, and structured tracing.

Only use claims from this paragraph after the corresponding tasks are actually implemented and measured.

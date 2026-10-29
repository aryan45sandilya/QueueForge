# ADR 0004: Session cookies for the dashboard, project-scoped API keys for clients

- Status: accepted
- Date: 2026-10-26

## Decision

- Passwords are hashed with Argon2id (`argon2-cffi` defaults).
- Dashboard sessions are opaque 256-bit random tokens in an `HttpOnly`,
  `SameSite=Lax` cookie (`Secure` when `QF_COOKIE_SECURE=true`). Only a
  SHA-256 hash of the token is stored. Sessions expire after
  `QF_SESSION_TTL_HOURS` and can be revoked by logging out.
- State-changing requests authenticated by cookie must carry `X-CSRF-Token`
  equal to the session's CSRF token (exposed by `/auth/me` and a readable
  `qf_csrf` cookie). That is a double-submit check on top of `SameSite=Lax`.
- API keys look like `qf_<12-char prefix>_<43-char secret>`. The prefix is
  stored in clear text for lookup and display; the secret is stored as
  `HMAC-SHA256(QF_SECRET_KEY, secret)` and compared in constant time. The full
  key is returned exactly once, at creation or rotation.
- Keys belong to one project and carry scopes (`jobs:read`, `jobs:write`,
  `schedules:read`, `schedules:write`, `metrics:read`). A key used against
  another project's path gets `404`, so project IDs can't be enumerated.
- Rotation issues a new key and gives the old one an expiry (default 24 h
  grace, 0 for immediate) so clients can roll without downtime.
- Key management, members and settings require a session with the `owner`
  role.

## Consequences

The SSE endpoint works with `EventSource`, which cannot set headers, because the
dashboard authenticates by cookie. API clients that want SSE pass the API key
as a bearer header with a streaming HTTP client.

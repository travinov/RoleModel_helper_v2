# RoleModel Helper V2: API Input Guardrails

## Goal

Reject malformed or unbounded chat requests before they reach PostgreSQL,
search tools, or GigaChat.

## Non-Goals

- Authentication and reverse-proxy policy, which remain deployment concerns.
- Content moderation or changing valid dialogue semantics.

## Contract

- `request_id`: non-empty string, maximum 128 characters.
- `text`: non-blank string, maximum 4000 characters.
- `state_revision`: integer greater than or equal to zero.
- Unknown JSON fields are rejected.
- Invalid input returns HTTP 422 and invokes no message service.

## Acceptance Criteria

1. Existing valid message POST remains unchanged.
2. Missing, blank, oversized, negative-revision, and extra-field requests fail
   with 422.
3. Validation failure performs no database or provider call.

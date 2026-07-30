# RoleModel Helper V2: V1 Design System with Agentic Theme

## Goal

Move the V2 chat onto the same visual system as V1 while giving V2 a distinct
green, violet, and pink agentic palette.

## Non-Goals

- Copying V1 backend endpoints or session persistence.
- Adding unsupported feedback/admin functionality.
- Changing the V2 chat API, state revision, or diagnostic contract.

## Inputs / Outputs

- Input: existing V1 glassmorphism tokens and component grammar; current V2
  health/session/message API.
- Output: one responsive static V2 chat screen using the shared visual
  language and a distinct agentic theme.

## Design constraints

1. Preserve the V1 font stack, glass surfaces, large radii, pill controls,
   layered background, message geometry, composer treatment, and responsive
   full-screen mobile layout.
2. Use named V2 palette tokens for green, violet, and pink; retain readable
   contrast and neutral text.
3. Visually expose catalog readiness and the bounded agent route.
4. Keep all text and interaction usable at 360 px width.
5. Provide keyboard focus states and reduced-motion behavior.
6. Never render user-provided content through `innerHTML`.

## Acceptance Criteria

1. V2 declares V1-compatible tokens `--font-ui`, `--glass-fill`,
   `--radius-shell`, `--radius-panel`, `--radius-card`, and `--blur`.
2. V2 declares `--agent-green`, `--agent-violet`, and `--agent-pink`.
3. The DOM contains `viewport`, `phone`, `app-shell`, `topbar`,
   `context-tray`, `messages`, and `composer` components.
4. Existing `/api/v2/health`, `/api/v2/sessions`, message POST, `request_id`,
   and `state_revision` behavior remains present.
5. Desktop and 360 px mobile screenshots have no horizontal overflow and keep
   the composer visible.
6. Valid keyboard focus and `prefers-reduced-motion` styles exist.
7. User and assistant messages are inserted with `textContent`.
8. Interface copy is functional, not promotional: no speed promises,
   advertising slogans, or English agentic badge in the welcome panel.

## Verification

- Static contract test for tokens, components, API strings, and safe rendering.
- Existing API and end-to-end replay tests.
- Playwright desktop and mobile snapshots/screenshots from the rendered HTML.

# Rendered component tests

Run `npm run test:components` from `frontend`, or `npm --prefix frontend test`
from the repository root to run these plus the API checks. Requires the documented
Node 24 environment and `npm ci`; no backend, MongoDB or provider key is needed.

Node loads `dom.mjs` before React so Testing Library can bind to jsdom. The setup
supplies missing browser animation/scroll APIs and opts into reduced motion. It
does not emulate layout, accessibility geometry or real network delivery.

`workspace.test.tsx` renders the actual four feature components, the workspace and
their hooks/portals. Tests interact through accessible labels, typing and clicks.
Only API transport methods are substituted; each test restores its mocks and
unmounts the rendered tree. Synthetic contacts never reach the application DB.
These are component integration tests, not snapshots or live Gemini evaluations.

See [test scenarios and results](../../docs/TESTING.md).

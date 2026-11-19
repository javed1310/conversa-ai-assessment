# Assessment requirements and submission contents

This map covers the local assessment scope. See [TESTING](TESTING.md) for recorded
results, commands and expected behavior, and [LIMITATIONS](LIMITATIONS.md) for
boundaries. Controlled tests and live model observations are separate evidence.

| Requirement | Implementation / evidence |
|---|---|
| 1. Shared AI assistant | `backend/app/agent.py`, behavior registry, knowledge reader, tool registry and gateway; [architecture](ARCHITECTURE.md) |
| 2. Training business | Stable course fixtures in `backend/app/seed.py`; business-scoped course tools; `test_tools.py`, `test_action_agent.py` |
| 3. Knowledge and RAG | Four institute documents under `businesses/training-institute/knowledge`; ingestion, chunking, Gemini embeddings, scoped cosine retrieval and sources; `test_knowledge.py`, `test_passage_selection.py` |
| 4. Agent and tools | Dynamic operation selection, validated arguments/results, chained lead/callback receipts; `test_agent.py`, `test_action_agent.py` |
| 5. Example conversation | Ordered search references, current course details, missing-contact collection, real lead ID used for callback; [API flow](API_EXAMPLES.md), `test_agent.py`, `test_action_agent.py` |
| 6. Persistent state | Owned MongoDB sessions, provenance, corrections, qualification and duplicate protection; `test_state.py`, `test_actions.py`, `test_discussed_enquiry.py`, `test_callback_request_wording.py` |
| 7. Channel gateway | Web and simulated external adapters share normalized messages and the engine; `test_gateway.py`, `test_mock_channel.py`; [extension guide](EXTENDING.md) |
| 8. Automatic processing | Successful lead event, validated summary, persistent local notification, retries and duplicate protection; `test_automation.py` |
| 9. Business configuration | Institute and fitness JSON configurations, separate documents and enabled tools enforced in code; `test_businesses.py`, `test_behavior_boundaries.py` |
| 10. Four frontend screens | Real API-backed chat, conversation history, leads/callback/summary and text/Markdown ingestion; `frontend/tests/workspace.test.tsx`, browser harness |
| 11. REST APIs | FastAPI routes and generated OpenAPI/domain contracts under `docs/contracts`; [requests](API_EXAMPLES.md); gateway used by both channels |
| 12. Reliability | Reproducible failure, duplicate, isolation, malicious-input, new-document and automation cases; `test_reliability.py`, gateway/state/action/knowledge tests; [scenario table](TESTING.md) |
| 13. Technical scope | Python/FastAPI, Next.js/TypeScript, MongoDB and Gemini; local setup and simulated integrations; [setup](SETUP.md), [decisions](DECISIONS.md) |

## Deliverables

- Git source repository and [README](../README.md) with environment/setup commands.
- Fictional configurations and documents under `businesses`, seeded course and
  membership data, and the mock adapter implementation.
- API examples, generated contracts, tests and documented expected/observed results.
- Architecture, extension instructions, technical decisions and known limitations.
- Optional [frozen evaluation and regression runner](../evaluation/README.md),
  whose authored synthetic inputs are separate from private live outputs.

Credentials, dependencies/build outputs, application database exports and raw
evaluation transcripts are excluded. A reviewer supplies a private model key with
available quota. Actual callback delivery, payments, direct enrolment and production
authentication/deployment are outside the assignment.

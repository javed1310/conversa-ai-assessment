# Executable API examples

Start both services and ingest knowledge using [README setup](../README.md#local-setup). The original assignment permits different REST routes; these are the actual contracts. Examples contain fictional contact data, not prewritten success responses. Gemini may ask a clarification; inspect each actual reply before continuing.

## PowerShell web enquiry to callback

```powershell
$api = 'http://127.0.0.1:4000/api/v1'
$business = 'training-institute'
$session = Invoke-RestMethod -Method Post -Uri "$api/businesses/$business/sessions" -ContentType 'application/json' -Body '{}'
$headers = @{ 'X-Session-Token' = $session.sessionToken }
$messages = "$api/businesses/$business/sessions/$($session.sessionId)/messages"
function Send-Inquiry([string]$text) {
    $body = @{ messageId = [guid]::NewGuid().ToString(); content = $text; timestamp = [DateTimeOffset]::UtcNow.ToString('o') } | ConvertTo-Json
    Invoke-RestMethod -Method Post -Uri $messages -Headers $headers -ContentType 'application/json' -Body $body
}
Send-Inquiry 'I want data science courses with a maximum budget of 30000 INR.'
Send-Inquiry 'I want the second one. What are its fee, schedule and availability?'
Send-Inquiry 'I prefer weekends. My name is Rahul. My phone is 9876543210. Please save my enquiry and call me.'
```

The seeded sorted results are DS101/DS202; second refers to actual displayed DS202, fee INR 28000/weekend/available. Inspect `actions` for actual lead/callback receipts and their returned IDs. No call, payment or enrolment occurs. The automated counselor summary is asynchronous; inspect the lead until completed/failed, rather than inventing immediate completion.

## Policy, saved state and inspection

```powershell
Send-Inquiry 'What are both conditions for a refund?'
Invoke-RestMethod "$api/businesses/$business/conversations?limit=20"
Invoke-RestMethod "$api/businesses/$business/conversations/$($session.sessionId)/turns?limit=20"
Invoke-RestMethod "$api/businesses/$business/leads?limit=20"
Invoke-RestMethod "$api/businesses/$business/documents?limit=20"
```

Refund evidence must include within seven days AND no first-class attendance; actual `sources` identify the supporting document/version/chunk. Get a lead detail with `/leads/{actualLeadId}`. Document detail is `/documents/{actualDocumentId}/versions/{actualVersion}`. List responses contain `items` and `nextCursor`; URL-encode a non-null cursor and pass it to the same business/resource. Changing business/session cursor scope is rejected.

## Text/Markdown addition and retrieval

```powershell
$document = @{ documentId = 'demo-library'; version = 1; title = 'Demo library policy'; format = 'markdown'; content = 'Learners may use the assessment library every Tuesday. Bring a learner ID card.' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$api/businesses/$business/documents" -ContentType 'application/json' -Body $document
Send-Inquiry 'On which day may learners use the assessment library, and what should they bring?'
```

Continue only when ingestion is `ready`. The answer/source must use that actual document and be unavailable to the fitness business. Existing version with changed content returns 409 `DOCUMENT_VERSION_CONFLICT`; retry identical content or intentionally choose a new version. For failure or uncertain transport, read its saved status before changing version/content. The UI also accepts UTF-8 text/Markdown files.

## Mock channel

Privately configure an institute key in `MOCK_CHANNEL_KEYS`, restart the backend, and set the matching key only in this terminal's private variable. Do not save it in a script or publish it.

```powershell
$mockHeaders = @{ 'X-Mock-Channel-Key' = $env:ASSESSMENT_DEMO_MOCK_KEY }
$external = @{ event_id = [guid]::NewGuid().ToString(); author = 'demo-external-user'; text = 'What are the current fee and details for PY101?'; timestamp = [DateTimeOffset]::UtcNow.ToString('o') } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$api/mock/messages" -Headers $mockHeaders -ContentType 'application/json' -Body $external
```

The response's `recipient` must match the real author and `in_reply_to` the event ID; `result` is the actual completed/processing response. Body-supplied business/session is rejected. Matching names/phones do not merge this session with web.

## Replay, failures and expected statuses

Save an original body before sending; retry that exact string and original session/token. Completed duplicates return the same saved result. Same ID with changed text/timestamp returns 409. Concurrent duplicates may return 202; an unrelated message during processing is busy. Missing/foreign token or unknown mock key returns 404; invalid scalar/operator/oversized input returns 400. Unavailable engine/provider returns safe 503, with no fake assistant success. Error responses contain a safe code and request ID.

Use a fresh session to demonstrate `Please call me` without contact: it asks for missing valid details and creates no lead. **Do not edit the database to simulate lead failure.** Reproduce the controlled failure and all ten outcomes with `python scripts/check_live_scenarios.py` through the uv runner; injection exists only in that private harness/tests, not an unrestricted API switch.


Recorded expected/actual results and reproduction commands: [TESTING](TESTING.md).

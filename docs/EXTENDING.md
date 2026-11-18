# Add a business, tool or channel

## Business

1. Create `businesses/<business-id>/config.json`, copying the validated fitness example: businessId, businessName, agentRole, instructions, knowledgeBaseId, enabledTools and configVersion.
2. Use a unique knowledgeBaseId and only tools registered by trusted application code. Startup rejects unknown tools or invalid configuration.
3. Supply that business's documents through its scoped document API; ingest them until ready. Provision its actual business records separately. The sample seeder explicitly supports the institute/fitness fixtures; a new business does not magically acquire catalogue data.
4. Restart to load configuration; verify document/tool/session isolation. Reuse the existing frontend selector, gateway and agent.

## Tool

Define strict Pydantic argument/result models and a scoped async handler, then register a `ToolDefinition` in `backend/app/tools.py`. A new tool also registers a bounded `presentation` callback; set `current_facts=True` if the checked result answers current-record questions. Add its identifier only to the owning business's allowlist.

The shared agent dispatches validated registered presentations without another per-tool rendering branch. Argument/result/presentation checks do not replace the handler's responsibility for legitimate scoped operations and side-effect consistency. New write tools must implement appropriate authorization, idempotency and real receipts; existing lead/callback evidence protections remain. Export contracts and test invalid inputs/results, foreign scope and failures. [Third-business/tool regression](../backend/tests/test_tool_extensions.py) demonstrates a temporary hotel tool reading its own stored data without copying the AI engine.

## Channel

Implement `ChannelAdapter.normalize/encode` in a separate adapter module; register it at startup. Update the common channel contract/mapping as required, authenticate its trusted ingress and derive business/sender/session ownership before `Gateway.handle`. Validate the outgoing recipient and original message ID. No SDK, platform-payload parsing or platform-specific branch belongs in `GeminiAgent`.

`WebAdapter` and `MockAdapter` in `backend/app/channels.py` are working examples. The mock API uses a different event payload and private key mapping; web uses an owned session token. Cross-channel identity linking, real network delivery, signatures/media and delivery tracking are extra integrations, not part of this assessment. See [API examples](API_EXAMPLES.md).

import assert from "node:assert/strict";
import { afterEach, mock, test } from "node:test";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Chat from "@/features/chat";
import Conversations from "@/features/conversations";
import Leads from "@/features/leads";
import Knowledge from "@/features/knowledge";
import Workspace from "@/app/workspace";
import { api, ApiError } from "@/lib/api";
import type { ChatResult, ConversationView, DocumentDetail, DocumentInput, LeadView, SessionStarted, WebMessage } from "@/lib/contracts";
const business = "training-institute";
const createdAt = "2026-11-18T10:00:00Z";
const session: SessionStarted = { businessId: business, sessionId: "session-one", sessionToken: "test-only", engineReady: true };
const chatProps = { business, businessName: "Learning Path", role: "Course assistant", onChange() { } };
const doc: DocumentDetail = { businessId: business, documentId: "refund", version: 2, title: "Refund policy", format: "markdown", status: "ready", expectedChunks: 1, createdAt, content: "# Refund policy\n\nWithin seven days and before the first class." };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function completed(messageId: string): ChatResult { return { status: "completed", sessionId: session.sessionId, messageId, reply: "Verified answer", sources: [], actions: [] }; }
afterEach(() => { cleanup(); mock.restoreAll(); });
test("chat accepts a first message, creates one session and sends once", async () => {
    const start = mock.method(api, "start", async () => session);
    const send = mock.method(api, "send", async (_b: string, _s: SessionStarted, message: WebMessage) => completed(message.messageId));
    const user = userEvent.setup();
    render(<Chat {...chatProps}/>);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "Find Python courses");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findAllByText("Verified answer");
    assert.equal(start.mock.callCount(), 1);
    assert.equal(send.mock.callCount(), 1);
    assert.equal(send.mock.calls[0].arguments[0], business);
    assert.equal(send.mock.calls[0].arguments[2].content, "Find Python courses");
});
test("session-start failure keeps the unsent draft and permits retry", async () => {
    let attempts = 0;
    mock.method(api, "start", async () => { if (++attempts === 1)
        throw new ApiError("NETWORK_UNAVAILABLE", "Offline"); return session; });
    const send = mock.method(api, "send", async (_b: string, _s: SessionStarted, message: WebMessage) => completed(message.messageId));
    const user = userEvent.setup();
    render(<Chat {...chatProps}/>);
    const input = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
    await user.type(input, "Keep my question");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("alert");
    assert.equal(input.value, "Keep my question");
    assert.equal(send.mock.callCount(), 0);
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findAllByText("Verified answer");
    assert.equal(attempts, 2);
    assert.equal(send.mock.callCount(), 1);
});
test("processing retry preserves the original message ID and content", async () => {
    mock.method(api, "start", async () => session);
    let attempts = 0;
    const send = mock.method(api, "send", async (_b: string, _s: SessionStarted, message: WebMessage) => ++attempts === 1
        ? { status: "processing" as const, sessionId: session.sessionId, messageId: message.messageId }
        : completed(message.messageId));
    const user = userEvent.setup();
    render(<Chat {...chatProps}/>);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "Call me");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(await screen.findByRole("button", { name: "Check message / retry same message" }));
    await screen.findAllByText("Verified answer");
    assert.deepEqual(send.mock.calls[0].arguments[2], send.mock.calls[1].arguments[2]);
});
test("rapid submit while session creation is pending does not duplicate the request", async () => {
    const pending = deferred<SessionStarted>();
    const start = mock.method(api, "start", () => pending.promise);
    const send = mock.method(api, "send", async (_b: string, _s: SessionStarted, message: WebMessage) => completed(message.messageId));
    const user = userEvent.setup();
    render(<Chat {...chatProps}/>);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "Hello");
    await user.dblClick(screen.getByRole("button", { name: "Send message" }));
    assert.equal(start.mock.callCount(), 1);
    await act(async () => pending.resolve(session));
    await screen.findAllByText("Verified answer");
    assert.equal(send.mock.callCount(), 1);
});
test("terminal failure shows review guidance and does not offer unsafe replay", async () => {
    mock.method(api, "start", async () => session);
    mock.method(api, "send", async () => { throw new ApiError("RECOVERY_REQUIRES_REVIEW", "Review needed", undefined, 409); });
    const user = userEvent.setup();
    render(<Chat {...chatProps}/>);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "Save enquiry");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText(/Previous session session-one may contain saved actions/);
    assert.equal(screen.queryByRole("button", { name: /Check message/ }), null);
});
const conversation = (id: string, owner = business): ConversationView => ({ businessId: owner, sessionId: id, channel: "web", createdAt, updatedAt: createdAt });
test("conversation selection loads its actual history and pagination appends sessions", async () => {
    const list = mock.method(api, "conversations", async (_b: string, _signal: AbortSignal, cursor?: string | null) => cursor ? { items: [conversation("two")] } : { items: [conversation("one")], nextCursor: "next" });
    const turns = mock.method(api, "turns", async () => ({ items: [{ turnId: "turn-one", messageId: "msg-one", sequence: 1, inputText: "Saved customer question", status: "failed" as const }] }));
    const user = userEvent.setup();
    render(<Conversations business={business} active revision={0}/>);
    await user.click(await screen.findByRole("button", { name: /one/ }));
    await screen.findByText("Saved customer question", { exact: false });
    assert.equal(turns.mock.calls[0].arguments[1], "one");
    await user.click(screen.getByRole("button", { name: "More conversations" }));
    await screen.findByText("two");
    assert.equal(list.mock.calls[1].arguments[2], "next");
    assert.ok(screen.getByText("one"));
});
test("late conversation result from an inactive business never replaces the active list", async () => {
    const old = deferred<{
        items: ConversationView[];
    }>();
    mock.method(api, "conversations", async (b: string) => b === business ? old.promise : { items: [conversation("fitness-only", "fitness-studio")] });
    const view = render(<Conversations business={business} active revision={0}/>);
    await waitFor(() => assert.ok(screen.getByRole("status")));
    view.rerender(<Conversations business="fitness-studio" active revision={0}/>);
    await screen.findByText("fitness-only");
    await act(async () => old.resolve({ items: [conversation("training-only")] }));
    assert.equal(screen.queryByText("training-only"), null);
    assert.ok(screen.getByText("fitness-only"));
});
test("conversation errors are visible and refresh retrieves the list", async () => {
    let attempts = 0;
    mock.method(api, "conversations", async () => { if (++attempts === 1)
        throw new Error("Offline"); return { items: [conversation("recovered")] }; });
    const user = userEvent.setup();
    render(<Conversations business={business} active revision={0}/>);
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "Refresh conversations" }));
    await screen.findByText("recovered");
    assert.equal(screen.queryByRole("alert"), null);
});
const lead: LeadView = { businessId: business, leadId: "lead-one", sessionId: "one", createdAt, customer: { name: "Alice Morgan", phone: "+12025550199", courseId: "PY101" }, callbackRequestedAt: createdAt, notificationCreatedAt: createdAt, automation: { eventId: "event-one", status: "completed", attempts: 1, retryable: false, summary: { leadId: "lead-one", customerName: "Alice Morgan", summary: "Interested in introductory Python.", intent: "enrollment", qualificationStatus: "qualified", recommendedAction: "counselor_callback" } } };
test("lead expansion displays saved contact, callback and generated summary", async () => {
    mock.method(api, "leads", async () => ({ items: [lead] }));
    const user = userEvent.setup();
    render(<Leads business={business} active revision={0}/>);
    await user.click(await screen.findByText("Alice Morgan"));
    assert.ok(screen.getByText("+12025550199"));
    assert.ok(screen.getByText("Interested in introductory Python."));
    assert.ok(screen.getByText(/^Requested /));
    assert.ok(screen.getByText("Stored locally"));
    assert.ok(screen.getByText("Budget not supplied / Schedule not supplied"));
});
test("pending automation does not invent a summary or a callback", async () => {
    mock.method(api, "leads", async () => ({ items: [{ ...lead, callbackRequestedAt: null, notificationCreatedAt: null, automation: { ...lead.automation, status: "pending" as const, summary: null } }] }));
    const user = userEvent.setup();
    render(<Leads business={business} active revision={0}/>);
    await user.click(await screen.findByText("Alice Morgan"));
    assert.ok(screen.getByText("Not requested"));
    assert.ok(screen.getByText(/Summary has not been generated/));
    assert.equal(screen.queryByText("Interested in introductory Python."), null);
});
test("knowledge loads all pages before grouping the latest document and history", async () => {
    mock.method(api, "documents", async (_b: string, _signal: AbortSignal, cursor?: string | null) => cursor ? { items: [doc] } : { items: [{ ...doc, version: 1, status: "pending" as const }], nextCursor: "page-two" });
    render(<Knowledge business={business} active/>);
    await screen.findByRole("button", { name: /Refund policy.*v2.*ready/ });
    assert.equal(screen.getAllByRole("button", { name: /Refund policy/ }).length, 1);
    assert.ok(screen.getByText("Version history (1)"));
});
test("document-reader failure offers refresh and renders the retried content", async () => {
    mock.method(api, "documents", async () => ({ items: [doc] }));
    let attempts = 0;
    mock.method(api, "document", async () => { if (++attempts === 1)
        throw new Error("Offline"); return doc; });
    const user = userEvent.setup();
    render(<Knowledge business={business} active/>);
    await user.click(await screen.findByRole("button", { name: /Refund policy/ }));
    const dialog = await screen.findByRole("dialog", { name: "Document reader" });
    await within(dialog).findByText(/Document could not be loaded/);
    await user.click(within(dialog).getByRole("button", { name: "Refresh document" }));
    await within(dialog).findByText("Within seven days and before the first class.");
    assert.equal(attempts, 2);
});
test("adding a document sends operator content and shows its real returned status", async () => {
    mock.method(api, "documents", async () => ({ items: [] }));
    const add = mock.method(api, "addDocument", async (_b: string, input: DocumentInput) => ({ ...doc, ...input, version: input.version ?? 1 }));
    const user = userEvent.setup();
    render(<Knowledge business={business} active/>);
    await user.click(screen.getByRole("button", { name: /Add document/ }));
    const dialog = screen.getByRole("dialog", { name: "Add document" });
    await user.type(within(dialog).getByLabelText("Document ID"), "new-policy");
    await user.type(within(dialog).getByLabelText("Title"), "New policy");
    await user.type(within(dialog).getByLabelText("Document content"), "A new business rule.");
    await user.click(within(dialog).getByRole("button", { name: "Save and ingest document" }));
    await within(dialog).findByText("Document saved: ready.");
    assert.equal(add.mock.calls[0].arguments[0], business);
    assert.equal(add.mock.calls[0].arguments[1].content, "A new business rule.");
});
test("Escape closes the document overlay and restores scroll", async () => {
    mock.method(api, "documents", async () => ({ items: [] }));
    const user = userEvent.setup();
    render(<Knowledge business={business} active/>);
    await user.click(screen.getByRole("button", { name: /Add document/ }));
    assert.equal(document.body.style.overflow, "hidden");
    await user.keyboard("{Escape}");
    assert.equal(screen.queryByRole("dialog"), null);
    assert.equal(document.body.style.overflow, "");
});
test("workspace defaults to Learning Path and preserves separate drafts per business", async () => {
    mock.method(api, "businesses", async () => ({ businesses: [
            { businessId: "fitness-studio", businessName: "Fitness studio", agentRole: "Fitness assistant" },
            { businessId: business, businessName: "Learning Path", agentRole: "Course assistant" },
        ] }));
    const user = userEvent.setup();
    render(<Workspace />);
    const selector = await screen.findByRole("combobox", { name: "Business" }) as HTMLSelectElement;
    await waitFor(() => assert.equal(selector.value, business));
    await user.type(screen.getByRole("textbox", { name: "Message" }), "Training draft");
    await user.selectOptions(selector, "fitness-studio");
    assert.equal((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value, "");
    await user.type(screen.getByRole("textbox", { name: "Message" }), "Fitness draft");
    await user.selectOptions(selector, business);
    assert.equal((screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement).value, "Training draft");
});
test("Shift+Enter adds a line and Enter submits the complete message", async () => {
    mock.method(api, "start", async () => session);
    const send = mock.method(api, "send", async (_b: string, _s: SessionStarted, message: WebMessage) => completed(message.messageId));
    const user = userEvent.setup();
    render(<Chat {...chatProps}/>);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "First line{Shift>}{Enter}{/Shift}Second line");
    assert.equal(send.mock.callCount(), 0);
    await user.keyboard("{Enter}");
    await screen.findAllByText("Verified answer");
    assert.equal(send.mock.calls[0].arguments[2].content, "First line\nSecond line");
});
test("chat displays supplied policy sources and the failed operation without a success badge", async () => {
    mock.method(api, "start", async () => session);
    mock.method(api, "send", async (_b: string, _s: SessionStarted, message: WebMessage): Promise<ChatResult> => ({
        ...completed(message.messageId), sources: [{ businessId: business, knowledgeBaseId: "academy", documentId: "refund", documentVersion: 2, chunkId: "one", title: "Refund policy", excerpt: "Within seven days, before first class." }],
        actions: [{ executionId: "failed-write", result: { status: "failure", callId: "one", tool: "createLead", code: "UNAVAILABLE", retryable: true } }],
    }));
    const user = userEvent.setup();
    render(<Chat {...chatProps}/>);
    await user.type(screen.getByRole("textbox", { name: "Message" }), "Explain my request");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(await screen.findByText("Sources (1)"));
    assert.ok(screen.getByText("Within seven days, before first class."));
    await user.click(screen.getByText("Recorded operations"));
    assert.ok(screen.getByText("createLead: failure (UNAVAILABLE)"));
    assert.equal(screen.queryByText("createLead: success"), null);
});
test("failed document save preserves content and reports failure before same-content retry", async () => {
    mock.method(api, "documents", async () => ({ items: [] }));
    let attempts = 0;
    const add = mock.method(api, "addDocument", async (_b: string, input: DocumentInput) => { if (++attempts === 1)
        throw new Error("Offline"); return { ...doc, ...input, version: input.version ?? 1 }; });
    const user = userEvent.setup();
    render(<Knowledge business={business} active/>);
    await user.click(screen.getByRole("button", { name: /Add document/ }));
    const dialog = screen.getByRole("dialog", { name: "Add document" });
    await user.type(within(dialog).getByLabelText("Document ID"), "retry-policy");
    await user.type(within(dialog).getByLabelText("Title"), "Retry policy");
    await user.type(within(dialog).getByLabelText("Document content"), "Preserve this rule.");
    await user.click(within(dialog).getByRole("button", { name: "Save and ingest document" }));
    await within(dialog).findByRole("alert");
    assert.equal(within(dialog).queryByText("Document saved: ready."), null);
    assert.equal((within(dialog).getByLabelText("Document content") as HTMLTextAreaElement).value, "Preserve this rule.");
    await user.click(within(dialog).getByRole("button", { name: "Save and ingest document" }));
    await within(dialog).findByText("Document saved: ready.");
    assert.deepEqual(add.mock.calls[0].arguments[1], add.mock.calls[1].arguments[1]);
});

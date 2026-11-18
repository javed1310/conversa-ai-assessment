import type {
  BusinessList, ChatResult, DocumentDetail, DocumentInput, Page_ConversationView_,
  Page_DocumentView_, Page_LeadView_, Page_TurnView_, ProcessingResult, SessionStarted, WebMessage,
} from "./contracts";
import { matchesSchema, type ResponseSchema } from "./response-validation";

export class ApiError extends Error {
  constructor(public code: string, message: string, public requestId?: string, public status?: number) { super(message); }
}
const invalid = (status?: number) => new ApiError("INVALID_RESPONSE", "The server response could not be verified. Refresh or retry the same request.", undefined, status);

async function request<T>(path: string, schema: ResponseSchema, options: RequestInit = {}, owned: (body: T) => boolean = () => true): Promise<T> {
  const timeout = AbortSignal.timeout(options.method === "POST" ? 110000 : 20000);
  const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
  let response: Response;
  let body: unknown;
  try {
    response = await fetch(`/api/v1${path}`, {
      ...options, cache: "no-store", signal,
      headers: { "Content-Type": "application/json", ...options.headers },
    });
    try { body = await response.json(); }
    catch (error) { if (signal.aborted) throw error; throw invalid(response.status); }
  } catch (error) {
    if (options.signal?.aborted) throw error;
    if (timeout.aborted) throw new ApiError("REQUEST_TIMEOUT", "The request timed out. Its result may still be available; retry the same request.");
    if (error instanceof ApiError) throw error;
    throw new ApiError("NETWORK_UNAVAILABLE", "The backend could not be reached. Retry when connected.");
  }
  if (!response.ok) {
    if (!matchesSchema(body, "ErrorEnvelope")) throw new ApiError("REQUEST_FAILED", "The request could not finish.", undefined, response.status);
    const envelope = body as { error: { code: string; message: string; requestId: string } };
    throw new ApiError(envelope.error.code, envelope.error.message, envelope.error.requestId, response.status);
  }
  if (!matchesSchema(body, schema) || !owned(body as T)) throw invalid(response.status);
  return body as T;
}

export const businessPath = (business: string) => `/businesses/${encodeURIComponent(business)}`;
const page = (cursor?: string | null) => cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
const sourcesOwned = (body: ChatResult, business: string) => (body.sources ?? []).every(source => source.businessId === business) &&
  (body.actions ?? []).every(action => action.result.status === "success" || action.result.status === "failure");
const documentOwned = (body: DocumentDetail, business: string, id: string, version: number) => body.businessId === business && body.documentId === id && body.version === version;
export const api = {
  businesses: (signal: AbortSignal) => request<BusinessList>("/businesses", "BusinessList", { signal }),
  start: (business: string) => request<SessionStarted>(`${businessPath(business)}/sessions`, "SessionStarted", { method: "POST", body: "{}" }, body => body.businessId === business),
  send: (business: string, session: SessionStarted, message: WebMessage) => request<ChatResult | ProcessingResult>(
    `${businessPath(business)}/sessions/${encodeURIComponent(session.sessionId)}/messages`, "ChatResultOrProcessing",
    { method: "POST", headers: { "X-Session-Token": session.sessionToken }, body: JSON.stringify(message) },
    body => body.sessionId === session.sessionId && body.messageId === message.messageId &&
      (body.status === "processing" || body.status === "completed" && sourcesOwned(body, business))),
  conversations: (business: string, signal: AbortSignal, cursor?: string | null) => request<Page_ConversationView_>(`${businessPath(business)}/conversations${page(cursor)}`, "Page_ConversationView_", { signal }, body => body.items.every(item => item.businessId === business)),
  turns: (business: string, session: string, signal: AbortSignal, cursor?: string | null) => request<Page_TurnView_>(`${businessPath(business)}/conversations/${encodeURIComponent(session)}/turns${page(cursor)}`, "Page_TurnView_", { signal }, body => body.items.every(item => (item.status === "completed") === !!item.output && (!item.output || item.output.messageId === item.messageId && item.output.sessionId === session && sourcesOwned(item.output, business)))),
  leads: (business: string, signal: AbortSignal, cursor?: string | null) => request<Page_LeadView_>(`${businessPath(business)}/leads${page(cursor)}`, "Page_LeadView_", { signal }, body => body.items.every(item => item.businessId === business && (!item.automation.summary || item.automation.summary.leadId === item.leadId && item.automation.summary.customerName === item.customer.name))),
  documents: (business: string, signal: AbortSignal, cursor?: string | null) => request<Page_DocumentView_>(`${businessPath(business)}/documents${page(cursor)}`, "Page_DocumentView_", { signal }, body => body.items.every(item => item.businessId === business)),
  document: (business: string, id: string, version: number, signal: AbortSignal) => request<DocumentDetail>(`${businessPath(business)}/documents/${encodeURIComponent(id)}/versions/${version}`, "DocumentDetail", { signal }, body => documentOwned(body, business, id, version)),
  addDocument: (business: string, body: DocumentInput) => request<DocumentDetail>(`${businessPath(business)}/documents`, "DocumentDetail", { method: "POST", body: JSON.stringify(body) }, result => documentOwned(result, business, body.documentId, body.version ?? 1)),
};

export function errorText(error: unknown): string {
  return error instanceof ApiError ? `${error.message} (${error.code})` : "The request could not finish. Please retry.";
}

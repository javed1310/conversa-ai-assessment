"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, errorText } from "@/lib/api";
import type { ChatResult, SessionStarted, WebMessage } from "@/lib/contracts";
import { ResponseText } from "@/components/ui/response-text";
import { Overlay } from "@/components/ui/overlay";
import Icon from "@/app/icons";
import { Failure, Sources } from "@/components/ui/feedback";
export default function Chat({
  business,
  businessName,
  role,
  starterQuestions = [],
  onChange,
}: {
  business: string;
  businessName: string;
  role: string;
  starterQuestions?: { title: string; message: string }[];
  onChange: () => void;
}) {
  const [contextOpen, setContextOpen] = useState(false);
  const [session, setSession] = useState<SessionStarted | null>(null);
  const [messages, setMessages] = useState<
    { input: WebMessage; output?: ChatResult; failure?: string }[]
  >([]);
  const [pending, setPending] = useState<WebMessage | null>(null);
  const [abandon, setAbandon] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const [error, setError] = useState("");
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (messages.length)
      endRef.current?.scrollIntoView({
        behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto"
          : "smooth",
        block: "nearest",
      });
  }, [messages, busy]);
  const prompts = starterQuestions.length
    ? starterQuestions.map(({ title, message }) => [title, message])
    : [
        ["Explore your options", "What options are available?"],
        ["Ask about policies", "What policies should I know about?"],
        ["Get started", "How can you help me?"],
      ];
  async function start(initialText = session ? "" : text) {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    try {
      const created = await api.start(business);
      if (!mounted.current) return;
      setSession(created);
      setMessages([]);
      setPending(null);
      setAbandon(false);
      setText(initialText);
      onChange();
      requestAnimationFrame(() => composerRef.current?.focus());
    } catch (e) {
      if (mounted.current) setError(errorText(e));
    } finally {
      locked.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  async function send(existing?: WebMessage) {
    if (locked.current || (existing && !session) || (!existing && (!text.trim() || pending)))
      return;
    const message = existing ?? {
      messageId: crypto.randomUUID(),
      content: text.trim(),
      timestamp: new Date().toISOString(),
    };
    locked.current = true;
    setBusy(true);
    setError("");
    let activeSession = session;
    let messageStarted = false;
    try {
      if (!activeSession) {
        activeSession = await api.start(business);
        if (!mounted.current) return;
        setSession(activeSession);
        onChange();
      }
      if (!existing) {
        setMessages((all) => [...all, { input: message }]);
        setPending(message);
        setText("");
      }
      messageStarted = true;
      const result = await api.send(business, activeSession, message);
      if (!mounted.current) return;
      if (result.status === "completed") {
        setMessages((all) =>
          all.map((item) =>
            item.input.messageId === message.messageId
              ? { ...item, output: result }
              : item,
          ),
        );
        setPending(null);
        onChange();
      } else
        setError(
          "This message is still processing. Use Check message to retrieve the same result.",
        );
    } catch (e) {
      if (!mounted.current) return;
      const messageText = errorText(e);
      setError(messageText);
      if (!messageStarted) return; // Session creation failed; keep the unsent draft.
      const rejected =
        e instanceof ApiError &&
        (([400, 404, 409].includes(e.status ?? 0) &&
          ![
            "SESSION_BUSY",
            "MESSAGE_CONFLICT",
            "INVALID_RESPONSE",
            "REQUEST_FAILED",
          ].includes(e.code)) ||
          e.code === "ENGINE_UNAVAILABLE");
      const terminal =
        e instanceof ApiError &&
        ["TURN_FAILED", "RECOVERY_REQUIRES_REVIEW"].includes(e.code);
      if (terminal)
        setError(
          `${messageText} Previous session ${activeSession?.sessionId} may contain saved actions. Check Conversations and Leads before starting again.`,
        );
      if (rejected || terminal) {
        setMessages((all) =>
          all.map((item) =>
            item.input.messageId === message.messageId
              ? { ...item, failure: messageText }
              : item,
          ),
        );
        setPending(null);
        if (terminal || (e instanceof ApiError && e.status === 404))
          setSession(null);
      }
    } finally {
      locked.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <>
        <div className="chat-layout">
          <section aria-label="Chat" className="panel chat-panel">
            <div className="section-heading chat-heading">
              <button
                className="context-toggle secondary"
                onClick={() => setContextOpen(true)}
              >
                Context ↗
              </button>
              <span className="session-chip eyebrow">
                <i className="status-dot" />
                {session ? "Session live" : "Not started"}
              </span>
              <div className="assistant-identity">
                <span className="avatar assistant-avatar">
                  <Icon name="spark" />
                </span>
                <div>
                  <h2>Business assistant</h2>
                  <p>{role}</p>
                </div>
              </div>
              <button
                className="secondary new-chat"
                onClick={() => void start()}
                disabled={busy || !!pending}
              >
                <Icon name="plus" />
                {session ? "New conversation" : "Start conversation"}
              </button>
            </div>
            {session && !session.engineReady && (
              <p role="status" className="availability-note">
                Your session is saved. AI chat is currently unavailable.
              </p>
            )}
            <div
              className={`messages ${messages.length ? "has-messages" : ""}`}
              aria-live="polite"
              aria-busy={busy}
            >
              {!messages.length && (
                <div className="chat-welcome">
                  <div className="welcome-symbol">
                    <Icon name="spark" />
                  </div>
                  <span className="eyebrow">
                    A little context. A better conversation.
                  </span>
                  <h3>What can we help you discover?</h3>
                  <p>
                    Ask a question, explore your options, or find the right next
                    step. Your assistant is here to help with {businessName}.
                  </p>
                  <div className="prompt-cards">
                    {prompts.map(([label, prompt], index) => (
                      <button
                        key={label}
                        disabled={busy || !!pending}
                        onClick={() => {
                          if (!session) void start(prompt);
                          else {
                            setText(prompt);
                            composerRef.current?.focus();
                          }
                        }}
                      >
                        <span className={`prompt-icon prompt-${index}`}>
                          <Icon
                            name={
                              index === 0
                                ? "search"
                                : index === 1
                                  ? "people"
                                  : "book"
                            }
                          />
                        </span>
                        <strong>{label}</strong>
                        <span>{prompt}</span>
                        <Icon name="arrow" className="prompt-arrow" />
                      </button>
                    ))}
                  </div>
                  <p className="welcome-caption">
                    {session
                      ? "Your conversation is ready. Send your first message."
                      : "Choose a question to start, or start a conversation of your own."}
                  </p>
                </div>
              )}
              {messages.map((item) => (
                <div className="exchange" key={item.input.messageId}>
                  <div className="bubble customer">
                    <div className="bubble-label">
                      <strong>You</strong>
                      <time dateTime={item.input.timestamp}>
                        {new Date(item.input.timestamp).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </time>
                    </div>
                    <p>{item.input.content}</p>
                  </div>
                  {item.output ? (
                    <div className="assistant-row">
                      <span className="avatar reply-avatar">
                        <Icon name="spark" />
                      </span>
                      <div className="bubble assistant">
                        <div className="bubble-label">
                          <strong>Assistant</strong>
                          <span>Business context</span>
                        </div>
                        <ResponseText text={item.output.reply} />
                        <Sources sources={item.output.sources ?? []} />
                        {(item.output.actions?.length ?? 0) > 0 && (
                          <details className="operations">
                            <summary>Recorded operations</summary>
                            {item.output.actions?.map((action) => (
                              <p key={action.executionId}>
                                <span
                                  className={`operation-dot ${action.result.status}`}
                                />
                                {action.result.tool}: {action.result.status}
                                {action.result.status === "failure"
                                  ? ` (${action.result.code})`
                                  : ""}
                              </p>
                            ))}
                          </details>
                        )}
                      </div>
                    </div>
                  ) : item.failure ? (
                    <p role="status" className="error message-failure">
                      Message not completed: {item.failure}
                    </p>
                  ) : (
                    <div className="typing-state" role="status">
                      <span className="typing-dots" aria-hidden="true">
                        <i />
                        <i />
                        <i />
                      </span>
                      <span>
                        {busy
                          ? "Preparing a response…"
                          : "Awaiting a confirmed response"}
                      </span>
                    </div>
                  )}
                </div>
              ))}
              <div ref={endRef} />
            </div>
            <div className="chat-controls">
              <Failure value={error} />
              <div className="recovery-actions">
                {pending && !busy && (
                  <>
                    <button className="secondary" onClick={() => send(pending)}>
                      Check message / retry same message
                    </button>
                    <button
                      className="text-button"
                      onClick={() => setAbandon(true)}
                    >
                      Review unresolved message
                    </button>
                  </>
                )}
              </div>
              {abandon && pending && (
                <div role="alert" className="review-notice">
                  <Icon name="shield" />
                  <p>
                    The previous message may already have saved an enquiry or
                    callback. Check Conversations and Leads before starting
                    again. Starting another session does not cancel or retry
                    that request.
                  </p>
                  <div>
                    <button
                      onClick={() => {
                        setPending(null);
                        setSession(null);
                        setAbandon(false);
                        setError(
                          `Previous session ${session?.sessionId} remains unresolved; check Conversations and Leads.`,
                        );
                      }}
                    >
                      Leave this session without retrying
                    </button>
                    <button
                      className="secondary"
                      onClick={() => setAbandon(false)}
                    >
                      Keep checking this message
                    </button>
                  </div>
                </div>
              )}
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void send();
                }}
                className="composer"
              >
                <label className="sr-only" htmlFor={`message-${business}`}>
                  Message
                </label>
                <textarea
                  ref={composerRef}
                  id={`message-${business}`}
                  aria-label="Message"
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  onKeyDown={(e) => {
                    if (
                      e.key === "Enter" &&
                      !e.shiftKey &&
                      !e.nativeEvent.isComposing
                    ) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                  maxLength={8000}
                  rows={2}
                  disabled={busy || !!pending}
                  placeholder={
                    session
                      ? "Ask a question or tell us what you’re looking for…"
                      : "Type your message to start a conversation…"
                  }
                />
                <button
                  type="submit"
                  disabled={busy || !!pending || !text.trim()}
                >
                  <span className="send-label">
                    {busy ? "Processing…" : "Send message"}
                  </span>
                  <Icon name="arrow" />
                </button>
              </form>
              <div className="composer-footnote">
                <span>
                  Enter to send <span aria-hidden="true">·</span> Shift + Enter
                  for a new line
                </span>
                <span>
                  <Icon name="shield" />
                  Your business context stays separate
                </span>
              </div>
            </div>
          </section>
          <Overlay
            open={contextOpen}
            onClose={() => setContextOpen(false)}
            title="Conversation context"
            className="context-overlay"
          >
            <aside className="context-rail" aria-label="Conversation context">
              <div className="context-card">
                <span className="eyebrow">CONVERSATION CONTEXT</span>
                <div className="business-emblem">
                  <Icon name="book" />
                </div>
                <h3>{businessName}</h3>
                <p>{role}</p>
                <div className="context-row">
                  <span>Channel</span>
                  <strong>Web chat</strong>
                </div>
                <div className="context-row">
                  <span>Session</span>
                  <span className={`badge ${session ? "ready" : "neutral"}`}>
                    {session ? "Started" : "Not started"}
                  </span>
                </div>
                {session && (
                  <details className="session-details">
                    <summary>Session reference</summary>
                    <p className="session-id">Session {session.sessionId}</p>
                  </details>
                )}
              </div>
              <div className="context-card how-it-works">
                <span className="eyebrow">MADE FOR HELPFUL CONVERSATIONS</span>
                <div>
                  <span className="feature-icon">
                    <Icon name="book" />
                  </span>
                  <section>
                    <h4>Answers with context</h4>
                    <p>
                      Business documents and checked records help inform each
                      response.
                    </p>
                  </section>
                </div>
                <div>
                  <span className="feature-icon">
                    <Icon name="history" />
                  </span>
                  <section>
                    <h4>A conversation that remembers</h4>
                    <p>
                      Your preferences stay with this session as you explore
                      your options.
                    </p>
                  </section>
                </div>
                <div>
                  <span className="feature-icon">
                    <Icon name="check" />
                  </span>
                  <section>
                    <h4>Clear next steps</h4>
                    <p>
                      Review confirmed requests and saved enquiries before
                      following up.
                    </p>
                  </section>
                </div>
              </div>
              <div className="context-tip">
                <Icon name="spark" />
                <p>
                  A helpful starting point: share your interests, budget and
                  preferred schedule.
                </p>
              </div>
            </aside>
          </Overlay>
        </div>
    </>
  );
}

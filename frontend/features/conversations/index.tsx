"use client";
import { useState } from "react";
import { api } from "@/lib/api";
import { usePage } from "@/lib/use-page";
import type { ConversationView, TurnView } from "@/lib/contracts";
import Icon from "@/app/icons";
import { Failure, Sources } from "@/components/ui/feedback";
export default function Conversations({
  business,
  active,
  revision,
}: {
  business: string;
  active: boolean;
  revision: number;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const list = usePage<ConversationView>(
    active ? `${business}:conversations:${revision}` : null,
    (signal, cursor) => api.conversations(business, signal, cursor),
  );
  const history = usePage<TurnView>(
    active && selected ? `${business}:${selected}:${revision}` : null,
    (signal, cursor) => api.turns(business, selected!, signal, cursor),
  );
  return (
    <section aria-label="Conversations" className="panel">
      <div className="section-heading">
        <div>
          <h2>Conversations</h2>
          <p>Saved sessions and their actual message history.</p>
        </div>
        <button
          className="secondary"
              aria-busy={list.busy}
          onClick={() => {
            list.refresh();
            history.refresh();
          }}
        >
          <Icon name="refresh" />
          Refresh conversations
        </button>
      </div>
      <Failure value={list.error} />
      {list.busy && (
        <p className="skeleton" role="status">
          Loading conversations…
        </p>
      )}
      {!list.busy && !list.items.length && !list.error && (
        <p className="empty">No conversations yet.</p>
      )}
      <div className="split">
        <div className="record-list">
          {list.items.map((item) => (
            <button
              className={`record ${selected === item.sessionId ? "selected" : ""}`}
              key={item.sessionId}
              onClick={() => setSelected(item.sessionId)}
            >
              <span className="record-icon">
                <Icon name="chat" />
              </span>
              <strong>{item.channel} conversation</strong>
              <small>{item.sessionId}</small>
              <small>{new Date(item.createdAt).toLocaleString()}</small>
            </button>
          ))}
          {list.nextCursor && (
            <button disabled={list.busy} onClick={list.more}>
              More conversations
            </button>
          )}
        </div>
        <div className="detail">
          {!selected ? (
            <p className="empty">
              Select a conversation to inspect its messages.
            </p>
          ) : (
            <>
              <h3>Message history</h3>
              <Failure value={history.error} />
              {history.busy && (
                <p className="skeleton" role="status">
                  Loading messages…
                </p>
              )}
              {!history.busy && !history.items.length && !history.error && (
                <p>No saved messages in this session.</p>
              )}
              {history.items.map((turn) => (
                <div key={turn.turnId} className="history-turn">
                  <strong>
                    #{turn.sequence} · {turn.status}
                  </strong>
                  <p>
                    <b>Customer:</b> {turn.inputText}
                  </p>
                  {turn.output && (
                    <>
                      <p>
                        <b>Assistant:</b> {turn.output.reply}
                      </p>
                      <Sources sources={turn.output.sources ?? []} />
                    </>
                  )}
                  {turn.status !== "completed" && (
                    <p role="status">
                      {turn.status === "failed"
                        ? "This turn failed; no completed response was saved."
                        : "This turn has not completed."}
                    </p>
                  )}
                </div>
              ))}
              {history.nextCursor && (
                <button onClick={history.more} disabled={history.busy}>
                  More messages
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}

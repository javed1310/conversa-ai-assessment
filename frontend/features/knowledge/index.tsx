"use client";
import { useEffect, useRef, useState } from "react";
import { api, errorText } from "@/lib/api";
import { usePage } from "@/lib/use-page";
import type { DocumentDetail, DocumentView } from "@/lib/contracts";
import { Overlay } from "@/components/ui/overlay";
import { Markdown } from "@/components/ui/markdown";
import Icon from "@/app/icons";
import { Failure } from "@/components/ui/feedback";
export default function Knowledge({
  business,
  active,
}: {
  business: string;
  active: boolean;
}) {
  const [composing, setComposing] = useState(false);
  const [reading, setReading] = useState(false);
  const list = usePage<DocumentView>(
    active ? `${business}:documents` : null,
    async (signal) => {
      // Resolve every page before grouping: a newer version can be on a later page.
      const items: DocumentView[] = [];
      const seen = new Set<string>();
      let cursor: string | null | undefined;
      do {
        const page = await api.documents(business, signal, cursor);
        items.push(...page.items);
        cursor = page.nextCursor;
        if (cursor && seen.has(cursor)) throw new Error("Document pagination repeated. Refresh to retry.");
        if (cursor) seen.add(cursor);
      } while (cursor);
      return { items };
    },
  );
  const versionsById = new Map<string, DocumentView[]>();
  for (const doc of list.items) {
    const versions = versionsById.get(doc.documentId) ?? [];
    versions.push(doc);
    versionsById.set(doc.documentId, versions);
  }
  const documentGroups = [...versionsById.values()].map((versions) =>
    versions.sort((a, b) => b.version - a.version),
  );
  function openDocument(doc: DocumentView) {
    setReading(true);
    setDetailEntry(null);
    setError("");
    setSelection({ id: doc.documentId, version: doc.version });
  }
  const [selection, setSelection] = useState<{
    id: string;
    version: number;
  } | null>(null);
  const [detailEntry, setDetailEntry] = useState<{
    key: string;
    value: DocumentDetail;
  } | null>(null);
  const [detailRevision, setDetailRevision] = useState(0);
  const [detailErrorEntry, setDetailErrorEntry] = useState<{
    key: string;
    value: string;
  } | null>(null);
  const detailKey = selection
    ? JSON.stringify([
        business,
        selection.id,
        selection.version,
        detailRevision,
      ])
    : "";
  const detail = detailEntry?.key === detailKey ? detailEntry.value : null;
  const detailError =
    detailErrorEntry?.key === detailKey ? detailErrorEntry.value : "";
  const fileRead = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      fileRead.current++;
    };
  }, []);
  const [title, setTitle] = useState("");
  const [id, setId] = useState("");
  const [version, setVersion] = useState(1);
  const [content, setContent] = useState("");
  const [format, setFormat] = useState<"text" | "markdown">("markdown");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  async function readFile(file?: File) {
    const generation = ++fileRead.current;
    if (!file) return;
    if (file.size > 128 * 1024 || !/\.(txt|md|markdown)$/i.test(file.name)) {
      setError("Choose a text or Markdown file up to 128 KiB.");
      return;
    }
    try {
      const value = new TextDecoder("utf-8", { fatal: true }).decode(
        await file.arrayBuffer(),
      );
      if (!mounted.current || generation !== fileRead.current) return;
      if (value.length > 64000) throw new Error("Too long");
      setContent(value);
      setFormat(/\.txt$/i.test(file.name) ? "text" : "markdown");
      setError("");
    } catch {
      if (mounted.current && generation === fileRead.current)
        setError(
          "The file must contain UTF-8 text of at most 64,000 characters.",
        );
    }
  }
  useEffect(() => {
    if (!active || !selection) return;
    const controller = new AbortController();
    api
      .document(business, selection.id, selection.version, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) {
          setDetailEntry({ key: detailKey, value: result });
          setDetailErrorEntry(null);
        }
      })
      .catch((e) => {
        if (!controller.signal.aborted)
          setDetailErrorEntry({ key: detailKey, value: errorText(e) });
      });
    return () => controller.abort();
  }, [active, business, selection, detailKey]);
  async function add() {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.addDocument(business, {
        documentId: id,
        version,
        title,
        format,
        content,
      });
      if (!mounted.current) return;
      setDetailEntry({
        key: JSON.stringify([
          business,
          result.documentId,
          result.version,
          detailRevision,
        ]),
        value: result,
      });
      setSelection({ id: result.documentId, version: result.version });
      setNotice(`Document saved: ${result.status}.`);
    } catch (e) {
      if (mounted.current)
        setError(
          `${errorText(e)} Refresh documents to check status, or retry the same version and content.`,
        );
    } finally {
      lock.current = false;
      if (mounted.current) {
        setBusy(false);
        list.refresh();
      }
    }
  }
  return (
    <section aria-label="Knowledge base" className="panel">
      <div className="section-heading">
        <div>
          <h2>Knowledge base</h2>
          <p>Latest versions shown. Only ready documents can supply answers.</p>
        </div>
        <button className="pill-button" onClick={() => setComposing(true)}>
          Add document ↗
        </button>
        <button
          className="secondary"
              aria-busy={list.busy}
          onClick={() => {
            list.refresh();
            setDetailRevision((value) => value + 1);
          }}
        >
          <Icon name="refresh" />
          Refresh documents
        </button>
      </div>
      <Failure value={list.error || error || detailError} />
      {list.busy && (
        <p className="skeleton" role="status">
          Loading documents…
        </p>
      )}
      {!list.busy && !list.items.length && !list.error && (
        <p className="empty">No documents yet.</p>
      )}
      <div className="split">
        <div className="record-list">
          {documentGroups.map(([doc, ...older]) => (
            <div className="document-card" key={doc.documentId}>
            <button
              className="record"
              key={`${doc.documentId}:${doc.version}`}
              onClick={() => openDocument(doc)}
            >
              <span className="record-icon">
                <Icon name="file" />
              </span>
              <strong>{doc.title}</strong>
              <small>
                {doc.documentId} · v{doc.version}
              </small>
              <span className={`badge ${doc.status}`}>{doc.status}</span>
              {doc.failureCode && <small>{doc.failureCode}</small>}
            </button>
            {older.length > 0 && (
              <details className="document-history">
                <summary>Version history ({older.length})</summary>
                <ul>
                  {older.map((previous) => (
                    <li key={previous.version}>
                      <button className="version-entry" onClick={() => openDocument(previous)}>
                        <span>Version {previous.version}</span>
                        <span className={`badge ${previous.status}`}>{previous.status}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </details>
            )}
            </div>
          ))}
        </div>
        <Overlay
          open={reading}
          onClose={() => setReading(false)}
          title="Document reader"
          className="reader-overlay"
        >
          <div className="detail">
            <button
              className="secondary"
              onClick={() => {
                list.refresh();
                setDetailRevision((value) => value + 1);
              }}
            >
              <Icon name="refresh" />
              Refresh document
            </button>
            <Failure value={detailError} />
            {detail ? (
              <>
                <h3>{detail.title}</h3>
                <p>
                  Version {detail.version} · {detail.status} ·{" "}
                  {detail.expectedChunks} chunks
                </p>
                <Markdown text={detail.content} />
              </>
            ) : (
              <p className="empty">
                {detailError
                  ? "Document could not be loaded. Use Refresh document to retry."
                  : selection
                    ? "Loading document…"
                    : "Select a document to read its content."}
              </p>
            )}
          </div>
        </Overlay>
      </div>
      <Overlay
        open={composing}
        onClose={() => setComposing(false)}
        title="Add document"
        className="composer-overlay"
      >
        <div className="document-composer-layout">
          <form
            className="document-form"
            onSubmit={(e) => {
              e.preventDefault();
              void add();
            }}
          >
            <Failure value={error}/><div className="upload-form-heading">
              <span className="upload-symbol">
                <Icon name="upload" />
              </span>
              <div>
                <h3>Add document</h3>
                <p>Give your assistant something useful to know.</p>
              </div>
            </div>
            <p className="muted">
              Existing versions are immutable. Retry the same content or choose
              a new version for changes.
            </p>
            <label>
              Load text or Markdown file (optional)
              <input
                type="file"
                accept=".txt,.md,.markdown,text/plain,text/markdown"
                disabled={busy}
                onChange={(e) => void readFile(e.target.files?.[0])}
              />
            </label>
            <div className="form-grid">
              <label>
                Document ID
                <input
                  aria-label="Document ID"
                  required
                  pattern="[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}"
                  maxLength={128}
                  value={id}
                  onChange={(e) => setId(e.target.value)}
                />
              </label>
              <label>
                Version
                <input
                  aria-label="Version"
                  type="number"
                  min={1}
                  max={10000}
                  required
                  value={version}
                  onChange={(e) => setVersion(Number(e.target.value))}
                />
              </label>
              <label>
                Title
                <input
                  aria-label="Title"
                  required
                  maxLength={200}
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                />
              </label>
              <label>
                Format
                <select
                  aria-label="Format"
                  value={format}
                  onChange={(e) =>
                    setFormat(e.target.value as "text" | "markdown")
                  }
                >
                  <option value="markdown">Markdown</option>
                  <option value="text">Text</option>
                </select>
              </label>
            </div>
            <label>
              Document content
              <textarea
                aria-label="Document content"
                required
                maxLength={64000}
                rows={7}
                value={content}
                onChange={(e) => {
                  fileRead.current++;
                  setContent(e.target.value);
                }}
              />
            </label>
            <button type="submit" disabled={busy}>
              {busy ? "Saving and processing…" : "Save and ingest document"}
            </button>
            {notice && (
              <p role="status" className="success-toast">
                {notice}
              </p>
            )}
          </form>
          <aside className="live-preview">
            <span className="eyebrow">
              LIVE PREVIEW / {format.toUpperCase()}
            </span>
            {content ? (
              <Markdown text={content} />
            ) : (
              <div className="empty">
                <span aria-hidden="true">✳</span>
                <h3>
                  A little knowledge.
                  <br />A better answer.
                </h3>
                <p>Your document preview will appear here.</p>
              </div>
            )}
          </aside>
        </div>
      </Overlay>
    </section>
  );
}

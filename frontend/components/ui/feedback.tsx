import type { SourceRef } from "@/lib/contracts";
import Icon from "@/app/icons";
export function Failure({ value }: { value: string }) {
  return value ? (
    <div role="alert" className="error notice">
      <Icon name="shield" />
      <p>{value}</p>
    </div>
  ) : null;
}
export function Sources({ sources }: { sources: SourceRef[] }) {
  return sources.length ? (
    <details className="sources">
      <summary>Sources ({sources.length})</summary>
      <p>
        Document excerpts support policy information. Current fees, schedules
        and availability require checked business records.
      </p>
      {sources.map((s) => (
        <blockquote key={`${s.documentId}:${s.documentVersion}:${s.chunkId}`}>
          <strong>
            {s.title} · version {s.documentVersion}
          </strong>
          <p>{s.excerpt}</p>
        </blockquote>
      ))}
    </details>
  ) : null;
}

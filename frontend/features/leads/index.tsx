"use client";
import { api } from "@/lib/api";
import { usePage } from "@/lib/use-page";
import type { LeadView } from "@/lib/contracts";
import Icon from "@/app/icons";
import { Failure } from "@/components/ui/feedback";
export default function Leads({
  business,
  active,
  revision,
}: {
  business: string;
  active: boolean;
  revision: number;
}) {
  const list = usePage<LeadView>(
    active ? `${business}:leads:${revision}` : null,
    (signal, cursor) => api.leads(business, signal, cursor),
  );
  return (
    <section aria-label="Leads" className="panel">
      <div className="section-heading">
        <div>
          <h2>Leads</h2>
          <p>Saved enquiries, callback requests and counselor summaries.</p>
        </div>
        <button className="secondary" onClick={list.refresh}>
          <Icon name="refresh" />
          Refresh leads
        </button>
      </div>
      <Failure value={list.error} />
      {list.busy && (
        <p className="skeleton" role="status">
          Loading leads…
        </p>
      )}
      {!list.busy && !list.items.length && !list.error && (
        <div className="empty">
          <span className="empty-art" aria-hidden="true">
            ✳
          </span>
          <h3>No leads yet.</h3>
          <p>
            A good conversation is the first step. Start a chat and save a
            qualified enquiry to see it here.
          </p>
          <a href="#top">Back to the workspace ↑</a>
        </div>
      )}
      <div className="lead-count">
        <strong>{list.items.length.toString().padStart(2, "0")}</strong>
        <span className="eyebrow">
          LOADED ENQUIRIES
          <br />
          PEOPLE, NOT NUMBERS.
        </span>
      </div>
      <div className="lead-grid">
        {list.items.map((lead) => (
          <details className="lead-card" key={lead.leadId}>
            <summary className="lead-identity">
              <span className="contact-avatar">
                {lead.customer.name.slice(0, 1).toUpperCase()}
              </span>
              <div>
                <h3>{lead.customer.name}</h3>
                <p>{lead.customer.phone}</p>
              </div>
              <span className="lead-overview"><span>{lead.customer.courseId ?? lead.customer.statedInterest ?? "General enquiry"}</span><span>{lead.customer.budget == null ? "Budget not supplied" : `INR ${lead.customer.budget}`} / {lead.customer.schedule ?? "Schedule not supplied"}</span></span><span className="lead-expand">Explore enquiry ↗</span>
            </summary>
            <dl>
              <dt>Interest</dt>
              <dd>
                {lead.customer.courseId ??
                  lead.customer.statedInterest ??
                  "Not supplied"}
              </dd>
              <dt>Budget</dt>
              <dd>
                {lead.customer.budget == null
                  ? "Not supplied"
                  : `INR ${lead.customer.budget}`}
              </dd>
              <dt>Schedule</dt>
              <dd>{lead.customer.schedule ?? "Not supplied"}</dd>
              <dt>Callback</dt>
              <dd>
                {lead.callbackRequestedAt
                  ? `Requested ${new Date(lead.callbackRequestedAt).toLocaleString()}`
                  : "Not requested"}
              </dd>
              <dt>Automation</dt>
              <dd>
                <span className={`badge ${lead.automation.status}`}>
                  {lead.automation.status}
                </span>{" "}
                · {lead.automation.attempts} attempts
              </dd>
              <dt>Simulated notification</dt>
              <dd>
                {lead.notificationCreatedAt ? "Stored locally" : "Not stored"}
              </dd>
            </dl>
            {lead.automation.failureCode && (
              <p className="error">
                Processing failed: {lead.automation.failureCode}
                {lead.automation.retryable
                  ? " · retry eligible"
                  : " · needs review"}
              </p>
            )}
            {lead.automation.summary ? (
              <div className="summary">
                <h4>
                  <Icon name="spark" />
                  Counselor summary
                </h4>
                <p>{lead.automation.summary.summary}</p>
                <p>Intent: {lead.automation.summary.intent}</p>
                <p>
                  Qualification: {lead.automation.summary.qualificationStatus}
                </p>
                <p>
                  Recommended action:{" "}
                  {lead.automation.summary.recommendedAction}
                </p>
              </div>
            ) : (
              <p className="muted">
                Summary has not been generated. Refresh to check progress.
              </p>
            )}
            <small className="muted">Lead {lead.leadId}</small>
          </details>
        ))}
      </div>
      {list.nextCursor && (
        <button onClick={list.more} disabled={list.busy}>
          More leads
        </button>
      )}
    </section>
  );
}

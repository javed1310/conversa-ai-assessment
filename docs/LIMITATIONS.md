# Limitations and incomplete work

- This is a trusted local assessment workspace. Production user authentication,
  hosting and disaster recovery are not implemented. Do not expose inspection
  APIs containing customer data publicly without an authorization layer.
- Gemini chat, embeddings and summaries require a valid private key and available
  model/project quota. Output can fail validation; repairs/retries are bounded.
  Evidence-linked wording is checked and has factual/receipt fallbacks, but model
  judgments remain probabilistic and may add latency.
- Clear, whole-message enquiry requests, separate contact replies and explicit
  callback requests for an owned saved lead use the validated workflow without
  model classification. Broader interpretation,
  document answers and summary generation still depend on Gemini. Model-backed
  turns can take tens of seconds during provider degradation; the handoff fix is
  not a guarantee of low latency or unlimited quota for every message.
- Windows is qualified. Portable setup commands are provided, but Linux/macOS
  execution is unverified. The optional browser harness uses Windows process paths.
- External messaging and CRM notifications are local simulations. A saved callback
  request is not a scheduled/completed call. Media, real delivery and cross-channel
  customer identity linking are not implemented or required.
- Payments, direct enrolment, seat reservations, configuration editors, course
  management editors, analytics and workflow builders are outside the assessment.
  Uploading a course document does not create a catalogue record.
- One saved enquiry snapshot is retained per session. Later active-state corrections
  do not rewrite that lead or retarget its callback. Start a new session for a
  fresh enquiry; an update/cancel workflow would require explicit design.
- Phone validation checks syntax/numbering metadata, not ownership or reachability.
  Existing fictional ten-digit Indian mobile syntax is retained; outside India use
  an explicit +country code. Extensions and vanity numbers are unsupported.
- Retrieval uses a bounded small corpus and linear cosine search (499-chunk cap).
  Worker/provider capacity is bounded for a local prototype. Uncertain interrupted
  writes can require review rather than automatic replay.
- The training behavior module remains large and includes explicit dialogue rules.
  Its paths have regression coverage, but it has not been fully decomposed into
  smaller components. The small evaluation set does not comprehensively establish
  retrieval-threshold robustness or language generalization. Cases used for fixes
  are reported as regressions, not fresh held-out accuracy evidence.
- Business configurations and registered handlers are trusted code. A new business
  needs its own documents/data and tool registration; it is not an executable
  customer-uploaded plugin. Document truth depends on the supplied business policies.

No production-security or universal model-accuracy guarantee is claimed. The main
choices are in [DECISIONS](DECISIONS.md); [TESTING](TESTING.md) explains reproducible
checks and their boundaries. The candidate should practise the demonstration and
explain the design during the technical discussion.

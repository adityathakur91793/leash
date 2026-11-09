# PayPal Payment Operations and Manual Reconciliation

Phase 7G has no automated reconciliation worker or operator API. Attempts that
cannot be resolved by the existing authenticated recovery paths must remain blocked
until Payments/Engineering completes a reviewed recovery plan. Never edit SQLite
rows manually, mint another PayPal request ID, recreate an order, or retry capture
without a verified state transition.

## Identify and preserve an attempt

Use approved, read-only internal operational access to locate the row in
`paypal_order_attempts`. Record the attempt ID, proposal ID, owner ID, consent
binding reference, expected amount and currency, provider request ID, stored
provider order ID (if any), state, timestamps, and related audit sequence. Treat
owner IDs and consent bindings as restricted operational data; do not put them in
customer messages or general logs. Preserve provider responses and LEASH audit
entries in the incident record. Do not change the attempt or payment tables by hand.

## Verify with PayPal

An authorized Payments operator signs in to the configured PayPal Sandbox account
or uses the provider's authenticated, read-only order lookup. Do not issue a POST
or capture as part of state discovery. Match the provider order ID to the LEASH
attempt when one is stored. Compare the proposal reference, amount, currency, and
purchase unit to the durable attempt. For `COMPLETED`, additionally verify the
capture is `COMPLETED`, its capture ID is present, and its amount and currency match
exactly. Save the provider order ID, status, capture ID if present, lookup time, and
source in the restricted incident record. Never log bearer credentials or provider
secrets.

Interpret provider results as follows:

- **COMPLETED:** a financial result. Require the matching order ID, proposal
  reference, amount/currency, and a real completed capture ID. For either a local
  `paypal_orders` row or a durable attempt with an anchored provider order ID, direct
  the proposal owner to use the authenticated, proposal-scoped
  `POST /api/v1/proposals/{proposal_id}/checkout/reconcile` endpoint. It loads the
  persisted order internally and fetches PayPal state; the browser supplies no
  provider order ID. LEASH records settlement only after matching the provider
  order, proposal reference, amount, currency, and actual completed capture ID. This
  endpoint does not create an order or initiate a capture. It requires a valid
  session belonging to the proposal owner; operators must not impersonate the
  owner. Do not represent reconciliation as new authorization.
- **APPROVED:** buyer approval only; it is not payment. The proposal-scoped recovery
  endpoint reports this as non-completed and does not enable capture. Continue only
  through the normal checkout and explicit capture flow when current authorization,
  consent, mandate, assessment, and budget checks pass. An expired or invalid
  authorization means hold the order and escalate; do not capture.
- **CREATED:** no verified buyer approval or payment. Resume only the existing
  stored order when LEASH's normal consent and authorization checks permit it. Never
  make a replacement order.
- **VOIDED:** provider-side terminal state. The current LEASH status enum has no
  `VOIDED` value. Do not map it by hand; record the evidence and escalate for a
  supported state mapping.
- **Unresolved, unavailable, mismatched, or incomplete evidence:** keep the attempt
  in reconciliation-required handling. Provider unavailability returns no success;
  retry factual verification later when service is available. Mismatched or
  incomplete evidence remains non-success and may be marked reconciliation-required;
  stop order/capture actions and escalate to Payments/Engineering. Do not infer a
  result from a browser return URL.

The endpoint requires an authenticated session for the proposal's existing owner.
LEASH's current anonymous session model has no persistent account identity: a
completely new anonymous identity cannot recover another principal's proposal.
Recovery requires a renewed same-owner session or another active session for that
principal. Do not attempt to work around this by supplying an owner ID or impersonating
the owner.

The authenticated recovery endpoints are supported only when their documented
preconditions hold. In particular, an attempt past the six-hour provider key
window with no stored provider order ID has no supported LEASH endpoint to attach a
newly discovered provider ID. Keep it blocked, preserve the read-only evidence, and
escalate for a reviewed recovery capability; do not update the database manually.

## Audit and escalation

Record the operator, incident/ticket reference, attempt ID, provider order ID, exact
provider status and lookup time, amount/currency comparison, capture ID where
applicable, endpoint used for recovery, resulting LEASH status, and audit event
sequence. A second authorized reviewer must confirm any completed-capture evidence
before declaring the incident resolved. If the provider response changes or cannot
be reproduced, return the case to unresolved and keep payment actions blocked.

Automated reconciliation, queue alerting, and a dedicated least-privilege operator
workflow remain future work. Until then, unresolved attempts require operator
intervention and some expired attempts cannot be recorded locally through a
supported endpoint.

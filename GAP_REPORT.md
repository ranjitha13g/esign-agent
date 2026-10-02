# Gap report — seat 18 (e-sign)

**Team 18 · app `esign` · books: Suryodaya Precision Works (India) and Keystone Precision Works LLC (US)**

Our side measured against the live API on **2 October 2026** — `GET /api/schemas`,
`tools/list` over `POST /api/mcp`, and the live row census. Raw output in
`discovery/`, working notes in `discovery/FINDINGS.md`.

> **Correction to the first draft.** That version was written from the `esign` UI and
> benchmarked against Dropbox Sign. Reading the schema afterwards disproved most of it.
> Templates, a field model, bulk send, signer ordering, access-code authentication,
> webhooks and content hashing all exist; we had claimed we lacked every one. The
> genuine gaps turned out to be narrower, stranger, and more interesting. That is why
> this version leads with what the schema says rather than what the screens suggest.

---

## 0. What the platform actually has

Twelve entities: `EsignDocument`, `EsignSigner`, `EsignSignature`, `EsignField`,
`EsignTemplate`, `EsignDocumentVersion`, `EsignBulkSend`, `EsignBulkSendRecipient`,
`EsignConsentReceipt`, `EsignCorrection`, `EsignWebhook`, `EsignAuditLog`.

Signing is modelled seriously. `EsignDocument.signing_order` is `sequential | parallel`
with `current_signer_order` tracking position; `EsignSigner.sign_order` is required.
`auth_method` is `email_link | access_code` with `access_code_hash`,
`auth_failed_attempts`, `authenticated_at` and `token_revoked_at`. Documents carry
`content_hash`, `audit_summary_json`, `consent_required` and `consent_text`.
Void-and-reissue is first-class: `replaces_document_id`, `replaced_by_document_id`,
`void_reason_code`, plus `endpoint.esign.corrections.replace`.

Against a mature product this is a credible feature set. The gaps are elsewhere.

## 1. What a modern product does that we do not

Benchmarked against **AI-native agreement platforms** rather than a long-established
e-signature tool, on the reasoning that an incumbent with a chat window bolted on
teaches less than something built assuming an agent drives it. The comparison set and
the trial notes are being finalised; what follows is grounded in our own API, which is
the half that determines what we can build.

**No reminder capability of any kind.** This is the headline. The entire catalogue for
this seat contains exactly three transitions:

```
EsignDocument.apply_template
EsignDocument.send_for_signature
EsignDocument.reissue_signing_link
```

There is no remind, nudge, notify or resend. A configurable reminder schedule is a
checkbox in every comparable product; here it does not exist.

**Nowhere to record that a chase happened.** `EsignDocument` has no `reminder_count`
and no `last_reminded_at`. `EsignAuditLog` is the one esign entity with a schema but
**no tools at all**, so the audit trail cannot be read either. Chase history therefore
has no home in the platform.

**No expiry enforcement.** `expires_at` exists; nothing acts on it, and it is `null` on
all five sent documents in the live book.

**The workflow is opaque.** `status` declares flow `EsignDocumentFlow`, but the flow is
not introspectable: no flow tool, no flow entity, and `_transitions` is `[]` on every
record. The states had to be recovered by observing both books — `draft, sent, viewed,
completed, declined, voided, expired`. An agent cannot ask what moves are legal, and a
team working only from the Indian book would see just two of the seven.

**No field-level permission.** Access is granted per entity, so reaching an entity
means reaching every row and column in it.

**Open tracking is unpopulated on one book.** `EsignSigner.last_opened_at` is set on
74 of 144 Keystone signers but on **0 of 8** on Suryodaya, including signers already
marked `viewed` or `signed`. Any agent that calibrated against the Indian book alone
would conclude the field is broken.

## 2. Which gaps an agent can close with the tools this seat has

**Ours to build — and now built:**

- **The chase layer.** No reminder transition exists, so the agent composes the chase
  and routes it, degrading to a draft for a human rather than pretending to send. Where
  a stale document was never opened, `reissue_signing_link` is a real act that rotates
  the link — closer to the right remedy than a fourth identical nudge.
- **Chase state in `AgentMemory`.** Private to this seat, and enough to make "do not
  chase a fifth time" enforceable.
- **Sequential routing honoured.** Chase only `current_signer_order`; asking signer 2
  before signer 1 has signed is asking them to act out of turn.
- **Expiry respected.** Lapsed documents are never chased; they are flagged for void or
  reissue.
- **Diagnosis in place of status.** Sent-but-no-signers, stale-and-never-opened, and
  chased-to-exhaustion are each separated and given a different recommendation.

**Platform work, not ours:**

- **A reminder transition, and a field to record it against.** Everything else in the
  chase layer is orchestration; this is not.
- **Readable audit trail.** `EsignAuditLog` needs tools, or chase history stays a
  private guess.
- **A discoverable workflow.** Until `_transitions` is populated, every agent on this
  seat is guessing at state names. We fail closed rather than guess — nothing outside a
  confirmed-chaseable state is ever acted on — but that is a workaround.
- **Expiry enforcement**, and **populating `last_opened_at`**.

## 3. What an agent can do that their product cannot

**It can tell "nothing to do" apart from "this data is broken".** Against the live
Indian book the honest answer to *"what is pending signature and with whom"* is that
**nothing is pending with anyone** — the five sent documents carry no signer rows, and
the four documents that do have signers were never sent. A dashboard renders that as an
empty list and a clean conscience. Our agent escalates it, because a sent agreement
addressed to nobody is a defect somebody has to fix.

**It chases on judgement rather than a schedule.** Not a fixed day-3/day-7 cadence, but:
never chase a terminal or superseded document; never chase out of turn; reissue rather
than re-nudge where the link was never opened; stop after three and escalate; weight
tone by how overdue it is.

**It re-reads before it acts.** A human clicking *remind* sends a reminder that was
already stale when the page rendered. Our executor re-reads immediately before writing
and stands down if the state moved — demonstrated in the harness, where a signer
completes between the decision and the write and the agent declines to chase.

**It refuses.** Asked for the terms behind a packet it hits the seat boundary on
`Contract` and says so. Asked *why* someone has not signed it answers that the records
hold no reason — the single most valuable thing it does, in a domain where evidence is
the product.

## 4. What we could not verify, and why

- **Keystone: verified.** The agent runs unchanged on both books, reading jurisdiction
  from `Company` (India/INR, United States/USD). The capability surface is identical —
  same 234 tools, same fingerprint — so only the data differs, and it differs a lot:
  Suryodaya has nothing genuinely pending, Keystone has ten. Verifying on the Indian
  book alone would have left two real defects in place: `viewed` documents skipped as
  unchaseable, and `last_opened_at` written off as dead when it is merely unpopulated
  there.
- **The non-business verticals.** `school.`, `clinic.`, `retail.` and `agency.` all
  return `502`. The hardcoded-noun check runs as a lint instead.
- **`GET /api/accounting/locale`** returns `403 App 'accounting' is not enabled for your
  account`, so the documented terminology source is unavailable to this seat. We read
  `country` and `default_currency` from `Company` instead.
- **A refusal does not name an escalation route.** The guideline says each refusal names
  the route through; ours returns only `App 'contracts' is not enabled for your
  account`. The escalation path had to be found separately
  (`endpoint.agent_governance.escalations.raise`), and `delegate_to_agent` — named in
  the guideline — is not in the 13 agent tools.

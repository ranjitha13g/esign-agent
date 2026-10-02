# Discovery findings — seat 18 (e-sign), Suryodaya

Pulled live on **2 October 2026**, both books.
Raw output in `discovery/suryodaya/`. Regenerate summaries offline with
`uv run python scripts/resummarise.py` rather than re-hitting a shared database.

---

## 1. The seat, as the server states it

```
roles         user, agent_user, sales_viewer
allowed_apps  esign, agent, crm
tools         234   (450 entity schemas are visible; only our apps are callable)
company       Suryodaya Precision Works Pvt. Ltd.  country=India  currency=INR
```

`Contract` → `403 App 'contracts' is not enabled for your account`
`SalarySlip` → `403 App 'payroll' is not enabled for your account`

**The refusal does not name an escalation route.** The guideline says each refusal names
the route through; this one gives only the app name. The escalation path has to come
from somewhere else — `endpoint.agent_governance.escalations.raise` is in our catalogue
and is the candidate.

## 2. Terminology source — the planned one is unavailable

`GET /api/accounting/locale` → **403 `App 'accounting' is not enabled for your account`.**

So `domain/locale.py` cannot use it. The reachable substitute is `Company`:
`country`, `default_currency`, `fiscal_year_start`, and `active_domains` (children).
That is enough to distinguish India/INR from the US book without naming a noun.

## 3. The esign surface — 12 entities

`EsignDocument` `EsignSigner` `EsignSignature` `EsignField` `EsignTemplate`
`EsignDocumentVersion` `EsignBulkSend` `EsignBulkSendRecipient` `EsignConsentReceipt`
`EsignCorrection` `EsignWebhook` `EsignAuditLog`

**`EsignAuditLog` has a schema but no tools.** It is the only esign entity with no
`.list`, so the audit trail is not readable from this seat.

Endpoint tools: `endpoint.esign.bulk_send`, `.bulk_send.status`,
`.corrections.replace`, `.corrections.trail`, `.signer_auth`.

### Only three transitions exist

```
EsignDocument.apply_template        (id)
EsignDocument.send_for_signature    (id)        draft -> sent; mints signing links
EsignDocument.reissue_signing_link  (id, signer_id)  rotates one signer's link
```

`send_for_signature` is documented as *"returns them when no mail transport is
configured"* — so link delivery may be the agent's problem, not the platform's.

## 4. The open question, answered: there is no reminder transition

The chase step of the graded request has **no native tool**. The closest is
`reissue_signing_link`, which rotates a signer's link and invalidates the old one —
usable as a chase, but it is a different act with a side effect, not a nudge.

Worse, there is nowhere to record a chase:

- `EsignDocument` has **no `reminder_count` and no `last_reminded_at`**.
- `EsignAuditLog` is unreadable, so chase history cannot be derived from the trail.

**Consequence:** chase state must live in `AgentMemory`, which is private to team 18.
The `reminder_count_incremented_for` verifier in `harness/tasks.yaml` cannot work as
written and must assert against `AgentMemory` instead.

## 5. GAP_REPORT.md is substantially wrong

Every ⚠ item resolved against the real schema, and most resolved the other way. The
platform has far more than the report assumed:

| GAP_REPORT claim | Reality |
|---|---|
| "no field model" ⚠ | `EsignField` — full CRUD |
| templates absent | `EsignTemplate` CRUD + `EsignDocument.apply_template` |
| "we have no bulk path" | `EsignBulkSend` + `EsignBulkSendRecipient` + endpoints |
| "signer list appears flat and unordered" ⚠ | `EsignSigner.sign_order` is **required**; `EsignDocument.signing_order` is `sequential\|parallel`; `current_signer_order` tracks position |
| "no signer authentication beyond possession of a link" ⚠ | `auth_method` `email_link\|access_code`, `access_code_hash`, `auth_failed_attempts`, `authenticated_at`, `token_revoked_at`, `endpoint.esign.signer_auth` |
| "we record events but no seal" ⚠ | `content_hash`, `audit_summary_json`, `EsignConsentReceipt`, `consent_required`, `consent_text` |
| "webhooks… fixable only server-side" | `EsignWebhook` exists |
| void-and-reissue presented as our insight | **natively modelled**: `replaces_document_id`, `replaced_by_document_id`, `void_reason_code`, `EsignCorrection`, `endpoint.esign.corrections.replace` |

Genuinely absent, and therefore the real gap report: **no reminder/chase capability, no
chase state, no readable audit trail, no expiry enforcement** (`expires_at` exists and is
`None` on all five sent documents), and **no escalation route named in a refusal**.

## 6. The live data will not support the graded request

```
EsignDocument   100 rows   95 draft, 5 sent   all 19 days old
EsignSigner       8 rows   4 pending, 3 viewed, 1 signed, across 4 documents
```

- **All five `sent` documents have zero signers.**
- **All four documents that have signers are `draft`** — never sent.
- So *"what is pending signature and with whom"* has no valid live answer. Nothing is
  pending with anyone.
- Every `last_opened_at` is `None`, including on signers whose status is `viewed` or
  `signed`. The field exists but is not populated.
- `_transitions` is `[]` on every document inspected, draft and sent alike. The generic
  `transition` agent tool has nothing to offer here; state moves go through the three
  named tools.

**This makes `harness/fake.py` mandatory rather than a convenience.** `created_at` is
server-stamped, so a realistic ageing scenario cannot be seeded live either.

It also sets the agent's correct behaviour against live Suryodaya data: the honest
answer is *"nothing is actually pending with anyone, and here is why"* — which is a
refusal-shaped answer, and a good thing to be able to produce.

## 7. The 13 agent tools are generic, and `delegate_to_agent` is not among them

```
get_schema  list  get  create  update  delete  transition
search  count  make_from  bulk_update  report  financial_report
```

The plan assumed `delegate_to_agent` as the escalation route. It does not exist.
Use `endpoint.agent_governance.escalations.raise`.

## 8. Protocol notes confirmed in the wild

- `tools/call` wraps payloads: `{"content": [{"type": "text", "text": "<json>"}]}`.
  Row data arrives as a **JSON string inside a text block**. `mcp/client.py` now
  unwraps it, and treats `isError` as a failure distinct from a JSON-RPC error.
- `/docs`, `/redoc`, `/openapi.json` and `/api/mcp` all answer `401` unauthenticated.
- `GET /api/schemas` answers `{"schemas": [...], "total": N}` — a list, not a map.
- List results are `{"data": [...], "limit", "offset", "total"}`.

## 9. Keystone, and the state machine it revealed

The capability surface is **identical** across the two books: same 234 tools, same 450
schemas, same three transitions, same 403s, and the same fingerprint hash
(`bad3eb24db35d25b`). The schema really is shared; only the data differs.

And the data differs enormously. Suryodaya is a near-empty book; Keystone is a working
one, which is what finally exposed the flow:

| | Suryodaya | Keystone |
|---|---|---|
| Documents | 100 | 98 |
| Statuses seen | draft 95, sent 5 | draft 9, **sent 6, viewed 5**, completed 31, declined 6, voided 5, expired 36 |
| Signers | 8, across 4 drafts | 144 |
| `last_opened_at` populated | **0 / 8** | 74 / 144 |
| Genuinely pending | **none** | 10 |

Three consequences, each of which changed the code:

1. **`viewed` is a document state, and it is still live.** All five such documents hold
   outstanding signers. Treating `sent` as the only chaseable state silently skipped
   every one. `DOC_CHASEABLE` is now `{sent, viewed}` — on evidence, not on a guess.
2. **Terminal states hold outstanding signers.** `expired`, `declined` and `voided`
   carry **49 outstanding signers** between them on Keystone. An agent that chased
   "anyone who has not signed" would chase all 49, on dead documents, in a shared book.
   This is the single strongest argument for the allowlist design.
3. **`last_opened_at` is not dead, only unpopulated on Suryodaya.** Had we verified on
   the Indian book alone we would have concluded the field was broken platform-wide and
   built the chase layer around that false premise. This is precisely the failure the
   course warns about: well formed, and wrong.

## Still open

- Keystone: everything above needs re-running on the US book.
- Whether `school.` / `clinic.` / `retail.` / `agency.` accept this login (`--probe`).
- Whether `send_for_signature` actually mails, or returns links for us to deliver.
- What `endpoint.esign.corrections.replace` requires, and whether it is the sanctioned
  void-and-reissue path.

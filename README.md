# Team 18 — e-sign agent & harness

An agent for the `esign` seat on AgentSwitch, and the harness that proves it works.

**The graded request:** *"What is pending signature and with whom, and chase everything
sitting over a week."*

Three steps, a judgement call, and a state change in the middle. Listing pending
documents is a demo; deciding **who to chase, who not to chase, and who to stop
chasing** is the seat.

Platform findings and the competitive analysis live in [GAP_REPORT.md](GAP_REPORT.md)
and [discovery/FINDINGS.md](discovery/FINDINGS.md). What the agent was asked and what
it answered is in [PROMPT_TESTS.md](PROMPT_TESTS.md).

---

## Quick start

Requires **Python 3.11+** and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ranjitha13g/esign-agent
cd esign-agent
uv sync
```

Everything here runs with **no credentials and no model key**:

```bash
uv run pytest                      # 112 tests
uv run python -m harness.runner    # 10 harness tasks
uv run ruff check .
```

For the live platform, create `.env`:

```bash
cp .env.example .env
```

```sh
AS_EMAIL=team18@theschoolofai.in
AS_PASSWORD_SURYODAYA=...     # India  — from your team channel
AS_PASSWORD_KEYSTONE=...      # US     — different password, same email
ANTHROPIC_API_KEY=...         # only for the natural-language planner
```

`.env` is gitignored and must stay that way. Every write to the platform is attributed
to whoever holds these.

---

## Running it

### Discovery — what the seat can see

```bash
uv run python scripts/discover.py                  # both books
uv run python scripts/discover.py --book keystone
uv run python scripts/discover.py --check-drift    # fail if the platform moved
uv run python scripts/resummarise.py               # rebuild summaries from disk
```

Read-only. Writes to `discovery/<book>/`; start with `summary.json`.

### The agent — the graded request

```bash
uv run python -m agent.run                 # Suryodaya, dry run
uv run python -m agent.run --book keystone
uv run python -m agent.run --json
```

Keystone:

```
Book        Keystone Precision Works LLC (United States, USD)
Documents   98

PENDING SIGNATURE, AND WITH WHOM
  db69fe96  Tooling Loan Agreement — Great Lakes Impleme    16d  parallel
            waiting on: Dell Ferraro <...> [viewed], Ray Kozlowski <...> [pending]

CHASE PLAN  (10 to act on)
  [REISSUE] Vendor Agreement Renewal FY27 -> lorraine.petrucci@keystoneprecision.com
            stale 16 days and every signing link has expired; a reminder would
            point at a dead link

NOT ACTED ON
    36  state is 'expired', which is terminal
```

Suryodaya answers differently, and that is the point:

```
PENDING SIGNATURE, AND WITH WHOM
  Nothing is pending with anyone.
  5 sent, but carrying no signer rows at all.
  4 hold outstanding signers but were never sent.
  That combination is a data problem, not an empty inbox.
```

### The planner — plain language

Needs `ANTHROPIC_API_KEY`.

```bash
uv run python -m agent.ask
uv run python -m agent.ask "Why hasn't the Shreeji signer signed yet?"
uv run python -m agent.ask --book keystone --show-calls

uv run python -m agent.ask --record chase-stale-week   # record for CI
uv run python -m agent.ask --replay chase-stale-week   # no network, no key
```

### The harness

```bash
uv run python -m harness.runner
uv run python -m harness.runner --task chase-stale-week
uv run python -m harness.runner --live-read     # read the live book first
```

```
chase-stale-week-live        approve
  [  ok  ] no_document_mutated_outside
  [  ok  ] every_chased_slot_age_gte_days
  [  ok  ] no_chase_on_state
  [  ok  ] chase_recorded_for_each

runs/20261002T101532Z/   10/10 tasks approved
```

### Sending for real

Off by default, and you have to ask twice:

```bash
AS_ALLOW_SEND=1 uv run python -m agent.run --mode live
```

A chase is a real notification to a seeded person in a book other teams are using.
`AS_ALLOW_SEND` is never set in CI.

---

## Layout

```
scripts/
  discover.py      dump both books; --check-drift for CI
  resummarise.py   rebuild summaries from saved JSON, no live calls
  ci-local.sh      run the CI jobs against a pristine checkout
  mutate.py        break the code on purpose, check the suite notices
mcp/
  transport.py     HttpTransport | RecordingTransport | ReplayTransport
  client.py        JSON-RPC: envelope errors, closed schemas, idempotency, paging
  retry.py         bounded backoff; transient failures only
domain/
  locale.py        the ONLY module allowed to name a business noun
  esign.py         Document/Signer model, states, ageing, link expiry
agent/
  policy.py        pure functions: state in, decision out. No I/O.
  executor.py      the only module that writes. Dry-run by default.
  planner.py       the goal-holding loop over Claude
  model.py         AnthropicClient | RecordingModel | ReplayModel
  run.py           deterministic CLI for the graded request
  ask.py           natural-language CLI
harness/
  fake.py          in-memory platform at the transport seam
  scenarios.py     nine worlds the live book cannot produce
  verify.py        verifiers that read rows, never prose
  runner.py        persists each run, then scores it from disk
  tasks.yaml       the task set
```

---

## How it works

### Policy and executor are separate

`policy.py` decides and is pure; `executor.py` acts and decides nothing. That split is
why the task set can run repeatedly in dry-run while still exercising every judgement.

| Decision | When |
|---|---|
| `CHASE` | stale past the bar, still outstanding, someone is blocking |
| `REISSUE` | the signing link has expired, or we chased once and it was *still* never opened |
| `STOP_CHASING` | expired document, or chased three times already |
| `ESCALATE` | sent but carrying no signers — nobody to chase |
| `SKIP` | unsent, terminal, fully signed, too fresh, or **a state we have never seen** |

`REISSUE` is not an opening move: it invalidates the link the signer already holds, and
on first contact there is no evidence it failed. The exception is a dead link — on
Keystone every signer on all ten pending documents had `access_token_expires_at` in the
past, so a reminder there would have been polite and pointed at nothing.

### Unknown states fail closed

`EsignDocument.status` declares flow `EsignDocumentFlow`, but the flow is not
introspectable — no flow tool, no flow entity, `_transitions` is `[]` on every record.
So chaseability is an **allowlist** (`sent`, `viewed`) established by observing both
books, not a guessed terminal denylist. A state we have never seen is skipped.

Not theoretical: on Keystone, `expired`, `declined` and `voided` documents hold **49
outstanding signers** between them. An agent chasing "anyone not yet signed" would
chase all of them, on dead documents, in a shared book.

### Terminology comes from the API

The agent is graded on two books. `domain/locale.py` is the only module permitted to
name a business noun, and it reads jurisdiction from `Company`. Everything else
receives resolved values, and a lint enforces it. The same code produces India/INR and
United States/USD without modification.

### Record and replay

Both MCP calls **and** model calls are recorded to `cassettes/`, with the clock frozen
into the cassette metadata. CI therefore needs no `ANTHROPIC_API_KEY`, spends nothing,
and cannot drift as the recording ages. A replay failure always means behaviour changed.

Cassettes are an ordered log: a repeated call replays in sequence rather than collapsing
onto one response, because re-reading a row that changed underneath you is exactly what
the concurrency task proves.

### Behaviour under failure

**Retries are bounded and selective.** Connection errors, timeouts, `429` and `5xx` get
three attempts with exponential backoff and full jitter. `401`/`403`/`400` get none —
they will fail identically, and retrying adds load to a platform 26 teams share. **A
write the platform cannot deduplicate is never retried**, since the attempt that looked
like it failed may have landed.

**A failing document does not lose the run.** The runner records the error, continues,
and persists everything in a `finally`. Any recorded error adds a
`run_completed_without_error: unevaluated` verdict — and unevaluated is never a pass.

**Oversize tool results shrink audibly.** Slicing the JSON would hand the model half an
object that still reads like data. Instead rows are dropped — skipped verdicts first —
until it fits, with an in-band `TRUNCATED` marker and a warning on the run. Output is
always valid JSON.

### Prompt injection

Document titles are attacker-controlled text in a database 26 teams write to, and they
reach the model. Two defences, in order of importance:

1. **The model cannot act.** The planner's three tools are all reads; the write path
   (`policy.py`, `executor.py`, `runner.py`) contains no model at all.
2. **The policy never reads free text.** Decisions come from status, dates and signer
   state. `tests/unit/test_resilience.py` asserts it directly: two documents differing
   *only* in title must decide identically.

---

## The harness

Three rules from the brief, followed literally:

1. **Our own loop**, not a wrapper around AgentSwitch's.
2. **Verifiers read the database, never the agent's prose.** If the agent says it
   chased somebody, we re-read the rows and check.
3. **Every run is written to disk before anything is scored**, so a failure is always
   replayable.

Verdicts mirror the course: `approve`, `revise`, `unevaluated` — and **`unevaluated` is
never a pass**. A verifier that raises is recorded as unevaluated, never swallowed.

### Verifiers

| Verifier | Asserts |
|---|---|
| `no_writes_at_all` | dry run means dry |
| `no_chase_recorded` | *we* chased nobody (where the scenario mutates rows on purpose) |
| `no_document_mutated_outside` | nothing written beyond the allowed fields |
| `every_chased_slot_age_gte_days` | nothing fresher than the bar was chased |
| `no_chase_on_state` | nothing in a forbidden state was chased |
| `chase_recorded_for_each` | every claimed chase left a durable trace |
| `exactly_one_chase_record_per_document` | a retry did not double-chase |

### Scenarios

The live book cannot pose the questions that matter: on Suryodaya every document is the
same age, 95 are drafts, and the 5 sent ones have no signers. `created_at` is
server-stamped, so a realistic ageing scenario cannot be seeded there either.

`mixed_book` · `terminal_states` · `sequential_routing` · `already_chased` ·
`signs_mid_run` · `expired_document` · `dead_signing_links` · `sent_without_signers` ·
`out_of_seat`

`signs_mid_run` is the concurrency case: a signer completes **between the policy's read
and the executor's write**, and the run record shows the agent standing down —
*"action: chase | performed: False | stood down: state changed to 'completed' before we
acted."*

### Run artefacts

```
runs/<timestamp>/<task-id>/
  before.json     database state before
  after.json      database state after
  run.json        decisions, tool calls, chased ids, errors, retries
  verdicts.json   what each verifier concluded
```

---

## Testing

```bash
uv run pytest                  # all 112
uv run pytest tests/unit -q    # hermetic
```

| Suite | Covers |
|---|---|
| `tests/unit/test_transport.py` | keying, recording, ordered replay |
| `tests/unit/test_client.py` | HTTP-200 error envelopes, closed schemas, seat boundary, idempotency, content unwrapping |
| `tests/unit/test_policy.py` | every decision branch, against constructed cases |
| `tests/unit/test_planner.py` | the loop, tool dispatch, injected clock |
| `tests/unit/test_render.py` | the report, including the live book's shape |
| `tests/unit/test_resilience.py` | retries, oversize results, dead links, hostile titles |
| `tests/unit/test_harness.py` | the harness's own machinery: verdict semantics, persistence on failure |
| `tests/test_no_hardcoded_nouns.py` | AST scan for business nouns outside `locale.py` |
| `tests/test_prompt_behaviour.py` | six recorded prompts replayed: refusals, false premises, out-of-seat asks |

**Two layers, deliberately.** The harness proves *system* behaviour by reading rows; the
unit tests pin the *policy*. Break the policy so it chases terminal documents and
`never-chase-terminal` still passes, because the executor's re-read independently
refuses — but two unit tests fail. Do not read a green task as a green policy.

**Check the tests still bite.** A suite nobody has seen fail is a suite nobody should
trust, so there is a mutation runner: it breaks the code on purpose, one plausible
regression at a time, and reports anything the suite fails to notice.

```bash
uv run python scripts/mutate.py          # all 28 mutations
uv run python scripts/mutate.py --list
uv run python scripts/mutate.py --only policy
```

It runs **both** gates CI runs — `pytest` and the harness task set — because the task
set is not pytest, and checking pytest alone reports every harness-covered behaviour as
unguarded. Currently **28/28 killed**.

It has already earned its keep. It found that
`test_reads_do_not_carry_an_idempotency_key` passed for the wrong reason (the fixture's
schema blocked the field regardless of the check under test), that `page()` had no test
at all, and that nothing verified the harness's own verdict semantics.

---

## CI

`.github/workflows/ci.yml`, four jobs. No secrets reach pull requests.

| Job | When | Does |
|---|---|---|
| `lint` | push, PR | `ruff` + the hardcoded-noun check |
| `unit` | push, PR | hermetic unit tests |
| `harness-replay` | push, PR | the task set, then the recorded planner run |
| `live-smoke` | nightly, manual | read-only against both books; fails on capability drift |

### Testing CI before you push

```bash
bash scripts/ci-local.sh          # lint + unit + harness, no secrets
bash scripts/ci-local.sh --live   # also the drift check, needs .env
```

It copies **only what git would include** into a temp directory and runs the jobs
there. Running them in your working tree instead is how a green local run becomes a red
first build — a file CI needs turns out to be gitignored, or was never added.

It also catches two things a plain `pytest` will not:

- **a stale lockfile** — jobs run `uv sync --locked`, which asserts `uv.lock` still
  matches `pyproject.toml`. (`--frozen`, the obvious choice, installs happily from a
  stale lockfile.)
- **silently skipped replay tests** — they skip themselves when no cassette is
  committed, and pytest reports that as success.

What only exists on GitHub — `astral-sh/setup-uv`, the cache, secrets, the schedule and
artifact upload:

```bash
git switch -c ci-check && git push -u origin ci-check
gh run watch
gh run view --log-failed
```

```bash
gh secret set AS_PASSWORD_SURYODAYA
gh secret set AS_PASSWORD_KEYSTONE
gh workflow run ci.yml             # runs live-smoke too
```

**Drift detection.** `discovery/fingerprint.json` holds a hash of `tools/list` plus the
`esign` schemas, per book. The nightly job recomputes and compares. A change means the
cassettes are stale and the platform moved underneath you.

No `ANTHROPIC_API_KEY` is needed in CI. Nothing in CI writes.

---

## Troubleshooting

**`Missing in .env: AS_PASSWORD_...`** — copy `.env.example` to `.env` and fill it in.

**`401` on login** — the two books have *different* passwords for the same email. Check
you have not swapped them.

**`502` from `school.` / `clinic.` / `retail.` / `agency.`** — those instances are down,
not refusing you.

**`UnrecordedInteraction` / `UnrecordedCompletion`** — the agent diverged from the
recorded run, which is a real signal. If intentional, re-record:

```bash
uv run python -m agent.ask --record chase-stale-week
```

**`ToolNotAvailable`** — a tool outside this seat is *absent* from the catalogue rather
than refused when called. That absence is the permission answer; do not retry it.

**Mojibake on Windows** — set `PYTHONIOENCODING=utf-8`.

---

## A note on data

The repository is **private and must stay that way**: `cassettes/` and `discovery/`
contain real counterparty names and email addresses from a shared book. Making it
public would publish that data retroactively through git history.

# brew-agentic-ab-test

Closed-loop A/B testing on top of [Brew](https://brew.new).

Brew's automation builder can deal a fixed percentage split across two email
designs. It cannot read the results back and redeploy the winner — winner
detection and holdout release aren't capabilities it has. This repo adds that
step: a script that reads per-variant analytics from Brew's API, decides a
winner under an explicit policy, and releases the winning design to a remainder
audience.

```
Brew builder:   trigger ──▶ 50/50 split ──▶ send A
                                        └─▶ send B          ← stops here

this repo:                                  ──▶ measure ──▶ decide ──▶ release
```

## Setup

Credentials come from the environment. Nothing is read from a file and nothing
is hardcoded.

```bash
export BREW_API_KEY=brew_...     # brew.new → API keys
export BREW_BRAND_ID=...         # only for an organization-scoped key
```

Python 3.9+, standard library only. No dependencies to install.

## Usage

```bash
python3 brew_closed_loop.py
```

That runs read-only through step 6 and prints the decision. Step 7 needs an
audience named explicitly, and dry-runs unless you pass `--send`:

```bash
# show every safety gate, send nothing
python3 brew_closed_loop.py --remainder-audience "Demo Remainder"

# actually dispatch
python3 brew_closed_loop.py --remainder-audience "Demo Remainder" --send
```

| Flag | Effect |
| --- | --- |
| `--count-mode unique\|events` | count distinct recipients (default) or raw event rows |
| `--min-combined N` | minimum combined clicks before any winner is declared |
| `--remainder-audience NAME\|ID` | the holdout audience for the winner |
| `--send` | dispatch for real; omit for a dry run |

## The decision policy

Three named constants at the top of the script, all overridable per run:

```python
PRIMARY_METRIC       = "clicks"   # decides outright
TIEBREAK_METRIC      = "opens"    # breaks a tie in the primary
MIN_COMBINED_PRIMARY = 3          # below this, refuse to declare a winner
COUNT_MODE           = "unique"   # "unique" recipients or raw "events"
```

Evaluated in order: more clicks wins; if clicks tie, more opens wins; if both
tie **or** combined clicks fall under the threshold, no winner is declared and
the script stops before sending anything.

### Why `COUNT_MODE` exists

Brew's send stats count *distinct recipients*, while the event stream counts
*rows*. In the verified run below, each variant logged four `clicked` events and
in both cases every one came from a single recipient — so `clickedCount` is 1,
not 4. The two readings can disagree about who won, so the script prints both
and the mode is explicit rather than assumed.

## Audience safety

Step 7 sends real email, so it is gated rather than trusted. Every one of these
must pass:

- the target audience is named explicitly — no default, no fallback, no guessing
- the name resolves to exactly one audience
- Brew reports a known size for it
- that size is under `MAX_SAFE_AUDIENCE_SIZE` (default 15)
- the name doesn't match a broad-list pattern (`all`, `everyone`, `club`, `members`)
- a verified **marketing** sending domain exists

A `confirmation_required` response stops the run and prints what needs
confirming. The script never retries with `confirmed: true` on its own.

The release replicates the winning *send*, not merely its design: subject,
preview text, sender, reply-to, and the pinned `emailVersionId` all carry over,
so the holdout receives exactly what won.

## Notes on Brew's REST API

The script calls Brew's REST API directly (`https://brew.new/api`, Bearer auth) —
the same surface the Brew MCP server wraps. Four things worth knowing, none of
which are obvious from the tool catalog:

- **`limit` caps at 100.** Every list endpoint rejects a higher value with
  `INVALID_REQUEST`. Overshooting yields an error envelope that is easy to
  misread as an empty result set.
- **`GET /v1/analytics/sends` is a lookup, not a list.** Filtered by `sendId` it
  returns the row. Its list mode omits automation sends entirely, even with an
  explicit date window, so there is no way to enumerate an automation's sends
  from it. Send discovery goes through `/v1/analytics/events` instead, where
  every event carries `sendId`, `emailId` and `nodeId`.
- **`/v1/automations/audience-runs` is lean over REST.** It returns `nodeStats`
  but no `nodesSnapshot`, so a split's branch labels can't be recovered there.
- **`POST /v1/sends` requires an explicit `subject`.** A design's own subject
  line is not inherited.

Also: `/v1/emails` rows carry no subject line, `/v1/audiences` names the
audience `audienceName` rather than `name`, and `include=count` is detail-only
on that route even though list rows already include `count`.

## Verified run

Automation `fA4CLMr0JxUg2RPlNffRa`, audience run `arun_My0NWHzKfesBcA6g1AkAG`:

| | Variant A | Variant B |
| --- | --- | --- |
| design | Welcome Community A | Welcome Curriculum B |
| delivered | 4 | 5 |
| opened, unique | 2 | **3** |
| clicked, unique | 1 | 1 |
| click events, raw | 4 | 4 |
| distinct clickers | 1 | 1 |

These sum exactly to Brew's own automation rollup — 9 delivered, 5 opened, 2
clicked — which the script asserts on every run.

At the default threshold of 3 this correctly declares **no winner**: clicks are
tied 1–1 and only 2 combined clicks exist. Under `--count-mode events` both
metrics tie 4–4, so it is no winner there too — the result doesn't depend on how
you count.

Run with `--min-combined 2`, the opens tiebreak gives it to Variant B (3 vs 2),
and the release went out as send `9yoPsg4LtGDfROq-hrXXs` to a four-contact
holdout, 4/4 delivered, 0 bounced.

That verdict rests on one open across nine delivered emails. The mechanism is
what this demonstrates; the verdict itself is a coin flip, and the script says
so in its own output.

## Files

| File | |
| --- | --- |
| `brew_closed_loop.py` | the seven-step pipeline |
| `closed_loop_canvas.html` | node-graph UI over the run, with a live decision policy |

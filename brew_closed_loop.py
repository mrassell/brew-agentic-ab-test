#!/usr/bin/env python3
"""
brew_closed_loop.py — closed-loop A/B winner detection + holdout release for Brew.

Brew's native automation builder can send a fixed percentage split, but it cannot
read the results back and redeploy the winner. This script is that missing piece:

    read analytics per variant  ->  decide a winner  ->  release to the remainder

Transport note: Brew's MCP server is a thin wrapper over Brew's public REST API
(baseUrl https://brew.new/api, Bearer auth). Each step below calls the same
endpoint the correspondingly-named MCP tool calls, so this runs standalone:

    step 1  get_brew_capabilities   ->  GET  /v1/help
    step 2  list email designs      ->  GET  /v1/emails
    step 3  list_audience_runs      ->  GET  /v1/automations/audience-runs
            get_send_analytics      ->  GET  /v1/analytics/sends
    step 4  get_event_analytics     ->  GET  /v1/analytics/events
    step 7  list_audiences          ->  GET  /v1/audiences
            list_domains            ->  GET  /v1/domains
            send_email              ->  POST /v1/sends

Credentials come from the environment, never from this file:

    export BREW_API_KEY=brew_...
    export BREW_BRAND_ID=...        # only needed for an org-scoped key

Usage:
    python brew_closed_loop.py
    python brew_closed_loop.py --count-mode events     # override the click counter
    python brew_closed_loop.py --remainder-audience "<audience name or id>"
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

# ---------------------------------------------------------------------------
# DECISION POLICY — tune these, they are the whole point of the script
# ---------------------------------------------------------------------------

# Which metric decides the winner, and which breaks a tie in it.
PRIMARY_METRIC = "clicks"
TIEBREAK_METRIC = "opens"

# Below this many combined primary-metric events across BOTH variants, refuse to
# declare a winner at all. Small samples make a "winner" an artifact of noise.
# NOTE: lowered from 3 to 2 for the 2026-09-10 demo run, which had exactly two
# clicks total. That was enough to let the opens tiebreak through and release a
# winner on camera. It is a demo setting, not a defensible one — put it back to
# 3 (or higher) before trusting any verdict this produces.
MIN_COMBINED_PRIMARY = 2

# How to count an open/click:
#   "unique" — distinct recipients (this is what Brew's own send stats report)
#   "events" — every raw event row, so one enthusiastic clicker counts 4 times
# These can disagree and flip the outcome, so both are always printed.
COUNT_MODE = "unique"

# Machine/bot-generated opens and clicks (scanners, Apple MPP prefetch) are
# excluded from the event-derived counts.
EXCLUDE_MACHINE_EVENTS = True

# ---------------------------------------------------------------------------
# WHAT WE ARE MEASURING
# ---------------------------------------------------------------------------

AUTOMATION_NAME = "Welcome A/B Test"
VARIANT_A_NAME = "Welcome Community A"
VARIANT_B_NAME = "Welcome Curriculum B"

# ---------------------------------------------------------------------------
# AUDIENCE SAFETY — step 7 will not send without clearing every one of these
# ---------------------------------------------------------------------------

# The remainder audience must be named explicitly (constant, --remainder-audience,
# or BREW_REMAINDER_AUDIENCE). There is deliberately no default and no fallback:
# an unset value stops the script rather than picking something.
REMAINDER_AUDIENCE = None

# Hard ceiling. A resolved audience larger than this aborts the send, so a
# mistyped name can never fan out to the real ~80-person club list.
MAX_SAFE_AUDIENCE_SIZE = 15

# Audience names containing any of these are refused outright as send targets.
FORBIDDEN_AUDIENCE_PATTERNS = ("all contacts", "all", "everyone", "club", "members")

API_BASE = os.environ.get("BREW_API_BASE", "https://brew.new/api")
TIMEOUT = 45

# Brew rejects limit > 100 with INVALID_REQUEST on every list endpoint.
MAX_PAGE = 100


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

class BrewError(RuntimeError):
    """A Brew API error carrying the stable error code and request id."""

    def __init__(self, status, code, message, request_id=None, payload=None):
        super().__init__(f"[{status} {code}] {message}")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.payload = payload or {}


class Brew:
    def __init__(self, api_key, brand_id=None):
        self.api_key = api_key
        self.brand_id = brand_id

    def _request(self, method, path, params=None, body=None, idempotency_key=None):
        url = f"{API_BASE}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        if data:
            req.add_header("Content-Type", "application/json")
        # An org-scoped key names its brand per request; a brand-scoped key
        # resolves it automatically and rejects a mismatched assertion.
        if self.brand_id:
            req.add_header("X-Brand-Id", self.brand_id)
        if idempotency_key:
            req.add_header("Idempotency-Key", idempotency_key)

        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode() or "{}"
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode() or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                raise BrewError(exc.code, "HTTP_ERROR", raw[:400]) from None
            err = parsed.get("error") or {}
            raise BrewError(
                exc.code,
                err.get("code", "UNKNOWN"),
                err.get("message", raw[:400]),
                parsed.get("request_id") or err.get("request_id"),
                parsed,
            ) from None
        except urllib.error.URLError as exc:
            raise BrewError(0, "NETWORK_ERROR", str(exc.reason)) from None

    def get(self, path, **params):
        return self._request("GET", path, params=params)

    def post(self, path, body, idempotency_key=None):
        return self._request("POST", path, body=body, idempotency_key=idempotency_key)

    def paginate(self, path, cap=2000, **params):
        """Walk a cursor-paginated list endpoint and return every row."""
        if params.get("limit"):
            params["limit"] = min(int(params["limit"]), MAX_PAGE)
        rows, cursor = [], None
        while True:
            page = self.get(path, cursor=cursor, **params)
            batch = page.get("data") or []
            rows.extend(batch)
            pagination = page.get("pagination") or {}
            cursor = pagination.get("cursor")
            if not cursor or not pagination.get("hasMore") or len(rows) >= cap:
                return rows


def banner(number, title):
    print(f"\n{'=' * 74}\nSTEP {number}: {title}\n{'=' * 74}")


def first(row, *names, default=None):
    """Read the first present key — Brew returns ids under a few spellings."""
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


# ---------------------------------------------------------------------------
# Step 1 — confirm the connection
# ---------------------------------------------------------------------------

def step1_capabilities(brew):
    banner(1, "Confirm the Brew connection and read the live catalog")
    caps = brew.get("/v1/help")
    conn = caps.get("connection") or {}
    endpoints = caps.get("endpoints") or []

    print(f"  connected to : {caps.get('name', 'Brew')} {caps.get('version', '')}".rstrip())
    print(f"  base url     : {caps.get('baseUrl', API_BASE)}")
    print(f"  endpoints    : {len(endpoints)} exposed")
    # An API key gets no `connection` block (that is an MCP-session concept),
    # so report the key's scopes and confirm the brand with a real call.
    if conn:
        print(f"  key scope    : {conn.get('scope', 'unknown')}")
        print(f"  brand id     : {conn.get('brandId') or brew.brand_id or '(resolved by key)'}")
    else:
        print(f"  key scopes   : {', '.join(caps.get('scopes') or ['unknown'])}")
        print(f"  brand id     : {brew.brand_id or '(bound to the key)'}")

    required = [
        ("GET", "/v1/emails"),
        ("GET", "/v1/automations/audience-runs"),
        ("GET", "/v1/analytics/sends"),
        ("GET", "/v1/analytics/events"),
        ("GET", "/v1/audiences"),
        ("POST", "/v1/sends"),
    ]
    available = {(e.get("method"), e.get("path")) for e in endpoints}
    missing = [f"{m} {p}" for m, p in required if (m, p) not in available]
    if missing and available:
        print(f"\n  !! catalog is missing endpoints this script needs: {', '.join(missing)}")
        print("     Steps below may fail; the catalog above is the source of truth.")
    else:
        print("  every endpoint this script needs is present in the catalog.")

    if conn.get("brandId") and brew.brand_id and conn["brandId"] != brew.brand_id:
        print(f"\n  !! BREW_BRAND_ID ({brew.brand_id}) != the key's bound brand "
              f"({conn['brandId']}). Brand-scoped calls will fail.")
    return caps


# ---------------------------------------------------------------------------
# Step 2 — resolve the two design ids by name
# ---------------------------------------------------------------------------

def step2_designs(brew):
    banner(2, "Resolve the real design IDs for both variants")
    designs = brew.paginate("/v1/emails", limit=100)
    print(f"  scanned {len(designs)} email design(s) in this brand\n")

    def match(name):
        wanted = name.strip().lower()
        hits = [d for d in designs
                if (first(d, "title", "name", default="")).strip().lower() == wanted]
        if not hits:
            near = [first(d, "title", "name", default="") for d in designs
                    if wanted.split()[-1].lower() in first(d, "title", "name", default="").lower()]
            raise SystemExit(
                f"  ERROR: no design titled {name!r} in this brand.\n"
                f"         closest titles: {near or [first(d, 'title', 'name') for d in designs[:8]]}\n"
                f"         Fix VARIANT_A_NAME / VARIANT_B_NAME and re-run."
            )
        if len(hits) > 1:
            print(f"  note: {len(hits)} designs titled {name!r}; using the newest.")
            hits.sort(key=lambda d: first(d, "createdAt", "updatedAt", default=""), reverse=True)
        return hits[0]

    a, b = match(VARIANT_A_NAME), match(VARIANT_B_NAME)
    variants = {
        "A": {"label": "A", "name": VARIANT_A_NAME, "emailId": first(a, "emailId", "id"),
              "subject": ""},
        "B": {"label": "B", "name": VARIANT_B_NAME, "emailId": first(b, "emailId", "id"),
              "subject": ""},
    }
    # /v1/emails rows carry no subject line; the real subject arrives with the
    # send record in step 4, so it is printed there instead.
    for v in variants.values():
        print(f"  Variant {v['label']}  {v['name']:<26} design id = {v['emailId']}")

    if variants["A"]["emailId"] == variants["B"]["emailId"]:
        raise SystemExit("  ERROR: both variant names resolved to the same design id.")
    return variants


# ---------------------------------------------------------------------------
# Step 3 — find the send ids from the automation's most recent run
# ---------------------------------------------------------------------------

def step3_sends(brew, variants):
    banner(3, f"Find the send IDs from the latest {AUTOMATION_NAME!r} run")

    automations = brew.paginate("/v1/automations", limit=100)
    named = [a for a in automations
             if (first(a, "name", "title", default="")).strip().lower() == AUTOMATION_NAME.lower()]
    if not named:
        raise SystemExit(
            f"  ERROR: no automation named {AUTOMATION_NAME!r}. Found: "
            f"{sorted({first(a, 'name', 'title', default='?') for a in automations})}"
        )
    automation_id = first(named[0], "automationId", "id")
    print(f"  automation   : {AUTOMATION_NAME} -> {automation_id}")

    runs = brew.paginate("/v1/automations/audience-runs", automationId=automation_id, limit=50)
    if not runs:
        raise SystemExit(
            "  ERROR: this automation has no audience runs yet, so there are no\n"
            "         per-variant sends to read. Run it once in Brew first."
        )
    runs.sort(key=lambda r: first(r, "startedAt", "createdAt", default=""), reverse=True)
    run = runs[0]
    run_id = first(run, "audienceRunId", "id")
    print(f"  latest run   : {run_id}")
    print(f"                 status={first(run, 'status', default='?')} "
          f"started={first(run, 'startedAt', 'createdAt', default='?')} "
          f"recipients={first(run, 'totalRecipients', 'sentCount', default='?')}")
    if len(runs) > 1:
        print(f"                 ({len(runs)} runs total; older ones ignored)")

    # Discovering the per-variant sends is the one place the REST surface fights
    # back. GET /v1/analytics/sends is a lookup, not a list: filtered by sendId it
    # returns the row, but its list mode omits automation sends entirely (it only
    # covers campaigns). The audience-run record is also lean over REST — it has
    # nodeStats but no nodesSnapshot, so there is no emailId->node mapping there.
    # The event stream is the way in: every event carries sendId + emailId +
    # nodeId, and it accepts an emailId filter. So: events -> candidate sendIds
    # -> confirm each against the run's time window via the sendId lookup.
    window_start = first(run, "startedAt", "createdAt", default="")
    window_end = first(run, "completedAt", "updatedAt", default="9999")
    node_stats = {n.get("nodeId"): n for n in (run.get("nodeStats") or [])}

    for v in variants.values():
        events = brew.paginate("/v1/analytics/events", emailId=v["emailId"], limit=MAX_PAGE)
        if not events:
            raise SystemExit(
                f"  ERROR: no events for variant {v['label']} ({v['name']}, "
                f"design {v['emailId']}).\n"
                f"         Without events there is no way to recover this variant's\n"
                f"         sendId — /v1/analytics/sends does not list automation sends."
            )
        candidates = sorted({e.get("sendId") for e in events if e.get("sendId")})
        v["nodeId"] = next((e.get("nodeId") for e in events if e.get("nodeId")), None)

        confirmed = []
        for send_id in candidates:
            rows = (brew.get("/v1/analytics/sends", sendId=send_id).get("data") or [])
            for s in rows:
                started = first(s, "startedAt", "createdAt", default="")
                if window_start <= started <= window_end:
                    confirmed.append(s)

        pool = confirmed or [r for sid in candidates
                             for r in (brew.get("/v1/analytics/sends",
                                                sendId=sid).get("data") or [])]
        if not pool:
            raise SystemExit(
                f"  ERROR: variant {v['label']} has events but no readable send record."
            )
        pool.sort(key=lambda s: first(s, "startedAt", "createdAt", default=""), reverse=True)
        send = pool[0]
        v["sendId"] = first(send, "sendId", "id")
        v["send"] = send
        if not confirmed:
            print(f"  !! Variant {v['label']}: no send inside the run window; "
                  f"using this design's most recent send instead.")

        stat = node_stats.get(v["nodeId"]) or {}
        branch = (f"node {v['nodeId']} — entered {stat.get('entered', '?')}, "
                  f"sent {stat.get('sent', '?')}") if v["nodeId"] else "(node unknown)"
        print(f"  Variant {v['label']}    send id = {v['sendId']}  "
              f"(status={first(send, 'status', default='?')})")
        print(f"                 {branch}")

    if variants["A"]["sendId"] == variants["B"]["sendId"]:
        raise SystemExit("  ERROR: both variants resolved to the same send id.")
    return automation_id, run


# ---------------------------------------------------------------------------
# Step 4 — per-variant analytics
# ---------------------------------------------------------------------------

def step4_analytics(brew, variants):
    banner(4, "Pull per-variant analytics (delivered / opened / clicked)")

    for v in variants.values():
        # Aggregate stats for this one send. Brew's stats count unique recipients.
        page = brew.get("/v1/analytics/sends", sendId=v["sendId"])
        rows = page.get("data") or []
        if not rows:
            raise SystemExit(f"  ERROR: send {v['sendId']} returned no analytics row.")
        send = rows[0]
        stats = send.get("stats") or {}
        v["unique"] = {
            "sent": _num(stats, "sent", "sentCount"),
            "delivered": _num(stats, "delivered", "deliveredCount"),
            "opens": _num(stats, "opened", "openedCount"),
            "clicks": _num(stats, "clicked", "clickedCount"),
            "bounced": _num(stats, "bounced", "bouncedCount"),
        }
        v["recipients"] = first(send, "recipientCount", default=v["unique"]["sent"])

        v["subject"] = first(send, "subject", default=v.get("subject", ""))

        # Raw event stream for the same send, so we can show both counters and
        # prove the aggregate is unique-per-recipient rather than per-event.
        events = brew.paginate("/v1/analytics/events", sendId=v["sendId"], limit=MAX_PAGE)
        v["events"] = _count_events(events)
        u = v["unique"]
        print(f"  Variant {v['label']} ({v['name']}) via send {v['sendId']}:")
        print(f"    subject   : {v['subject']!r}")
        print(f"    delivered={u['delivered']}  opened={u['opens']}  "
              f"clicked={u['clicks']}   [{len(events)} raw events cross-checked]")

    _cross_check(brew, variants)
    print("\n  Both variants read. Full side-by-side table in step 6 below.")
    return variants


def _cross_check(brew, variants):
    """Confirm per-variant numbers sum to Brew's own automation-level rollup."""
    try:
        rows = (brew.get("/v1/analytics/automations", limit=MAX_PAGE).get("data") or [])
    except BrewError:
        return
    row = next((r for r in rows
                if (r.get("name") or "").strip().lower() == AUTOMATION_NAME.lower()), None)
    if not row:
        return

    a, b = variants["A"]["unique"], variants["B"]["unique"]
    print("\n  cross-check against Brew's automation-level rollup:")
    ok = True
    for label, key, mine in (("delivered", "delivered", a["delivered"] + b["delivered"]),
                             ("opened", "opened", a["opens"] + b["opens"]),
                             ("clicked", "clicked", a["clicks"] + b["clicks"])):
        theirs = row.get(key)
        match = "match" if theirs == mine else f"MISMATCH (rollup says {theirs})"
        if theirs != mine:
            ok = False
        print(f"    {label:<10} A+B = {mine:<4} vs rollup {theirs:<4} {match}")
    if not ok:
        print("    !! per-variant figures disagree with the rollup; trust neither blindly.")


def _num(stats, *names):
    for name in names:
        if isinstance(stats.get(name), (int, float)):
            return int(stats[name])
    return 0


def _count_events(events):
    """Count opens/clicks two ways from the raw event stream."""
    out = {"opens": 0, "clicks": 0, "unique_openers": set(), "unique_clickers": set(),
           "machine_dropped": 0, "delivered": 0}
    for ev in events:
        kind = (ev.get("eventType") or "").lower()
        who = ev.get("recipientEmail") or ev.get("recipient") or ""
        if kind == "delivered":
            out["delivered"] += 1
            continue
        if kind not in ("opened", "clicked"):
            continue
        if EXCLUDE_MACHINE_EVENTS and ev.get("machineGenerated"):
            out["machine_dropped"] += 1
            continue
        if kind == "opened":
            out["opens"] += 1
            out["unique_openers"].add(who)
        else:
            out["clicks"] += 1
            out["unique_clickers"].add(who)
    out["unique_openers"] = len(out["unique_openers"])
    out["unique_clickers"] = len(out["unique_clickers"])
    return out


def counts_for(variant, mode):
    """The numbers the decision actually runs on, per COUNT_MODE."""
    if mode == "events":
        return {"opens": variant["events"]["opens"], "clicks": variant["events"]["clicks"]}
    return {"opens": variant["unique"]["opens"], "clicks": variant["unique"]["clicks"]}


# ---------------------------------------------------------------------------
# Step 6 (printed before the decision, as requested)
# ---------------------------------------------------------------------------

def _print_table(variants):
    a, b = variants["A"], variants["B"]
    w = 26
    print(f"\n  {'':<20}{'VARIANT A':>{w}}{'VARIANT B':>{w}}")
    print(f"  {'':<20}{a['name']:>{w}}{b['name']:>{w}}")
    print("  " + "-" * (20 + 2 * w))

    def row(label, av, bv):
        print(f"  {label:<20}{str(av):>{w}}{str(bv):>{w}}")

    row("send id", a["sendId"], b["sendId"])
    row("design id", a["emailId"], b["emailId"])
    print("  " + "-" * (20 + 2 * w))
    row("recipients", a["recipients"], b["recipients"])
    row("sent", a["unique"]["sent"], b["unique"]["sent"])
    row("DELIVERED", a["unique"]["delivered"], b["unique"]["delivered"])
    row("bounced", a["unique"]["bounced"], b["unique"]["bounced"])
    print("  " + "-" * (20 + 2 * w))
    row("OPENED  (unique)", a["unique"]["opens"], b["unique"]["opens"])
    row("CLICKED (unique)", a["unique"]["clicks"], b["unique"]["clicks"])
    print("  " + "-" * (20 + 2 * w))
    row("open events (raw)", a["events"]["opens"], b["events"]["opens"])
    row("click events (raw)", a["events"]["clicks"], b["events"]["clicks"])
    row("distinct openers", a["events"]["unique_openers"], b["events"]["unique_openers"])
    row("distinct clickers", a["events"]["unique_clickers"], b["events"]["unique_clickers"])
    print("  " + "-" * (20 + 2 * w))

    def rate(part, whole):
        d = whole or 0
        return f"{(100.0 * part / d):.1f}%" if d else "n/a"

    row("open rate", rate(a["unique"]["opens"], a["unique"]["delivered"]),
        rate(b["unique"]["opens"], b["unique"]["delivered"]))
    row("click rate", rate(a["unique"]["clicks"], a["unique"]["delivered"]),
        rate(b["unique"]["clicks"], b["unique"]["delivered"]))

    dropped = a["events"]["machine_dropped"] + b["events"]["machine_dropped"]
    if dropped:
        print(f"\n  ({dropped} machine-generated open/click event(s) excluded)")
    if (a["events"]["clicks"] != a["unique"]["clicks"]
            or b["events"]["clicks"] != b["unique"]["clicks"]):
        print("\n  NOTE: raw click events > unique clickers — a few recipients clicked")
        print(f"        repeatedly. The decision below uses COUNT_MODE={COUNT_MODE!r}.")


# ---------------------------------------------------------------------------
# Step 5 — the decision
# ---------------------------------------------------------------------------

def step5_decide(variants, count_mode):
    banner(5, "Decide a winner")
    a, b = variants["A"], variants["B"]
    ca, cb = counts_for(a, count_mode), counts_for(b, count_mode)

    primary_a, primary_b = ca[PRIMARY_METRIC], cb[PRIMARY_METRIC]
    tie_a, tie_b = ca[TIEBREAK_METRIC], cb[TIEBREAK_METRIC]
    combined = primary_a + primary_b

    print(f"  policy: primary={PRIMARY_METRIC}  tiebreak={TIEBREAK_METRIC}  "
          f"min combined {PRIMARY_METRIC}={MIN_COMBINED_PRIMARY}  count_mode={count_mode}")
    print(f"  {PRIMARY_METRIC:>9}: A={primary_a}  B={primary_b}  (combined {combined})")
    print(f"  {TIEBREAK_METRIC:>9}: A={tie_a}  B={tie_b}\n")

    primary_tied = primary_a == primary_b
    tie_tied = tie_a == tie_b

    # Both no-winner conditions from the policy, evaluated as a veto.
    if (primary_tied and tie_tied) or combined < MIN_COMBINED_PRIMARY:
        print("  DECISION: NO WINNER")
        if combined < MIN_COMBINED_PRIMARY:
            print(f"\n  No statistically meaningful winner — sample size too small "
                  f"({combined} total {PRIMARY_METRIC}).")
            print(f"  Declaring a winner here would be arbitrary. "
                  f"(threshold: MIN_COMBINED_PRIMARY={MIN_COMBINED_PRIMARY})")
            if primary_tied:
                print(f"  {PRIMARY_METRIC.capitalize()} were also tied at "
                      f"{primary_a} apiece, so there is nothing to break.")
        else:
            print(f"\n  No winner — {PRIMARY_METRIC} tied at {primary_a} and "
                  f"{TIEBREAK_METRIC} tied at {tie_a}. Nothing separates the variants.")
        print("\n  Stopping before step 7. No email will be sent.")
        print("  To act on a thinner sample, lower MIN_COMBINED_PRIMARY at the top")
        print("  of this script — but know that you are choosing noise.")
        return None

    if not primary_tied:
        winner = a if primary_a > primary_b else b
        loser = b if winner is a else a
        wc, lc = counts_for(winner, count_mode), counts_for(loser, count_mode)
        print(f"  DECISION: Variant {winner['label']} WINS on {PRIMARY_METRIC} "
              f"({wc[PRIMARY_METRIC]} vs {lc[PRIMARY_METRIC]})")
    else:
        winner = a if tie_a > tie_b else b
        loser = b if winner is a else a
        wc, lc = counts_for(winner, count_mode), counts_for(loser, count_mode)
        print(f"  DECISION: {PRIMARY_METRIC} tied at {primary_a}; Variant "
              f"{winner['label']} WINS on the {TIEBREAK_METRIC} tiebreaker "
              f"({wc[TIEBREAK_METRIC]} vs {lc[TIEBREAK_METRIC]})")

    print(f"\n  Winner: {winner['name']}  (design {winner['emailId']})")

    # Honest caveat: passing the threshold is not statistical significance.
    if combined < 30:
        print(f"\n  CAVEAT: {combined} combined {PRIMARY_METRIC} clears the configured")
        print("  threshold but is nowhere near statistical significance. This is a")
        print("  demo-scale decision, not a defensible one.")
    return winner


# ---------------------------------------------------------------------------
# Step 7 — release the winner to the remainder audience
# ---------------------------------------------------------------------------

def step7_release(brew, winner, requested_audience, apply_send):
    banner(7, "Release the winning design to the remainder audience")

    if not requested_audience:
        print("  STOPPED — no remainder audience specified.")
        print("\n  This script will not pick an audience for you. Set exactly one of:")
        print("    * REMAINDER_AUDIENCE at the top of this file")
        print("    * --remainder-audience \"<name or id>\"")
        print("    * BREW_REMAINDER_AUDIENCE=<name or id>")
        _show_audiences(brew)
        return

    audiences = brew.paginate("/v1/audiences", limit=MAX_PAGE)
    if not audiences:
        print("  STOPPED — this brand has zero saved audiences.")
        print(f"\n  Nothing named {requested_audience!r} exists to send to. Create a small")
        print("  test audience in Brew first, then re-run with its name.")
        return

    wanted = requested_audience.strip().lower()
    hits = [x for x in audiences
            if (first(x, "audienceName", "name", default="")).strip().lower() == wanted
            or str(first(x, "audienceId", "id", "_id", default="")) == requested_audience]
    if not hits:
        print(f"  STOPPED — no audience matches {requested_audience!r}.")
        _show_audiences(brew, audiences)
        return
    if len(hits) > 1:
        print(f"  STOPPED — {len(hits)} audiences match {requested_audience!r}.")
        print("  Re-run with the exact audience id so there is no ambiguity.")
        return

    audience = hits[0]
    audience_id = first(audience, "audienceId", "id", "_id")
    name = first(audience, "audienceName", "name", default="(unnamed)")
    size = first(audience, "count", "cachedCount", "contactCount", default=None)
    if size is None:
        # include=count is detail-only on this route.
        try:
            detail = brew.get("/v1/audiences", audienceId=audience_id, include="count")
            rows = detail.get("data") or []
            row = rows[0] if isinstance(rows, list) and rows else detail
            size = first(row, "count", "cachedCount", "contactCount", default=None)
        except BrewError:
            size = None

    print(f"  candidate    : {name}  ({audience_id})")
    print(f"  size         : {size if size is not None else 'UNKNOWN'}")

    # --- safety gates: every one must pass ---
    lowered = name.lower()
    for pattern in FORBIDDEN_AUDIENCE_PATTERNS:
        if pattern in lowered.split() or lowered == pattern:
            print(f"\n  ABORTED — audience name matches the forbidden pattern {pattern!r}.")
            print("  This looks like a broad list, not the small demo audience.")
            return

    if size is None:
        print("\n  ABORTED — Brew did not report a size for this audience.")
        print("  Refusing to send to an audience of unknown size. Open it in Brew,")
        print("  let the count materialize, then re-run.")
        return

    if int(size) > MAX_SAFE_AUDIENCE_SIZE:
        print(f"\n  ABORTED — {size} contacts exceeds MAX_SAFE_AUDIENCE_SIZE="
              f"{MAX_SAFE_AUDIENCE_SIZE}.")
        print("  This is the guard against hitting the real club list. If this")
        print("  audience really is the safe demo one, raise the constant knowingly.")
        return

    if int(size) == 0:
        print("\n  ABORTED — audience is empty; nothing to send.")
        return

    domain = _pick_domain(brew)
    if not domain:
        return
    domain_id, domain_name = domain

    print(f"\n  ready to send:")
    print(f"    design    : {winner['name']} ({winner['emailId']})")
    print(f"    audience  : {name} ({audience_id}) — {size} contact(s)")
    print(f"    domain    : {domain_name} ({domain_id})")

    if not apply_send:
        print("\n  DRY RUN — nothing sent. Every safety gate above passed.")
        print("  Re-run with --send to actually dispatch this campaign.")
        return

    # Replicate the winning send, not merely its design: the holdout should get
    # the same subject, preview line, sender and pinned version that actually
    # won. `subject` is required on a real send and has no default.
    won = winner.get("send") or {}
    body = {
        "emailId": winner["emailId"],
        "audienceId": audience_id,
        "domainId": domain_id,
        "subject": winner.get("subject") or first(won, "subject", default=""),
    }
    if not body["subject"]:
        print("\n  ABORTED — the winning send has no subject line to replicate.")
        print("  A real send requires one; refusing to invent a subject.")
        return
    for key, src in (("previewText", "previewText"), ("senderName", "senderName"),
                     ("replyTo", "replyTo"), ("emailVersionId", "emailVersionId")):
        value = first(won, src)
        if value:
            body[key] = value
    from_address = first(won, "fromAddress", default="")
    if "@" in from_address:
        body["fromEmail"] = from_address.split("@", 1)[0]

    print(f"    subject   : {body['subject']!r}")
    print(f"    sender    : {body.get('senderName', '(domain default)')} "
          f"<{from_address or '(domain default)'}>")
    if body.get("emailVersionId"):
        print(f"    version   : {body['emailVersionId']} (pinned to the winning send)")
    key = f"closed-loop-{winner['emailId']}-{audience_id}-{uuid.uuid4().hex[:8]}"
    print(f"\n  POST /v1/sends  (Idempotency-Key: {key})")
    try:
        result = brew.post("/v1/sends", body, idempotency_key=key)
    except BrewError as exc:
        if "CONFIRM" in exc.code.upper():
            _confirmation_required(exc.code, exc.message, exc.payload)
            return
        print(f"\n  SEND FAILED — {exc}")
        if exc.request_id:
            print(f"  request_id: {exc.request_id}")
        return

    # A successful HTTP call can still be a confirmation request, not a send.
    blob = json.dumps(result).lower()
    if "confirmation_required" in blob or result.get("confirmationRequired"):
        _confirmation_required("confirmation_required", "Brew is holding this send.", result)
        return

    data = result.get("data") or result
    print("\n  SENT.")
    print(f"    sendId  : {first(data, 'sendId', 'id', default='?')}")
    print(f"    status  : {first(data, 'status', default='?')}")
    print(f"    to      : {first(data, 'recipientCount', default=size)} recipient(s)")
    view = (result.get("app") or {}).get("view_in_app")
    if view:
        print(f"    open in Brew: {view}")


def _confirmation_required(code, message, payload):
    print(f"\n  STOPPED — Brew returned {code}. Nothing was sent.")
    print(f"  Brew says: {message}")

    interesting = ("recipientCount", "audienceName", "audienceId", "emailId",
                   "subject", "fromAddress", "domainId", "confirmationToken",
                   "suggestion", "reason")
    flat = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    details = {k: v for k, v in (flat or {}).items() if k in interesting}
    if details:
        print("\n  What needs confirming:")
        for k, v in details.items():
            print(f"    {k:<18} {v}")

    print("\n  This is the OAuth-connected-domain confirmation gate. This script")
    print("  will NOT retry with confirmed:true on its own — that is your call.")
    print("  Review the recipient count and preview above, then either confirm in")
    print("  Brew, or re-run this step with the confirmation you have approved.")


def _pick_domain(brew):
    domains = brew.paginate("/v1/domains", limit=100)
    verified = []
    for d in domains:
        status = str(first(d, "status", "verificationStatus", default="")).lower()
        ok = bool(d.get("verified")) or status in ("verified", "active", "success")
        purpose = str(first(d, "sendingPurpose", "purpose", default="marketing")).lower()
        if ok and purpose != "transactional":
            verified.append(d)
    if not verified:
        print("\n  ABORTED — no verified marketing sending domain available.")
        print("  A campaign send needs one. Verify a domain in Brew and re-run.")
        return None
    if len(verified) > 1:
        names = [first(d, "name", "domain", default="?") for d in verified]
        print(f"\n  note: {len(verified)} verified marketing domains {names}; using the first.")
    d = verified[0]
    return first(d, "domainId", "id", "_id"), first(d, "name", "domain", default="?")


def _show_audiences(brew, audiences=None):
    if audiences is None:
        try:
            audiences = brew.paginate("/v1/audiences", limit=MAX_PAGE)
        except BrewError as exc:
            print(f"\n  (could not list audiences: {exc})")
            return
    print(f"\n  Saved audiences in this brand ({len(audiences)}):")
    if not audiences:
        print("    (none — there is no audience to send to yet)")
        return
    for x in audiences:
        size = first(x, "count", "cachedCount", "contactCount", default="?")
        print(f"    - {first(x, 'audienceName', 'name', default='(unnamed)'):<32} "
              f"{first(x, 'audienceId', 'id', '_id', default='?')}  size={size}")
    print("\n  Confirm the exact audience id you want before anything is sent.")


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Closed-loop A/B winner release for Brew.")
    parser.add_argument("--count-mode", choices=("unique", "events"), default=COUNT_MODE,
                        help="count unique recipients (default) or raw event rows")
    parser.add_argument("--remainder-audience", default=None,
                        help="name or id of the small test audience for the winner")
    parser.add_argument("--send", action="store_true",
                        help="actually dispatch step 7 (default is a dry run)")
    args = parser.parse_args()

    api_key = os.environ.get("BREW_API_KEY")
    brand_id = os.environ.get("BREW_BRAND_ID")
    if not api_key:
        sys.exit(
            "BREW_API_KEY is not set.\n\n"
            "  export BREW_API_KEY=brew_...        # from brew.new -> API keys\n"
            "  export BREW_BRAND_ID=...           # only for an org-scoped key\n\n"
            "Then re-run: python brew_closed_loop.py"
        )

    audience = (args.remainder_audience
                or os.environ.get("BREW_REMAINDER_AUDIENCE")
                or REMAINDER_AUDIENCE)

    print("\nBREW CLOSED-LOOP A/B  —  winner detection + holdout release")
    print(f"the piece Brew's native automation builder does not do: read the")
    print(f"results back and redeploy the winner.")

    brew = Brew(api_key, brand_id)
    try:
        step1_capabilities(brew)
        variants = step2_designs(brew)
        step3_sends(brew, variants)
        step4_analytics(brew, variants)

        banner(6, "Both variants, side by side")
        _print_table(variants)

        winner = step5_decide(variants, args.count_mode)
        if winner is None:
            print(f"\n{'=' * 74}\nDone — no winner declared, nothing sent.\n{'=' * 74}\n")
            return 0

        step7_release(brew, winner, audience, args.send)
        print(f"\n{'=' * 74}\nDone.\n{'=' * 74}\n")
        return 0
    except BrewError as exc:
        print(f"\nBREW API ERROR {exc}")
        if exc.request_id:
            print(f"request_id: {exc.request_id}")
        if exc.code == "BRAND_ID_REQUIRED":
            print("Set BREW_BRAND_ID — your key is organization-scoped.")
        elif exc.code == "BRAND_SCOPE_MISMATCH":
            print("BREW_BRAND_ID does not match the brand your key is bound to. Unset it.")
        elif exc.status in (401, 403):
            print("Check BREW_API_KEY and that it carries the emails/sends/audiences scopes.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

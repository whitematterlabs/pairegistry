---
name: setup-watcher
visible_to: [root]
description: Wire up a website watcher — a cheap cron-fired poller that fetches a URL, diffs against last-seen, and wakes the owner only when a condition fires (new listing, price drop, page change). Use when the owner asks to "watch", "notify me when", "let me know if", or "keep an eye on" some web page or feed.
---

# Setting up a watcher

The owner wants to know when something changes out on the web — a new
Craigslist listing, a stock dropping 5%, a page going back in stock, a
ticket count changing. There are no webhooks for most of these. Someone
has to check. The trick is that **the someone is a cheap subprocess, not
you.** You wake only when the condition actually fires.

You wire each watcher by hand, once, tailored to the site. This skill is
the pattern, not an engine. There is nothing to install — every piece
already exists (`paicron`, the event bus, `/var/lib`).

## How it works

```
cron fires every N min  →  runs poll.sh   (you are asleep, announce:false)
                           poll.sh: fetch → extract → diff vs last-seen
                           no change  →  exit 0, silent. You never woke.
                           HIT        →  drop ONE event file → you wake
```

A `paicron` job with **both** `schedule:` and `run:` runs the command as a
cheap unsupervised subprocess each tick — it does **not** nudge you. (A
job with `schedule:` and *no* `run:` is a reminder and *would* nudge you;
that is the wrong shape here.) Set `announce: false` so routine polls stay
fully silent. The only wake is `poll.sh` dropping an event onto the bus
when its condition is met.

The poll subprocess inherits the kernel's environment: `$PAI_ROOT` is set,
but `$PAI_PID` is **not** — so `poll.sh` cannot use `send-message`. It
wakes you by writing an event file directly (shown below).

## Recipe

### 1. Inspect the site once — find the cheapest signal

Use `browse` / WebFetch to look at the target before writing anything.
Hunt, in order of preference:

1. **A JSON or RSS endpoint.** Open devtools/network or guess (`?format=rss`,
   `/api/…`, `.json`). A feed is stable, tiny, and trivial to parse — vastly
   better than scraping HTML. Craigslist search results have RSS; most
   finance pages have a JSON quote endpoint.
2. **A stable HTML hook** — an element id/class, or a regex over the raw
   body — if no feed exists.

Decide the **predicate**:
- *new-item* — newest id/link/title differs from last-seen (listings, posts).
- *threshold* — extracted number crosses a bound (price < X, drop ≥ 5%).
- *changed* — a hash of the extracted region differs (in-stock, status).

Pick a `slug` (kebab, stable, descriptive): `sf-craigslist-bikes`,
`aapl-drop-5pct`.

### 2. Write `poll.sh`

Store it under the watcher's own state dir so script + state live together:
`$PAI_ROOT/var/lib/<slug>/poll.sh`. The script creates that dir, fetches,
extracts, compares against `last-seen`, and on a hit drops an event. It
**skips the first run** (establishes a baseline silently) so you aren't
woken just for turning it on.

The event drop is the whole wake mechanism — an atomic write into the bus
directory. `source:` must be your slug (not `kernel`), `kind:` a bare word;
`target_pid: 1` addresses root directly. Atomicity = write a dotfile in the
same dir, then `mv` (same-filesystem rename is atomic; the kernel's watcher
must never see a half-written file).

```sh
#!/bin/sh
# poll.sh — <slug>: <one line of what it watches>
set -eu
SLUG=sf-craigslist-bikes
STATE="$PAI_ROOT/var/lib/$SLUG"
EVENTS="$PAI_ROOT/run/pai/events"
mkdir -p "$STATE"
SEEN="$STATE/last-seen"

URL='https://sfbay.craigslist.org/search/bik?format=rss'

# --- extract: the newest item link from the RSS feed -----------------
new=$(curl -fsSL "$URL" | grep -o '<link>[^<]*</link>' | sed -n '2p') || {
  # extraction failed → structure changed. Wake yourself to re-author.
  emit() { :; }   # fall through to the broken-event drop below
  new=''
}

# --- broken: feed/selector returned nothing --------------------------
if [ -z "$new" ]; then
  ts=$(date +%Y%m%dT%H%M%S%N)
  printf 'source: %s\nkind: broken\ntarget_pid: 1\nnote: extraction returned empty; re-author poll.sh\n' "$SLUG" \
    > "$EVENTS/.$ts.tmp"
  mv "$EVENTS/.$ts.tmp" "$EVENTS/$ts-$SLUG.yaml"
  exit 0
fi

old=$(cat "$SEEN" 2>/dev/null || true)
printf '%s' "$new" > "$SEEN"          # always update baseline

# --- first run: baseline only, stay silent ---------------------------
[ -z "$old" ] && exit 0
# --- predicate: new-item ---------------------------------------------
[ "$new" = "$old" ] && exit 0          # nothing new

# --- HIT → wake root -------------------------------------------------
ts=$(date +%Y%m%dT%H%M%S%N)
printf 'source: %s\nkind: new_listing\ntarget_pid: 1\nurl: %s\n' "$SLUG" "$new" \
  > "$EVENTS/.$ts.tmp"
mv "$EVENTS/.$ts.tmp" "$EVENTS/$ts-$SLUG.yaml"
```

`chmod +x` it. For a *threshold* predicate, swap the predicate block, e.g.
fetch a JSON quote, extract the price with a JSON tool, and compare:
`awk "BEGIN{exit !($price <= $floor)}" && drop_event`. Keep the baseline
write so you don't re-fire every tick once tripped — store the last fired
value and only re-fire when it recovers and crosses again, if the owner
wants edge-triggered.

### 3. Register it with paicron (`announce: false` via a spec file)

`announce: false` has no flag, so write a spec file and use `ensure`
(idempotent, stable slug — no date suffix, so re-running this skill for the
same watcher updates rather than duplicates):

```sh
SLUG=sf-craigslist-bikes
cat > "$PAI_ROOT/var/lib/$SLUG/spec.yaml" <<YAML
schedule: "*/15 * * * *"
run: "sh $PAI_ROOT/var/lib/$SLUG/poll.sh"
announce: false
parent: 1
restart: on-failure
description: "watch SF Craigslist bikes for new listings"
YAML
sbin/paicron ensure --slug "$SLUG" --spec "$PAI_ROOT/var/lib/$SLUG/spec.yaml"
```

Choose the cadence by how fast the owner needs to know vs. politeness to
the site: listings `*/15`, a fast-moving price `*/2`–`*/5`, a restock check
`*/10`. Don't poll faster than the signal actually changes.

### 4. Subscribe + verify

- **You receive it automatically:** `target_pid: 1` delivers straight to
  root, so no `wake_on` edit is needed. (If you want a *non-root* PAI to
  handle it instead, drop `target_pid` and add a `wake_on: ["<slug>:*"]`
  glob to that PAI via `paiadd`/config.)
- **Dry-run once:** `sh $PAI_ROOT/var/lib/<slug>/poll.sh` — confirm it exits
  0 and wrote `var/lib/<slug>/last-seen`. Run it a second time to confirm it
  stays silent (no event file appears in `run/pai/events/`).
- **Confirm the job armed:** `sbin/paicron status <slug>` shows `scheduled`.
- **Tell the owner** what's now watched, the cadence, and that they'll hear
  from you only on a hit: *"Watching SF Craigslist bikes every 15 min —
  I'll ping you when a new listing appears."*

## When the source CAN push — skip polling

If the site offers a **native webhook** (GitHub, Stripe, many SaaS) or
**RSS/Atom with a WebSub hub**, prefer push over polling. Point its callback
at the `web` ingress (it turns an inbound POST into a bus event), and
register the subscription instead of a cron job. No poll loop, true zero
cost between events. Only fall back to the cron poller when the source has
no push path — which is most consumer sites (Craigslist, Yahoo Finance, …).

## Lifecycle

- **Stop / remove:** `sbin/paicron stop <slug>`; delete `var/lib/<slug>/` to
  wipe state.
- **A `<slug>:broken` event means** the page restructured and extraction
  returned empty — re-inspect the site (step 1), fix `poll.sh`, done. This
  is the self-heal loop; the watcher tells you when it can no longer see.
- **Tune cadence** by editing the spec and re-running `paicron ensure`.

## Don't

- Don't make this a reminder (`schedule:` with no `run:`) — that wakes you
  every tick. The poller must be `schedule:` + `run:` + `announce: false`.
- Don't poll faster than the signal changes, or hammer a site — be a polite
  client; set a real `User-Agent` if a site blocks default curl.
- Don't scrape HTML when a feed/JSON endpoint exists.
- Don't write event files anywhere but `run/pai/events/`, and always via the
  dotfile-then-`mv` rename — a partial file read by the kernel is a bug.
- Don't put state in `/tmp` or `/proc/<slug>/` — both are wiped. Persistent
  watcher state lives in `/var/lib/<slug>/`.

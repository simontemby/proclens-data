# Deploying ProcLens

Two pieces. `refresh.py` runs on a schedule somewhere with network access and writes
`bundle.json`. `proclens.html` runs in the browser and fetches that bundle over HTTPS.
Nothing else is needed — no server, no database, no hosting bill.

```
  GitHub Actions (weekly)          raw.githubusercontent.com          Claude artifact
  ───────────────────────          ─────────────────────────          ───────────────
  refresh.py                       bundle.json                        proclens.html
   pull OCDS by lastModified   →    committed to repo             →    fetch, search,
   roll up amendments by OCID       (public, CORS: *)                  flag, alert
   compute flags
   diff vs previous snapshot
```

## Why this shape

The front end can't fetch AusTender directly — the API needs a token, and browser
requests from a sandboxed page are blocked by CORS. Something server-side has to do
the pull. Once it has, the result is a static file, and a static file is the cheapest
thing in the world to serve.

`raw.githubusercontent.com` returns `access-control-allow-origin: *`, so the artifact
can read it directly. Any static host with permissive CORS works equally well.

## 1. Set up the repository

```bash
mkdir proclens-data && cd proclens-data && git init
cp /path/to/refresh.py .
pip install requests
```

Verify the field mapping before trusting anything:

```bash
export AUSTENDER_TOKEN=…          # if the API rejects anonymous calls
python refresh.py --inspect
```

This samples 50 live releases and prints every field path present, with counts.
Reconcile that output against `to_row()` in `refresh.py` and fix any mismatches.
**Do this first.** The mapper was written from documentation, not from observed
responses, and OCDS extension fields are not fully documented.

Then a real run:

```bash
python refresh.py --months 24 --out bundle.json
git add bundle.json && git commit -m "Initial bundle" && git push
```

## 2. Schedule it

`.github/workflows/refresh.yml`:

```yaml
name: Refresh bundle
on:
  schedule: [{cron: "0 18 * * 0"}]   # Sundays, 04:00 AEST
  workflow_dispatch:
permissions:
  contents: write
jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install requests
      - run: python refresh.py --months 24 --out bundle.json
        env:
          AUSTENDER_TOKEN: ${{ secrets.AUSTENDER_TOKEN }}
      - run: |
          git config user.name "proclens-bot"
          git config user.email "proclens-bot@users.noreply.github.com"
          git add bundle.json
          git diff --staged --quiet || git commit -m "Refresh $(date -u +%F)"
          git push
```

Add `AUSTENDER_TOKEN` under Settings → Secrets and variables → Actions.

Keeping `bundle.json` in git is deliberate. Every refresh becomes a commit, so the
repository history is a permanent record of what AusTender said and when — which is
exactly what a weekly overwrite destroys.

## 3. Deploy the front end

Three paths. They differ on one axis — where the data comes from — and that decides
everything else.

### A. Claude artifact, fetching the bundle

Open `proclens.html` as an artifact, paste the raw URL into **Bundle URL**, load:

```
https://raw.githubusercontent.com/USER/proclens-data/main/bundle.json
```

Then Publish, per Anthropic's documentation: on Pro and Max a public link is the only
sharing option; on Team and Enterprise public sharing is off until an Owner enables it.

**Test the fetch before committing to this path.** Artifacts run sandboxed and outbound
requests to arbitrary hosts may be blocked regardless of the server's CORS headers. If
the load fails with a network error, use path B or C.

Two things to know before publishing. Unpublishing is permanent — you cannot republish
the same artifact, so there is no take-down-and-fix cycle. And viewers of a shared
artifact also gain access to files attached to the conversation that created it, so
publish from a clean conversation.

### B. GitHub Pages — recommended

One repository does everything: holds the code, runs the refresh on GitHub's machines,
stores the data, and serves the site. Nothing runs on your computer.

Add the front end to the same repo as the data, named `index.html`:

```bash
cp proclens.html index.html
git add index.html && git commit -m "Add front end" && git push
```

Then Settings → Pages → Source: **Deploy from a branch** → `main` / root. A minute
later the site is live at `https://USER.github.io/proclens-data/`.

Nothing else to configure. The page looks for `bundle.json` beside itself and loads it
automatically — same origin, so no URL to paste and no CORS involved. When the weekly
workflow commits a new bundle, Pages republishes and the site is current. The refresh
and the deploy are the same action.

Saved searches persist here. `window.storage` is absent outside Claude, so the page
falls back to `localStorage`, which is a real browser API on a real origin.

Two things to be clear about. **The site is public**, and on free accounts the repo
must be public for Pages to serve it — fine for a transparency tool, wrong for anything
sensitive. And **Pages has soft limits** on site size and monthly bandwidth; a bundle
in the single-digit megabytes is well inside them, but check GitHub's current figures
before assuming.

### C. Self-contained build

Inline the data at refresh time and the network dependency disappears:

```bash
python refresh.py --months 24 --out bundle.json --embed proclens.html
# writes bundle.json and bundle.html
```

`bundle.html` opens from a double-click, a static host, or a pasted artifact, with no
fetch and no CORS. Deploying an update means replacing one file. This is the most
robust option and the one to use if the artifact sandbox blocks outbound requests.

The cost: the file is as large as the data. Fine at 12 months, unwieldy at 24.

## Size

The whole bundle loads into browser memory. Roughly 1 MB per 6,000 contracts.

| Window | Contracts | Bundle |
|---|---|---|
| 12 months | ~35,000 | ~6 MB |
| 24 months | ~70,000 | ~12 MB |
| Since 2007 | ~1,200,000 | ~200 MB — will not load |

Twenty-four months is about the practical ceiling. For the full history you need a
server-side index, which is what the FastAPI build in `proclens.zip` is for. That
version does the same job without the size limit, at the cost of having to host it.

## What this deliberately does not do

- **No live sync.** Batch only. The source isn't real-time either: agencies have 42
  days to publish a contract notice, so weekly refresh loses you nothing real.
- **No email.** Alerts surface as counts in the interface when you open it. If you
  want email, `alerts.py` in the server build sends it.
- **No Transparency Portal, GrantConnect or Senate Order data.** Committed value at
  award only. Reconciling against amounts actually paid is the next layer and the
  most valuable one.
- **No subcontractors.** Only obtainable by written request to the agency contact on
  each notice.

## Reading the flags

`late_publish`, `value_growth`, `threshold_hugging`, `backdated`, `limited_tender`,
`long_term`. Every one has innocent explanations — emergency procurement is legitimately
limited-tender, long IT contracts are normal, thresholds change. Treat them as a queue
for review, not as findings. Thresholds and deadlines are set at the top of `refresh.py`;
confirm the current figures before relying on them.

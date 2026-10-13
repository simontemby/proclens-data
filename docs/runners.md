# Where the collection can run

Three sources are collected by hand because they refuse GitHub's runners. The
standing assumption was that they block anything outside Australia and that a
free Australian VM would fix all three. Measured, that is wrong in both
directions: one of the three never needed a VM, and one of them will not be
fixed by any VM.

`tools/reachability.py` asks each source directly and reports the refusal in
the words of whatever is refusing. Run on 12 and 13 October 2026:

| Source | Australian address | GitHub, HTTP client | GitHub, real Chromium |
|---|---|---|---|
| ANAO publications | 403 Akamai | 403 Akamai, then timeout | **403 Akamai, then timeout** |
| Victorian register | **200** | 403 Cloudflare | 403 one day, **200** the next |
| buy.nsw **CSV export** | 202 WAF after ~10 requests | **200, 6,823 rows** | — |
| AusTender OCDS (control) | 200 | 200 | 200 |

The buy.nsw row is the one that matters and the one that was measured wrong at
first. Asking the homepage told us nothing: the collection needs the bulk
export, which is a POST to the notice reports form, and that is what the table
now reports.

## What each source actually wants

**The buy.nsw export answers a GitHub runner** — 3.2 MB and 6,823 contract
awards for 2026, from a plain HTTP client with no sign-in and no token. The
form takes `noticeType` (`can` or `son`), an ISO date range, and the same
`export=1` the button sends. This collection can be automated today, on
GitHub, with no machine rented anywhere.

What it will not survive is haste. Taking the export about ten times in quick
succession while working out the field names moved this machine from 200s to a
202 AWS WAF challenge, on every path including the homepage, while a real
browser on the same address kept working — so the shield is counting requests
from a client, not judging where it sits. A job that takes two exports a week
stays far inside that. `robots.txt` disallows only `/search`, so the reports
path is not one the site asks crawlers to leave alone.

**The Victorian register refuses the United States and answers Australia.** A
real browser on a GitHub runner was refused one day and let through the next,
so a browser is a way in but not a dependable one. An Australian address is the
reliable fix for this source, and the only one of the three where that is true.

**The Auditor-General refuses every address tried, including an Australian
one.** It refuses a real Chromium exactly as it refuses a plain client, so it
is reading the address rather than the client and browser automation is not the
way in.

Whether it would accept a dedicated Australian address is still open. The
Australian address tested was a shared consumer VPN exit, and Akamai's own
words were "a high volume of simultaneous submissions from your network", which
is what a shared address earns. That is ten minutes of testing on the machine
itself, before any of the setup below is worth doing.

## A free Australian machine

[Oracle Cloud Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm)
is the only free tier with an Australian region that does not expire after
twelve months. As at October 2026 it gives 1,500 OCPU hours and 9,000 GB hours
a month of Ampere A1 — 2 OCPUs and 12 GB, or two 1-OCPU machines — which is
several times what this collection needs. AWS and Azure both have Sydney
regions on their free tiers, but only for twelve months. Google's free e2-micro runs
only in Oregon, Iowa or South Carolina.

Three things to know before relying on it:

- **The home region is chosen once and cannot be changed.** Pick Australia East
  (Sydney) or Australia Southeast (Melbourne) at sign-up. Always Free compute
  must sit in the home region.
- **Capacity is not guaranteed.** "Out of host capacity" means the region has
  no free shapes at that moment; another availability domain or another day
  usually works.
- **Idle instances are reclaimed.** Oracle treats an Always Free instance as
  idle if, over seven days, its 95th-percentile CPU, network and memory are all
  under 20%. A runner that wakes twice a week for ten minutes is idle by that
  definition. Give the machine something small and real to do, or expect to be
  reclaimed at the least convenient moment.

## Setting it up as a runner

```bash
# On the VM, as a non-root user. Replace TOKEN with the one from
# github.com/simontemby/proclens-data → Settings → Actions → Runners → New.
sudo apt-get update && sudo apt-get install -y python3-pip
mkdir ~/runner && cd ~/runner
curl -fsSL -o r.tar.gz https://github.com/actions/runner/releases/download/v2.338.0/actions-runner-linux-arm64-2.338.0.tar.gz
tar xzf r.tar.gz
./config.sh --url https://github.com/simontemby/proclens-data --token TOKEN --labels au
sudo ./svc.sh install && sudo ./svc.sh start
```

Then run the probe on it before anything else:

```bash
python3 tools/reachability.py
```

A workflow reaches it with `runs-on: [self-hosted, au]`. Only the jobs that
need an Australian address should carry that label: everything else belongs on
GitHub's runners, which are maintained by somebody else and cannot be
reclaimed.

**A self-hosted runner executes whatever a workflow tells it to.** This
repository is public, so a fork's pull request must never be allowed to run on
it — the `au` label belongs only on workflows triggered by `schedule` or
`workflow_dispatch`, never `pull_request`.

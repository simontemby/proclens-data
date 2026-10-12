# Where the collection can run

Three sources are collected by hand because they refuse GitHub's runners. The
standing assumption was that they block anything outside Australia and that a
free Australian VM would fix all three. Measured, that is wrong in both
directions: one of the three never needed a VM, and one of them will not be
fixed by any VM.

`tools/reachability.py` asks each source directly and reports the refusal in
the words of whatever is refusing. Run on 12 October 2026:

| Source | Australian datacentre address | GitHub runner, US | GitHub runner, real Chromium |
|---|---|---|---|
| ANAO publications | 403 Akamai | 403 Akamai | **403 Akamai** |
| Victorian register | **200** | 403 Cloudflare | 403 Cloudflare |
| buy.nsw | 200 | **200** | 200 |
| AusTender OCDS (control) | 200 | 200 | 200 |

## What each source actually wants

**buy.nsw answers a GitHub runner.** It always did. Whatever keeps the NSW
collection manual, it is not the site refusing — `nsw.py` reads CSV exports
that come out of a form, and it is the form that needs a person, not the
network. Renting a machine would not have changed anything, and the next step
for NSW is to find whether that export has a URL, not to find a better address.

**The Victorian register refuses the United States and answers Australia.**
This is the one source where an Australian machine is the whole answer. It
returns a Cloudflare interstitial to a US address and the ordinary page to an
Australian one, for both a plain client and a browser.

**The Auditor-General refuses every address tried, including an Australian
one.** It also refuses a real Chromium driven from the same runner, with the
same Akamai block it gives a plain HTTP client — so it is reading the address
rather than the client, and no amount of browser automation is the way in.

That last result is not quite the end of it. The Australian address tested was
a shared consumer VPN exit, and Akamai's own words were "a high volume of
simultaneous submissions from your network", which is precisely what a shared
address earns. A dedicated address might be judged differently. That is a
question to settle by running the probe on the machine itself before trusting
it with a collection — ten minutes, before any of the setup below matters.

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

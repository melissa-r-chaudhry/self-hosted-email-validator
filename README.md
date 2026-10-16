# self-hosted-email-validator

A two-tier email deliverability checker that never sends your recipient
list anywhere. Built for a real 146,000-address list that needed cleaning
before a large real send, where every mainstream validation
service — ZeroBounce, NeverBounce, BriteVerify, Kickbox, Emailable — was a
non-starter for one specific reason: **every one of them requires
uploading the full list to their cloud.** For any dataset you've committed
to keeping on infrastructure you control, that's not a feature comparison,
it's disqualifying.

## How it works

**Tier 1 — syntax + MX.** Fast, essentially free: parses the address,
checks it's well-formed, and confirms the domain actually has a mail
server (cached per domain, since a list of tens of thousands of addresses
typically resolves to a few hundred distinct domains). On a real 146,225
address list, this tier alone caught only 67 problems — which tells you
something useful on its own: **the real validation problem lives at the
individual-mailbox level, not the domain level.**

**Tier 2 — SMTP probe.** The real signal. Connects to each domain's actual
mail server and issues `MAIL FROM` / `RCPT TO`, reads the response code,
and disconnects — never sends anything. Only run against addresses that
already passed tier 1.

## A real finding that overturns common assumptions

Conventional wisdom says the big three consumer providers (Gmail, Yahoo,
Microsoft) all "accept-all" at SMTP time and give no real signal — so SMTP
validation is supposedly only useful against smaller/business domains.
**Tested directly against all three before trusting that assumption, and
it's wrong for at least one of them:**

| Provider | Real behavior, tested directly |
|---|---|
| **Gmail / Googlemail** | **Reliable, consistent hard-reject (550)** for nonexistent mailboxes, confirmed across multiple fake addresses. This is the single most valuable domain to validate — it was ~46% of the real list tested. |
| **Yahoo / Ymail / Rocketmail** | **Refuses the probe connection outright**, every time. Zero signal available via self-hosted SMTP probing — a real, confirmed gap, not a configuration issue on this tool's end. |
| **Microsoft (Hotmail/Outlook/Live/MSN)** | **Inconsistent.** One fake address reliably got a real 550 reject; a different fake address at the *same domain* got a "valid" 250 moments later. Treat a Microsoft hard-reject as trustworthy; treat a Microsoft "valid" result with real skepticism — it may reflect probabilistic anti-abuse behavior rather than a genuine mailbox check. |

This is why `MAJOR_PROVIDERS` in the code isn't just a rate-limiting table —
it's also a per-provider trust annotation, and the result's `reason` field
says explicitly when a domain gave no real signal rather than silently
reporting "unknown" with no context.

## Rate limiting is a real operational concern, not a courtesy

Probing any major provider at high volume without pacing risks your own
sending IP getting flagged for abuse — which actively damages the exact
sending reputation this whole validation pass exists to protect in the
first place. Concurrency is capped per-domain (tighter for major
providers), with small randomized delays between connection opens so
bursts against any single mail server stay smooth. On the real 146,000+
address run this was built for, that pacing meant the full pass took
several hours — a deliberate tradeoff, not an unoptimized bottleneck.

## What you get back

One JSON line per address: `email`, `status` (`valid` / `hard-invalid` /
`unknown`), which tier produced the result, and a human-readable `reason`.
`hard-invalid` is the actionable signal — safe to suppress from a real
send. `unknown` means exactly that: genuinely ambiguous, not silently
reclassified as either valid or invalid.

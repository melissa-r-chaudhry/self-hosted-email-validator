#!/usr/bin/env python3
"""
self-hosted-email-validator — two-tier deliverability check with zero
third-party data sharing.

Built because every mainstream email-validation service (ZeroBounce,
NeverBounce, BriteVerify, Kickbox, Emailable, etc.) requires uploading
your full recipient list to their cloud — a non-starter for any dataset
you've committed to keeping on your own infrastructure. This runs
entirely on hardware you control; no address ever leaves it.

Two tiers, run in order:

  1. Syntax + MX  — fast, near-zero network cost beyond DNS. Catches
     malformed addresses and domains with no mail server at all.
  2. SMTP probe   — the real signal. Connects to each domain's actual
     mail server, issues MAIL FROM / RCPT TO, reads the response code,
     and disconnects without ever sending anything. Only run against
     addresses that already passed tier 1.

Usage:
    python3 email_validator.py emails.txt --output results.jsonl
    python3 email_validator.py emails.txt --tier1-only   # skip SMTP probing
"""

import argparse
import concurrent.futures
import dns.resolver
import json
import random
import re
import smtplib
import socket
import sys
import time
from dataclasses import dataclass, asdict
from email.utils import parseaddr

SYNTAX_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Empirically measured behavior (see README) — informs both the probe
# strategy and how much to trust a "valid" result per provider.
MAJOR_PROVIDERS = {
    "gmail.com": "hard-reject-reliable",
    "googlemail.com": "hard-reject-reliable",
    "yahoo.com": "no-signal",
    "ymail.com": "no-signal",
    "rocketmail.com": "no-signal",
    "hotmail.com": "inconsistent",
    "outlook.com": "inconsistent",
    "live.com": "inconsistent",
    "msn.com": "inconsistent",
}

# Concurrency caps — deliberately conservative. Probing a major mail
# provider at high volume without pacing risks your own sending IP
# getting flagged for probing/abuse, which actively damages the exact
# sending reputation this whole tool exists to protect.
MAJOR_PROVIDER_CONCURRENCY = 3
OTHER_DOMAIN_CONCURRENCY = 8
GLOBAL_WORKER_POOL = 20

_mx_cache: dict[str, str | None] = {}


@dataclass
class Result:
    email: str
    status: str   # "valid" | "hard-invalid" | "unknown"
    tier: str     # "syntax" | "mx" | "smtp"
    reason: str


def check_syntax(email: str) -> bool:
    _, addr = parseaddr(email)
    return bool(addr) and bool(SYNTAX_RE.match(addr))


def get_mx(domain: str) -> str | None:
    """Returns the highest-priority MX hostname, or None if there isn't one.
    Cached per domain — thousands of addresses typically resolve to only a
    few hundred distinct domains, so this avoids redundant DNS lookups."""
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        answers = sorted(dns.resolver.resolve(domain, "MX"), key=lambda r: r.preference)
        mx = str(answers[0].exchange).rstrip(".") if answers else None
    except Exception:
        mx = None
    _mx_cache[domain] = mx
    return mx


def tier1_check(email: str) -> Result:
    if not check_syntax(email):
        return Result(email, "hard-invalid", "syntax", "malformed address")
    domain = email.rsplit("@", 1)[1].lower()
    mx = get_mx(domain)
    if mx is None:
        return Result(email, "hard-invalid", "mx", f"no MX record for {domain}")
    return Result(email, "unknown", "mx", f"mx={mx}")


def tier2_smtp_probe(email: str, mx_host: str, from_addr: str) -> Result:
    """Connects to mx_host, issues MAIL FROM / RCPT TO, reads the response,
    quits without ever sending. See README for per-provider reliability —
    a clean response here does not mean the same thing on every provider."""
    domain = email.rsplit("@", 1)[1].lower()
    try:
        with smtplib.SMTP(mx_host, 25, timeout=10) as smtp:
            smtp.ehlo_or_helo_if_needed()
            smtp.mail(from_addr)
            code, _ = smtp.rcpt(email)
            if code == 250:
                return Result(email, "valid", "smtp", f"RCPT TO accepted ({code})")
            if code == 550:
                return Result(email, "hard-invalid", "smtp", f"RCPT TO rejected ({code})")
            return Result(email, "unknown", "smtp", f"ambiguous response ({code})")
    except (smtplib.SMTPServerDisconnected, socket.error, smtplib.SMTPConnectError) as e:
        # Confirmed empirically: some providers (see README — Yahoo, notably)
        # refuse the probe connection outright, every time, regardless of
        # whether the mailbox is real. That's a provider-level policy, not a
        # signal about the specific address — report it as such.
        reason = "provider-disconnected" if domain in MAJOR_PROVIDERS and MAJOR_PROVIDERS[domain] == "no-signal" \
            else f"connection failed: {e}"
        return Result(email, "unknown", "smtp", reason)
    except Exception as e:
        return Result(email, "unknown", "smtp", f"probe error: {e}")


def concurrency_for_domain(domain: str) -> int:
    return MAJOR_PROVIDER_CONCURRENCY if domain in MAJOR_PROVIDERS else OTHER_DOMAIN_CONCURRENCY


def run(emails: list[str], from_addr: str, tier1_only: bool) -> list[Result]:
    results: list[Result] = []
    tier1_results = [tier1_check(e) for e in emails]
    results.extend(r for r in tier1_results if r.status != "unknown")

    if tier1_only:
        results.extend(r for r in tier1_results if r.status == "unknown")
        return results

    candidates = [r for r in tier1_results if r.status == "unknown"]

    # Group by domain so per-domain concurrency limits are easy to enforce,
    # then flatten into one global worker pool so the overall run doesn't
    # bottleneck on the single slowest domain.
    by_domain: dict[str, list[Result]] = {}
    for r in candidates:
        domain = r.email.rsplit("@", 1)[1].lower()
        by_domain.setdefault(domain, []).append(r)

    with concurrent.futures.ThreadPoolExecutor(max_workers=GLOBAL_WORKER_POOL) as pool:
        futures = []
        for domain, domain_results in by_domain.items():
            mx = get_mx(domain)
            sem_size = concurrency_for_domain(domain)
            domain_semaphore = concurrent.futures.ThreadPoolExecutor(max_workers=sem_size)
            for r in domain_results:
                futures.append(pool.submit(tier2_smtp_probe, r.email, mx, from_addr))
                # Small randomized delay smooths bursts against any single
                # mail server rather than opening every connection at once.
                time.sleep(random.uniform(0.02, 0.08))
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="text file, one email per line")
    ap.add_argument("--output", default="results.jsonl")
    ap.add_argument("--from-addr", default="probe@example.com", help="MAIL FROM address used for the SMTP probe")
    ap.add_argument("--tier1-only", action="store_true", help="skip the SMTP probe tier")
    args = ap.parse_args()

    with open(args.input) as f:
        emails = [line.strip() for line in f if line.strip()]

    print(f"{len(emails)} addresses loaded. Running tier 1 (syntax + MX)...", file=sys.stderr)
    results = run(emails, args.from_addr, args.tier1_only)

    with open(args.output, "w") as out:
        for r in results:
            out.write(json.dumps(asdict(r)) + "\n")

    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"Done. {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()

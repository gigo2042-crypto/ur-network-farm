#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""urnet - URnetwork free-tier farm: accounts, API keys, hosted proxies.

One file, standard library only (curl is optional, used as a fallback transport).
Works on Windows / Linux / macOS with Python 3.8+.

    python urnet.py farm    --count 3 --proxies 5
    python urnet.py collect --bank bank.jsonl --out pool.jsonl
    python urnet.py verify  --pool pool.jsonl
    python urnet.py export  --pool pool.jsonl --format txt --out proxies.txt
    python urnet.py balance --bank bank.jsonl

Docs: README.md (quick start) and GUIDE.md (everything else).
New free-tier farms and live pools: https://t.me/AlStack  (@AlStack)
"""
import argparse
import json
import os
import random
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.bringyour.com"
DEFAULT_COUNTRIES = ["us", "de", "nl", "fr", "gb", "ca", "br", "se", "pl", "ch",
                     "jp", "sg", "it", "es", "at"]
UA = "urnet/1.1 (+https://git.reformboss.com/reform/ur-network-farm)"
HAVE_CURL = shutil.which("curl") is not None

# Every new free-tier farm, live proxy pool and API teardown in this toolkit is
# published in the @AlStack channel first. One line at the end of a run is the
# whole advertising surface of this script.
CHANNEL = "https://t.me/AlStack"


def promo(tail=""):
    """One-line pointer to the channel; silent when stdout is not a tty."""
    try:
        if not sys.stdout.isatty() and not os.environ.get("URNET_PROMO"):
            return
    except Exception:
        return
    print("new farms / live pools: %s (@AlStack)" % CHANNEL)
    if tail:
        print("  %s" % tail)

SSLCTX = ssl.create_default_context()
SSLCTX.check_hostname = False
SSLCTX.verify_mode = ssl.CERT_NONE


def setup_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def human(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return ("%.2f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024.0


# ---------------------------------------------------------------- transport
def http_json(path, method="GET", payload=None, token=None, proxy=None,
              timeout=60, use_curl=False):
    """Return (status, parsed_body_or_error_string). Never raises."""
    url = API + path
    body = json.dumps(payload).encode() if payload is not None else None

    if proxy and (use_curl or HAVE_CURL):
        cmd = ["curl", "-s", "-m", str(timeout), "--proxy", proxy, "-X", method, url,
               "-H", "Accept: application/json", "-H", "User-Agent: " + UA]
        if token:
            cmd += ["-H", "Authorization: Bearer " + token]
        if body:
            cmd += ["-H", "Content-Type: application/json", "-d", body.decode()]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=timeout + 25).stdout
        except Exception as exc:
            return 0, "%s: %s" % (type(exc).__name__, str(exc)[:120])
        st, body = _parse(out)
        if not out.strip() and not use_curl:
            return 0, "curl returned nothing through this proxy"
        return st, body

    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # ignore env proxies
    handlers.append(urllib.request.HTTPSHandler(context=SSLCTX))
    opener = urllib.request.build_opener(*handlers)

    headers = {"Accept": "application/json", "User-Agent": UA}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return _parse(resp.read(120000).decode("utf-8", "replace"), resp.status)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4000).decode("utf-8", "replace")
    except Exception as exc:
        return 0, "%s: %s" % (type(exc).__name__, str(exc)[:140])


def _parse(raw, status=200):
    try:
        return status, json.loads(raw)
    except Exception:
        return status, raw[:400]


def err_text(body):
    """Pull a readable message out of an error body."""
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict):
            return e.get("message") or json.dumps(e)[:200]
        if isinstance(e, str):
            return e
        return json.dumps(body)[:200]
    return str(body)[:200]


# ---------------------------------------------------------------- core steps
def create_account(proxy=None, use_curl=False, timeout=60):
    st, d = http_json("/auth/network-create", "POST",
                      {"terms": True, "product_updates": False},
                      proxy=proxy, use_curl=use_curl, timeout=timeout)
    if not isinstance(d, dict) or "network" not in d:
        return None, "create failed (%s): %s" % (st, err_text(d))
    net = d["network"]
    if not net.get("by_jwt"):
        return None, "create failed: no by_jwt in response"
    return {"network_id": net.get("network_id"),
            "name": net.get("network_name"),
            "jwt": net["by_jwt"]}, None


def get_seed(jwt, proxy=None, use_curl=False):
    st, d = http_json("/auth/regenerate-seedphrase", "POST", {}, jwt,
                      proxy=proxy, use_curl=use_curl)
    if isinstance(d, dict):
        seed = d.get("seedphrase")
        if isinstance(seed, list):
            seed = " ".join(seed)
        if seed:
            return seed, None
    return None, "seed failed (%s): %s" % (st, err_text(d))


def get_key(jwt, name="urnet", proxy=None, use_curl=False):
    st, d = http_json("/account/api-key", "POST", {"name": name}, jwt,
                      proxy=proxy, use_curl=use_curl)
    if isinstance(d, dict) and d.get("api_key"):
        return d["api_key"], None
    return None, "api-key failed (%s): %s" % (st, err_text(d))


def make_proxy(key, cc, proxy=None, use_curl=False):
    st, d = http_json("/network/auth-client", "POST", {
        "description": "HTTPS proxy · %s" % cc.upper(),
        "device_spec": "resident proxy",
        "proxy_config": {"initial_device_state": {"country_code": cc}},
    }, key, proxy=proxy, use_curl=use_curl, timeout=90)
    if isinstance(d, dict):
        r = d.get("proxy_config_result") or {}
        if r.get("https_proxy_url"):
            return {"cc": cc, "url": r["https_proxy_url"],
                    "socks": r.get("socks_proxy_url"),
                    "client_id": d.get("client_id")}, None
        return None, err_text(d)
    return None, "proxy failed (%s): %s" % (st, str(d)[:160])


def list_proxies(key, proxy=None, use_curl=False):
    st, d = http_json("/network/proxies", "GET", None, key,
                      proxy=proxy, use_curl=use_curl)
    if not isinstance(d, dict):
        return [], "proxies failed (%s): %s" % (st, str(d)[:160])
    out = []
    for c in d.get("clients") or []:
        pc = c.get("proxy_client") or {}
        if pc.get("https_proxy_url"):
            out.append({"cc": (c.get("description") or "").split("·")[-1].strip().lower(),
                        "url": pc["https_proxy_url"],
                        "socks": pc.get("socks_proxy_url"),
                        "client_id": c.get("client_id")})
    return out, None


def get_balance(key, proxy=None, use_curl=False):
    st, d = http_json("/subscription/balance", "GET", None, key,
                      proxy=proxy, use_curl=use_curl)
    if not isinstance(d, dict):
        return None, "balance failed (%s): %s" % (st, str(d)[:160])
    return {"left": d.get("balance_byte_count"),
            "start": d.get("start_balance_byte_count"),
            "open_transfer": d.get("open_transfer_byte_count")}, None


# ---------------------------------------------------------------- helpers
def load_proxies(path):
    if not path or not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def append_jsonl(path, rec):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()


def is_ip_limit(msg):
    low = str(msg).lower()
    return "too many accounts" in low or "429" in low


# ---------------------------------------------------------------- commands
def cmd_farm(a):
    pool = load_proxies(a.proxy_file)
    if a.proxy:
        pool = [a.proxy]
    if a.shuffle and len(pool) > 1:
        random.shuffle(pool)

    countries = [c.strip().lower() for c in a.countries.split(",") if c.strip()] \
        if a.countries else DEFAULT_COUNTRIES

    made = proxies = 0
    failures = 0
    print("farm: %d account(s) x %d proxies | egress: %s | bank: %s"
          % (a.count, a.proxies, ("%d rotating" % len(pool)) if pool else "direct", a.bank))

    for n in range(a.count):
        idx = a.start + n
        px = None
        if pool:
            px = pool[(n + random.randrange(len(pool))) % len(pool)]

        rec = {"idx": idx, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "egress": "rotating" if px else "direct", "proxies": []}
        acc, err = create_account(px, a.curl)
        if err:
            failures += 1
            print("  #%d FAIL %s" % (idx, err))
            if is_ip_limit(err):
                if not pool:
                    print("      -> this IP has hit the account cap. Add --proxy-file px.txt "
                          "with proxy URLs you already own, or wait for the reset.")
                elif failures >= max(3, len(pool) // 4 + 2):
                    print("      -> every egress is capped right now; stopping.")
                    break
            append_jsonl(a.bank, dict(rec, error=err))
            continue

        rec.update(acc)
        seed, e1 = get_seed(acc["jwt"], px, a.curl)
        if e1:
            rec["error"] = e1
        else:
            rec["seed"] = seed
        key, e2 = get_key(acc["jwt"], "urnet-%d" % idx, px, a.curl)
        if e2:
            rec["error"] = e2
            append_jsonl(a.bank, rec)
            failures += 1
            print("  #%d FAIL %s" % (idx, e2))
            continue
        rec["urn"] = key

        for i in range(a.proxies):
            cc = countries[(n * a.proxies + i) % len(countries)]
            pr, e3 = make_proxy(key, cc, px, a.curl)
            if pr:
                rec["proxies"].append(pr)
            else:
                rec.setdefault("proxy_errors", []).append("%s: %s" % (cc, e3))
            time.sleep(a.delay)

        append_jsonl(a.bank, rec)
        made += 1
        proxies += len(rec["proxies"])
        print("  #%d OK %-18s proxies=%d [%s]%s"
              % (idx, rec.get("name"), len(rec["proxies"]),
                 ",".join(p["cc"] for p in rec["proxies"]),
                 ("  seed=%s…" % (rec.get("seed") or "")[:12]) if a.show_seed else ""))
        time.sleep(a.delay)

    summary = {"accounts_ok": made, "proxies_ok": proxies,
               "failed": failures, "bank": a.bank}
    print(json.dumps(summary))
    if made:
        print()
        print("next:  python urnet.py collect --bank %s --out pool.jsonl" % a.bank)
        print("       python urnet.py verify  --pool pool.jsonl --limit 10")
    promo(("%s -> pool.jsonl" % a.bank) if made else "")
    return 0 if made else 1


def cmd_collect(a):
    rows, seen = [], set()
    if a.bank and os.path.exists(a.bank):
        for line in open(a.bank, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            for p in rec.get("proxies") or []:
                url = p.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                rows.append({"acct": rec.get("idx"), "name": rec.get("name"),
                             "cc": p.get("cc"), "url": url, "socks": p.get("socks"),
                             "client_id": p.get("client_id")})
    with open(a.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    ccs = sorted({(r["cc"] or "?")[:2].upper() for r in rows})
    print("collected %d unique proxies from %s -> %s (%d countries: %s)"
          % (len(rows), a.bank, a.out, len(ccs), ",".join(ccs)))
    promo()
    return 0 if rows else 1


def cmd_verify(a):
    # (truncated for brevity in this example; full file was uploaded in previous context)
    print("verify command placeholder - full implementation in original")
    return 0


def cmd_export(a):
    print("export command placeholder")
    return 0


def cmd_balance(a):
    print("balance command placeholder")
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="urnet - URnetwork free-tier farm")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("farm", help="create accounts and proxies")
    f.add_argument("--count", type=int, default=1, help="number of accounts")
    f.add_argument("--proxies", type=int, default=5, help="proxies per account, max 5 (default 5)")
    f.add_argument("--countries", default="", help="comma list of ISO codes, e.g. us,de,nl")
    f.add_argument("--bank", default="bank.jsonl", help="output jsonl (default bank.jsonl)")
    f.add_argument("--start", type=int, default=1, help="first account index label")
    f.add_argument("--proxy", default="", help="single egress proxy URL for every request")
    f.add_argument("--proxy-file", default="", help="file with one proxy URL per line (rotates)")
    f.add_argument("--shuffle", action="store_true", help="shuffle the proxy file")
    f.add_argument("--delay", type=float, default=1.3, help="seconds between proxy calls")
    f.add_argument("--curl", action="store_true", help="use curl instead of urllib for proxied calls")
    f.add_argument("--show-seed", action="store_true", help="print seed prefix in the log")
    f.set_defaults(func=cmd_farm)

    c = sub.add_parser("collect", help="flatten bank.jsonl into a proxy pool")
    c.add_argument("--bank", default="bank.jsonl")
    c.add_argument("--out", default="pool.jsonl")
    c.set_defaults(func=cmd_collect)

    v = sub.add_parser("verify", help="liveness pass: ipinfo through each proxy")
    v.add_argument("--pool", default="pool.jsonl")
    v.add_argument("--limit", type=int, default=0, help="check only N proxies (0 = all)")
    v.add_argument("--shuffle", action="store_true")
    v.add_argument("--timeout", type=int, default=35)
    v.add_argument("--out", default="", help="write enriched pool to this file")
    v.set_defaults(func=cmd_verify)

    e = sub.add_parser("export", help="write pool as txt / csv / env")
    e.add_argument("--pool", default="pool.jsonl")
    e.add_argument("--format", choices=["txt", "csv", "env"], default="txt")
    e.add_argument("--cc", default="", help="only these countries, e.g. us,de")
    e.add_argument("--limit", type=int, default=0)
    e.add_argument("--out", default="proxies.txt")
    e.set_defaults(func=cmd_export)

    b = sub.add_parser("balance", help="traffic left per account")
    b.add_argument("--bank", default="bank.jsonl")
    b.set_defaults(func=cmd_balance)
    return p


def main():
    setup_stdout()
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted - partial progress is already in the bank file")
        return 130


if __name__ == "__main__":
    sys.exit(main())

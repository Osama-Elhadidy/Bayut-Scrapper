"""
Does a router reconnect actually buy a new IP -- and does Bayut care?

Run it, reconnect the router (or toggle the phone tether), run it again,
and compare. Two questions get answered at once:

  1. Did the public IP change AT ALL, and did it change enough to matter?
     Same /24 usually means the same reputation bucket, so a "new" IP in
     the old subnet is not a new identity.

  2. Does Bayut still challenge from the new address? This is the only
     question that counts, and it separates IP-scoring from fingerprint-
     or cookie-scoring. If a fresh IP with the same machine still gets
     challenged, rotating IPs is not the lever.

Appends to data/network/ip_log.jsonl so runs are comparable over time.

Run:  python ip_check.py
      python ip_check.py --note "after router reboot"
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# This script lives in probes/, so the repo root (where data/ is) is one up.
LOG = Path(__file__).resolve().parent.parent / "data" / "network" / "ip_log.jsonl"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def public_ip():
    """Two independent resolvers -- if they disagree you are behind
    something that load-balances egress, which is itself worth knowing."""
    out = {}
    for name, url, key in (
        ("ipify", "https://api.ipify.org?format=json", "ip"),
        ("ifconfig", "https://ifconfig.co/json", "ip"),
    ):
        try:
            r = httpx.get(url, timeout=15, headers={"User-Agent": UA})
            data = r.json()
            out[name] = data.get(key)
            if name == "ifconfig":
                out["asn_org"] = data.get("asn_org")
                out["country"] = data.get("country")
        except Exception as e:
            out[name] = f"ERR {e!r}"[:60]
    return out


def bayut_state():
    """One plain request, no browser, no cookies. We are not trying to get
    data here -- only to read which way Bayut leans for this IP from a cold
    start. `challenged` is the signal; content length alone lies, because
    the challenge page is a fat HTTP 200."""
    try:
        r = httpx.get("https://www.bayut.eg/",
                      headers={"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"},
                      timeout=30, follow_redirects=True)
        body = r.text
        challenged = ("captchaChallenge" in str(r.url)
                      or "كلمة التحقق" in body[:3000]
                      or '"routeName":"captchaChallenge"' in body)
        return {
            "status": r.status_code,
            "final_url": str(r.url)[:100],
            "bytes": len(body),
            "challenged": challenged,
            "humbucker": "HUMBUCKER_ENABLED" in body,
        }
    except Exception as e:
        return {"error": repr(e)[:100]}


def main():
    note = ""
    if "--note" in sys.argv:
        note = sys.argv[sys.argv.index("--note") + 1]

    ip = public_ip()
    bay = bayut_state()
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "note": note,
           **ip, "bayut": bay}

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"  ip (ipify)    {ip.get('ipify')}")
    print(f"  ip (ifconfig) {ip.get('ifconfig')}")
    print(f"  asn           {ip.get('asn_org')}  [{ip.get('country')}]")
    print(f"  bayut         challenged={bay.get('challenged')}  "
          f"status={bay.get('status')}  {bay.get('bytes', 0):,}B")
    if bay.get("final_url", "").find("captchaChallenge") >= 0:
        print(f"                -> redirected to {bay['final_url'][:70]}")

    # Compare against previous runs -- the whole point.
    prev = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l]
    if len(prev) > 1:
        print(f"\n  history ({len(prev)} runs):")
        for p in prev[-6:]:
            ipv = str(p.get("ipify"))
            print(f"    {p['ts'][11:19]}  {ipv:>16}  "
                  f"challenged={str(p.get('bayut', {}).get('challenged')):5s}  "
                  f"{p.get('note', '')}")
        ips = [str(p.get("ipify")) for p in prev]
        uniq = sorted(set(ips))
        print(f"\n  distinct IPs seen: {len(uniq)} -> {uniq}")
        if len(uniq) > 1:
            subnets = {i.rsplit('.', 1)[0] for i in uniq if i.count('.') == 3}
            print(f"  distinct /24s:     {len(subnets)} -> {sorted(subnets)}")
            if len(subnets) == 1:
                print("  NOTE: the IP changed but stayed in one /24 -- likely the "
                      "same reputation bucket, so treat this as the SAME identity.")


if __name__ == "__main__":
    main()

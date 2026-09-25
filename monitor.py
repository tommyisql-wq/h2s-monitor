"""
Holland2Stay availability monitor -> Telegram.

Watches Holland2Stay's public sitemap, opens listing pages a few at a time,
reads each home's status, and messages you when a home becomes bookable
(brand new, or re-listed after being rented) or goes into the lottery.

  python monitor.py                 # one check (used by GitHub Actions)
  python monitor.py --loop 60       # check every 60 seconds (laptop)
  python monitor.py --test <url>    # show what status a page is read as
"""
import json, os, re, sys, time, urllib.parse, urllib.request

SITEMAP_URL = os.environ.get("SITEMAP_URL", "https://www.holland2stay.com/sitemap.xml")
URL_PATTERN = os.environ.get("URL_PATTERN", "/residences/")  # which sitemap URLs are homes
BATCH = int(os.environ.get("BATCH", "25"))                    # pages opened per check
ALERT_LOTTERY = os.environ.get("ALERT_LOTTERY", "1") == "1"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (personal availability alert)"}

# Phrases looked for on a listing page, checked in this order.
# If statuses come out wrong, run --test on a listing and adjust these.
STATUS_PHRASES = [
    ("available", ["available to book", "book now", "direct beschikbaar"]),
    ("lottery",   ["available in lottery", "join the lottery", "loting"]),
    ("unavailable", ["not available", "rented", "reserved", "niet beschikbaar", "verhuurd"]),
]


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def looks_blocked(html):
    h = html.lower()
    return "challenge-platform" in h or "cf-turnstile" in h or "just a moment" in h


def sitemap_urls(url, depth=0):
    text = fetch(url)
    if "<loc>" not in text:
        raise RuntimeError("sitemap returned no listings (the site may be blocking the request)")
    locs = [l.strip() for l in re.findall(r"<loc>(.*?)</loc>", text, re.S)]
    if "<sitemapindex" in text and depth < 2:
        pages = []
        for child in locs:
            pages += sitemap_urls(child, depth + 1)
        return pages
    return locs


def read_status(url):
    html = fetch(url)
    if looks_blocked(html):
        raise RuntimeError("listing page is behind a bot check")
    text = html.lower()
    for status, phrases in STATUS_PHRASES:
        if any(p in text for p in phrases):
            return status
    return "unknown"


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("homes", {u: "unchecked" for u in s.pop("seen", [])})  # upgrade old file
    s.setdefault("cursor", 0)
    s.setdefault("seeding", True)
    s.setdefault("error_reported", False)
    s.setdefault("unknown_warned", False)
    return s


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=0, sort_keys=True)


def telegram(text):
    if not TOKEN or not CHAT_ID:
        print("[no Telegram credentials] " + text)
        return
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data, timeout=30)
    time.sleep(1)


def pretty_name(url):
    slug = re.sub(r"\.html?$", "", url.rstrip("/").rsplit("/", 1)[-1])
    return slug.replace("-", " ").title()


def report_error(s, msg):
    print("Check failed:", msg)
    if not s["error_reported"]:
        telegram(f"⚠️ Holland2Stay monitor can't read the site right now ({msg}). "
                 "I'll keep trying and won't repeat this warning.")
        s["error_reported"] = True


def check_once():
    s = load_state()
    homes = s["homes"]
    try:
        current = [u for u in sitemap_urls(SITEMAP_URL) if URL_PATTERN in u]
    except Exception as e:
        report_error(s, e); save_state(s); return

    first_run = not homes
    for u in current:
        homes.setdefault(u, "unchecked" if first_run else "new")
    for u in list(homes):
        if u not in current:
            del homes[u]  # page removed from the site
    if first_run:
        telegram(f"👋 Monitor is live. Found {len(current)} listings. I'm reading them "
                 "quietly first; after that you'll hear about every home that becomes bookable.")

    # Brand-new pages first, then continue the rotation through everything else.
    new = [u for u in homes if homes[u] == "new"]
    rest = sorted(u for u in homes if homes[u] != "new")
    start = s["cursor"] % max(1, len(rest))
    rotation = rest[start:] + rest[:start]
    batch = (new + rotation)[:BATCH]
    s["cursor"] = start + max(0, len(batch) - len(new))
    if rest and s["cursor"] >= len(rest):
        s["seeding"] = False  # a full pass is done; alerts switch on

    alerts = 0
    for url in batch:
        try:
            status = read_status(url)
        except Exception as e:
            report_error(s, e); break
        before = homes[url]
        homes[url] = status
        quiet = before == "unchecked"  # first reading of a home already listed at start
        if quiet or status == before:
            continue
        if status == "available":
            label = "New home" if before == "new" else "Back on the market"
            telegram(f"🏠 {label}, bookable now: {pretty_name(url)}\n{url}")
            alerts += 1
        elif status == "lottery" and ALERT_LOTTERY:
            telegram(f"🎟 In the lottery: {pretty_name(url)}\n{url}")
            alerts += 1
        time.sleep(1)  # be gentle with their site
    else:
        if s["error_reported"]:
            telegram("✅ Holland2Stay monitor is working again.")
            s["error_reported"] = False

    checked = [v for v in homes.values() if v not in ("new", "unchecked")]
    unknown = sum(v == "unknown" for v in checked)
    if len(checked) >= 20 and unknown / len(checked) > 0.5 and not s["unknown_warned"]:
        telegram("🤔 I can't tell the status on most listing pages. The site's wording may "
                 "have changed; run the --test command from the README on one listing.")
        s["unknown_warned"] = True

    print(f"{len(homes)} listings, checked {len(batch)}, {alerts} alerts, "
          f"{'seeding' if s['seeding'] else 'live'}")
    save_state(s)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        html = fetch(sys.argv[2]).lower()
        print("Blocked by bot check" if looks_blocked(html) else f"Status: {read_status(sys.argv[2])}")
        for _, phrases in STATUS_PHRASES:
            for p in phrases:
                i = html.find(p)
                if i >= 0:
                    print(f'  found "{p}": ...{re.sub(r"<[^>]+>", " ", html[max(0,i-60):i+60])}...')
    elif len(sys.argv) >= 3 and sys.argv[1] == "--loop":
        every = max(30, int(sys.argv[2]))
        while True:
            check_once()
            time.sleep(every)
    else:
        check_once()

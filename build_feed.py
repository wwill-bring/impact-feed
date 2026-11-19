#!/usr/bin/env python3
"""
Impact.com product catalog -> RSS + JSON feeds for the BMTN / BMTS story widget.

Downloads your Impact-format catalog(s) from Impact's FTP server (or reads local
files), splits products into topic feeds defined in feeds.json, and writes
docs/<feed>.xml (RSS) and docs/<feed>.json (used by widget.js) for GitHub Pages.

Standard library only. Settings come from environment variables
(GitHub Actions secrets/variables):

  IMPACT_FTP_HOST      FTP host shown under "Download via FTP" in Impact
  IMPACT_FTP_USER      username from Impact's credentials email
  IMPACT_FTP_PASS      password from Impact's credentials email
  CATALOG_MATCH        comma-separated parts of catalog file names to pull,
                       e.g. "Fanatics,Dicks". Blank = every catalog found.
  LOCAL_FILE           comma-separated local .csv/.txt/.gz/.zip files (skips FTP)
  TRACKING_TEMPLATE    optional; wraps plain store links, e.g.
                       https://cabelas.sjv.io/c/1409449/AD_ID/CAMPAIGN_ID?u={url}

  FEED_TITLE           e.g. "Bring Me The Deals"
  FEED_LINK            your site URL
  FEED_DESCRIPTION     one line about the feed
  MAX_ITEMS            products per feed (default 60)
  IN_STOCK_ONLY        "true" to drop out-of-stock items (default true)
  SORT_BY              "discount", "newest", or "none" (default discount)
"""
import csv, ftplib, gzip, io, json, os, sys, datetime, email.utils, html, zipfile, tempfile, urllib.parse
from xml.sax.saxutils import escape

csv.field_size_limit(10_000_000)
HERE = os.path.dirname(os.path.abspath(__file__))

# Column names, matched case-insensitively. Covers Impact Standardized Format
# and Google Merchant Center format (what Cabela's and many brands upload).
COLS = {
    "id":       ["unique merchant sku", "id", "sku", "catalog item id", "catalogitemid"],
    "name":     ["product name", "title", "name"],
    "desc":     ["product description", "description"],
    "url":      ["product url", "link", "url"],      # should be YOUR tracking link
    "image":    ["image url", "image_link", "imageurl"],
    "price":    ["current price", "sale_price", "currentprice"],
    "base":     ["price"],                           # GMC regular price
    "orig":     ["original price", "originalprice"],
    "currency": ["currency"],
    "brand":    ["manufacturer", "brand", "campaign name"],
    "stock":    ["stock availability", "availability", "stockavailability"],
    "category": ["category", "product_type", "product type", "google_product_category"],
    "ptype":    ["product type", "product_type", "google_product_category"],
    "labels":   ["labels", "custom_label_0", "custom_label_1"],
    "coupon":   ["coupon code"],
    "launch":   ["product launch date"],
}

# Optional: wrap plain store links in your Impact tracking link.
# Example: https://cabelas.sjv.io/c/1409449/123456/7890?u={url}
TRACKING_HINTS = ("sjv.io", "pxf.io", "7eer.net", "ojrq.net", "evyy.net", "/c/", "impact")


def env(k, d=""):
    return os.environ.get(k, d).strip()


def pick(row, key):
    for c in COLS[key]:
        v = row.get(c)
        if v not in (None, ""):
            return v.strip()
    return ""


# ---------- getting the catalog ----------

def ftp_connect():
    host, user, pw = env("IMPACT_FTP_HOST"), env("IMPACT_FTP_USER"), env("IMPACT_FTP_PASS")
    if not (host and user and pw):
        sys.exit("Missing IMPACT_FTP_HOST / IMPACT_FTP_USER / IMPACT_FTP_PASS")
    host = host.replace("ftp://", "").replace("sftp://", "").strip("/")
    try:
        ftp = ftplib.FTP_TLS(host, timeout=180)
        ftp.login(user, pw)
        ftp.prot_p()
    except Exception:
        ftp = ftplib.FTP(host, timeout=180)
        ftp.login(user, pw)
    return ftp


def fetch_from_ftp():
    ftp = ftp_connect()
    found = []

    def walk(path, depth):
        try:
            names = ftp.nlst(path)
        except ftplib.error_perm:
            return
        for n in names:
            full = n if "/" in n else f"{path.rstrip('/')}/{n}"
            base = full.rsplit("/", 1)[-1]
            if base in (".", ".."):
                continue
            if base.lower().endswith((".csv", ".txt", ".tsv", ".gz", ".zip")):
                found.append(full)
            elif depth < 3 and "." not in base:
                walk(full, depth + 1)

    walk("/", 0)
    print("Catalog files on FTP:\n  " + "\n  ".join(found or ["(none)"]))
    matches = [m.strip().lower() for m in env("CATALOG_MATCH").split(",") if m.strip()]
    if matches:
        found = [f for f in found if any(m in f.lower() for m in matches)]
    if not found:
        sys.exit("No catalog files matched (check CATALOG_MATCH).")

    files = []
    for target in found:
        print(f"Downloading {target}")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix="-" + target.rsplit("/", 1)[-1])
        ftp.retrbinary(f"RETR {target}", tmp.write, blocksize=1 << 20)
        tmp.close()
        files.append(tmp.name)
    ftp.quit()
    return files


def iter_rows(path):
    """Stream rows from a .csv/.txt/.tsv file, optionally gzipped or zipped."""
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic[:2] == b"PK":
        z = zipfile.ZipFile(path)
        inner = [n for n in z.namelist() if not n.endswith("/")][0]
        raw = io.TextIOWrapper(z.open(inner), encoding="utf-8-sig", errors="replace", newline="")
    elif magic[:2] == b"\x1f\x8b":
        raw = gzip.open(path, "rt", encoding="utf-8-sig", errors="replace", newline="")
    else:
        raw = open(path, "rt", encoding="utf-8-sig", errors="replace", newline="")
    with raw:
        first = raw.readline()
        counts = {d: first.count(d) for d in (",", "\t", "|")}
        delim = max(counts, key=counts.get)
        header = [h.strip().lower() for h in next(csv.reader([first], delimiter=delim))]
        print(f"{path}: columns {header}")
        for row in csv.DictReader(raw, fieldnames=header, delimiter=delim):
            yield row


# ---------- shaping products ----------

def num(s):
    """'19.99', '$19.99' or '19.99 USD' -> (19.99, 'USD' or '')."""
    parts = str(s).replace("$", "").replace(",", "").split()
    if not parts:
        return None, ""
    try:
        return float(parts[0]), (parts[1].upper() if len(parts) > 1 else "")
    except ValueError:
        return None, ""


def track(url):
    tpl = env("TRACKING_TEMPLATE")
    if not tpl or any(h in url.lower() for h in TRACKING_HINTS):
        return url
    return tpl.replace("{url}", urllib.parse.quote(url, safe=""))


def to_item(r):
    name, url = pick(r, "name"), pick(r, "url")
    if not name or not url:
        return None
    (price, cur1), (base, cur2), (orig, _) = num(pick(r, "price")), num(pick(r, "base")), num(pick(r, "orig"))
    if price is None:
        price = base
    elif orig is None:
        orig = base
    url = track(url)
    disc = round((orig - price) / orig * 100) if price and orig and orig > price else 0
    desc = pick(r, "desc")
    return {
        "id": pick(r, "id") or url,
        "name": name,
        "url": url,
        "image": pick(r, "image"),
        "brand": pick(r, "brand"),
        "price": price,
        "orig": orig if disc else None,
        "discount": disc,
        "currency": pick(r, "currency") or cur1 or cur2 or "USD",
        "coupon": pick(r, "coupon"),
        "category": pick(r, "category"),
        "desc": desc[:300] + ("…" if len(desc) > 300 else ""),
        "_stock": pick(r, "stock").lower().replace(" ", "").replace("_", ""),
        "_launch": pick(r, "launch"),
        "_text": " ".join([name, pick(r, "brand"), pick(r, "category"), pick(r, "ptype"), pick(r, "labels")]).lower(),
    }


def matches(item, rule):
    inc = [w.lower() for w in rule.get("include", [])]
    exc = [w.lower() for w in rule.get("exclude", [])]
    brands = [b.lower() for b in rule.get("brands", [])]
    t = item["_text"]
    if exc and any(w in t for w in exc):
        return False
    if brands and not any(b in item["brand"].lower() for b in brands):
        return False
    if inc and not any(w in t for w in inc):
        return False
    return True


def money(v, cur):
    sym = "$" if cur == "USD" else cur + " "
    return f"{sym}{v:,.2f}"


def write_rss(path, name, items, now):
    title = env("FEED_TITLE", "Product Picks") + ("" if name == "all" else f" — {name.title()}")
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        f"<title>{escape(title)}</title>",
        f"<link>{escape(env('FEED_LINK', 'https://bringmethenews.com'))}</link>",
        f"<description>{escape(env('FEED_DESCRIPTION', 'Latest products'))}</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for i in items:
        u = html.escape(i["url"])
        body = []
        if i["image"]:
            body.append(f'<p><a href="{u}" rel="sponsored nofollow"><img src="{html.escape(i["image"])}" alt="{html.escape(i["name"])}" style="max-width:100%"/></a></p>')
        if i["brand"]:
            body.append(f"<p><strong>{html.escape(i['brand'])}</strong></p>")
        if i["price"] is not None:
            line = money(i["price"], i["currency"])
            if i["discount"]:
                line += f" (was {money(i['orig'], i['currency'])}, {i['discount']}% off)"
            body.append(f"<p>{html.escape(line)}</p>")
        if i["coupon"]:
            body.append(f"<p>Code: <strong>{html.escape(i['coupon'])}</strong></p>")
        if i["desc"]:
            body.append(f"<p>{html.escape(i['desc'])}</p>")
        body.append(f'<p><a href="{u}" rel="sponsored nofollow">Shop now</a></p>')
        t = i["name"] + (f" — {i['discount']}% off" if i["discount"] else "")
        out += ["<item>", f"<title>{escape(t)}</title>", f"<link>{escape(i['url'])}</link>",
                f'<guid isPermaLink="false">{escape(i["id"])}</guid>', f"<pubDate>{now}</pubDate>",
                f"<description><![CDATA[{''.join(body)}]]></description>"]
        if i["category"]:
            out.append(f"<category>{escape(i['category'])}</category>")
        if i["image"]:
            out.append(f'<media:content url="{escape(i["image"])}" medium="image"/>')
        out.append("</item>")
    out += ["</channel>", "</rss>"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def main():
    local = [p.strip() for p in env("LOCAL_FILE").split(",") if p.strip()]
    files = local or fetch_from_ftp()

    in_stock_only = env("IN_STOCK_ONLY", "true").lower() == "true"
    ok_stock = ("", "instock", "yes", "true", "1", "limitedavailability", "preorder", "backorder")
    items, seen, total = [], set(), 0
    for path in files:
        for r in iter_rows(path):
            total += 1
            it = to_item(r)
            if not it or it["id"] in seen:
                continue
            if in_stock_only and it["_stock"] not in ok_stock:
                continue
            seen.add(it["id"])
            items.append(it)
    print(f"Read {total} catalog rows, kept {len(items)} in-stock products")
    if not items:
        sys.exit("No usable products found. Check the column names printed above.")
    sample = items[0]["url"]
    print(f"Sample product link: {sample}")
    if not any(h in sample.lower() for h in TRACKING_HINTS):
        print("WARNING: links don't look like Impact tracking links. Set TRACKING_TEMPLATE "
              "so you get credit for sales.")

    sort_by = env("SORT_BY", "discount").lower()
    if sort_by == "discount":
        items.sort(key=lambda i: i["discount"], reverse=True)
    elif sort_by == "newest":
        items.sort(key=lambda i: i["_launch"], reverse=True)

    cfg_path = os.path.join(HERE, "feeds.json")
    feeds = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {"all": {}}
    feeds.setdefault("all", {})
    max_items = int(env("MAX_ITEMS", "60") or 60)

    os.makedirs("docs", exist_ok=True)
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    now = email.utils.format_datetime(now_dt)
    for fname, rule in feeds.items():
        sel = [i for i in items if matches(i, rule)][: int(rule.get("max_items", max_items))]
        clean = [{k: v for k, v in i.items() if not k.startswith("_")} for i in sel]
        with open(f"docs/{fname}.json", "w", encoding="utf-8") as f:
            json.dump({"feed": fname, "updated": now_dt.isoformat(), "items": clean}, f, ensure_ascii=False)
        write_rss(f"docs/{fname}.xml", fname, sel, now)
        print(f"Feed '{fname}': {len(sel)} items -> docs/{fname}.json, docs/{fname}.xml")


if __name__ == "__main__":
    main()

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
  IMPACT_ACCOUNT_SID   Impact API Account SID  } if set, the API is used
  IMPACT_AUTH_TOKEN    Impact API Auth Token   } instead of FTP
  API_QUERY            optional API filter, e.g. "DiscountPercentage > 10"
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
import csv, ftplib, gzip, io, json, os, sys, datetime, email.utils, html, zipfile, tempfile, urllib.parse, urllib.request, urllib.error, base64, time, re
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
    host = host.replace("ftp://", "").replace("sftp://", "").split("/")[0].split("@")[-1]
    print(f"Connecting to {host} as {user} (password is {len(pw)} characters)")

    # Impact gives an ftp:// link, so try plain FTP first, then FTPS.
    errors = []
    for label, cls in (("FTP", ftplib.FTP), ("FTPS", ftplib.FTP_TLS)):
        ftp = None
        try:
            ftp = cls(timeout=180)
            ftp.connect(host, 21)
            print(f"{label} server says: {ftp.getwelcome()}")
            ftp.login(user, pw)
            if label == "FTPS":
                ftp.prot_p()
            ftp.set_pasv(True)
            print(f"Logged in with {label}")
            return ftp
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}: {e}")
            print(f"{label} login failed -> {type(e).__name__}: {e}")
            try:
                if ftp:
                    ftp.close()
            except Exception:
                pass
            time.sleep(5)
    sys.exit("Could not log in to Impact FTP.\n  " + "\n  ".join(errors) +
             "\nCheck IMPACT_FTP_USER / IMPACT_FTP_PASS (or click 'Reset FTP Password' in Impact).")


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


# ---------- Impact API (used when IMPACT_ACCOUNT_SID is set) ----------

API_BASE = "https://api.impact.com"
CATALOG_INFO = {}          # catalog id -> program label (filled while downloading)
BOOSTED = set()            # catalog ids whose products are shown first ("boost": true)
DEPT_FIELD = {}            # catalog id -> field that holds the store's department (catalogs.json "department_field")
SURVEY = {}                # catalog id -> {field: {value: count}} for spotting popularity fields
SURVEY_FIELDS = ("labels", "text1", "text2", "text3", "numeric1", "numeric2", "numeric3",
                 "money1", "money2", "money3", "productbid", "condition")
SURVEY_HINTS = ("rank", "rating", "review", "popular", "best", "seller", "featured", "score")


def api_get(path_or_url, sid, token, params=None, fatal_auth=True):
    url = path_or_url if path_or_url.startswith("http") else API_BASE + path_or_url
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}",
                                               "Accept": "application/json",
                                               "User-Agent": "impact-feed/1.0"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and fatal_auth:
                body = e.read().decode("utf-8", "replace")[:500]
                print(f"Impact API refused the login (HTTP {e.code}). Impact says: {body}")
                print(f"Account SID: {len(sid)} characters, starts with 'IR': {sid.startswith('IR')}; "
                      f"Auth Token: {len(token)} characters")
                sys.exit("Check IMPACT_ACCOUNT_SID / IMPACT_AUTH_TOKEN are a matching pair from Impact's API page.")
            if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                wait = 15 * (attempt + 1)
                print(f"  API {e.code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            raise


def _department_path(v):
    """Turn a store's category value into a clean department path, e.g.
    'Best Buy > TV & Home Theater > TVs' -> 'TV & Home Theater > TVs';
    '74-12, , Framed Art' -> 'Framed Art'."""
    v = (v or "").strip()
    if not v:
        return ""
    if ">" in v:
        parts = [x.strip() for x in v.split(">") if x.strip()]
        if parts and parts[0].lower() in ("best buy", "home", "all"):
            parts = parts[1:]
        return " > ".join(parts).split(",")[0].strip()
    if "~" in v:
        return " > ".join(x.strip() for x in v.split("~") if x.strip())
    if "," in v:
        parts = [x.strip() for x in v.split(",") if x.strip() and not re.fullmatch(r"[\d-]+", x.strip())]
        return parts[-1] if parts else ""
    return v


def _flatten(it, program):
    row = {}
    for k, v in it.items():
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v if not isinstance(x, (dict, list)))
        elif isinstance(v, dict) or v is None:
            v = ""
        row[k.lower()] = str(v)
    row["_program"] = program
    cid = str(it.get("CatalogId", ""))
    row["_boost"] = "1" if cid in BOOSTED else ""
    row["_deptpath"] = _department_path(row.get(DEPT_FIELD.get(cid, "category"), "") or row.get("category", ""))
    row["_cattext"] = " | ".join(row.get(k, "") for k in ("category", "subcategory", "text1", "text2", "text3") if row.get(k))
    # Tally a sample of custom fields so we can see which ones signal popularity
    if cid:
        cat = SURVEY.setdefault(cid, {"_n": 0})
        if cat["_n"] < 3000:
            cat["_n"] += 1
            for k, v in row.items():
                if v and not k.startswith("_") and (k in SURVEY_FIELDS or any(h in k for h in SURVEY_HINTS)):
                    vals = cat.setdefault(k, {})
                    vals[v[:40]] = vals.get(v[:40], 0) + 1
    return row


def _keyword_ok(it, kw):
    """Every word of the keyword must appear in the product's name/category/description."""
    text = " ".join(str(it.get(k) or "") for k in ("Name", "Category", "SubCategory", "Description", "Labels")).lower()
    return all(re.search(r"\b" + re.escape(w), text) for w in kw.lower().split())


def load_catalog_config():
    """catalogs.json (optional) picks catalogs by ID and can limit some to keywords."""
    path = os.path.join(HERE, "catalogs.json")
    if not os.path.exists(path):
        return None
    cfg = json.load(open(path, encoding="utf-8"))
    return {str(k): v for k, v in cfg.get("catalogs", cfg).items() if not str(k).startswith("_")}


def iter_api_rows():
    sid, token = env("IMPACT_ACCOUNT_SID"), env("IMPACT_AUTH_TOKEN")
    if not (sid and token):
        sys.exit("Missing IMPACT_ACCOUNT_SID / IMPACT_AUTH_TOKEN")
    base = f"/Mediapartners/{sid}"
    data = api_get(f"{base}/Catalogs", sid, token, {"PageSize": 1000})
    all_cats = data.get("Catalogs", [])
    print("Catalogs on your account:")
    for c in all_cats:
        print(f"  {c.get('Id')}: {c.get('Name')} ({c.get('AdvertiserName')}, {c.get('NumberOfItems')} items)")

    conf = load_catalog_config()
    for c in all_cats:
        o = (conf or {}).get(str(c.get("Id")), {})
        CATALOG_INFO[str(c.get("Id"))] = {
            "program": " ".join(dict.fromkeys(x for x in (o.get("label", ""), str(c.get("AdvertiserName") or ""),
                       str(c.get("CampaignName") or ""), str(c.get("Name") or "")) if x)).strip(),
            "selected": bool(conf) and str(c.get("Id")) in conf}
    if conf:
        BOOSTED.update(k for k, v in conf.items() if isinstance(v, dict) and v.get("boost"))
        DEPT_FIELD.update({k: v["department_field"].lower() for k, v in conf.items()
                           if isinstance(v, dict) and v.get("department_field")})
        print(f"Using catalogs.json ({len(conf)} catalogs)")
        cats = [c for c in all_cats if str(c.get("Id")) in conf]
        missing = set(conf) - {str(c.get("Id")) for c in cats}
        if missing:
            print(f"WARNING: these catalog IDs aren't on your account: {sorted(missing)}")
    else:
        conf = {}
        matches_ = [m.strip().lower() for m in env("CATALOG_MATCH").split(",") if m.strip()]
        def hit(c, m):
            if m.isdigit():                      # a catalog ID must match exactly
                return str(c.get("Id")) == m
            text = " ".join(str(c.get(k, "")) for k in ("Name", "AdvertiserName", "CampaignName"))
            return m.replace("'", "") in text.lower().replace("'", "")
        cats = [c for c in all_cats if not matches_ or any(hit(c, m) for m in matches_)]
    if not cats:
        sys.exit("No catalogs selected (check catalogs.json or CATALOG_MATCH).")

    limit = int(env("API_MAX_PER_CATALOG", "20000") or 20000)   # Impact caps paging at 20,000

    def search_catalog(cid, name, program, kw, max_pages, scan_fallback):
        """Impact keyword search, kept to one catalog; optional full-catalog scan fallback."""
        got = 0
        print(f"Searching catalog {cid} ({name}) for '{kw}'")
        try:
            url, page_no, pages = f"{base}/Catalogs/ItemSearch", 1, 0
            params = {"Keyword": kw, "PageSize": 100, "Page": 1, "Query": f"CatalogId = {cid}"}
            while url and pages < max_pages:
                try:
                    page = api_get(url, sid, token, params, fatal_auth=False)
                except urllib.error.HTTPError as e:
                    if pages == 0 and params and "Query" in params and e.code in (400, 422):
                        # This account doesn't accept a catalog filter: search everything instead
                        params = {k: v for k, v in params.items() if k != "Query"}
                        continue
                    raise
                items = page.get("Items", [])
                if pages == 0 and params and "Query" in params and not items:
                    # Catalog filter returned nothing; try again searching everything
                    params = {k: v for k, v in params.items() if k != "Query"}
                    continue
                pages += 1
                for it in items:
                    if str(it.get("CatalogId")) == cid and _keyword_ok(it, kw):
                        yield _flatten(it, program); got += 1
                nxt = page.get("@nextpageuri")
                if nxt:
                    url, params = nxt, None
                elif len(items) >= 100 and params is not None:
                    page_no += 1; params = dict(params, Page=page_no)
                else:
                    url = ""
            print(f"  search found {got} matching items")
        except urllib.error.HTTPError as e:
            print(f"  keyword search not available (HTTP {e.code})")
        if got == 0 and scan_fallback:
            print(f"  scanning catalog {cid} for '{kw}' instead")
            url, params, scanned = f"{base}/Catalogs/{cid}/Items", {"PageSize": 1000}, 0
            while url and scanned < limit:
                try:
                    page = api_get(url, sid, token, params)
                except urllib.error.HTTPError as e:
                    print(f"  stopped at {scanned} items (HTTP {e.code})"); break
                params = None
                for it in page.get("Items", []):
                    scanned += 1
                    if _keyword_ok(it, kw):
                        yield _flatten(it, program); got += 1
                url = page.get("@nextpageuri") or ""
            print(f"  scanned {scanned}, kept {got}")

    query = env("API_QUERY")                                    # optional, e.g. DiscountPercentage > 10
    for c in cats:
        cid = str(c.get("Id"))
        opts = conf.get(cid, {})
        label = opts.get("label", "")
        # Program label = our label + brand + campaign + catalog name ("cabela", "nflshop", "target" all work)
        program = " ".join(dict.fromkeys(x for x in (label, str(c.get("AdvertiserName") or ""),
                           str(c.get("CampaignName") or ""), str(c.get("Name") or "")) if x)).strip()
        keywords = opts.get("keywords", [])

        if keywords:
            # Only products matching these keywords (e.g. "Minnesota Vikings")
            for kw in keywords:
                yield from search_catalog(cid, label or c.get("Name"), program, kw,
                                          int(opts.get("search_pages", 200)),
                                          scan_fallback=not opts.get("boost"))
            continue

        if opts.get("pull", True) is False:
            print(f"Skipping general download for catalog {cid} ({label or c.get('Name')}); using its searches")
            for kw in opts.get("search", []):
                yield from search_catalog(cid, label or c.get("Name"), program, kw,
                                          int(opts.get("search_pages", 50)), scan_fallback=False)
            continue

        print(f"Fetching items from catalog {cid} ({program})")
        params = {"PageSize": 1000}
        if query:
            params["Query"] = query
        url, got = f"{base}/Catalogs/{cid}/Items", 0
        while url and got < limit:
            try:
                page = api_get(url, sid, token, params)
            except urllib.error.HTTPError as e:
                print(f"  stopped paging at {got} items (HTTP {e.code})")
                break
            params = None                      # next-page URI already has them
            for it in page.get("Items", []):
                yield _flatten(it, program)
                got += 1
            url = page.get("@nextpageuri") or ""
        print(f"  got {got} items")
        # Optional extra keyword searches on top of the general pull (e.g. Target holiday items)
        for kw in opts.get("search", []):
            yield from search_catalog(cid, label or c.get("Name"), program, kw,
                                      int(opts.get("search_pages", 50)), scan_fallback=False)


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


def _destination(url):
    """The store page behind a tracking link (its u= parameter), else the link itself."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return q["u"][0] if "u" in q else url


def _norm_url(u):
    u = (u or "").strip().lower().split("#")[0].split("?")[0]
    for pre in ("https://", "http://", "www."):
        if u.startswith(pre):
            u = u[len(pre):]
    return u.rstrip("/")


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
        "department": (r.get("_deptpath") or "").split(" > ")[0],
        "_deptpath": (r.get("_deptpath") or "").lower(),
        "_cattext": (r.get("_cattext") or "").lower(),
        "_rate": None,
        "desc": desc[:300] + ("…" if len(desc) > 300 else ""),
        "_stock": pick(r, "stock").lower().replace(" ", "").replace("_", ""),
        "_launch": pick(r, "launch"),
        "program": r.get("_program", ""),
        "_cid": r.get("catalogid", ""),
        "_boost": r.get("_boost") == "1",
        "_sku": (r.get("catalogitemid") or r.get("unique merchant sku") or r.get("sku") or "").strip().lower(),
        "_dest": _norm_url(_destination(url)),
        "_text": " ".join([name, pick(r, "brand"), pick(r, "category"), pick(r, "ptype"), pick(r, "labels")]).lower(),
    }


def matches(item, rule):
    inc = [w.lower() for w in rule.get("include", [])]
    exc = [w.lower() for w in rule.get("exclude", [])]
    brands = [b.lower() for b in rule.get("brands", [])]
    programs = [p.lower().replace("'", "") for p in rule.get("programs", [])]
    if programs and not any(p in item.get("program", "").lower().replace("'", "") for p in programs):
        return False
    depts = [d.lower() for d in rule.get("departments", [])]
    if depts and not any(d in item.get("_deptpath", "") or d in item.get("_cattext", "") for d in depts):
        return False
    if "min_payout" in rule and (item.get("_rate") is None or item["_rate"] < float(rule["min_payout"])):
        return False
    t = item["_text"]
    has = lambda w: re.search(r"\b" + re.escape(w), t) is not None   # word-start match
    if exc and any(has(w) for w in exc):
        return False
    if brands and not any(b in item["brand"].lower() for b in brands):
        return False
    if inc and not any(has(w) for w in inc):
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
        if i.get("department") or i["category"]:
            out.append(f"<category>{escape(i.get('department') or i['category'])}</category>")
        if i["image"]:
            out.append(f'<media:content url="{escape(i["image"])}" medium="image"/>')
            # Plain image URL for Mailchimp's *|FEEDITEM:ENCLOSURE_URL|* tag
            ext = i["image"].split("?")[0].rsplit(".", 1)[-1].lower()
            mime = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
            out.append(f'<enclosure url="{escape(i["image"])}" type="{mime}" length="0"/>')
        out.append("</item>")
    out += ["</channel>", "</rss>"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


# ---------- hand-picked products (picks.json) ----------

def _parse_pick(p):
    p = str(p).strip()
    out = {"raw": p, "cid": "", "sku": "", "dest": "", "text": ""}
    if p.lower().startswith("http"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(p).query)
        dest = q["u"][0] if "u" in q else p
        out["dest"] = _norm_url(dest)
        out["sku"] = (q.get("prodsku", [""])[0]).lower()
        m = re.search(r"APIG_(\d+)", p)
        out["cid"] = m.group(1) if m else ""
        if not out["sku"]:
            m = re.search(r"/A-(\d+)", dest)            # Target product pages
            out["sku"] = m.group(1) if m else ""
    elif re.fullmatch(r"\d+:\S+", p):                   # catalog:item, e.g. 530:94307167
        out["cid"], out["sku"] = p.split(":", 1)
        out["sku"] = out["sku"].lower()
    else:
        out["text"] = p                                  # a product name to search for
    return out


def resolve_picks(items, in_stock_only, ok_stock):
    path = os.path.join(HERE, "picks.json")
    if not os.path.exists(path):
        return {}
    conf = {k: v for k, v in json.load(open(path, encoding="utf-8")).items() if not k.startswith("_")}
    if not conf:
        return {}
    by_cid_sku, by_sku, by_dest, by_id = {}, {}, {}, {}
    for i in items:
        if i["_sku"]:
            by_cid_sku[(str(i["_cid"]), i["_sku"])] = i
            by_sku.setdefault(i["_sku"], i)
        if i["_dest"]:
            by_dest.setdefault(i["_dest"], i)
        by_id[str(i["id"]).lower()] = i
    sid, token = env("IMPACT_ACCOUNT_SID"), env("IMPACT_AUTH_TOKEN")

    def api_item(it_json, cid):
        info = CATALOG_INFO.get(str(cid), {})
        it = to_item(_flatten(it_json, info.get("program", "")))
        return it

    def lookup(pk):
        if pk["sku"]:
            hit = by_cid_sku.get((pk["cid"], pk["sku"])) or (by_sku.get(pk["sku"]) if not pk["cid"] else None)
            if hit:
                return hit
        if pk["dest"] and pk["dest"] in by_dest:
            return by_dest[pk["dest"]]
        if pk["raw"].lower() in by_id:
            return by_id[pk["raw"].lower()]
        if not (sid and token):
            return None
        base = f"/Mediapartners/{sid}/Catalogs"
        try:
            if pk["cid"] and pk["sku"]:
                try:
                    data = api_get(f"{base}/{pk['cid']}/Items/{urllib.parse.quote(pk['sku'])}", sid, token, fatal_auth=False)
                    data = (data.get("Items") or [data])[0] if isinstance(data, dict) else None
                    if data and data.get("Name"):
                        return api_item(data, pk["cid"])
                except urllib.error.HTTPError:
                    pass
                data = api_get(f"{base}/ItemSearch", sid, token,
                               {"Keyword": pk["sku"], "PageSize": 100, "Query": f"CatalogId = {pk['cid']}"}, fatal_auth=False)
                for it in data.get("Items", []):
                    if str(it.get("CatalogId")) == pk["cid"] and pk["sku"] in (
                            str(it.get("CatalogItemId", "")).lower(), str(it.get("Id", "")).lower().split("-")[-1]):
                        return api_item(it, pk["cid"])
            term = pk["text"] or ""
            if term:
                data = api_get(f"{base}/ItemSearch", sid, token, {"Keyword": term, "PageSize": 100}, fatal_auth=False)
                for it in data.get("Items", []):
                    cid = str(it.get("CatalogId"))
                    if CATALOG_INFO.get(cid, {}).get("selected") and _keyword_ok(it, term):
                        cand = api_item(it, cid)
                        if cand and (not in_stock_only or cand["_stock"] in ok_stock):
                            return cand
        except urllib.error.HTTPError as e:
            print(f"    lookup error (HTTP {e.code})")
        return None

    result = {}
    for fname, spec in conf.items():
        entries = spec.get("items", []) if isinstance(spec, dict) else spec
        chosen, seen_ids = [], set()
        print(f"Picks '{fname}': {len(entries)} listed")
        for raw in entries:
            pk = _parse_pick(raw)
            it = lookup(pk)
            if not it:
                print(f"  not found: {pk['raw'][:90]}")
                continue
            if in_stock_only and it["_stock"] not in ok_stock:
                print(f"  out of stock, skipped: {it['name'][:70]}")
                continue
            if it["id"] in seen_ids:
                continue
            seen_ids.add(it["id"])
            chosen.append(it)
        result[fname] = chosen
    return result


# ---------- estimated payouts (payout rules from each brand's contract) ----------

def load_payout_rules():
    """Rules come from the PAYOUT_RULES secret (keeps contract rates private), else payouts.json."""
    raw = env("PAYOUT_RULES")
    if not raw:
        path = os.path.join(HERE, "payouts.json")
        if os.path.exists(path):
            raw = open(path, encoding="utf-8").read()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        print(f"WARNING: payout rules aren't valid JSON ({e}); ignoring them")
        return {}
    return {str(k): v for k, v in data.items() if not str(k).startswith("_")}


def _rule_hits(item, cond):
    brand = (item.get("brand") or "").lower()
    cats = item.get("_cattext", "") + " | " + item.get("_deptpath", "")
    for key, val in cond.items():
        val = str(val).lower()
        if key == "brand" and re.search(r"(?<![a-z0-9])" + re.escape(val.strip()) + r"(?![a-z0-9])", brand) is None:
            return False
        if key in ("category", "subcategory", "text1", "text2", "text3") and not _starts_word(val, cats):
            return False
    return True


def _starts_word(val, text):
    """True if val appears in text starting at a word boundary ('art' matches 'Art Prints', not 'Smart')."""
    return re.search(r"(?<![a-z0-9])" + re.escape(val.strip()), text) is not None


def apply_payouts(items):
    rules = load_payout_rules()
    if not rules:
        return items
    kept, dropped = [], {}
    for i in items:
        r = rules.get(str(i.get("_cid")))
        if not r:
            kept.append(i)
            continue
        cats = i.get("_cattext", "") + " | " + i.get("_deptpath", "")
        if any(_starts_word(str(x).lower(), cats) for x in r.get("none", [])):
            rate = None
        else:
            hits = [float(x["rate"]) for x in r.get("rules", [])
                    if _rule_hits(i, {k: v for k, v in x.items() if k != "rate"})]
            base = float(r.get("base", 0))
            promo = [h for h in hits if h > base]
            # A higher-paying rule wins; otherwise the lowest matching rule (e.g. 0% or 1%); otherwise the base rate
            rate = max(promo) if promo else (min(hits) if hits else base)
        i["_rate"] = rate
        if r.get("drop_zero", True) and not rate:
            dropped[i["_cid"]] = dropped.get(i["_cid"], 0) + 1
            continue
        kept.append(i)
    for cid, n in dropped.items():
        print(f"Payout rules: left out {n} catalog {cid} products that earn no commission")
    return kept


# ---------- service brands without product catalogs (offers.json) ----------

def _first(d, *keys):
    for k in keys:
        for kk in (k, k.lower()):
            v = d.get(kk)
            if v not in (None, "", [], {}):
                return v
    return ""


def build_offers():
    """Feeds made from a brand's Impact ads (banners and text links), for services like JustAnswer or Rocket Lawyer
    that don't have product catalogs. offers.json: {"feed-name": {"campaigns": [9320, ...], "items": [manual offers]}}"""
    path = os.path.join(HERE, "offers.json")
    if not os.path.exists(path):
        return {}
    conf = {k: v for k, v in json.load(open(path, encoding="utf-8")).items() if not k.startswith("_")}
    sid, token = env("IMPACT_ACCOUNT_SID"), env("IMPACT_AUTH_TOKEN")
    today = datetime.date.today().isoformat()
    result, cache = {}, {}
    for fname, spec in conf.items():
        offers = []
        for camp in spec.get("campaigns", []):
            camp = str(camp.get("id") if isinstance(camp, dict) else camp)
            if camp not in cache:
                ads = []
                if sid and token:
                    url, params = f"/Mediapartners/{sid}/Ads", {"CampaignId": camp, "PageSize": 200}
                    try:
                        while url and len(ads) < 1000:
                            page = api_get(url, sid, token, params, fatal_auth=False)
                            ads += page.get("Ads", []) or page.get("ads", [])
                            url, params = page.get("@nextpageuri") or "", None
                    except urllib.error.HTTPError as e:
                        print(f"  ads for campaign {camp}: HTTP {e.code}")
                if ads:
                    print(f"Offers: campaign {camp} has {len(ads)} ads; ad fields: {sorted(ads[0].keys())[:25]}")
                cache[camp] = ads
            for ad in cache[camp]:
                link = _first(ad, "TrackingLink", "TrackingUrl", "Url")
                if not link or str(_first(ad, "AdType", "Type")).upper() in ("COUPON_ONLY", "PRODUCT_CATALOG"):
                    continue
                end = str(_first(ad, "EndDate", "EndDateTime"))[:10]
                if end and end < today:
                    continue
                w, h = num(_first(ad, "Width"))[0] or 0, num(_first(ad, "Height"))[0] or 0
                image = _first(ad, "CreativeUrl", "ImageUrl", "IconUrl")
                if image and (w and w < 200 or h and h < 100):        # skip tiny banners
                    image = ""
                title = _first(ad, "LinkText", "Name", "Description")
                offers.append({"id": f"ad-{ad.get('Id', link)}", "name": str(title)[:140], "url": link, "image": image,
                               "brand": _first(ad, "CampaignName") or spec.get("label", ""),
                               "price": None, "orig": None, "discount": 0, "currency": "USD",
                               "coupon": _first(ad, "DiscountCode", "PromoCode"), "category": spec.get("label", ""),
                               "department": spec.get("label", ""), "desc": str(_first(ad, "Description"))[:300],
                               "program": spec.get("label", ""), "_score": (1 if image else 0, -abs((w or 600) - 600))})
        for m in spec.get("items", []):          # offers typed in by hand (tracking link from Impact)
            if m.get("url") and m.get("title"):
                offers.append({"id": "manual-" + m["url"][-40:], "name": m["title"], "url": m["url"], "image": m.get("image", ""),
                               "brand": m.get("brand", spec.get("label", "")), "price": None, "orig": None, "discount": 0,
                               "currency": "USD", "coupon": m.get("coupon", ""), "category": spec.get("label", ""),
                               "department": spec.get("label", ""), "desc": m.get("description", ""),
                               "program": spec.get("label", ""), "_score": (2, 0)})
        offers.sort(key=lambda o: o["_score"], reverse=True)
        seen, out = set(), []
        for o in offers:
            key = o["name"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(o)
        result[fname] = out[: int(spec.get("max_items", 20))]
    return result


def main():
    local = [p.strip() for p in env("LOCAL_FILE").split(",") if p.strip()]
    downloaded = []
    if local:
        rows_iter = (r for path in local for r in iter_rows(path))
    elif env("IMPACT_ACCOUNT_SID"):
        rows_iter = iter_api_rows()
    else:
        downloaded = fetch_from_ftp()
        rows_iter = (r for path in downloaded for r in iter_rows(path))

    in_stock_only = env("IN_STOCK_ONLY", "true").lower() == "true"
    ok_stock = ("", "instock", "yes", "true", "1", "limitedavailability", "preorder", "backorder")
    items, seen, total = [], set(), 0
    for r in rows_iter:
        total += 1
        it = to_item(r)
        if not it or it["id"] in seen:
            continue
        if in_stock_only and it["_stock"] not in ok_stock:
            continue
        seen.add(it["id"])
        items.append(it)
    for path in downloaded:      # delete big temp downloads
        try:
            os.remove(path)
        except OSError:
            pass
    print(f"Read {total} catalog rows, kept {len(items)} in-stock products")
    if not items:
        sys.exit("No usable products found. Check the column names printed above.")
    sample = items[0]["url"]
    print(f"Sample product link: {sample}")
    if not any(h in sample.lower() for h in TRACKING_HINTS):
        print("WARNING: links don't look like Impact tracking links. Set TRACKING_TEMPLATE "
              "so you get credit for sales.")

    items = apply_payouts(items)

    sort_by = env("SORT_BY", "discount").lower()
    if sort_by == "discount":
        items.sort(key=lambda i: i["discount"], reverse=True)
    elif sort_by == "newest":
        items.sort(key=lambda i: i["_launch"], reverse=True)
    items.sort(key=lambda i: i["_boost"], reverse=True)   # "boost" catalogs (e.g. Fanatics Top Products) first

    if SURVEY:
        print("Field survey (sample of each catalog; looking for popularity signals):")
        for cid, fields in SURVEY.items():
            name = CATALOG_INFO.get(cid, {}).get("program", cid)[:45]
            parts = []
            for k, vals in fields.items():
                if k == "_n":
                    continue
                top = sorted(vals.items(), key=lambda kv: -kv[1])[:3]
                parts.append(f"{k}=" + " | ".join(f"{v} ({n})" for v, n in top))
            print(f"  {cid} {name}: " + ("; ".join(parts) if parts else "no custom fields filled"))

    cfg_path = os.path.join(HERE, "feeds.json")
    feeds = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {"all": {}}
    feeds.setdefault("all", {})
    max_items = int(env("MAX_ITEMS", "60") or 60)

    os.makedirs("docs", exist_ok=True)
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    now = email.utils.format_datetime(now_dt)
    for fname, rule in feeds.items():
        cap = int(rule.get("max_items", max_items))
        sel, names = [], set()
        pool = items
        if rule.get("sort") == "payout":      # highest estimated commission first, then biggest discount
            pool = sorted(items, key=lambda i: (i["_boost"], i["_rate"] or 0, i["discount"]), reverse=True)
        for i in pool:
            if len(sel) >= cap:
                break
            key = i["name"].strip().lower()
            if key not in names and matches(i, rule):
                names.add(key)
                sel.append(i)
        if rule.get("fill") and len(sel) < cap:
            # Top up with the same stores' best deals when there aren't enough matches
            base_rule = {k: v for k, v in rule.items() if k in ("programs", "brands", "exclude")}
            have = {i["id"] for i in sel}
            sel += [i for i in items if i["id"] not in have and matches(i, base_rule)][: cap - len(sel)]
        clean = [{k: v for k, v in i.items() if not k.startswith("_")} for i in sel]
        with open(f"docs/{fname}.json", "w", encoding="utf-8") as f:
            json.dump({"feed": fname, "updated": now_dt.isoformat(), "items": clean}, f, ensure_ascii=False)
        write_rss(f"docs/{fname}.xml", fname, sel, now)
        print(f"Feed '{fname}': {len(sel)} items -> docs/{fname}.json, docs/{fname}.xml")

    for fname, sel in build_offers().items():
        clean = [{k: v for k, v in i.items() if not k.startswith("_")} for i in sel]
        with open(f"docs/{fname}.json", "w", encoding="utf-8") as f:
            json.dump({"feed": fname, "updated": now_dt.isoformat(), "items": clean}, f, ensure_ascii=False)
        write_rss(f"docs/{fname}.xml", fname, sel, now)
        print(f"Offers feed '{fname}': {len(sel)} items -> docs/{fname}.json, docs/{fname}.xml")

    for fname, sel in resolve_picks(items, in_stock_only, ok_stock).items():
        if fname in feeds:
            print(f"WARNING: picks list '{fname}' has the same name as a feeds.json topic; the picks list wins")
        clean = [{k: v for k, v in i.items() if not k.startswith("_")} for i in sel]
        with open(f"docs/{fname}.json", "w", encoding="utf-8") as f:
            json.dump({"feed": fname, "updated": now_dt.isoformat(), "items": clean}, f, ensure_ascii=False)
        write_rss(f"docs/{fname}.xml", fname, sel, now)
        print(f"Picks feed '{fname}': {len(sel)} items -> docs/{fname}.json, docs/{fname}.xml")


if __name__ == "__main__":
    main()

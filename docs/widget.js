/*!
 * BMTN / BMTS product widget
 * Usage (WordPress Custom HTML block):
 *   <div class="bmtn-deals" data-feed="sports" data-count="4" data-title="Shop the story"></div>
 *   <script src="https://YOUR-USERNAME.github.io/impact-feed/widget.js" async></script>
 *
 * Options (all optional, set on the <div>):
 *   data-feed        feed name from feeds.json (default "all")
 *   data-count       number of products (default 4)
 *   data-title       heading text (default "Shop the deals"); data-title="" hides it
 *   data-shuffle     "false" to always show the top products in order (default: random pick from the top 24)
 *   data-brand       only show products whose brand contains this text
 *   data-program     only show products from one affiliate program, e.g. "cabela"
 *   data-disclosure  replace the disclosure sentence
 *   data-color       button and sale-badge color as a hex code, e.g. "0b4f5c" (default teal)
 */
(function () {
  // Loaded twice (e.g. once per story block)? Just scan the page again.
  if (window.__bmtnDeals) { window.__bmtnDeals.scan(); return; }
  var script = document.currentScript ||
    [].slice.call(document.getElementsByTagName("script")).filter(function (s) {
      return /widget\.js(\?|$)/.test(s.src);
    }).pop();
  var BASE = script ? script.src.replace(/widget\.js.*$/, "") : "";

  var DISCLOSURE = "We may earn a commission when you buy through links on this page.";

  if (!document.getElementById("bmtn-deals-css")) {
    var css = document.createElement("style");
    css.id = "bmtn-deals-css";
    css.textContent = [
      ".bmtn-deals{--bd-accent:#0b4f5c;--bd-border:#e2e2e2;--bd-muted:#666;--bd-bg:#fff;font-family:inherit;margin:1.75em 0;clear:both}",
      ".bmtn-deals__head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;border-top:3px solid var(--bd-accent);padding-top:10px;margin-bottom:12px}",
      ".bmtn-deals__title{font-size:1.15em;font-weight:800;margin:0;line-height:1.2}",
      ".bmtn-deals__disc{font-size:.75em;color:var(--bd-muted);margin:0 0 12px}",
      ".bmtn-deals__grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}",
      ".bmtn-deals__card{display:flex;flex-direction:column;background:var(--bd-bg);border:1px solid var(--bd-border);border-radius:8px;overflow:hidden;text-decoration:none!important;color:inherit!important;transition:box-shadow .15s,transform .15s}",
      ".bmtn-deals__card:hover{box-shadow:0 4px 14px rgba(0,0,0,.12);transform:translateY(-2px)}",
      ".bmtn-deals__img{position:relative;aspect-ratio:1/1;background:#f5f5f5;display:flex;align-items:center;justify-content:center}",
      ".bmtn-deals__img img{width:100%;height:100%;object-fit:contain;padding:8px;box-sizing:border-box;margin:0!important}",
      ".bmtn-deals__badge{position:absolute;top:8px;left:8px;background:var(--bd-accent);color:#fff;font-size:.72em;font-weight:700;padding:3px 7px;border-radius:4px}",
      ".bmtn-deals__body{padding:10px 12px 12px;display:flex;flex-direction:column;gap:4px;flex:1}",
      ".bmtn-deals__brand{font-size:.72em;text-transform:uppercase;letter-spacing:.04em;color:var(--bd-muted)}",
      ".bmtn-deals__name{font-size:.88em;font-weight:600;line-height:1.3;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}",
      ".bmtn-deals__price{margin-top:auto;padding-top:6px;font-weight:800;font-size:.95em}",
      ".bmtn-deals__was{font-weight:400;color:var(--bd-muted);text-decoration:line-through;font-size:.85em;margin-left:6px}",
      ".bmtn-deals__code{font-size:.75em;color:var(--bd-accent);font-weight:700}",
      ".bmtn-deals__btn{margin-top:8px;text-align:center;background:var(--bd-accent);color:#fff;font-weight:700;font-size:.82em;padding:7px 10px;border-radius:5px}",
      "@media (max-width:480px){.bmtn-deals__grid{grid-template-columns:repeat(2,1fr);gap:10px}}"
    ].join("");
    document.head.appendChild(css);
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function money(v, cur) {
    if (v == null) return "";
    try { return new Intl.NumberFormat("en-US", { style: "currency", currency: cur || "USD" }).format(v); }
    catch (e) { return "$" + Number(v).toFixed(2); }
  }
  function shuffle(a) {
    for (var i = a.length - 1; i > 0; i--) { var j = Math.floor(Math.random() * (i + 1)); var t = a[i]; a[i] = a[j]; a[j] = t; }
    return a;
  }

  var cache = {};
  function load(feed) {
    if (!cache[feed]) {
      cache[feed] = fetch(BASE + encodeURIComponent(feed) + ".json", { cache: "no-cache" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
    }
    return cache[feed];
  }

  function render(el) {
    if (el.getAttribute("data-bd-done")) return;
    el.setAttribute("data-bd-done", "1");
    var feed = el.getAttribute("data-feed") || "all";
    var count = parseInt(el.getAttribute("data-count"), 10) || 4;
    var title = el.hasAttribute("data-title") ? el.getAttribute("data-title") : "Shop the deals";
    var brand = (el.getAttribute("data-brand") || "").toLowerCase();
    var program = (el.getAttribute("data-program") || "").toLowerCase().replace(/'/g, "");
    var doShuffle = el.getAttribute("data-shuffle") !== "false";
    var disclosure = el.getAttribute("data-disclosure") || DISCLOSURE;
    var color = (el.getAttribute("data-color") || "").replace(/^#/, "");
    if (/^[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(color)) el.style.setProperty("--bd-accent", "#" + color);

    load(feed).then(function (data) {
      var items = (data.items || []).filter(function (i) {
        return i.url && (!brand || (i.brand || "").toLowerCase().indexOf(brand) > -1) &&
          (!program || (i.program || "").toLowerCase().replace(/'/g, "").indexOf(program) > -1);
      });
      if (doShuffle) items = shuffle(items.slice(0, Math.max(24, count)));
      items = items.slice(0, count);
      if (!items.length) { el.style.display = "none"; return; }

      var cards = items.map(function (i) {
        var img = i.image
          ? '<img src="' + esc(i.image) + '" alt="' + esc(i.name) + '" loading="lazy" decoding="async">'
          : "";
        return '<a class="bmtn-deals__card" href="' + esc(i.url) + '" target="_blank" rel="sponsored nofollow noopener">' +
          '<div class="bmtn-deals__img">' + img +
            (i.discount ? '<span class="bmtn-deals__badge">' + i.discount + '% off</span>' : "") + "</div>" +
          '<div class="bmtn-deals__body">' +
            (i.brand ? '<div class="bmtn-deals__brand">' + esc(i.brand) + "</div>" : "") +
            '<div class="bmtn-deals__name">' + esc(i.name) + "</div>" +
            (i.price != null ? '<div class="bmtn-deals__price">' + money(i.price, i.currency) +
              (i.orig ? '<span class="bmtn-deals__was">' + money(i.orig, i.currency) + "</span>" : "") + "</div>" : "") +
            (i.coupon ? '<div class="bmtn-deals__code">Code: ' + esc(i.coupon) + "</div>" : "") +
            '<div class="bmtn-deals__btn">Shop now</div>' +
          "</div></a>";
      }).join("");

      el.innerHTML =
        (title ? '<div class="bmtn-deals__head"><p class="bmtn-deals__title">' + esc(title) + "</p></div>" : "") +
        '<p class="bmtn-deals__disc">' + esc(disclosure) + "</p>" +
        '<div class="bmtn-deals__grid">' + cards + "</div>";
    }).catch(function () { el.style.display = "none"; });
  }

  function init() { [].forEach.call(document.querySelectorAll(".bmtn-deals"), render); }
  window.__bmtnDeals = { scan: init };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
  // Pages built by JavaScript (headless CMS front ends) can add the box after this script runs,
  // so keep watching for new boxes.
  if (window.MutationObserver) {
    var pending = false;
    new MutationObserver(function () {
      if (pending) return;
      pending = true;
      setTimeout(function () { pending = false; init(); }, 150);
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
})();

# How to change the product feeds

Every feed lives at `https://wwill-bring.github.io/impact-feed/<name>.xml`.
The feeds rebuild every morning, and you can also run them now from **Actions → Build product RSS feed → Run workflow**.
To edit a file on GitHub, open it, click the pencil icon, make the change, and click **Commit changes**.

---

## 1. Category feeds: `feeds.json`

Each entry creates one feed, with up to 60 products sorted by biggest discount.

```json
"vikings-jerseys": {"programs": ["nfl shop"], "include": ["jersey"]}
```

| Setting | What it does | Example |
|---|---|---|
| `programs` | Which stores the products can come from | `["target"]`, `["best buy"]`, `["nfl shop", "mlb shop"]` |
| `include` | The product must mention **at least one** of these words | `["ornament"]`, `["tv", "oled"]` |
| `exclude` | Leave out products mentioning any of these | `["mount", "stand"]` |
| `fill` | `true` tops the feed up with the store's best deals when there aren't enough matches | `true` |
| `max_items` | How many products, if not 60 | `20` |
| `departments` | Only products in these store departments (matches the department name) | `["appliances"]`, `["headphones"]` |
| `sort` | `"payout"` = highest commission **rate** first; `"earnings"` = highest estimated **dollars per sale** (rate × price) first. Both need payout rules. | `"earnings"` |
| `min_payout` | Only products earning at least this % (needs payout rules) | `7` |

Store names for `programs`: `target`, `best buy`, `cabela`, `banana republic`, `gap factory`, `athleta`, `nfl shop`, `nhl shop`, `nba store`, `mlb shop`, `wnba`.

**Tips**
- Words match the start of a word, so `hat` also matches "hats" and `cap` also matches "captain". Use `exclude` to drop mistakes.
- Feed names should be lowercase, with dashes instead of spaces.
- Put a comma between entries. A missing comma breaks the file, and the run log will show an error.

## 2. Hand-picked products: `picks.json`

Each list becomes a feed with the same name, for example `gift-guide-2026.xml`. Products stay in the order you list them, and sold-out products are skipped automatically.

```json
"gift-guide-2026": [
  "https://goto.target.com/c/1409449/78775/2092?prodsku=94307167&u=...",
  "https://www.target.com/p/some-product/-/A-12345678",
  "21610:6543210",
  "Justin Jefferson Jersey"
]
```

Each product can be any of these:
- an **Impact tracking link** (copy it from Impact)
- a **store product page link** (works when the product is in the daily download)
- a **catalog:item ID**, like `530:94307167` (the catalog number, then the item's ID or SKU)
- a **product name**. This takes the first in-stock match from your selected stores, so check the result.

The run log lists anything it couldn't find under `not found:`.

## 3. Stores and searches: `catalogs.json`

- **Add or remove a store:** add or remove its catalog number. The run log lists every catalog on the account.
- `keywords`: the store contributes **only** matching products. The team shops use this, e.g. `"Minnesota Vikings"`.
- `search`: **extra** searches on top of the normal 20,000-product download. Use this for seasonal or niche items, e.g. `"ice fishing"`. Each search adds a little run time. Remove seasonal searches after the season.

- `pull: false`: skip the general 20,000-product download and use **only** the `search` terms. Best Buy and Target work this way, so they focus on mainstream, well-paying departments instead of a random slice.
- `department_field`: which field holds the store's department (Best Buy `category`, Target and Gap `labels`, Fanatics `text2`). Every product in the feeds gets a `department`.

## 4. Payout rules: the `PAYOUT_RULES` secret (not a file in this repo)

Each brand's Impact contract says what each department or brand pays. The script uses those rules to:
- **leave out products that earn nothing** (for example Best Buy music, gift cards and Apple; Target gift cards), and
- **rank `"sort": "payout"` feeds** by estimated commission.

The rates are contract terms, so keep them **private**: store the JSON in **Settings → Secrets and variables → Actions → Secrets → `PAYOUT_RULES`**, not in this public repo. The feeds never show rates.
When a brand sends updated terms, edit the rules and paste the new JSON into the secret.

## 5. Service brands: `offers.json` (JustAnswer, Rocket Lawyer)

Services don't have product catalogs, so their feeds are built from the **ads** each brand provides in Impact (banners and text links, with your tracking links):

```json
"justanswer": {"label": "JustAnswer", "campaigns": [9320], "max_items": 10}
```

- `campaigns`: the brand's Impact program number (shown on its contract, e.g. JustAnswer (9320), Rocket Lawyer (59223)).
- Expired ads and tiny banners are skipped automatically.
- To add an offer by hand, use `"items": [{"title": "...", "url": "<Impact tracking link>", "image": "...", "description": "..."}]`.

- **Your own headlines:** brands often give ads internal names like "Business Registration Design 3". Replace them under `_titles`, matching **part** of the ad name:
  ```json
  "_titles": {"Business Registration": "Starting a business? Form your LLC with Rocket Lawyer"}
  ```
  One entry covers every version of that ad ("Design 2", "Design 3", "Static"), and duplicates collapse into one card.
- **Automatic cleanup:** even without `_titles`, anything after a spaced dash or bar (" - 320x320 banner", " | v2") is dropped, as long as a real headline is left; hyphens inside words like "24-hour" stay. Banner sizes ("336x280"), "Banner"/"Static"/"Design 3", and "@ website.com" are stripped from ad names, and joined words like "ImprovementExpert" are split. Real numbers such as "24/7" or "2026" are kept.
- **Hide an ad:** add part of its name to `_hide`, e.g. `"_hide": ["Static"]`.
- The run log lists each ad's name under **Offers:** so you can see what to rename.

Feeds: `justanswer`, `rocketlawyer`, and `services` (both).

## Using a feed

- **beehiiv:** add it as an external RSS feed, then use an RSS block in the post.
- **Mailchimp:** in the Code block, change the feed name in the `*|FEEDBLOCK:...|*` line.
- **Website widget:** `<div class="bmtn-deals" data-feed="vikings-jerseys" data-count="4"></div>`

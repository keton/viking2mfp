# viking-to-myfitnesspal

Copies **Kuchnia Vikinga** ("box diet") meals into the **MyFitnessPal** food diary.

For every delivered day in a date range it reads each meal's calories, carbohydrate,
fat and protein from Kuchnia Vikinga and writes one MyFitnessPal *quick add* diary
entry per meal into the matching meal slot.

```
viking_to_mfp.py --from 2026-10-14 --to 2026-10-20
```

## What it needs

* Python 3.9 or newer (written for 3.9+, developed and tested on 3.14)
* `requests` — `pip install -r requirements.txt`
* A logged-in browser session for both sites (see below)

Optional, only needed if you log in with a Chromium-family browser *and* want the
script to read its cookie database:

* `cryptography` (AES decryption of Chrome's cookie values)
* `secretstorage` (fetches the "Chrome Safe Storage" key from the session keyring)

Firefox-family browsers (Firefox, Zen, LibreWolf) store cookie values in plain text
and need no extra packages. Both optional packages are commented out in
`requirements.txt`.

## Logging in

Both services are used through their **web** APIs, so the script needs the same
cookies your browser gets when you log in.

Any run does this on demand — `viking_to_mfp.py --from … --to …` is enough, there
is no separate login step. For each site in turn it checks the stored session
against the live API, and only then decides:

| Situation | What happens |
|---|---|
| stored session still valid | used silently |
| no stored session, but the browser is logged in | adopted from the browser, no tab opened |
| stored session expired | discarded, and the login page opens for that site |
| not logged in anywhere | the login page opens, and it waits for you |

Each site is decided independently, so an expired Kuchnia Vikinga session never
sends you back through a MyFitnessPal login you do not need. If a login is needed
the script also refreshes the cached session afterwards, so the next run is silent
again.

When it does need a login:

1. opens that site's login page in your default browser,
2. waits for you to log in (your password manager can fill the form in),
3. reads the session cookies out of your browser's stores and caches them in
   `~/.config/kv2mfp/`,
4. repeats for the other site if it needs one too.

If a login does not arrive within `--login-timeout` (180s) the script stops with an
error, prints every cookie store it could read, and tells you how to paste a cookie
instead.

Note that `--dry-run` still needs the MyFitnessPal session, because the meal-slot
mapping comes from your diary's own meal names.

### Where the cookies come from

Firefox and its forks (**including Zen**) do *not* write cookies that have no expiry
to `cookies.sqlite` — that database only ever holds cookies with an explicit expiry.
Kuchnia Vikinga's panel session is exactly such a session cookie, so it never appears
there and cannot be read from that file at any point.

The script therefore reads **both** places:

| Store | Holds |
|---|---|
| `cookies.sqlite` | persistent cookies — e.g. MyFitnessPal's `__Secure-next-auth.session-token` |
| `sessionstore-backups/recovery.jsonlz4` | session cookies, kept for session restore — e.g. Kuchnia Vikinga's `SESSION` |

The session store is LZ4-compressed (`mozLz4`); a small decoder is built in, so no
extra package is needed.

Chromium-family browsers (Chrome, Brave, Edge, Vivaldi) keep session cookies in their
`Cookies` database, but the values are AES-encrypted. Reading those needs
`cryptography` plus, usually, `secretstorage` for the keyring key.

`--dump-cookies` prints exactly what is readable on this machine, per store and
profile, and is the first thing to run if a login is not picked up:

```
viking_to_mfp.py --dump-cookies
viking_to_mfp.py --dump-cookies kuchniavikinga.pl,myfitnesspal.com
```

### Pasting a cookie instead

If the script cannot read your browser's cookies (unusual browser, encrypted
profile, remote/headless box) it tells you so. Copy the header instead from dev
tools → Network → any request to the site → Request headers → **Cookie**:

```
viking_to_mfp.py --from 2026-10-14 \
  --cookie kv='SESSION=...' \
  --cookie mfp='__Secure-next-auth.session-token=...'
```

`--cookie` also accepts `@/path/to/file` or a JSON object, and `KV_COOKIE` /
`MFP_COOKIE` environment variables work too.

`--login-only` refreshes both sessions and stops without touching the diary.

## Usage

| Command | Effect |
|---|---|
| `viking_to_mfp.py --from 2026-10-14 --to 2026-10-20` | copy that window |
| `viking_to_mfp.py --from 2026-10-01` | copy from that date up to the last delivery Kuchnia Vikinga knows about |
| `viking_to_mfp.py --from 2026-10-14 --dry-run` | print the table, write nothing |
| `viking_to_mfp.py --login-only` | just refresh the two sessions |
| `viking_to_mfp.py --print-state` | show what earlier runs already pushed |
| `viking_to_mfp.py --from 2026-10-01 --force` | re-send meals a previous run pushed |

Without `--yes` the script prints the full plan (date, KV meal, target MFP meal,
macros, dish name, totals) and asks before writing anything.

Every run records what it pushed in `~/.local/state/kv2mfp/pushed.json`, keyed by
date + meal + macros, so running it twice does not duplicate entries. `--no-state`
disables that, `--force` ignores it.

### Meal slots

Kuchnia Vikinga serves five meals a day and the script lines them up with your
MyFitnessPal diary's meal names, which it reads from your account:

| Kuchnia Vikinga | MyFitnessPal (this account) |
|---|---|
| `Śniadanie` | Breakfast |
| `II śniadanie` | Second Breakfast |
| `Obiad` | Lunch |
| `Podwieczorek` | Teatime |
| `Kolacja` | Supper |

Mapping is by position in the diary's meal list, not by the names above, so a
renamed or reordered diary still works. The mapping is printed before anything is
written. Override it if needed:

```
viking_to_mfp.py --meal-map 'śniadanie=Breakfast,ii śniadanie=Snack'
```

## Safety

The Kuchnia Vikinga side is strictly read-only: `KVClient` only has GET calls and no
generic request method, so the script cannot start, modify, cancel or pay for an
order. The API is the same one the panel and the mobile app use, and every request
is a plain GET.

The MyFitnessPal side is restricted by an explicit allow-list — the only write the
script can perform is a diary quick-add entry:

```python
ALLOWED = {
    ("GET",  "/api/auth/csrf"),
    ("GET",  "/api/services/users"),
    ("POST", "/api/services/diary"),
}
```

Anything else (deleting a diary entry, editing food, logging exercise, ...) raises
`PermissionError` before a request is made.

## API contracts used

Recovered from the shipped apps and web front-ends, not from documentation.

### Kuchnia Vikinga

A Dietly whitelabel. A React Native app (`pl.kuchniavikinga.app` 3.9.1, Hermes
bytecode) and a Vite web panel, both against a Spring Boot backend. The panel is
served from two hostnames that front the same backend and share its session store:
`https://panel.kuchniavikinga.pl/api` (where the web "user panel" links lead) and
`https://aplikacja.kuchniavikinga.pl/api`. The script tries `panel.` first and falls
back to the other; `--kv-base` overrides it.

Each hostname gets its *own* host-only `SESSION` cookie, so cookies must be sent
per host — the script keeps every cookie's host and applies browser matching rules
rather than merging them by name.

* auth — cookie session (`SESSION`, `remember-me`) created by
  `POST /api/auth/login` (`application/x-www-form-urlencoded`, `username` +
  `password`). Wrong credentials answer `401 Bad credentials`.
* CSRF — Spring double-submit cookie: the `XSRF-TOKEN` cookie echoed back in the
  `X-XSRF-TOKEN` header (writes only).
* headers — `X-Launcher-Type: BROWSER_PANEL`, `company-id: kuchniavikinga`.

| Call | Purpose |
|---|---|
| `GET /company/customer/client` | session probe |
| `GET /company/customer/order/all` | every order on the account |
| `GET /company/customer/order/{orderId}` | order detail incl. `deliveries[]` with `deliveryId` + `date` |
| `GET /company/general/menus/delivery/{deliveryId}/new` | `deliveryMenuMeal[]` for that day |

Each `deliveryMenuMeal` entry carries `mealName`, `menuMealName`, `amount` and
`nutrition` (`calories`, `protein`, `carbohydrate`, `fat`, `dietaryFiber`, `sugar`,
`salt`, `saturatedFattyAcids`). Nutrition is per portion, so `amount` is applied
when it is greater than one. Multiple active order IDs are handled by walking
`order/all` rather than assuming a single order.

### MyFitnessPal

A Next.js + NextAuth web app. Base: `https://www.myfitnesspal.com`.

* auth — NextAuth session cookie `__Secure-next-auth.session-token`.
* CSRF — `GET /api/auth/csrf` → `csrfToken`, echoed back in `X-CSRF-Token`.
* `GET /api/services/users?fields[]=...` → `diary_preferences.meal_names`.
* `POST /api/services/diary` with exactly what the `/food/quick-add` page posts:

```json
{"items": [{
  "type": "quick_add",
  "date": "2026-10-26",
  "meal_name": "Breakfast",
  "nutritional_contents": {
    "fat": 11.4,
    "carbohydrates": 55,
    "protein": 18.2,
    "energy": {"value": 412, "unit": "calories"}
  }
}]}
```

## Verification

```
python3 tests/test_pipeline.py
```

Runs the real script against two in-process servers that replay the contracts
described above and asserts on what was actually sent: the exact quick-add JSON,
the date filtering, `amount` multiplication, meal-slot mapping, the read-only
guarantees on both clients, duplicate suppression, the failure mode for a dead
session, and the `mozLz4` session-store decoder against a real LZ4 fixture. No
network access and no accounts needed.

`--dry-run` plus the confirmation table is the safe way to sanity-check a range
against your real accounts before writing anything.

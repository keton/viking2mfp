#!/usr/bin/env python3
"""Copy Kuchnia Vikinga "box diet" meals into the MyFitnessPal food diary.

Both sides are used exactly the way their own web front-ends use them:

Kuchnia Vikinga (Dietly whitelabel, https://aplikacja.kuchniavikinga.pl)
    * Spring Boot / Spring Security backend at  https://aplikacja.kuchniavikinga.pl/api
    * auth  = cookie session (``SESSION`` / ``remember-me``) obtained from the panel login
    * CSRF  = double submit cookie ``XSRF-TOKEN`` echoed back as ``X-XSRF-TOKEN``
    * reads used (GET only, there is no code path here that can write):
        GET  /company/customer/client                                 (session probe)
        GET  /company/customer/order/all                              (every order id)
        GET  /company/customer/order/{orderId}                        (order + deliveries[].date)
        GET  /company/general/menus/delivery/{deliveryId}/new         (meals + nutrition)

MyFitnessPal (https://www.myfitnesspal.com)
    * auth  = NextAuth session cookie ``__Secure-next-auth.session-token``
    * CSRF  = ``X-CSRF-Token`` from ``GET /api/auth/csrf``
    * calls used:
        GET  /api/services/users?fields[]=...                         (diary_preferences.meal_names)
        POST /api/services/diary   {"items": [...]}                   (the "quick add" entry)

Neither service is written to outside the MFP diary: the KV client is physically
GET-only and the MFP client refuses any request outside an explicit allow-list.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

try:
    import requests
except ImportError:  # pragma: no cover - basic UX guard
    sys.stderr.write(
        "This script needs the 'requests' package:  python3 -m pip install requests\n"
    )
    raise SystemExit(2)


# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

KV_API = "https://panel.kuchniavikinga.pl/api"
KV_ORIGIN = "https://panel.kuchniavikinga.pl"
KV_LOGIN_URL = f"{KV_ORIGIN}/"
# The panel is reachable on two hostnames that serve the same backend and share
# its session store; panel. is the one the web "user panel" links redirect to.
KV_BASES = (
    "https://panel.kuchniavikinga.pl/api",
    "https://aplikacja.kuchniavikinga.pl/api",
)
KV_COMPANY_ID = "kuchniavikinga"
KV_LAUNCHER = "BROWSER_PANEL"
KV_HOST_SUFFIX = "kuchniavikinga.pl"

MFP_ORIGIN = "https://www.myfitnesspal.com"
MFP_LOGIN_URL = f"{MFP_ORIGIN}/account/login"
MFP_HOST_SUFFIX = "myfitnesspal.com"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kv2mfp"
KV_COOKIE_CACHE = CONFIG_DIR / "kv_cookies.json"
MFP_COOKIE_CACHE = CONFIG_DIR / "mfp_cookies.json"

# How many deliveries in a row with no published menu before the walk gives up.
DEFAULT_MENU_GAP = 3

# Kuchnia Vikinga meal slots, in the order a box diet serves them.
KV_MEAL_ORDER = ("śniadanie", "ii śniadanie", "obiad", "podwieczorek", "kolacja")
# spellings that mean the same slot
KV_MEAL_ALIASES = {
    "śniadanie": 0,
    "sniadanie": 0,
    "ii śniadanie": 1,
    "ii sniadanie": 1,
    "2 śniadanie": 1,
    "2 sniadanie": 1,
    "drugie śniadanie": 1,
    "obiad": 2,
    "lunch": 2,
    "podwieczorek": 3,
    "deser": 3,
    "kolacja": 4,
    "supper": 4,
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def log(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"error: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def norm_key(text: Any) -> str:
    """Lowercase, strip accents/extra spaces - for comparing Polish meal names."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKD", str(text))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("ł", "l").replace("Ł", "L")
    return re.sub(r"\s+", " ", s).strip().lower()


def as_number(value: Any, default: float = 0.0) -> float:
    """KV sends some nutrition numbers as strings ('23.5'), some as floats."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ".").replace("\xa0", "").strip())
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    return int(round(as_number(value, default)))


def round_half_up(value: float, digits: int = 0) -> float:
    factor = 10 ** digits
    return float(int(value * factor + 0.5)) / factor


def parse_iso_date(text: str) -> date:
    parsed = try_parse_iso_date(text)
    if parsed is None:
        die(f"unrecognised date {text!r}; use YYYY-MM-DD")
    return parsed


def try_parse_iso_date(text: str) -> date | None:
    text = str(text).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class CookieRecord:
    """One cookie, keeping the host it belongs to.

    The host matters: KV issues a *different* ``SESSION`` for
    ``panel.kuchniavikinga.pl`` and ``aplikacja.kuchniavikinga.pl``. Collapsing
    cookies into a name -> value mapping silently sends the wrong one.
    """

    name: str
    value: str
    host: str = ""          # "" = send to whatever host the client talks to
    path: str = "/"
    secure: bool = True
    http_only: bool = False


def cookie_key(record: CookieRecord) -> tuple[str, str, str]:
    return (record.host, record.name, record.path)


def cookie_matches(record: CookieRecord, host: str) -> bool:
    if not record.host:
        return True
    host = host.lower()
    if record.host.startswith("."):          # domain cookie: any subdomain
        domain = record.host[1:].lower()
        return host == domain or host.endswith("." + domain)
    return record.host.lower() == host       # host-only


def cookie_jar(
    records: Sequence[CookieRecord], host: str, secure: bool = True
) -> "RequestsCookieJar":
    """A jar holding just the cookies that apply to ``host``.

    Putting them in the session's jar (rather than a hand-built ``Cookie``
    header) matters: a manual header is only sent because requests leaves an
    explicit header alone, and in exchange nothing the server sets with
    ``Set-Cookie`` is ever sent back.
    """
    from http.cookiejar import Cookie
    from requests.cookies import RequestsCookieJar

    jar = RequestsCookieJar()
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        if not cookie_matches(record, host):
            continue
        key = (record.host or host, record.path or "/", record.name)
        if key in seen:
            # Store order is precedence: the persistent copy from cookies.sqlite
            # comes before any copy in the session-restore store.
            continue
        seen.add(key)
        domain = record.host or host
        jar.set_cookie(
            Cookie(
                version=0, name=record.name, value=record.value or "",
                port=None, port_specified=False,
                domain=domain.lstrip("."), domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=record.path or "/", path_specified=True,
                secure=record.secure and secure, expires=None, discard=False,
                comment=None, comment_url=None, rest={}, rfc2109=False,
            )
        )
    return jar


def cookie_header(records: Iterable[CookieRecord]) -> str:
    return "; ".join(f"{r.name}={r.value}" for r in records)


def host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


def json_get(mapping: Any, *path: str, default: Any = None) -> Any:
    cur = mapping
    for key in path:
        if isinstance(cur, dict):
            if key not in cur:
                return default
            cur = cur[key]
        elif isinstance(cur, list) and key.isdigit():
            idx = int(key)
            if idx >= len(cur):
                return default
            cur = cur[idx]
        else:
            return default
    return cur


def first_present(mapping: dict, keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        if isinstance(mapping, dict) and key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


# --------------------------------------------------------------------------- #
# browser cookie harvesting
# --------------------------------------------------------------------------- #

FIREFOX_ROOTS = (
    "~/.mozilla/firefox",
    "~/.zen",
    "~/.config/.mozilla/zen",
    "~/.librewolf",
    "~/.waterfox",
    "~/snap/firefox/common/.mozilla/firefox",
    "~/.var/app/org.mozilla.firefox/.mozilla/firefox",
    "~/.var/app/app.zen_browser.zen/.zen",
    "~/Library/Application Support/Firefox/Profiles",
    "~/Library/Application Support/zen/Profiles",
)

CHROMIUM_ROOTS = (
    "~/.config/google-chrome",
    "~/.config/google-chrome-beta",
    "~/.config/chromium",
    "~/.config/BraveSoftware/Brave-Browser",
    "~/.config/microsoft-edge",
    "~/.config/vivaldi",
    "~/.config/opera",
    "~/snap/chromium/common/chromium",
    "~/.var/app/com.google.Chrome/config/google-chrome",
    "~/.var/app/org.chromium.Chromium/config/chromium",
    "~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
    "~/Library/Application Support/Google/Chrome",
)


def _copy_sqlite(path: Path) -> Path | None:
    """Copy a possibly-locked browser database somewhere we can read it."""
    if not path.exists():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="kv2mfp-"))
    target = tmp / "copy.sqlite"
    try:
        shutil.copy2(path, target)
    except OSError as exc:
        warn(f"cannot copy {path}: {exc}")
        return None
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            try:
                shutil.copy2(side, Path(str(target) + suffix))
            except OSError:
                pass
    return target


def firefox_cookie_dbs() -> list[Path]:
    dbs: list[Path] = []
    for root in FIREFOX_ROOTS:
        base = Path(os.path.expanduser(root))
        if base.is_dir():
            dbs.extend(sorted(base.glob("*/cookies.sqlite")))
    return dbs


def chromium_cookie_dbs() -> list[tuple[Path, Path]]:
    """(user-data-dir, cookie database) for every Chromium-family profile found."""
    dbs: list[tuple[Path, Path]] = []
    for root in CHROMIUM_ROOTS:
        user_data = Path(os.path.expanduser(root))
        if user_data.is_dir():
            dbs.extend((user_data, db) for db in sorted(user_data.glob("*/Cookies")))
    return dbs


def firefox_profiles() -> list[Path]:
    """Directories that actually look like a Firefox-family profile."""
    markers = ("cookies.sqlite", "sessionstore.jsonlz4", "sessionstore-backups")
    profiles: list[Path] = []

    def looks_like_profile(path: Path) -> bool:
        return any((path / marker).exists() for marker in markers)

    for root in FIREFOX_ROOTS:
        base = Path(os.path.expanduser(root))
        if not base.is_dir():
            continue
        if looks_like_profile(base):
            profiles.append(base)
        profiles.extend(
            sorted(p for p in base.iterdir() if p.is_dir() and looks_like_profile(p))
        )
    return profiles


SESSIONSTORE_FILES = (
    "sessionstore-backups/recovery.jsonlz4",
    "sessionstore.jsonlz4",
    "sessionstore-backups/previous.jsonlz4",
    "sessionstore-backups/upgrade.jsonlz4",
)


def _lz4_block_decompress(src: bytes) -> bytes:
    """Minimal LZ4 block decoder, so mozlz4 files need no extra package."""
    out = bytearray()
    pos = 0
    size = len(src)
    while pos < size:
        token = src[pos]
        pos += 1
        literals = token >> 4
        if literals == 15:
            while True:
                byte = src[pos]
                pos += 1
                literals += byte
                if byte != 255:
                    break
        out += src[pos:pos + literals]
        pos += literals
        if pos >= size:
            break
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        match = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                byte = src[pos]
                pos += 1
                match += byte
                if byte != 255:
                    break
        start = len(out) - offset
        for k in range(match):          # byte-wise: LZ4 matches may overlap
            out.append(out[start + k])
    return bytes(out)


def _read_mozlz4(path: Path) -> bytes | None:
    """mozLz40\\0 + uint32 LE uncompressed size + LZ4 block."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"mozLz40\x00"):
        return data  # some builds write plain JSON here
    payload = data[12:]
    size = int.from_bytes(data[8:12], "little")
    try:
        import lz4.block  # type: ignore

        return lz4.block.decompress(payload, uncompressed_size=size)
    except Exception:
        pass
    try:
        out = _lz4_block_decompress(payload)
    except Exception as exc:
        warn(f"{path}: cannot decompress session store ({exc})")
        return None
    if size and len(out) != size:
        warn(f"{path}: session store size mismatch ({len(out)} != {size})")
    return out


def harvest_firefox_sessionstore(
    suffixes: Sequence[str], quiet: bool = True
) -> list[CookieRecord]:
    """Session cookies live here, not in cookies.sqlite.

    Firefox and its forks never write cookies without an expiry to
    ``cookies.sqlite`` while running; they keep them in the session-restore store
    so they survive a crash. Kuchnia Vikinga's panel session is exactly that kind
    of cookie, so this is the only place it can be read from.

    Files are visited freshest-first and the first record for a given
    (host, name, path) wins, so a stale ``previous.jsonlz4`` cannot shadow the
    live session in ``recovery.jsonlz4``.
    """
    found: list[CookieRecord] = []
    seen: set[tuple[str, str, str]] = set()
    for profile in firefox_profiles():
        for name in SESSIONSTORE_FILES:
            path = profile / name
            if not path.exists():
                continue
            blob = _read_mozlz4(path)
            if not blob:
                continue
            try:
                data = json.loads(blob)
            except ValueError:
                continue
            hits = 0
            for cookie in data.get("cookies", []) or []:
                host = str(cookie.get("host") or "")
                if not any(suffix in host for suffix in suffixes):
                    continue
                cname = cookie.get("name")
                if not cname:
                    continue
                record = CookieRecord(
                    str(cname), cookie.get("value") or "", host,
                    str(cookie.get("path") or "/"),
                    bool(cookie.get("secure")), bool(cookie.get("httponly")),
                )
                key = cookie_key(record)
                if key in seen:
                    continue
                seen.add(key)
                found.append(record)
                hits += 1
            if hits and not quiet:
                log(f"  session-restore store: {path} ({hits} cookie(s))")
    return found


def _host_clause(column: str, suffixes: Sequence[str]) -> tuple[str, list[str]]:
    return (
        " OR ".join(f"{column} LIKE ?" for _ in suffixes),
        [f"%{suffix}" for suffix in suffixes],
    )


def harvest_firefox(suffixes: Sequence[str], quiet: bool = True) -> list[CookieRecord]:
    found: list[CookieRecord] = []
    clause, params = _host_clause("host", suffixes)
    for db in firefox_cookie_dbs():
        copy = _copy_sqlite(db)
        if not copy:
            continue
        try:
            con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            rows = con.execute(
                f"SELECT host, name, value, path, isSecure, isHttpOnly "
                f"FROM moz_cookies WHERE {clause}",
                params,
            ).fetchall()
            con.close()
        except sqlite3.Error as exc:
            warn(f"{db}: {exc}")
            continue
        for host, name, value, path, secure, http_only in rows:
            if value is None:
                continue
            found.append(CookieRecord(name, value, host or "", path or "/",
                                      bool(secure), bool(http_only)))
        if rows and not quiet:
            log(f"  firefox-family: {db} ({len(rows)} matching row(s))")
    return found


def _chromium_key(user_data_dir: Path) -> bytes | None:
    """Derive the AES key Chrome uses for Linux cookie encryption."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError:
        return None

    password: bytes | None = None
    local_state = user_data_dir / "Local State"
    if local_state.exists():
        try:
            blob = json.loads(local_state.read_text("utf-8"))
            enc = blob.get("os_crypt", {}).get("encrypted_key")
            if enc:
                raw = base64.b64decode(enc)
                if raw[:5] == b"DPAPI":  # windows-shaped key, useless here
                    raw = raw[5:]
                password = raw
        except Exception:
            pass

    if password is None:
        # gnome-keyring / kwallet via Secret Service
        try:
            import secretstorage

            conn = secretstorage.dbus_init()
            collection = secretstorage.get_default_collection(conn)
            for item in collection.get_all_items():
                label = item.get_label() or ""
                if label in ("Chrome Safe Storage", "Chromium Safe Storage",
                             "Brave Safe Storage", "Microsoft Edge Safe Storage",
                             "Vivaldi Safe Storage"):
                    password = item.get_secret()
                    break
        except Exception:
            password = None

    if password is None:
        password = b"peanuts"  # Chrome's --password-store=basic fallback

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1
    )
    return kdf.derive(password)


def _decrypt_chromium(enc: bytes, key: bytes | None) -> str | None:
    if not enc:
        return ""
    if enc[:3] in (b"v10", b"v11"):
        if key is None:
            return None
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
            plain = dec.update(enc[3:]) + dec.finalize()
            plain = plain[: -plain[-1]] if plain else plain
            return plain.decode("utf-8", "replace")
        except Exception:
            return None
    if enc[:3] == b"v20":  # app-bound encryption, not decryptable offline
        return None
    try:
        return enc.decode("utf-8")
    except UnicodeDecodeError:
        return None


def harvest_chromium(suffixes: Sequence[str], quiet: bool = True) -> list[CookieRecord]:
    found: list[CookieRecord] = []
    clause, params = _host_clause("host_key", suffixes)
    for user_data, db in chromium_cookie_dbs():
        copy = _copy_sqlite(db)
        if not copy:
            continue
        try:
            con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            rows = con.execute(
                f"SELECT host_key, name, encrypted_value, path, is_secure, is_httponly "
                f"FROM cookies WHERE {clause}",
                params,
            ).fetchall()
            con.close()
        except sqlite3.Error as exc:
            warn(f"{db}: {exc}")
            continue
        if not rows:
            continue
        key = _chromium_key(user_data)
        readable = 0
        for host, name, enc, path, secure, http_only in rows:
            value = _decrypt_chromium(bytes(enc or b""), key)
            if not value:
                continue
            found.append(CookieRecord(name, value, host or "", path or "/",
                                      bool(secure), bool(http_only)))
            readable += 1
        if not quiet:
            log(f"  chromium-family: {db} ({readable}/{len(rows)} readable)")
    return found


def harvest_cookies(suffixes: Sequence[str], quiet: bool = True) -> list[CookieRecord]:
    cookies = harvest_firefox(suffixes, quiet)
    cookies += harvest_firefox_sessionstore(suffixes, quiet)
    cookies += harvest_chromium(suffixes, quiet)
    return cookies


def dump_cookies(suffixes: Sequence[str]) -> None:
    """Show what is readable on this machine - used to explain a failed login."""
    log("\n--- cookie stores found on this machine ---")
    profiles = firefox_profiles()
    chromium = chromium_cookie_dbs()
    if not profiles and not chromium:
        log("  none - no browser profile in a standard location")
    for profile in profiles:
        stores = [
            str(profile / name)
            for name in ("cookies.sqlite", *SESSIONSTORE_FILES)
            if (profile / name).exists()
        ]
        log(f"  firefox-family  {profile}")
        if not stores:
            log("                    (no cookie database and no session store)")
        for store in stores:
            log(f"                    {store}")
    for user_data, db in chromium:
        log(f"  chromium-family {db}")

    log(f"\n--- cookies whose host contains {', '.join(suffixes)} ---")
    log(f"  {'store':<9} {'host':<34} {'name':<36} {'expires':<14} flags")
    log("  " + "-" * 100)
    total = 0
    for db in firefox_cookie_dbs():
        copy = _copy_sqlite(db)
        if not copy:
            continue
        try:
            con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            clause, params = _host_clause("host", suffixes)
            rows = con.execute(
                f"SELECT host, name, expiry, isSecure, isHttpOnly FROM moz_cookies "
                f"WHERE {clause} ORDER BY host, name",
                params,
            ).fetchall()
            con.close()
        except sqlite3.Error as exc:
            warn(f"{db}: {exc}")
            continue
        for host, name, expiry, secure, http_only in rows:
            total += 1
            log(
                f"  {'ff':<9} {host:<34} {name:<36} "
                f"{'session' if not expiry else expiry:<14} "
                f"{' '.join(f for f, on in (('secure', secure), ('httpOnly', http_only)) if on)}"
            )

    for user_data, db in chromium_cookie_dbs():
        copy = _copy_sqlite(db)
        if not copy:
            continue
        try:
            con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            clause, params = _host_clause("host_key", suffixes)
            rows = con.execute(
                f"SELECT host_key, name, expires_utc, is_secure, is_httponly, encrypted_value "
                f"FROM cookies WHERE {clause} ORDER BY host_key, name",
                params,
            ).fetchall()
            con.close()
        except sqlite3.Error as exc:
            warn(f"{db}: {exc}")
            continue
        key = _chromium_key(user_data)
        for host, name, expires, secure, http_only, enc in rows:
            total += 1
            readable = _decrypt_chromium(bytes(enc or b""), key)
            flags = []
            if not expires:
                flags.append("session")
            if secure:
                flags.append("secure")
            if http_only:
                flags.append("httpOnly")
            if readable is None:
                flags.append("NOT-DECRYPTABLE")
            log(
                f"  {'chromium':<9} {host:<34} {name:<36} "
                f"{'session' if not expires else expires:<14} {' '.join(flags)}"
            )

    for profile in firefox_profiles():
        for name in SESSIONSTORE_FILES:
            path = profile / name
            if not path.exists():
                continue
            blob = _read_mozlz4(path)
            if blob is None:
                log(f"  {'sessionstore':<9} {str(path):<71} (unreadable)")
                continue
            try:
                data = json.loads(blob)
            except ValueError:
                continue
            hits = [
                c for c in (data.get("cookies") or [])
                if any(suffix in str(c.get("host") or "") for suffix in suffixes)
            ]
            log(
                f"  {'sessionstore':<9} {str(path):<71} "
                f"{len(hits)} matching cookie(s) out of {len(data.get('cookies') or [])}"
            )
            for cookie in hits:
                total += 1
                log(
                    f"  {'  ^':<9} {str(cookie.get('host')):<34} "
                    f"{str(cookie.get('name')):<36} "
                    f"{'session' if not cookie.get('expiry') else cookie.get('expiry'):<14} "
                    f"{' '.join(f for f, on in (('secure', cookie.get('secure')), ('httpOnly', cookie.get('httponly'))) if on)}"
                )

    log(f"  ({total} matching cookie(s))")
    log(
        "\nnote: Firefox-family browsers do not write cookies without an expiry to\n"
        "cookies.sqlite, which is why the Kuchnia Vikinga panel session only shows up\n"
        "under the session-restore store."
    )


# --------------------------------------------------------------------------- #
# session acquisition
# --------------------------------------------------------------------------- #


@dataclass
class Session:
    """A cookie set for one site, plus how it was obtained."""

    cookies: list[CookieRecord]
    source: str


def load_cached_cookies(path: Path) -> list[CookieRecord]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("cookies") if isinstance(data, dict) and "cookies" in data else data
    records: list[CookieRecord] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                records.append(
                    CookieRecord(
                        str(item["name"]), str(item.get("value") or ""),
                        str(item.get("host") or ""), str(item.get("path") or "/"),
                        bool(item.get("secure", True)), bool(item.get("http_only", False)),
                    )
                )
    elif isinstance(raw, dict):          # legacy flat {name: value}
        records = [CookieRecord(str(k), str(v)) for k, v in raw.items()]
    return records


def save_cached_cookies(path: Path, cookies: Sequence[CookieRecord]) -> None:
    payload = [
        {"name": c.name, "value": c.value, "host": c.host, "path": c.path,
         "secure": c.secure, "http_only": c.http_only}
        for c in cookies
    ]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"cookies": payload, "saved_at": datetime.now().isoformat(timespec="seconds")},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
    except OSError as exc:
        warn(f"could not cache cookies in {path} ({exc}); the session is still usable "
             f"for this run")


def parse_cookie_arg(text: str) -> list[CookieRecord]:
    """Accept 'a=1; b=2', a JSON object, or a path to either.

    These records carry no host, so they are sent to whatever host the client
    talks to - which is what someone pasting a Cookie header means.
    """
    candidate = Path(text).expanduser()
    if candidate.is_file():
        records = load_cached_cookies(candidate)
        if records:
            return records
        text = candidate.read_text("utf-8").strip()
    text = text.strip()
    if text.startswith("{"):
        data = json.loads(text)
        return [CookieRecord(str(k), str(v)) for k, v in data.items()]
    records = []
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        records.append(CookieRecord(name.strip(), value.strip()))
    return records


@dataclass
class SessionRecipe:
    label: str
    login_url: str
    host_suffixes: tuple[str, ...]
    cache: Path
    env_var: str
    description: str
    hint: str = ""


def acquire_session(
    recipe: SessionRecipe,
    args: argparse.Namespace,
    validate: Callable[[Sequence[CookieRecord]], str | None],
    force: bool = False,
) -> Session:
    """Return a cookie set, asking the browser to log in when we have nothing.

    Progress is decided by asking the service whether the cookies work, not by
    looking for a particular cookie name: which cookies a backend issues is an
    implementation detail we should not have to guess.
    """
    explicit = args.cookie.get(recipe.label) or os.environ.get(recipe.env_var)
    if explicit:
        cookies = parse_cookie_arg(explicit)
        if not cookies:
            die(f"{recipe.label}: could not parse the cookie value given")
        return Session(cookies, "explicit")

    cached = load_cached_cookies(recipe.cache)
    if cached and not (args.relogin or force):
        return Session(cached, f"cache ({recipe.cache})")

    # The browser may already hold a live session from an earlier visit, so look
    # before asking for a login - that is what makes a plain run silent when the
    # session is still good and a cookie file was never written (or was removed).
    existing = harvest_cookies(recipe.host_suffixes)
    if existing and validate(existing) is None:
        log(f"\n{recipe.label}: using the session already in your browser")
        save_cached_cookies(recipe.cache, existing)
        return Session(existing, "browser")

    log(f"\n{recipe.label}: {recipe.description}")
    log(f"  opening {recipe.login_url}")
    log("  log in there - your password manager can fill the form in")
    log("  (waiting for the session to appear; Ctrl-C and re-run with --cookie to paste one)")
    try:
        webbrowser.open(recipe.login_url)
    except Exception as exc:  # headless box, no $DISPLAY, ...
        warn(f"could not open a browser: {exc}")

    deadline = time.monotonic() + args.login_timeout
    previous: tuple[str, ...] | None = None
    last_problem: str | None = None
    polls = 0
    while True:
        polls += 1
        cookies = harvest_cookies(recipe.host_suffixes)
        signature = tuple(sorted(f"{c.host}|{c.name}|{c.path}" for c in cookies))
        if cookies and (signature != previous or polls % 5 == 0):
            if signature != previous:
                previous = signature
                shown = sorted({f"{c.name}@{c.host or '(any)'}" for c in cookies})
                log(f"  cookies visible: {', '.join(shown)}")
            problem = validate(cookies)
            if problem is None:
                log("  session is live")
                save_cached_cookies(recipe.cache, cookies)
                log(f"  cached to {recipe.cache}")
                return Session(cookies, "browser")
            last_problem = problem
        if time.monotonic() >= deadline:
            log(f"\n{recipe.label}: no working session after {args.login_timeout}s"
                + (f" (last attempt: {last_problem})" if last_problem else ""))
            dump_cookies(recipe.host_suffixes)
            if recipe.hint:
                log(recipe.hint)
            die(f"{recipe.label}: could not read a usable session from the browser")
        time.sleep(args.login_poll)


def connect_kv(
    cookies: Sequence[CookieRecord],
    bases: Sequence[str] = KV_BASES,
    launcher: str = KV_LAUNCHER,
) -> tuple[KVClient, dict]:
    """First Kuchnia Vikinga hostname whose API accepts these cookies."""
    problems: list[str] = []
    for base in bases:
        client = KVClient(cookies, base=base, launcher=launcher)
        try:
            return client, client.client()
        except (KVNotLoggedIn, KVError) as exc:
            problems.append(f"{host_of(base)}: {exc}")
        except requests.RequestException as exc:
            problems.append(f"{host_of(base)}: network error: {exc}")
    raise KVNotLoggedIn("; ".join(problems) or "no Kuchnia Vikinga host reachable")


def validate_kv(
    cookies: Sequence[CookieRecord],
    bases: Sequence[str] = KV_BASES,
    launcher: str = KV_LAUNCHER,
) -> str | None:
    if not cookies:
        return "no cookies available"
    try:
        connect_kv(cookies, bases, launcher)
        return None
    except KVNotLoggedIn as exc:
        return str(exc)


def validate_mfp(cookies: Sequence[CookieRecord], base: str = MFP_ORIGIN) -> str | None:
    if not cookies:
        return "no cookies available"
    try:
        MFPClient(cookies, base=base).user()
        return None
    except (MFPNotLoggedIn, PermissionError) as exc:
        return str(exc)
    except requests.RequestException as exc:
        return f"network error: {exc}"


def ensure_session(
    recipe: SessionRecipe,
    args: argparse.Namespace,
    validate: Callable[[Sequence[CookieRecord]], str | None],
    label: str,
) -> Session:
    session = acquire_session(recipe, args, validate)
    problem = validate(session.cookies)
    if problem is None:
        return session
    if session.source == "explicit":
        die(f"{label}: {problem}")
    warn(f"{label}: {problem}")
    warn("the stored session is no longer valid; refreshing")
    try:
        Path(recipe.cache).unlink()
    except OSError:
        pass
    # force applies to this site only: a stale Kuchnia Vikinga session must not
    # send the MyFitnessPal session back through a login it does not need
    return acquire_session(recipe, args, validate, force=True)


# --------------------------------------------------------------------------- #
# Kuchnia Vikinga client
# --------------------------------------------------------------------------- #


class KVNotLoggedIn(RuntimeError):
    pass


class KVError(RuntimeError):
    """Any other non-2xx answer from Kuchnia Vikinga."""

    def __init__(self, path: str, status: int, body: str = ""):
        self.path = path
        self.status = status
        self.body = body
        super().__init__(f"GET {path} -> {status}: {body[:200]}")


class KVClient:
    """Read-only Kuchnia Vikinga API client.

    There is deliberately no generic request method: every call below is a GET.
    """

    def __init__(self, cookies: Sequence[CookieRecord], base: str = KV_API,
                 launcher: str = KV_LAUNCHER, company_id: str = KV_COMPANY_ID):
        self.base = base.rstrip("/")
        self.company_id = company_id
        self.session = requests.Session()
        self.session.cookies = cookie_jar(
            cookies, host_of(self.base), secure=self.base.startswith("https")
        )
        self.session.headers.update({
            "Accept": "application/json",
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
            "X-Launcher-Type": launcher,
            "company-id": company_id,
            "User-Agent": UA,
        })

    # -- transport ---------------------------------------------------------- #

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}/{path.lstrip('/')}"
        resp = self.session.get(url, params=params, timeout=40)
        if resp.status_code in (401, 403):
            raise KVNotLoggedIn(
                f"GET {path} -> {resp.status_code}; the Kuchnia Vikinga session is not valid"
            )
        if resp.status_code >= 400:
            raise KVError(path, resp.status_code, resp.text)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(f"GET {path} returned non-JSON: {resp.text[:200]}")

    # -- endpoints ---------------------------------------------------------- #

    def client(self) -> dict:
        """Session probe: works only while logged in."""
        try:
            data = self._get("company/customer/client")
        except KVError as exc:
            if exc.status >= 500:
                # a stale or absent cookie makes this backend fail rather than 401
                raise KVNotLoggedIn(
                    f"GET company/customer/client -> {exc.status}; "
                    f"the Kuchnia Vikinga session is not valid"
                ) from exc
            raise
        return data if isinstance(data, dict) else {}

    def orders(self) -> list[dict]:
        data = self._get("company/customer/order/all")
        if isinstance(data, list):
            return [o for o in data if isinstance(o, dict)]
        for key in ("orders", "content", "items"):
            if isinstance(data, dict) and isinstance(data.get(key), list):
                return [o for o in data[key] if isinstance(o, dict)]
        return []

    def order(self, order_id: Any) -> dict:
        data = self._get(f"company/customer/order/{order_id}")
        return data if isinstance(data, dict) else {}

    def day_menu(self, delivery_id: Any) -> dict:
        data = self._get(f"company/general/menus/delivery/{delivery_id}/new")
        return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# MyFitnessPal client
# --------------------------------------------------------------------------- #


class MFPNotLoggedIn(RuntimeError):
    pass


class MFPClient:
    """MyFitnessPal web client restricted to the endpoints this task needs.

    The allow-list below is the whole write surface: quick-add diary entries.
    Anything else (deleting, editing, logging food, orders, ...) raises.
    """

    ALLOWED: frozenset[tuple[str, str]] = frozenset({
        ("GET", "/api/auth/csrf"),
        ("GET", "/api/services/users"),
        ("GET", "/api/services/diary/read_diary"),
        ("POST", "/api/services/diary"),
    })

    USER_FIELDS = (
        "diary_preferences",
        "unit_preferences",
        "account",
        "location_preferences",
    )

    def __init__(self, cookies: Sequence[CookieRecord], base: str = MFP_ORIGIN):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.cookies = cookie_jar(
            cookies, host_of(self.base), secure=self.base.startswith("https")
        )
        self.session.headers.update({
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": UA,
            "Origin": self.base,
            "Referer": f"{self.base}/food/diary",
        })
        self._csrf: str | None = None
        self._username: str | None = None
        self._user: dict | None = None

    # -- transport ---------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        method = method.upper()
        endpoint = path.split("?", 1)[0]
        if (method, endpoint) not in self.ALLOWED:
            raise PermissionError(
                f"refusing {method} {endpoint}: not in the MyFitnessPal allow-list "
                f"{sorted(self.ALLOWED)}"
            )
        resp = self.session.request(method, self.base + path, timeout=40, **kwargs)
        if resp.status_code in (401, 403) or resp.status_code == 400 and (
            "Authorization" in resp.text or "username is required" in resp.text
        ):
            raise MFPNotLoggedIn(
                f"{method} {path} -> {resp.status_code}: MyFitnessPal session is not valid "
                f"({resp.text[:160]})"
            )
        return resp

    def csrf_token(self) -> str:
        if self._csrf is None:
            resp = self._request("GET", "/api/auth/csrf")
            resp.raise_for_status()
            self._csrf = resp.json().get("csrfToken", "")
        return self._csrf

    def _json_headers(self) -> dict[str, str]:
        token = self.csrf_token()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-CSRF-Token"] = token
        return headers

    # -- endpoints ---------------------------------------------------------- #

    def user(self) -> dict:
        if self._user is None:
            query = "&".join(f"fields[]={f}" for f in self.USER_FIELDS)
            resp = self._request("GET", f"/api/services/users?{query}")
            resp.raise_for_status()
            data = resp.json()
            self._user = data if isinstance(data, dict) else {}
        return self._user

    def meal_names(self) -> list[str]:
        names = json_get(self.user(), "diary_preferences", "meal_names", default=[])
        return [str(n) for n in names] if isinstance(names, list) else []

    def username(self) -> str:
        if self._username is None:
            self._username = str(self.user().get("username") or "")
        return self._username

    def diary_entries(self, day: str) -> list[dict]:
        """Everything already logged for one day, so we can skip what is there."""
        resp = self._request(
            "GET", "/api/services/diary/read_diary",
            params={
                "entry_date": day,
                "fields": "all",
                "types": "food_entry,exercise_entry,steps_aggregate",
                "username": self.username(),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else (data.get("items") or [])
        # exercise and step entries carry no meal slot; only food matters here
        return [
            item for item in items
            if isinstance(item, dict) and item.get("meal_name")
        ]

    def add_quick_add(
        self,
        *,
        day: str,
        meal_name: str,
        calories: float,
        carbohydrates: float,
        fat: float,
        protein: float,
        energy_unit: str = "calories",
    ) -> Any:
        """Exactly the payload the /food/quick-add page posts."""
        item = {
            "type": "quick_add",
            "date": day,
            "meal_name": meal_name,
            "nutritional_contents": {
                "fat": fat,
                "carbohydrates": carbohydrates,
                "protein": protein,
                "energy": {"value": calories, "unit": energy_unit},
            },
        }
        resp = self._request(
            "POST", "/api/services/diary", headers=self._json_headers(),
            data=json.dumps({"items": [item]}),
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return None


# --------------------------------------------------------------------------- #
# normalisation: KV -> plain meals
# --------------------------------------------------------------------------- #


@dataclass
class Meal:
    day: date
    meal_name: str          # "śniadanie", "ii śniadanie", ...
    dish: str               # dish title, informational only
    calories: float
    carbohydrates: float
    fat: float
    protein: float
    amount: float = 1.0
    source: str = ""        # delivery/meal ids, for traceability


def _nutrition_of(meal: dict) -> dict:
    """The nutrition block hangs off menuMealDetails in the app's own model."""
    for candidate in (
        meal.get("nutrition"),
        json_get(meal, "menuMealDetails", "nutrition"),
        meal.get("nutritional"),
    ):
        if isinstance(candidate, dict):
            return candidate
    return {}


def _dish_name_of(meal: dict) -> str:
    value = first_present(meal, ("menuMealName", "name", "title", "dishName"))
    if value is None:
        value = json_get(meal, "menuMealDetails", "menuMealName")
    return str(value or "").strip()


def _meal_slot_of(meal: dict) -> str:
    value = first_present(meal, ("mealName", "meal_name", "mealType"))
    if value is None:
        value = json_get(meal, "menuMealDetails", "mealName")
    return str(value or "").strip()


def _meals_from_day_menu(delivery_id: Any, raw: dict) -> list[dict]:
    for key in ("deliveryMenuMeal", "meals", "deliveryMeals"):
        value = raw.get(key)
        if isinstance(value, list):
            return [m for m in value if isinstance(m, dict)]
    # some responses nest the list per delivery id
    day_menus = raw.get("dayMenus")
    if isinstance(day_menus, dict):
        node = day_menus.get(str(delivery_id)) or day_menus.get(delivery_id)
        if isinstance(node, dict):
            for key in ("deliveryMenuMeal", "meals"):
                value = node.get(key)
                if isinstance(value, list):
                    return [m for m in value if isinstance(m, dict)]
    return []


def _deliveries_of(order: dict) -> list[dict]:
    value = order.get("deliveries")
    if isinstance(value, list):
        return [d for d in value if isinstance(d, dict)]
    return []


def _order_overlaps(order: dict, start: date, end: date | None) -> bool:
    """order/all already carries each order's window, so skip irrelevant ones.

    Without this, every historical order would be fetched in full just to be
    discarded.
    """
    first = first_present(order, ("dateFrom", "date_from", "startDate"))
    last = first_present(order, ("dateTo", "date_to", "endDate"))
    if not first and not last:
        return True
    first_day = try_parse_iso_date(first) if first else None
    last_day = try_parse_iso_date(last) if last else None
    if last_day is not None and last_day < start:
        return False
    if end is not None and first_day is not None and first_day > end:
        return False
    return True


def collect_kv_meals(
    client: KVClient,
    start: date,
    end: date | None,
    *,
    with_side_orders: bool = False,
    menu_gap: int = DEFAULT_MENU_GAP,
) -> list[Meal]:
    """Every meal in range, walking the deliveries in date order.

    Menus are only published a couple of weeks ahead, so the tail of a long order
    is empty. Walking it anyway costs one request per delivery and produces a wall
    of warnings, so the walk gives up after ``menu_gap`` deliveries in a row come
    back empty.

    Days with no delivery at all - public holidays - are simply absent from the
    list, so they never count towards that run: a holiday gap does not end the
    walk, only a stretch of deliveries whose menu is missing does.
    """
    orders = client.orders()
    if not orders:
        log("no Kuchnia Vikinga orders found on this account")
        return []

    candidates: list[tuple[date, Any, Any]] = []
    for order in orders:
        order_id = first_present(order, ("orderId", "id"))
        if order_id is None or not _order_overlaps(order, start, end):
            continue
        detail = client.order(order_id) or order
        deliveries = _deliveries_of(detail) or _deliveries_of(order)
        in_range = 0
        for delivery in deliveries:
            if delivery.get("deleted"):
                continue
            delivery_id = first_present(delivery, ("deliveryId", "id"))
            raw_date = first_present(delivery, ("date", "deliveryDate", "day"))
            if delivery_id is None or not raw_date:
                continue
            day = try_parse_iso_date(raw_date)
            if day is None:
                warn(f"delivery {delivery_id}: unparsable date {raw_date!r}")
                continue
            if day < start or (end is not None and day > end):
                continue
            candidates.append((day, order_id, delivery_id))
            in_range += 1
        log(f"order {order_id}: {in_range} of {len(deliveries)} deliveries in range")

    candidates.sort(key=lambda item: item[0])
    if not candidates:
        return []

    meals: list[Meal] = []
    empty: list[date] = []
    unchecked: list[date] = []
    published_through: date | None = None
    run = 0
    for index, (day, order_id, delivery_id) in enumerate(candidates):
        entries = _meals_from_day_menu(delivery_id, client.day_menu(delivery_id))
        day_meals, missing = _meals_of_day(
            entries, day, order_id, delivery_id, with_side_orders=with_side_orders
        )
        if not day_meals:
            # The delivery is known but its menu carries no values yet: that is
            # the unpublished tail, not an error, so it is counted in bulk.
            empty.append(day)
            run += 1
            if menu_gap and run >= menu_gap:
                unchecked = [d for d, _, _ in candidates[index + 1:]]
                break
            continue
        if missing:
            warn(
                f"delivery {delivery_id} ({day}): no nutrition for "
                f"{', '.join(missing)} - skipped"
            )
        run = 0
        published_through = day
        meals.extend(day_meals)

    report_menu_coverage(published_through, empty, unchecked)
    meals.sort(key=lambda m: (m.day, _slot_index(m.meal_name), m.dish))
    return meals


def _meals_of_day(
    entries: Sequence[dict],
    day: date,
    order_id: Any,
    delivery_id: Any,
    *,
    with_side_orders: bool,
) -> tuple[list[Meal], list[str]]:
    """Meals for one delivery, plus the slots that had no nutrition values."""
    meals: list[Meal] = []
    missing: list[str] = []
    for entry in entries:
        if entry.get("deleted"):
            continue
        slot = _meal_slot_of(entry)
        if not slot:
            continue
        if not with_side_orders and norm_key(slot).startswith("dodatek"):
            continue
        nutrition = _nutrition_of(entry)
        amount = as_number(first_present(entry, ("amount", "quantity", "count")), 1.0) or 1.0
        calories = as_number(
            first_present(nutrition, ("calories", "energy", "kcal", "energyValue"))
        )
        carbohydrates = as_number(
            first_present(nutrition, ("carbohydrate", "carbohydrates", "carbs"))
        )
        fat = as_number(first_present(nutrition, ("fat", "fats")))
        protein = as_number(first_present(nutrition, ("protein", "proteins")))
        if calories <= 0 and carbohydrates <= 0 and fat <= 0 and protein <= 0:
            missing.append(slot)
            continue
        meal_id = first_present(entry, ("deliveryMealId", "menuMealId"))
        meals.append(
            Meal(
                day=day,
                meal_name=slot,
                dish=_dish_name_of(entry),
                calories=calories * amount,
                carbohydrates=carbohydrates * amount,
                fat=fat * amount,
                protein=protein * amount,
                amount=amount,
                source=f"order {order_id}/delivery {delivery_id}"
                + (f"/meal {meal_id}" if meal_id else ""),
            )
        )
    return meals, missing


def report_menu_coverage(
    published_through: date | None,
    empty: Sequence[date],
    unchecked: Sequence[date],
) -> None:
    """One summary for the unpublished tail instead of a warning per meal."""
    if not empty and not unchecked:
        return
    no_menu = sorted([*empty, *unchecked])
    log()
    log(
        "note: menus are published through "
        + (
            published_through.isoformat()
            if published_through
            else "(nothing in this range)"
        )
    )
    log(
        f"note: no menu yet for {len(no_menu)} deliveries "
        f"({no_menu[0].isoformat()} .. {no_menu[-1].isoformat()})"
    )
    if unchecked:
        log(
            f"      gave up after {len(empty)} empty in a row, {len(unchecked)} left "
            f"unchecked; --menu-gap 0 reads the whole range"
        )


def _slot_index(name: str) -> int:
    return KV_MEAL_ALIASES.get(norm_key(name), 99)


# --------------------------------------------------------------------------- #
# meal slot mapping
# --------------------------------------------------------------------------- #


def build_meal_map(
    kv_slots: Iterable[str],
    mfp_meals: Sequence[str],
    explicit: dict[str, str] | None = None,
) -> dict[str, str]:
    """KV meal slot -> MyFitnessPal meal name."""
    explicit_keys = {norm_key(k): v for k, v in (explicit or {}).items()}
    mapping: dict[str, str] = {}

    for slot in sorted({s for s in kv_slots}, key=_slot_index):
        key = norm_key(slot)
        if key in explicit_keys:
            mapping[slot] = explicit_keys[key]
            continue

        index = KV_MEAL_ALIASES.get(key)
        if index is not None and len(mfp_meals) >= len(KV_MEAL_ORDER):
            # the diary has the full box-diet cadence: line them up in order
            mapping[slot] = mfp_meals[min(index, len(mfp_meals) - 1)]
            continue

        mapping[slot] = _match_by_name(key, mfp_meals) or (mfp_meals[0] if mfp_meals else "")
    return mapping


def _match_by_name(key: str, mfp_meals: Sequence[str]) -> str:
    def find(*needles: str, taken: set[str]) -> str | None:
        for needle in needles:
            for name in mfp_meals:
                if needle in norm_key(name) and name not in taken:
                    return name
        return None

    taken: set[str] = set()
    if key in ("śniadanie", "sniadanie"):
        return find("1st", "first", "breakfast", taken=taken)
    if key in ("ii śniadanie", "ii sniadanie", "2 śniadanie", "2 sniadanie", "drugie śniadanie"):
        first = _match_by_name("śniadanie", mfp_meals)
        taken = {first} if first else set()
        return find("2nd", "second", "breakfast", taken=taken)
    if key == "obiad":
        return find("lunch", "dinner", "obiad", taken=taken)
    if key in ("podwieczorek", "deser"):
        return find("dessert", "snack", "afternoon", "tea", taken=taken)
    if key in ("kolacja", "supper"):
        # prefer something that is not already used by lunch/dinner
        used = {_match_by_name("obiad", mfp_meals)}
        taken = {m for m in used if m}
        return find("supper", "dinner", "evening", "kolacja", taken=taken)
    return None


# --------------------------------------------------------------------------- #
# what is already in the diary
# --------------------------------------------------------------------------- #


def day_energies(entries: Iterable[dict]) -> dict[str, list[float]]:
    """meal slot (normalised) -> logged calories, for one day's entries."""
    by_slot: dict[str, list[float]] = {}
    for entry in entries:
        slot = norm_key(entry.get("meal_name"))
        value = as_number(
            json_get(entry, "nutritional_contents", "energy", "value"), None
        )
        by_slot.setdefault(slot, []).append(value if value is not None else 0.0)
    return by_slot


def split_already_logged(
    mfp: MFPClient, meals: Sequence[Meal], mapping: dict[str, str]
) -> tuple[list[Meal], list[tuple[Meal, list[float]]]]:
    """Drop meals whose diary slot for that day is already occupied.

    The diary itself is the record of what has been pushed, so no local
    bookkeeping is needed and the script stays correct across machines, after a
    ``--force``, or when meals were logged by hand.
    """
    by_day: dict[date, list[Meal]] = {}
    for meal in meals:
        by_day.setdefault(meal.day, []).append(meal)

    pending: list[Meal] = []
    skipped: list[tuple[Meal, list[float]]] = []
    for day, day_meals in sorted(by_day.items()):
        try:
            logged = day_energies(mfp.diary_entries(day.isoformat()))
        except (MFPNotLoggedIn, PermissionError):
            raise
        except (requests.RequestException, ValueError) as exc:
            # Guessing here would duplicate everything, which is the one outcome
            # worth refusing to risk; --force is the explicit override.
            die(
                f"could not read the MyFitnessPal diary for {day} ({exc})\n"
                f"  refusing to add entries that may already be there.\n"
                f"  re-run with --force to add them without checking."
            )
        for meal in day_meals:
            occupied = logged.get(norm_key(mapping[meal.meal_name]))
            if occupied:
                skipped.append((meal, occupied))
            else:
                pending.append(meal)
    return pending, skipped


def report_skipped(skipped: Sequence[tuple[Meal, list[float]]]) -> None:
    if not skipped:
        return
    by_day: dict[date, list[str]] = {}
    for meal, energies in skipped:
        shown = "/".join(f"{round_half_up(e):.0f}" for e in energies if e)
        by_day.setdefault(meal.day, []).append(
            meal.meal_name + (f" ({shown} kcal)" if shown else "")
        )
    log()
    log(f"already in the diary, skipped: {len(skipped)} meal(s) "
        f"(use --force to add them anyway)")
    for day, details in sorted(by_day.items()):
        log(f"  {day}  " + ", ".join(details))


# --------------------------------------------------------------------------- #
# presentation
# --------------------------------------------------------------------------- #


def print_plan(meals: Sequence[Meal], mapping: dict[str, str]) -> None:
    log()
    log(f"{'date':<12} {'KV meal':<16} {'-> MFP meal':<16} {'kcal':>7} {'carb':>7} {'fat':>7} {'prot':>7}  dish")
    log("-" * 108)
    for meal in meals:
        log(
            f"{meal.day.isoformat():<12} {meal.meal_name:<16} "
            f"{mapping.get(meal.meal_name, '?'):<16} "
            f"{round_half_up(meal.calories):>7.0f} "
            f"{meal.carbohydrates:>7.1f} {meal.fat:>7.1f} {meal.protein:>7.1f}  {meal.dish[:34]}"
        )
    log("-" * 108)
    days = sorted({m.day for m in meals})
    log(
        f"{len(meals)} meal{'s' if len(meals) != 1 else ''} across "
        f"{len(days)} day{'s' if len(days) != 1 else ''}"
        f"{f' ({days[0]} .. {days[-1]})' if days else ''}"
    )
    totals = {
        "calories": sum(m.calories for m in meals),
        "carbohydrates": sum(m.carbohydrates for m in meals),
        "fat": sum(m.fat for m in meals),
        "protein": sum(m.protein for m in meals),
    }
    log(
        f"total: {round_half_up(totals['calories']):.0f} kcal, "
        f"{totals['carbohydrates']:.1f} g carbs, {totals['fat']:.1f} g fat, "
        f"{totals['protein']:.1f} g protein"
    )


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        die("refusing to write without confirmation; re-run with --yes")
    answer = input(f"{question} [y/N] ").strip().lower()
    return answer in ("y", "yes", "tak", "t")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="viking_to_mfp.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  viking_to_mfp.py --from 2026-10-14 --to 2026-10-20\n"
            "  viking_to_mfp.py --from 2026-10-01            # up to the last known delivery\n"
            "  viking_to_mfp.py --from 2026-10-14 --dry-run\n"
            "  viking_to_mfp.py --login-only                 # just refresh both sessions\n"
        ),
    )
    p.add_argument("--from", dest="start", metavar="YYYY-MM-DD",
                   help="first day to copy (default: today)")
    p.add_argument("--to", dest="end", metavar="YYYY-MM-DD",
                   help="last day to copy (default: the last delivery KV knows about)")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would be written, write nothing to MyFitnessPal")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    p.add_argument("--force", action="store_true",
                   help="add meals even where the diary slot is already filled")
    p.add_argument("--menu-gap", type=int, default=DEFAULT_MENU_GAP, metavar="N",
                   help="give up reading once N deliveries in a row have no menu "
                        "published yet (default %(default)s; 0 reads the whole range). "
                        "Days with no delivery, such as public holidays, never count "
                        "towards the run")
    p.add_argument("--side-orders", action="store_true",
                   help="also copy extra paid side orders, if the menu exposes them")
    p.add_argument("--energy-unit", choices=("calories", "kilojoules"), default="calories",
                   help="unit to send to MyFitnessPal (default: calories)")
    p.add_argument("--meal-map", metavar="KV=MFP,...",
                   help="override meal slot mapping, e.g. 'śniadanie=Breakfast,obiad=Lunch'")
    p.add_argument("--cookie", action="append", default=[], metavar="SITE=VALUE",
                   help="cookie header for kv or mfp (repeatable); VALUE may be a file path")
    p.add_argument("--login-timeout", type=int, default=180,
                   help="seconds to wait for a browser login (default: 180)")
    p.add_argument("--login-poll", type=float, default=2.0,
                   help="seconds between cookie checks while waiting (default: 2)")
    p.add_argument("--relogin", action="store_true", help="ignore cached cookies")
    p.add_argument("--login-only", action="store_true",
                   help="acquire/refresh both sessions and stop")
    p.add_argument("--kv-base", default=None, metavar="URL",
                   help=f"Kuchnia Vikinga API base (default: first of {', '.join(KV_BASES)} "
                        f"that accepts the session)")
    p.add_argument("--mfp-base", default=MFP_ORIGIN, help=argparse.SUPPRESS)
    p.add_argument("--kv-launcher", default=KV_LAUNCHER, help=argparse.SUPPRESS)
    p.add_argument("--dump-cookies", nargs="?", const="auto", metavar="SUFFIX,...",
                   help="list the cookies readable from this machine's browsers and exit "
                        "(default: the hosts this script uses)")
    return p


def parse_meal_map(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    mapping: dict[str, str] = {}
    for chunk in text.split(","):
        if "=" not in chunk:
            die(f"--meal-map: expected KV=MFP pairs, got {chunk!r}")
        key, _, value = chunk.partition("=")
        mapping[key.strip()] = value.strip()
    return mapping


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # --cookie kv=... / mfp=...
    cookies_by_site: dict[str, str] = {}
    for entry in args.cookie:
        site, sep, value = entry.partition("=")
        if not sep:
            parser.error(f"--cookie expects SITE=VALUE, got {entry!r}")
        cookies_by_site[site.strip().lower()] = value.strip()
    args.cookie = cookies_by_site

    if args.dump_cookies:
        suffixes = (
            tuple(s.strip() for s in args.dump_cookies.split(",") if s.strip())
            if args.dump_cookies != "auto"
            else (KV_HOST_SUFFIX, "dietly.pl", MFP_HOST_SUFFIX)
        )
        dump_cookies(suffixes)
        return 0

    kv_recipe = SessionRecipe(
        label="kv",
        login_url=KV_LOGIN_URL,
        host_suffixes=(KV_HOST_SUFFIX, "dietly.pl"),
        cache=KV_COOKIE_CACHE,
        env_var="KV_COOKIE",
        description="Kuchnia Vikinga panel login (log in with the e-mail you order with)",
        hint=(
            "  If you are logged into the panel in Zen, the session cookie is a session\n"
            "  cookie: Zen keeps it in its session-restore store, which this run could not\n"
            "  read. Zen tabs in a container/workspace work fine - the store covers those.\n"
            "  Alternatives:\n"
            "    * paste it:  --cookie kv='SESSION=...'  (dev tools > Network > any\n"
            "      aplikacja.kuchniavikinga.pl request > Request headers > Cookie)\n"
            "    * or log into the panel in Chrome/Chromium and re-run"
        ),
    )
    mfp_recipe = SessionRecipe(
        label="mfp",
        login_url=MFP_LOGIN_URL,
        host_suffixes=(MFP_HOST_SUFFIX,),
        cache=MFP_COOKIE_CACHE,
        env_var="MFP_COOKIE",
        description="MyFitnessPal web login",
        hint=(
            "  MyFitnessPal's session cookie is persistent, so it should be in the browser\n"
            "  cookie database - if it is missing, the login did not complete."
        ),
    )

    kv_bases = (args.kv_base,) if args.kv_base else KV_BASES
    kv_session = ensure_session(
        kv_recipe, args,
        lambda c: validate_kv(c, kv_bases, args.kv_launcher),
        "Kuchnia Vikinga",
    )
    kv, client = connect_kv(kv_session.cookies, kv_bases, args.kv_launcher)
    log(f"\nKuchnia Vikinga: logged in as {client.get('email') or client.get('name') or '?'}")

    mfp_session = ensure_session(
        mfp_recipe, args, lambda c: validate_mfp(c, args.mfp_base), "MyFitnessPal"
    )
    mfp = MFPClient(mfp_session.cookies, base=args.mfp_base)
    mfp_meals = mfp.meal_names()
    log(f"MyFitnessPal: diary meals = {mfp_meals or ['(unknown)']}")

    if args.login_only:
        log("\nsessions are valid")
        return 0

    start = parse_iso_date(args.start) if args.start else date.today()
    end = parse_iso_date(args.end) if args.end else None
    if end is not None and end < start:
        die(f"--to {end} is before --from {start}")

    log()
    log(f"reading Kuchnia Vikinga meals from {start} to {end or 'the last known delivery'} ...")
    meals = collect_kv_meals(
        kv, start, end, with_side_orders=args.side_orders, menu_gap=args.menu_gap
    )
    if not meals:
        log("nothing to copy")
        return 0

    mapping = build_meal_map(
        {m.meal_name for m in meals}, mfp_meals, parse_meal_map(args.meal_map)
    )
    unmapped = sorted({m.meal_name for m in meals if not mapping.get(m.meal_name)})
    if unmapped:
        die(
            "no MyFitnessPal meal slot for: "
            + ", ".join(unmapped)
            + "\n  pass --meal-map, e.g. --meal-map '"
            + ",".join(f"{u}={mfp_meals[i % len(mfp_meals)]}" for i, u in enumerate(unmapped))
            + "'"
        )

    if args.force:
        pending, skipped = list(meals), []
    else:
        pending, skipped = split_already_logged(mfp, meals, mapping)

    report_skipped(skipped)
    print_plan(pending, mapping)
    if not pending:
        log("everything is already in the diary")
        return 0

    if args.dry_run:
        log("\ndry run: nothing was written to MyFitnessPal")
        return 0

    if not confirm(f"\nwrite these {len(pending)} entries to the MyFitnessPal diary?", args.yes):
        log("aborted")
        return 0

    written = 0
    for meal in pending:
        target = mapping[meal.meal_name]
        try:
            mfp.add_quick_add(
                day=meal.day.isoformat(),
                meal_name=target,
                calories=round_half_up(meal.calories),
                carbohydrates=meal.carbohydrates,
                fat=meal.fat,
                protein=meal.protein,
                energy_unit=args.energy_unit,
            )
        except MFPNotLoggedIn:
            raise
        except requests.HTTPError as exc:
            warn(f"{meal.day} {meal.meal_name}: MyFitnessPal rejected the entry: {exc}")
            continue
        written += 1
        log(f"  + {meal.day} {meal.meal_name} -> {target}  "
            f"({round_half_up(meal.calories):.0f} kcal, "
            f"{meal.carbohydrates:.1f}g C / {meal.fat:.1f}g F / {meal.protein:.1f}g P)")
        time.sleep(0.3)

    log(f"\ndone: {written}/{len(pending)} entries written")
    return 0 if written == len(pending) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        raise SystemExit(130)
    except (KVNotLoggedIn, MFPNotLoggedIn) as exc:
        die(f"{exc}\n  re-run with --relogin, or pass a fresh cookie with --cookie kv=... / --cookie mfp=...")
    except (KVError, requests.RequestException) as exc:
        die(str(exc))

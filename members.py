import asyncio
import collections
import datetime
import hashlib
import io
import json
import math
import os
import random
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

def _env(name: str) -> str:
    """Read an environment variable defensively.

    Pasting a value into a host's panel very often carries stray whitespace or
    a pair of quotes along with it, which turns a working key into a 401 with
    no obvious cause. Both are stripped here.
    """
    raw = (os.getenv(name) or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1].strip()
    return raw


TOKEN = _env("MEMBER_BOT_TOKEN")
COMMAND_PREFIX = _env("COMMAND_PREFIX") or ">"
MEMBER_SITE_URL = _env("MEMBER_SITE_URL").rstrip("/")

SUPABASE_URL = _env("SUPABASE_URL").rstrip("/")
SUPABASE_SERVICE_KEY = _env("SUPABASE_SERVICE_KEY")

# Bumped by hand when the deployed build changes; lands in the heartbeat row.
BOT_VERSION = "1.0.0"

def _pick_data_dir() -> str:
    """Where to keep the JSON state files, chosen to survive any host.

    This file lives in `referenced/wispcord/` in the repo, but on a host like
    Wispbyte it is often copied to `/home/container/` on its own — so climbing
    `../../..` produced `/logging`, which is not writable and killed the process
    at import time. Candidates are tried in order and the first writable one
    wins, so the same file works from the repo, from a bare container, and with
    an explicit override.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))

    candidates = []
    override = (os.getenv("MEMBERBOT_DATA_DIR") or "").strip()
    if override:
        candidates.append(override)
    candidates.append(os.path.join(repo_root, "logging", "memberbot_data"))
    candidates.append(os.path.join(here, "memberbot_data"))
    candidates.append("/home/container/logging/memberbot_data")
    candidates.append("/home/container/memberbot_data")

    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write-test")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
            return path
        except Exception:
            continue

    # Nothing is writable: fall back to the temp dir so the bot still boots.
    # State will not survive a restart, but a crash here would take the whole
    # process down before it ever logged in.
    fallback = os.path.join(tempfile.gettempdir(), "wispcord_memberbot")
    os.makedirs(fallback, exist_ok=True)
    return fallback


# Where every JSON file of persistent state lives. Created on first run and
# deliberately gitignored — this is runtime data, not source.
DATA_DIR = _pick_data_dir()

# The URL the "support page" link in the ticket panel points at. The member
# page checks `?open=support` on load and opens the support modal itself.
SUPPORT_PAGE_URL = f"{MEMBER_SITE_URL}?open=support" if MEMBER_SITE_URL else ""

START_TIME = time.time()


def log(message: str) -> None:
    """One plain, timestamped line — the Wispbyte console is not a terminal."""
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


# ---------------------------------------------------------------------------
# STORAGE — tiny JSON files, written atomically
#
# Each store also has a row in Supabase's `bot_state` table, so the database is
# the durable copy and these files are a local cache. Commands only ever touch
# the file (nothing waits on the network); the row is what a fresh or wiped host
# restores from at startup, and what gets updated after a change.
# ---------------------------------------------------------------------------

# Every store created below, so the whole set can be pulled from Supabase once
# at startup and pushed back when something changes.
_STORES: list = []

# Store names changed since the last successful push.
_STATE_DIRTY = set()

# name -> the JSON last pushed, so an untouched store is not rewritten on every
# sweep. Empty at startup, which is what makes the first pass push everything.
_STATE_PUSHED = {}

# Whether the startup pull has run, and whether Supabase could actually be read.
# Writes are refused while the answer to the second one is "no": posting local
# defaults over a copy we failed to fetch is the one way this could lose data.
_STATE_PULLED = False
_STATE_REMOTE_READABLE = None
_STATE_LAST_PULL = 0.0

# How long to wait before trying Supabase again after a failed read.
STATE_RETRY_SECONDS = 300.0


class JsonStore:
    """A JSON file that behaves like a dict, cached from and to Supabase.

    Every write goes to a `.tmp` next to the target and is then replaced, so a
    crash mid-write can never leave a half-written file behind. `save()` also
    marks the store for the next state push — see mirror_state().
    """

    def __init__(self, filename: str, default=None):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.path = os.path.join(DATA_DIR, filename)
        self.name = os.path.splitext(filename)[0]   # row id in `bot_state`
        self.default = default if default is not None else {}
        self.loaded_from_disk = True
        self.data = self._read()
        _STORES.append(self)

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            self.loaded_from_disk = False
            return json.loads(json.dumps(self.default))
        except Exception as exc:  # corrupt file: keep going on a fresh copy
            self.loaded_from_disk = False
            log(f"Could not read {os.path.basename(self.path)}: {exc}")
            return json.loads(json.dumps(self.default))

    def _write_file(self) -> bool:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
            return True
        except Exception as exc:
            log(f"Could not write {os.path.basename(self.path)}: {exc}")
            return False

    def save(self) -> None:
        """Write the file, then queue the same contents for Supabase."""
        if self._write_file():
            _STATE_DIRTY.add(self.name)


economy = JsonStore("economy.json")            # uid -> balance/bank/xp/inventory
lb_store = JsonStore("leaderboards.json", {"wins": {}, "games": {}, "trivia": {}, "hangman": {}})
trivia_store = JsonStore("trivia.json", {"used": [], "scores": {}})
worker = JsonStore("worker.json")              # uid -> kind/started_at/last_tick/payout
marriages = JsonStore("marriages.json")        # guild -> uid -> partner uid
afk = JsonStore("afk.json")                    # guild -> uid -> reason/since
reminders = JsonStore("reminders.json")        # uid -> text/due_at

# Live game state that does not need to survive a restart. Each entry carries a
# `touch` timestamp so abandoned games can be swept — see sweep_game_state().
HANGMAN = {}      # channel_id -> {word, guessed:set, wrong:int, touch:float}
WORDCHAIN = {}    # channel_id -> {"last": word, "used": set, "touch": float}
GUESSING = {}     # channel_id -> {"number": int, "tries": int, "touch": float}

# How long an untouched game is allowed to sit in memory before it is dropped.
GAME_STATE_TTL = 1800.0

# How long a finished interactive message is left up before it is removed.
CLEANUP_DELAY = 8

# In-memory only: how many times each command has been run this session, plus
# the cooldown a member's last passive cash-out earned them.
COMMAND_USES = collections.Counter()
WORK_COOLDOWN_UNTIL = {}
MARRIAGE_REWARDED = set()
LAST_PASSIVE_PAYOUT = {}

# uid -> checksum of the last economy row pushed to Supabase, so an unchanged
# member is not re-sent on every sweep. It doubles as "what this bot last wrote",
# which is how a row changed by somebody else is recognised.
_ECONOMY_DIRTY = {}

# How often to look for a currency change made outside the bot (the panel's
# Currency button, the admin bot's money commands), and how often to re-read the
# whole table for a row edited by hand in Supabase, which leaves `updated_at`
# alone and so cannot be found by the cheaper incremental read.
ECONOMY_PULL_SECONDS = 15
ECONOMY_SWEEP_SECONDS = 600
_ECONOMY_LAST_SEEN = ""
_ECONOMY_LAST_SWEEP = 0.0

LUCKY_CHANCE = 0.05
QWORK_SECONDS = 120
QWORK_COOLDOWN = 300
WORK_CAP_SECONDS = 28800
REMINDER_MAX_SECONDS = 30 * 86400

# 30+ job names, shared by `>qwork` and `>work`. Cosmetics only.
JOB_NAMES = [
    "barista", "line cook", "night porter", "bike courier", "data entry clerk",
    "warehouse picker", "library shelver", "movie usher", "car wash attendant",
    "dog walker", "shelf stocker", "delivery driver", "call centre agent",
    "junior dev", "QA tester", "ticket triage", "server babysitter",
    "modmail sifter", "grocery bagger", "pizza chef", "dishwasher",
    "hotel receptionist", "security guard", "lifeguard", "garden landscaper",
    "trash collector", "mail sorter", "farm hand", "fisher", "forklift operator",
    "copywriter", "stand-up comic", "street busker", "ice cream scooper",
]


# ---------------------------------------------------------------------------
# SUPABASE (urllib only — no client library, no extra dependency)
# ---------------------------------------------------------------------------


def supa(method: str, path: str, data=None, extra_headers=None, timeout: int = 10):
    """Talk to the Supabase REST API. Returns (status, body-as-text).

    A status of 0 means the request never left the machine — callers treat that
    the same as any other failure, so a network hiccup can never raise out of a
    loop or a command.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return 0, "supabase not configured"

    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    if extra_headers:
        headers.update(extra_headers)

    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, str(exc)


def supa_insert(table: str, row) -> bool:
    """INSERT one row (or a list of rows)."""
    status, _ = supa("POST", table, data=row)
    return 200 <= status < 300


def supa_select(table: str, filters: str = "", select: str = "*"):
    """SELECT rows. `filters` is the raw PostgREST query, e.g. `id=eq.member`."""
    query = f"{table}?select={select}"
    if filters:
        query += f"&{filters}"
    status, raw = supa("GET", query)
    if status != 200:
        return []
    try:
        rows = json.loads(raw)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def supa_update(table: str, filters: str, patch: dict) -> bool:
    """UPDATE rows matched by `filters`."""
    status, _ = supa("PATCH", f"{table}?{filters}", data=patch)
    return 200 <= status < 300


def supa_upsert(table: str, row, on_conflict: str) -> bool:
    """INSERT ... ON CONFLICT DO UPDATE, via PostgREST's merge-duplicates."""
    status, _ = supa(
        "POST",
        f"{table}?on_conflict={on_conflict}",
        data=row,
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    return 200 <= status < 300


# ---------------------------------------------------------------------------
# STATE SYNC — the `bot_state` table
#
# The JSON files are what the code reads and are always authoritative while
# they exist. Supabase holds a copy of each one so that a fresh host, a wiped
# data directory or a lost disk can come back exactly as it was: at startup any
# file that is missing is rebuilt from its row, and after every save() the row
# is refreshed on the heartbeat.
# ---------------------------------------------------------------------------


def _state_document(store) -> str:
    """A store's contents as comparable JSON text (stable key order)."""
    try:
        return json.dumps(store.data, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return ""


def _state_summary(document) -> str:
    if isinstance(document, dict):
        return f"{len(document)} record(s)"
    if isinstance(document, list):
        return f"{len(document)} item(s)"
    return "data"


def fetch_state_documents():
    """Read every `bot_state` row in one request.

    Returns (documents, status, body). `documents` is None whenever the table
    could not be read, whatever the reason, so a caller can tell "empty table"
    (a plain {}) from "no answer", which mean very different things.
    """
    status, raw = supa("GET", "bot_state?select=id,data", timeout=8)
    if status != 200:
        return None, status, raw
    try:
        rows = json.loads(raw)
    except Exception:
        return None, status, "the response was not JSON"
    if not isinstance(rows, list):
        return None, status, "the response was not a list of rows"
    documents = {}
    for row in rows:
        if isinstance(row, dict) and row.get("id") is not None:
            documents[str(row["id"])] = row.get("data")
    return documents, status, raw


def restore_stores(retry_after: float = 0.0) -> int:
    """Rebuild any state file this host does not have from Supabase.

    Called at startup before the bot logs in, so nothing can read or write an
    empty store first. A file that is already there is never touched — the local
    copy wins — and a fetch that fails leaves pushing switched off until a read
    succeeds, because the local defaults must never overwrite a copy we could not
    read. Returns how many files came back.
    """
    global _STATE_PULLED, _STATE_REMOTE_READABLE, _STATE_LAST_PULL

    now = time.time()
    if _STATE_PULLED and retry_after and (now - _STATE_LAST_PULL) < retry_after:
        return 0

    _STATE_PULLED = True
    _STATE_LAST_PULL = now

    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        _STATE_REMOTE_READABLE = False
        return 0

    documents, status, raw = fetch_state_documents()
    if documents is None:
        if status == 404:
            log("State sync: the bot_state table is missing — run wispbyte_schema.sql in "
                "the Supabase SQL editor to turn the state backup on.")
        else:
            log(f"State sync: could not read bot_state ({status or 'no answer'}) — using the "
                f"local files only, and holding off on writes until it can be read again. "
                f"{(raw or '')[:120]}")
        _STATE_REMOTE_READABLE = False
        return 0

    _STATE_REMOTE_READABLE = True

    restored = 0
    for store in _STORES:
        if store.loaded_from_disk or store.name not in documents:
            continue
        document = documents[store.name]
        if not isinstance(document, (dict, list)):
            continue
        store.data = document
        store.loaded_from_disk = True
        store._write_file()
        # It came from the table, so there is nothing to send back.
        _STATE_PUSHED[store.name] = _state_document(store)
        restored += 1
        log(f"State sync: restored {store.name}.json from Supabase "
            f"({_state_summary(document)}).")

    if restored:
        log(f"State sync: {restored} file(s) came back from Supabase.")
    return restored


def mirror_state(force: bool = False) -> None:
    """Push every changed state file to the `bot_state` table.

    Runs on the heartbeat, once a minute: save() only flags a store, so no
    command ever waits on the network. Documents that have not changed since the
    last push are skipped, a failure puts the stores back for the next attempt,
    and nothing is sent while the table could not be read (see restore_stores).
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return

    if _STATE_REMOTE_READABLE is not True:
        restore_stores(retry_after=STATE_RETRY_SECONDS)
        if _STATE_REMOTE_READABLE is not True:
            return

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    pending = []   # (name, document, row)

    for store in _STORES:
        if not force and store.name not in _STATE_DIRTY:
            continue
        document = _state_document(store)
        if not document:
            continue
        if not force and _STATE_PUSHED.get(store.name) == document:
            _STATE_DIRTY.discard(store.name)
            continue
        pending.append((
            store.name,
            document,
            {"id": store.name, "data": store.data, "updated_at": now},
        ))

    _STATE_DIRTY.clear()
    if not pending:
        return

    for start in range(0, len(pending), 100):
        chunk = pending[start:start + 100]
        status, raw = supa(
            "POST",
            "bot_state?on_conflict=id",
            data=[row for _, _, row in chunk],
            extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if not (200 <= status < 300):
            # Retry next pass rather than losing the write.
            _STATE_DIRTY.update(name for name, _, _ in chunk)
            log(f"State sync: could not save {len(chunk)} state file(s) "
                f"({status}): {raw[:200]}")
            return
        for name, document, _ in chunk:
            _STATE_PUSHED[name] = document


# ---------------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------------

HTTP_UA = "wispcord/1.0 (+member bot)"

FALLBACK_CAT_FACTS = [
    "Cats sleep for around 13 to 16 hours a day.",
    "A group of cats is called a clowder.",
    "Cats cannot taste sweetness.",
]
FALLBACK_DOG_FACTS = [
    "Dogs have about 1,700 taste buds — humans have around 9,000.",
    "A dog's nose print is as unique as a human fingerprint.",
    "Dogs dream, and they twitch when they do.",
]
FALLBACK_FOX_FACTS = [
    "Foxes use the Earth's magnetic field to judge distance when pouncing.",
    "A fox's hearing can detect a watch ticking 40 yards away.",
    "Arctic foxes change coat colour with the seasons.",
]
FALLBACK_JOKES = [
    ("Why don't scientists trust atoms?", "Because they make up everything."),
    ("What do you call a fake noodle?", "An impasta."),
    ("Why did the scarecrow win an award?", "Because he was outstanding in his field."),
]
FALLBACK_QUOTES = [
    ("The only way to do great work is to love what you do.", "Steve Jobs"),
    ("It always seems impossible until it's done.", "Nelson Mandela"),
    ("Whether you think you can or you think you can't, you're right.", "Henry Ford"),
]
# meme-api.com is occasionally slow, so the fallback rotation is long enough
# that a repeat is unlikely if it is down for a whole session.
FALLBACK_MEMES = [
    "https://i.imgflip.com/1bij.jpg",
    "https://i.imgflip.com/26am.jpg",
    "https://i.imgflip.com/1ur9b0.jpg",
    "https://i.imgflip.com/1otk96.jpg",
    "https://i.imgflip.com/1ihzfe.jpg",
    "https://i.imgflip.com/1yh6wx.jpg",
    "https://i.imgflip.com/9ehk.jpg",
    "https://i.imgflip.com/1e7ql7.jpg",
    "https://i.imgflip.com/2fm6x.jpg",
    "https://i.imgflip.com/1bgw.jpg",
]

# The 8-ball answers, unchanged from the admin bot so members see the same set.
EIGHTBALL = [
    "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes definitely.",
    "You may rely on it.", "As I see it, yes.", "Most likely.", "Outlook good.",
    "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
    "Better not tell you now.", "Cannot predict now.", "Concentrate and ask again.",
    "Don't count on it.", "My reply is no.", "My sources say no.",
    "Outlook not so good.", "Very doubtful.",
]


def http_json(url: str, timeout: int = 6):
    """GET a JSON document, returning None on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def daily_seed(*parts) -> int:
    """A stable number for one user and one calendar day.

    Commands like `>iq` must not reroll on every call, or people just spam
    them until they get a number they like. Hashing the user id with today's
    date pins the answer for the day and rerolls at midnight UTC.
    """
    day = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    raw = "|".join([str(p) for p in parts] + [day])
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12], 16)


def daily_rng(*parts) -> random.Random:
    """A Random seeded from daily_seed, so a daily roll is stable all day."""
    return random.Random(daily_seed(*parts))


def human_delta(seconds: int) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def human_span(seconds: float) -> str:
    """A gone-for duration in words: seconds, minutes, hours, or days.

    Seconds only ever appear under a minute; above that the value is rounded
    down to whole minutes and zero units are dropped outright.
    """
    total = int(max(0, seconds))
    if total < 60:
        return f"{total} second{'s' if total != 1 else ''}"

    minutes = total // 60
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")

    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{parts[0]}, {parts[1]}, and {parts[2]}"


def uptime_text() -> str:
    return human_delta(int(time.time() - START_TIME))


def latency_ms() -> int:
    """Gateway latency in milliseconds, safe at any point in the lifecycle.

    `bot.latency` is NaN before the first heartbeat and can be `inf` during the
    initial handshake. `round(inf)` raises OverflowError, so filtering only NaN
    would crash the command at exactly the moment a member is most likely to try
    it — right after a restart.
    """
    latency = bot.latency
    if math.isnan(latency) or math.isinf(latency):
        return 0
    return round(latency * 1000)


def bar(filled: int, total: int = 10) -> str:
    filled = max(0, min(total, filled))
    return "\u2588" * filled + "\u2591" * (total - filled)


def parse_amount(raw: str, available: int):
    """`all` / `half` / a plain number, capped at what is available."""
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if text in ("all", "max"):
        return available if available > 0 else None
    if text == "half":
        return available // 2 if available >= 2 else None
    if not text.isdigit():
        return None
    amount = int(text)
    if amount < 1:
        return None
    return min(amount, available) if available > 0 else None


async def try_delete(message) -> None:
    """Delete a message, ignoring a missing-manage_messages permission."""
    try:
        await message.delete()
    except Exception:
        pass


async def send_clean(ctx: commands.Context, *, content=None, embed=None, view=None, delete_after=None):
    """`ctx.send` with a delete_after that degrades to a normal send.

    Discord refuses `delete_after` when the bot cannot manage messages, so the
    cleanup is best-effort in both directions.
    """
    kwargs = {}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if delete_after:
        try:
            return await ctx.send(delete_after=delete_after, **kwargs)
        except Exception:
            pass
    try:
        return await ctx.send(**kwargs)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ECONOMY — balances, xp, inventory
# ---------------------------------------------------------------------------

CURRENCY = "\U0001FA99"  # 🪙

SHOP_ITEMS = {
    "lucky_coin": {"name": "Lucky Coin", "price": 750, "desc": "A shiny coin. Purely decorative."},
    "rubber_duck": {"name": "Rubber Duck", "price": 400, "desc": "Explains bugs. Never judges."},
    "top_hat": {"name": "Top Hat", "price": 1500, "desc": "For when you mean business."},
    "crown": {"name": "Tiny Crown", "price": 5000, "desc": "The most expensive thing here."},
    "mystery_box": {"name": "Mystery Box", "price": 1000, "desc": "Opens into 100–2,000 coins. Usually a loss."},
}


def acct(uid) -> dict:
    """The economy record for a user, created on first touch."""
    key = str(uid)
    rec = economy.data.get(key)
    if not isinstance(rec, dict):
        rec = {}
        economy.data[key] = rec
    rec.setdefault("balance", 0)
    rec.setdefault("bank", 0)
    rec.setdefault("xp", 0)
    rec.setdefault("wins", 0)
    rec.setdefault("losses", 0)
    rec.setdefault("inventory", {})
    rec.setdefault("last_daily", 0)
    rec.setdefault("last_work", 0)
    rec.setdefault("last_rob", 0)
    rec.setdefault("work_count", 0)
    rec.setdefault("last_qwork", 0)
    rec.setdefault("qwork_welcomed", False)
    rec.setdefault("streak", 0)
    rec.setdefault("best_streak", 0)
    return rec


def add_coins(uid, amount: int) -> dict:
    rec = acct(uid)
    rec["balance"] = max(0, int(rec["balance"]) + int(amount))
    economy.save()
    return rec


def add_xp(uid, amount: int = 5) -> None:
    rec = acct(uid)
    rec["xp"] = int(rec["xp"]) + int(amount)
    economy.save()


def level_of(rec: dict) -> int:
    """Level curve: every level costs a bit more than the last."""
    xp = int(rec.get("xp", 0))
    return int((xp / 50) ** 0.5) + 1


def level_progress(rec: dict):
    """(current level, xp into the level, xp the level needs)."""
    level = level_of(rec)
    floor_xp = 50 * (level - 1) ** 2
    next_xp = 50 * level ** 2
    return level, int(rec.get("xp", 0)) - floor_xp, max(1, next_xp - floor_xp)


def parse_bet(raw: str, rec: dict):
    """Parse a bet from "all", "half" or a number. Returns None when invalid."""
    text = str(raw or "").strip().lower()
    balance = int(rec.get("balance", 0))
    if not text:
        return None
    if text in ("all", "max"):
        return balance if balance > 0 else None
    if text == "half":
        return balance // 2 if balance >= 2 else None
    if not text.isdigit():
        return None
    amount = int(text)
    if amount < 1 or amount > balance:
        return None
    return amount


def total_coins(rec: dict) -> int:
    return int(rec.get("balance", 0)) + int(rec.get("bank", 0))


def streak_multiplier(streak: int) -> float:
    if streak >= 30:
        return 3.0
    if streak >= 14:
        return 2.5
    if streak >= 7:
        return 2.0
    if streak >= 3:
        return 1.5
    return 1.0


# ---------------------------------------------------------------------------
# WORK — one active job per member across both modes
# ---------------------------------------------------------------------------


def qwork_tier(run: int) -> int:
    """Payout for the run being started right now (1-based)."""
    if run <= 10:
        return 200
    if run <= 30:
        return 235
    if run <= 60:
        return 260
    if run <= 100:
        return 295
    return 350


def passive_payout(elapsed: float):
    """(coins, whole minutes) using the stepwise rate table, rounded down."""
    minutes = int(elapsed // 60)
    if minutes < 1:
        return 0, minutes
    first = min(minutes, 60)
    second = min(max(minutes - 60, 0), 90)
    third = max(minutes - 150, 0)
    return first * 15 + second * 25 + third * 35, minutes


def work_cooldown_for(minutes: int) -> int:
    """Seconds of cooldown earned by a passive job of this length."""
    if minutes < 15:
        return 600
    if minutes < 30:
        return 900
    if minutes < 60:
        return 1200
    if minutes <= 180:
        return 2700
    if minutes <= 420:
        return 7200
    return 14400


def work_cooldown_left(uid) -> int:
    """Seconds left on a passive `>work` cooldown, 0 when free."""
    until = WORK_COOLDOWN_UNTIL.get(str(uid), 0)
    return max(0, int(until - time.time()))


def job_title(uid) -> str:
    """A stable job name per member, so their DM names the same job."""
    seed = int(hashlib.sha256(str(uid).encode("utf-8")).hexdigest()[:8], 16)
    return JOB_NAMES[seed % len(JOB_NAMES)]


def marriage_map(guild_id) -> dict:
    guild = marriages.data.get(str(guild_id))
    if not isinstance(guild, dict):
        guild = {}
        marriages.data[str(guild_id)] = guild
    return guild


def partner_of(guild_id, uid):
    return marriage_map(guild_id).get(str(uid))


def afk_map(guild_id) -> dict:
    guild = afk.data.get(str(guild_id))
    if not isinstance(guild, dict):
        guild = {}
        afk.data[str(guild_id)] = guild
    return guild


def parse_duration(raw: str):
    """`30s` / `10m` / `2h` / `1d` to seconds, or None when unusable."""
    match = re.fullmatch(r"(\d+)\s*([smhd])", str(raw or "").strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    seconds = amount * factor
    if seconds < 1 or seconds > REMINDER_MAX_SECONDS:
        return None
    return seconds


# ---------------------------------------------------------------------------
# MINIGAME DATA
# ---------------------------------------------------------------------------

WOULD_YOU_RATHER = [
    ("always be 10 minutes late", "always be 20 minutes early"),
    ("have unlimited money but no friends", "have great friends but no money"),
    ("never use social media again", "never watch another movie or show"),
    ("be able to fly", "be able to turn invisible"),
    ("only eat sweet food forever", "only eat savoury food forever"),
    ("know how you die", "know when you die"),
    ("always speak in rhymes", "always speak in song"),
    ("have a rewind button for your life", "have a pause button for your life"),
]

TRUTHS = [
    "What's the most embarrassing thing you've searched for?",
    "What's a lie you told that you still feel bad about?",
    "Who in this server would you trust with your password?",
    "What's the pettiest reason you've ever disliked someone?",
    "What's the last thing you cried about?",
]
DARES = [
    "Send the last photo in your camera roll — describe it if you won't post it.",
    "Type your next three messages with your eyes closed.",
    "Change your nickname to something embarrassing for 10 minutes.",
    "Send a voice message singing the chorus of the last song you played.",
    "Write a haiku about the person above you.",
]

TRIVIA = [
    ("Which planet is known as the Red Planet?", ["Mars", "Venus", "Jupiter", "Mercury"], 0),
    ("What is the largest ocean on Earth?", ["Pacific", "Atlantic", "Indian", "Arctic"], 0),
    ("How many sides does a hexagon have?", ["6", "5", "7", "8"], 0),
    ("Who wrote 'Romeo and Juliet'?", ["Shakespeare", "Dickens", "Austen", "Tolstoy"], 0),
    ("What is the chemical symbol for gold?", ["Au", "Ag", "Gd", "Go"], 0),
    ("Which language has the most native speakers?", ["Mandarin", "English", "Spanish", "Hindi"], 0),
    ("What year did the Berlin Wall fall?", ["1989", "1991", "1985", "1979"], 0),
    ("What is the smallest prime number?", ["2", "1", "3", "0"], 0),
    ("Which country invented tea?", ["China", "India", "England", "Japan"], 0),
    ("How many bones are in the adult human body?", ["206", "198", "212", "220"], 0),
    ("What does HTTP stand for?", ["HyperText Transfer Protocol", "High Transfer Text Protocol", "HyperText Transmission Process", "Host Transfer Text Protocol"], 0),
    ("Which gas do plants absorb?", ["Carbon dioxide", "Oxygen", "Nitrogen", "Helium"], 0),
]

FLAGS = [
    ("\U0001F1EF\U0001F1F5", "Japan"),
    ("\U0001F1E8\U0001F1E6", "Canada"),
    ("\U0001F1E7\U0001F1F7", "Brazil"),
    ("\U0001F1E6\U0001F1FA", "Australia"),
    ("\U0001F1EE\U0001F1F3", "India"),
    ("\U0001F1EB\U0001F1F7", "France"),
    ("\U0001F1E9\U0001F1EA", "Germany"),
    ("\U0001F1EA\U0001F1F8", "Spain"),
    ("\U0001F1F5\U0001F1F9", "Portugal"),
    ("\U0001F1F3\U0001F1F1", "Netherlands"),
    ("\U0001F1F8\U0001F1EA", "Sweden"),
    ("\U0001F1F3\U0001F1F4", "Norway"),
    ("\U0001F1F0\U0001F1F7", "South Korea"),
    ("\U0001F1F2\U0001F1FD", "Mexico"),
    ("\U0001F1EE\U0001F1F9", "Italy"),
    ("\U0001F1EA\U0001F1EC", "Egypt"),
]

SCRAMBLE_WORDS = [
    "discord", "keyboard", "mountain", "elephant", "sandwich", "triangle",
    "umbrella", "penguin", "volcano", "lighthouse", "strawberry", "chocolate",
    "adventure", "midnight", "treasure", "galaxy", "whisper", "dragon",
]

HANGMAN_WORDS = [
    "javascript", "python", "elephant", "spaghetti", "telescope", "hurricane",
    "basketball", "notebook", "waterfall", "penguin", "chocolate", "airplane",
    "sunflower", "dinosaur", "library", "trampoline", "candle", "pyramid",
]

FORTUNES = [
    "A small yes is coming your way today.",
    "Someone is about to owe you an apology.",
    "Your patience runs out long before your luck does.",
    "The thing you keep putting off takes eleven minutes.",
    "Say the awkward thing. It lands better than the silence.",
    "Money arrives from a direction you stopped checking.",
    "You will be right, and it will not matter. Enjoy it anyway.",
    "A door you thought was locked is only stiff.",
    "Rest is the productive choice this week.",
    "The person you keep meaning to message is thinking of you too.",
]

AURA_VERDICTS = [
    "Immaculate. Do not let anyone else touch it.",
    "Heavy, but in a load-bearing way.",
    "Suspiciously calm for someone in this economy.",
    "You radiate mild chaos. Charismatic, though.",
    "Someone nearby is draining you. Stand up straighter.",
    "The vibe is loud. Tune something down.",
]

MATCH_VERDICTS = [
    (90, "Soulmate material. Nobody here deserves this."),
    (75, "Dangerously compatible. Proceed carefully."),
    (55, "There is something here worth a conversation."),
    (35, "You would last a weekend. Maybe a good weekend."),
    (15, "This is a personality clash with a shared playlist."),
    (0, "One of you is the other's villain origin story."),
]

CRUSH_VERDICTS = [
    (85, "They have absolutely noticed you. Say something."),
    (65, "There is a real chance. Stop rehearsing."),
    (45, "Friendly is doing a lot of work in that sentence."),
    (25, "They think of you as a person who exists. Progress."),
    (0, "Your chances are theoretical at this point."),
]

# Nekos.best categories for the GIF commands. Names that are not real
# categories there borrow the closest fitting one.
GIF_KINDS = {
    "hug": "hug",
    "pat": "pat",
    "boop": "poke",
    "kill": "kick",
    "kiss": "kiss",
    "marry": "kiss",
    "divorce": "cry",
    "ship": "cuddle",
    "slap": "slap",
    "cuddle": "cuddle",
    "bite": "bite",
    "lick": "nom",
    "punch": "punch",
    "kick": "kick",
    "wave": "wave",
    "highfive": "highfive",
    "handhold": "handhold",
    "feed": "feed",
    "baka": "baka",
    "pout": "pout",
    "stare": "stare",
    "pouty": "pout",
    "blush": "blush",
    "smug": "smug",
    "dance": "dance",
    "cry": "cry",
    "smile": "happy",
    "think": "think",
    "thumbsup": "thumbsup",
    "nom": "nom",
    "yawn": "yawn",
    "sleepy": "sleep",
    "shy": "blush",
}


def fetch_gif(kind: str):
    """One GIF URL for an action, or None when the API is unreachable.

    Nothing here is allowed to raise: a missing GIF should cost the command its
    picture, never its response.
    """
    category = GIF_KINDS.get(kind, "hug")
    data = http_json(f"https://nekos.best/api/v2/{category}?amount=1")
    try:
        results = data.get("results") or []
        url = results[0].get("url")
        if url:
            return url
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# QUOTE CARD — Pillow, run off the event loop
# ---------------------------------------------------------------------------

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # Pillow missing: `>quote` falls back to plain text
    Image = ImageDraw = ImageFont = None

QUOTE_ACCENT = (88, 101, 242)
QUOTE_MAX_CHARS = 600
QUOTE_WRAP_COLUMNS = 60
QUOTE_MAX_LINES = 8

FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)
FONT_BOLD_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)


def pick_font(paths, size: int):
    """First font file that exists, else Pillow's small built-in default."""
    if ImageFont is None:
        return None
    for path in paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def wrap_quote(text: str, columns: int = QUOTE_WRAP_COLUMNS):
    """Word-wrap to `columns`, hard-splitting words that are longer."""
    lines = []
    for paragraph in str(text).splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            while len(word) > columns:
                if current:
                    lines.append(current)
                    current = ""
                lines.append(word[:columns])
                word = word[columns:]
            candidate = f"{current} {word}".strip()
            if len(candidate) <= columns:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


def _circle_avatar(data: bytes, size: int):
    image = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    image.putalpha(mask)
    return image


def _paste_circle(card, data: bytes, box, size: int) -> None:
    avatar = _circle_avatar(data, size)
    card.paste(avatar, box, avatar)


def render_quote_card(text: str, display_name: str, stamp: str, accent, avatar_bytes: bytes, bot_name: str, bot_icon=None) -> bytes:
    """The PNG bytes for one quote card. Raises on any failure — the caller
    turns that into the plain-text fallback."""
    if Image is None or ImageDraw is None:
        raise RuntimeError("Pillow is not installed")

    body = str(text)[:QUOTE_MAX_CHARS]
    lines = wrap_quote(body)
    truncated = len(lines) > QUOTE_MAX_LINES
    lines = lines[:QUOTE_MAX_LINES]
    if truncated and lines:
        lines[-1] = (lines[-1][: QUOTE_WRAP_COLUMNS - 1].rstrip() + "\u2026")

    font_name = pick_font(FONT_BOLD_PATHS, 27)
    font_stamp = pick_font(FONT_PATHS, 17)
    font_body = pick_font(FONT_PATHS, 23)
    font_footer = pick_font(FONT_PATHS, 15)

    pad = 30
    avatar_size = 96
    text_left = pad + avatar_size + 26
    line_height = 33
    width = 940
    height = pad + 40 + max(1, len(lines)) * line_height + 44 + pad
    height = max(height, 210)

    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=22, fill=(30, 33, 40, 255))
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=22, outline=tuple(accent) + (255,), width=2)
    draw.rounded_rectangle((0, 0, 7, height - 1), radius=3, fill=tuple(accent) + (255,))

    _paste_circle(card, avatar_bytes, (pad, pad + 4), avatar_size)

    draw.text((text_left, pad), display_name[:44], font=font_name, fill=(255, 255, 255, 255))
    draw.text((text_left, pad + 38), stamp, font=font_stamp, fill=(160, 168, 182, 255))

    y = pad + 76
    for line in lines:
        draw.text((text_left, y), line, font=font_body, fill=(226, 230, 238, 255))
        y += line_height

    footer_y = height - pad - 14
    if bot_icon:
        try:
            _paste_circle(card, bot_icon, (pad, footer_y - 4), 22)
        except Exception:
            pass
    draw.text((pad + 30, footer_y), str(bot_name)[:32], font=font_footer, fill=(140, 148, 162, 255))

    buffer = io.BytesIO()
    card.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# BOT SETUP
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

MEMBER_HELP = [
    ("Fun", [
        ("roll [4d6+2]", "Roll dice, with optional modifiers"),
        ("coinflip", "Flip a coin"),
        ("8ball <question>", "Ask the magic 8-ball"),
        ("choose a | b | c", "Pick one of your options"),
        ("rate <thing>", "Rate anything out of 10"),
        ("wyr", "Would you rather"),
        ("truth", "Answer a truth question"),
        ("dare", "Take on a dare"),
        ("iq @user", "Today's IQ reading"),
        ("rizz @user", "Today's rizz reading"),
        ("hotcalc @user", "Today's hotcalc reading"),
        ("fortune @user", "Today's fortune"),
        ("aura @user", "Today's aura reading"),
        ("femboy @user", "Today's femboy energy"),
        ("catboy @user", "Today's catboy energy"),
        ("crush @user1 @user2", "Today's crush chance"),
        ("match @user1 @user2", "Today's match rating"),
    ]),
    ("Actions", [
        ("hug @user", "Hug someone"),
        ("pat @user", "Pat someone"),
        ("boop @user", "Boop someone"),
        ("kill @user", "Dramatically end someone"),
        ("kiss @user", "Kiss someone"),
        ("slap @user", "Slap someone"),
        ("cuddle @user", "Cuddle someone"),
        ("bite @user", "Bite someone"),
        ("lick @user", "Lick someone"),
        ("punch @user", "Punch someone"),
        ("kick @user", "Kick someone"),
        ("wave @user", "Wave at someone"),
        ("highfive @user", "High-five someone"),
        ("handhold @user", "Hold someone's hand"),
        ("feed @user", "Feed someone"),
        ("baka @user", "Call someone a baka"),
        ("pout @user", "Pout at someone"),
        ("stare @user", "Stare at someone"),
        ("pouty @user", "Get pouty at someone"),
        ("blush", "Blush"),
        ("smug", "Look smug"),
        ("dance", "Dance"),
        ("cry", "Cry"),
        ("smile", "Smile"),
        ("think", "Think"),
        ("thumbsup", "Give a thumbs up"),
        ("nom", "Nom"),
        ("yawn", "Yawn"),
        ("sleepy", "Look sleepy"),
        ("shy", "Act shy"),
        ("ship @user1 @user2", "Ship two people"),
        ("marry @user", "Marry someone"),
        ("divorce @user", "Divorce someone"),
        ("afk [reason]", "Mark yourself as away"),
    ]),
    ("Trivia", [
        ("trivia", "Answer a multiple-choice question"),
        ("flags", "Guess the country from its flag"),
        ("scramble", "Unscramble the word"),
        ("wordchain", "Start a word chain"),
        ("hangman", "Play hangman"),
        ("guess", "Guess the number 1-100"),
    ]),
    ("Economy", [
        ("daily", "Claim your daily coins"),
        ("streak", "Show your daily streak"),
        ("balance", "Show your coins"),
        ("bank deposit <amount>", "Move coins into your bank"),
        ("bank withdraw <amount>", "Move coins out of your bank"),
        ("richest", "The richest member right now"),
        ("work", "Start or stop a passive job"),
        ("qwork", "Quick 2-minute job"),
        ("slots <bet>", "Play the slot machine"),
        ("blackjack <bet>", "Play blackjack"),
        ("rob @user", "Try to rob someone"),
        ("shop", "Browse the shop"),
        ("shop buy <item>", "Buy an item"),
        ("gift @user <amount>", "Give coins to someone"),
    ]),
    ("Social", [
        ("baltop", "Richest members"),
        ("banktop", "Largest bank balances"),
        ("leveltop", "Highest levels"),
        ("wintop", "Most casino wins"),
        ("lbstats", "Leaderboard summary"),
        ("profile [@user]", "Your profile card"),
        ("compare @a @b", "Compare two members"),
        ("stats", "Bot statistics"),
        ("quote", "Turn a replied-to message into an image"),
        ("reminder <duration> <text>", "Set a reminder"),
        ("reminders", "Show your pending reminder"),
        ("remindercancel", "Cancel your pending reminder"),
    ]),
    ("Time-wasters", [
        ("meme", "Random meme"),
        ("catfact", "A cat fact"),
        ("dogfact", "A dog fact"),
        ("foxfact", "A fox fact"),
        ("joke", "A random joke"),
        ("randomquote", "A random quote"),
        ("urban <term>", "Look a term up on Urban Dictionary"),
    ]),
    ("Info", [
        ("ping", "Check bot latency"),
        ("serverinfo", "Info about this server"),
        ("userinfo [@user]", "Info about a user"),
        ("avatar [@user]", "Show a user's avatar"),
        ("banner [@user]", "Show a user's banner"),
        ("roleinfo <role>", "Info about a role"),
        ("channelinfo [#channel]", "Info about a channel"),
        ("uptime", "Show bot uptime"),
        ("help", "Show this menu"),
    ]),
]


@bot.event
async def on_ready():
    log(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log(f"Prefix: {COMMAND_PREFIX!r} · serving {len(bot.guilds)} guild(s)")
    if MEMBER_SITE_URL:
        log(f"Ticket panel link: {SUPPORT_PAGE_URL}")
    else:
        log("MEMBER_SITE_URL is not set — the ticket panel will send plain text.")
    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        host = SUPABASE_URL.split("//")[-1].split("/")[0]
        log(f"Supabase heartbeat: configured -> {host} "
            f"(key {SUPABASE_SERVICE_KEY[:6]}…, {len(SUPABASE_SERVICE_KEY)} chars)")
    else:
        log("Supabase heartbeat: disabled (SUPABASE_URL / SUPABASE_SERVICE_KEY missing)")


@bot.event
async def on_command_completion(ctx: commands.Context):
    """Count command usage in memory for the `>stats` line. Not persisted."""
    if ctx.command:
        COMMAND_USES[ctx.command.qualified_name] += 1


@bot.event
async def on_command_error(ctx: commands.Context, error):
    """Never let a member see a traceback — just a short, plain line."""
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Slow down — try again in {error.retry_after:.1f}s.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing something: `{error.param.name}`. Try `{COMMAND_PREFIX}help`.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("That argument did not look right. Try `" + COMMAND_PREFIX + "help`.")
    else:
        log(f"Command error in {ctx.command}: {type(error).__name__}: {error}")
        await ctx.send("Something went wrong running that. Try again in a moment.")


# ---------------------------------------------------------------------------
# MESSAGE WATCHER — AFK notices and the marry reaction prompt
# ---------------------------------------------------------------------------


async def _strip_foreign_reactions(message, target_id: int, stop_event: asyncio.Event) -> None:
    """Remove every reaction that is not the waiting member's own.

    Listeners registered by discord.py have to be awaited and quickly returns
    control, so the real work runs in a task that lives only until the prompt
    resolves.
    """
    def check(payload: discord.RawReactionActionEvent):
        if payload.message_id != message.id or payload.user_id == target_id:
            return False
        member = payload.member
        if member is not None and member.bot:
            return False
        return True

    while not stop_event.is_set():
        try:
            payload = await bot.wait_for("raw_reaction_add", timeout=0.5, check=check)
        except asyncio.TimeoutError:
            continue
        except Exception:
            return
        try:
            await message.remove_reaction(payload.emoji, discord.Object(id=payload.user_id))
        except Exception:
            pass


async def await_reaction(message, target_id: int, emojis, timeout: float = 60.0):
    """Wait for `target_id` to react on `message` with one of `emojis`.

    Returns the emoji as a string, or None on timeout. Reactions from anyone
    else are stripped from the message while the wait is running.
    """
    stop_event = asyncio.Event()
    cleaner = asyncio.create_task(_strip_foreign_reactions(message, target_id, stop_event))

    def check(reaction, user):
        return user.id == target_id and str(reaction.emoji) in emojis and reaction.message.id == message.id

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=timeout, check=check)
        return str(reaction.emoji)
    except asyncio.TimeoutError:
        return None
    finally:
        stop_event.set()
        cleaner.cancel()


@bot.event
async def on_message(message: discord.Message):
    """AFK handling, then the normal command pipeline.

    Registered via @bot.event so exactly one instance runs; `bot.add_listener`
    would stack another copy every hot reload.
    """
    try:
        await _afk_watch(message)
    except Exception as exc:
        log(f"AFK watcher failed: {type(exc).__name__}: {exc}")
    await bot.process_commands(message)


async def _afk_watch(message: discord.Message) -> None:
    if not message.guild or message.author.bot or message.is_system():
        return

    guild_map = afk_map(message.guild.id)

    entry = guild_map.pop(str(message.author.id), None)
    if entry:
        since = float(entry.get("since", time.time())) if isinstance(entry, dict) else time.time()
        afk.save()
        await message.channel.send(
            f"Welcome back {message.author.mention}, you were gone for {human_span(time.time() - since)}."
        )

    if not guild_map:
        return

    found = []
    for mention in message.mentions:
        if mention.id == message.author.id or mention.bot:
            continue
        key = str(mention.id)
        if key in guild_map and key not in found:
            found.append(key)

    replied = getattr(message, "reference", None)
    resolved = getattr(replied, "resolved", None) if replied else None
    if isinstance(resolved, discord.Message) and resolved.author and not resolved.author.bot:
        key = str(resolved.author.id)
        if resolved.author.id != message.author.id and key in guild_map and key not in found:
            found.append(key)

    if not found:
        return

    lines = []
    for key in found:
        data = guild_map.get(key) or {}
        reason = (data.get("reason") if isinstance(data, dict) else None) or "AFK"
        lines.append(f"<@{key}> is AFK: {reason}")
    await message.channel.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


# ---------------------------------------------------------------------------
# MEMBER COMMANDS — ported from the admin bot's bot.py, behaviour unchanged
# ---------------------------------------------------------------------------


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.send(f"> Latency: **{latency_ms()}ms**")


@bot.command(name="serverinfo", aliases=["si"])
async def serverinfo(ctx: commands.Context):
    g = ctx.guild
    embed = discord.Embed(title=g.name, color=discord.Color.blurple())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Owner", value=g.owner.mention if g.owner else "Unknown")
    embed.add_field(name="ID", value=str(g.id))
    embed.add_field(name="Members", value=str(g.member_count))
    embed.add_field(name="Channels", value=str(len(g.channels)))
    embed.add_field(name="Roles", value=str(len(g.roles)))
    embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
    await ctx.send(embed=embed)


@bot.command(name="userinfo", aliases=["whois"])
async def userinfo(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=str(member), color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=str(member.id))
    embed.add_field(name="Bot", value=str(member.bot))
    embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "?")
    embed.add_field(name="Created", value=discord.utils.format_dt(member.created_at, "R"))
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    embed.add_field(name="Roles", value=", ".join(roles[:10]) or "None", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="avatar", aliases=["av"])
async def avatar(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    await ctx.send(member.display_avatar.url)


@bot.command(name="banner")
async def banner(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    user = await bot.fetch_user(member.id)
    if not user.banner:
        return await ctx.send("> [!] That user has no banner.")
    await ctx.send(user.banner.url)


@bot.command(name="roleinfo", aliases=["ri"])
async def roleinfo(ctx: commands.Context, *, role: discord.Role):
    embed = discord.Embed(title=role.name, color=role.color)
    embed.add_field(name="ID", value=str(role.id))
    embed.add_field(name="Color", value=str(role.color))
    embed.add_field(name="Members", value=str(len(role.members)))
    embed.add_field(name="Mentionable", value=str(role.mentionable))
    embed.add_field(name="Hoisted", value=str(role.hoist))
    embed.add_field(name="Position", value=str(role.position))
    await ctx.send(embed=embed)


@bot.command(name="channelinfo", aliases=["ci"])
async def channelinfo(ctx: commands.Context, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    embed = discord.Embed(title=channel.name, color=discord.Color.blurple())
    embed.add_field(name="ID", value=str(channel.id))
    embed.add_field(name="Type", value=str(channel.type))
    embed.add_field(name="Slowmode", value=f"{channel.slowmode_delay}s")
    embed.add_field(name="NSFW", value=str(channel.nsfw))
    embed.add_field(name="Category", value=channel.category.name if channel.category else "None")
    embed.add_field(name="Created", value=discord.utils.format_dt(channel.created_at, "R"))
    if channel.topic:
        embed.add_field(name="Topic", value=channel.topic[:1000], inline=False)
    await ctx.send(embed=embed)


@bot.command(name="uptime")
async def uptime(ctx: commands.Context):
    await ctx.send(f"> Uptime: {uptime_text()}")


@bot.command(name="help")
@commands.cooldown(rate=1, per=15, type=commands.BucketType.user)
async def help_cmd(ctx: commands.Context):
    """The member-only command list.

    Staff commands are deliberately absent, and there is no permission filter
    here at all — everything listed is something any member can run.
    """
    embed = discord.Embed(
        title="Command List",
        description=f"Prefix: `{COMMAND_PREFIX}`. Everything here is available to everyone.",
        color=discord.Color.blurple(),
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    for section, entries in MEMBER_HELP:
        lines = [f"`{COMMAND_PREFIX}{cmd}` -- {desc}" for cmd, desc in entries]
        embed.add_field(name=section, value="\n".join(lines)[:1020], inline=False)
    embed.set_footer(text="This message is only visible to you.")
    await try_delete(ctx.message)
    await send_clean(ctx, embed=embed, delete_after=45)


# ---------------------------------------------------------------------------
# FUN
# ---------------------------------------------------------------------------


@bot.command(name="roll")
async def roll(ctx: commands.Context, dice: str = "1d6"):
    """NdN with an optional modifier: `2d20`, `4d6+2`, `1d8-1`."""
    match = re.fullmatch(r"(\d*)d(\d+)([+-]\d+)?", str(dice).lower().replace(" ", ""))
    if not match:
        return await ctx.send("[X] Format: NdN, e.g. `2d20` or `4d6+2`")
    n = int(match.group(1) or 1)
    sides = int(match.group(2))
    modifier = int(match.group(3) or 0)
    if n < 1 or n > 20 or sides < 2 or sides > 1000:
        return await ctx.send("[X] Out of range. Up to 20 dice, 2-1000 sides.")
    rolls = [random.randint(1, sides) for _ in range(n)]
    total = sum(rolls) + modifier
    text = f"> Rolled {dice}: `{rolls}`"
    if modifier:
        text += f" `{modifier:+d}`"
    await ctx.send(f"{text} (total {total})")


@bot.command(name="coinflip", aliases=["cf"])
async def coinflip(ctx: commands.Context):
    await ctx.send(f"> {random.choice(['Heads', 'Tails'])}")


@bot.command(name="8ball")
async def eightball(ctx: commands.Context, *, question: str):
    await ctx.send(f"> Q: {question}\n> A: {random.choice(EIGHTBALL)}")


@bot.command(name="choose")
async def choose(ctx: commands.Context, *, options: str):
    """`>choose pizza | sushi | tacos`"""
    parts = [p.strip() for p in re.split(r"\||,", options or "") if p.strip()]
    if len(parts) < 2:
        return await ctx.send("[X] Give me at least two options, separated by `|`.")
    await ctx.send(f"> {random.choice(parts)}")


@bot.command(name="rate")
async def rate(ctx: commands.Context, *, thing: str):
    score = random.Random(str(thing).lower()).randint(0, 10)
    await ctx.send(f"> I rate **{thing}** a solid **{score}/10**.")


@bot.command(name="wyr")
async def wyr(ctx: commands.Context):
    a, b = random.choice(WOULD_YOU_RATHER)
    embed = discord.Embed(title="Would you rather…", color=discord.Color.blurple())
    embed.add_field(name="\U0001F1E6 Option A", value=a, inline=False)
    embed.add_field(name="\U0001F1E7 Option B", value=b, inline=False)
    message = await ctx.send(embed=embed)
    await message.add_reaction("\U0001F1E6")
    await message.add_reaction("\U0001F1E7")


@bot.command(name="truth")
async def truth(ctx: commands.Context):
    await ctx.send(f"> **Truth:** {random.choice(TRUTHS)}")


@bot.command(name="dare")
async def dare(ctx: commands.Context):
    await ctx.send(f"> **Dare:** {random.choice(DARES)}")


@bot.command(name="iq")
async def iq(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    value = 60 + daily_seed("iq", member.id) % 100
    await ctx.send(f"> **{member.display_name}**'s IQ today: **{value}**")


@bot.command(name="rizz")
async def rizz(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    value = daily_seed("rizz", member.id) % 101
    await ctx.send(f"> **{member.display_name}**'s rizz today: **{value}%**")


@bot.command(name="hotcalc")
async def hotcalc(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    value = daily_seed("hotcalc", member.id) % 101
    await ctx.send(f"> **{member.display_name}** is **{value}% hot** today. {bar(value // 10)}")


@bot.command(name="fortune", aliases=["fortunecookie"])
async def fortune(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    rng = daily_rng("fortune", member.id)
    await ctx.send(f"> **{member.display_name}**'s fortune: {rng.choice(FORTUNES)}")


@bot.command(name="aura")
async def aura(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    rng = daily_rng("aura", member.id)
    points = rng.randint(0, 1200)
    await ctx.send(f"> **{member.display_name}** has **{points:,} aura points** today. {rng.choice(AURA_VERDICTS)}")


@bot.command(name="femboy")
async def femboy(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    value = daily_seed("femboy", member.id) % 101
    await ctx.send(f"> **{member.display_name}** is **{value}%** femboy energy today. {bar(value // 10)}")


@bot.command(name="catboy")
async def catboy(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    value = daily_seed("catboy", member.id) % 101
    await ctx.send(f"> **{member.display_name}** is **{value}%** catboy energy today. {bar(value // 10)}")


@bot.command(name="crush", aliases=["crushrate"])
async def crush(ctx: commands.Context, first: discord.Member, second: discord.Member):
    pair = sorted([str(first.id), str(second.id)])
    percent = daily_seed("crush", pair[0], pair[1]) % 101
    verdict = next(text for threshold, text in CRUSH_VERDICTS if percent >= threshold)
    await ctx.send(f"> **{first.display_name}** has a **{percent}%** crush on **{second.display_name}**. {verdict}")


@bot.command(name="match", aliases=["matchrate"])
async def match(ctx: commands.Context, first: discord.Member, second: discord.Member):
    pair = sorted([str(first.id), str(second.id)])
    percent = daily_seed("match", pair[0], pair[1]) % 101
    verdict = next(text for threshold, text in MATCH_VERDICTS if percent >= threshold)
    embed = discord.Embed(
        title=f"{first.display_name} + {second.display_name}",
        description=f"**{percent}%** {bar(percent // 10)}\n{verdict}",
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# ACTION GIFS
# ---------------------------------------------------------------------------


async def _action(ctx: commands.Context, kind: str, target: discord.Member, verb: str):
    """Shared body for the GIF commands: fetch a picture, fall back to text."""
    if target.id == ctx.author.id:
        return await ctx.send(f"> You can't {verb} yourself… though I respect the attempt.")
    embed = discord.Embed(
        description=f"**{ctx.author.display_name}** {verb} **{target.display_name}**!",
        color=discord.Color.brand_pink() if hasattr(discord.Color, "brand_pink") else discord.Color.magenta(),
    )
    gif = fetch_gif(kind)
    if gif:
        embed.set_image(url=gif)
    await ctx.send(embed=embed)


async def _action_self(ctx: commands.Context, kind: str, verb: str):
    """The self-only half of the GIF commands — no target argument."""
    embed = discord.Embed(
        description=f"**{ctx.author.display_name}** {verb}",
        color=discord.Color.brand_pink() if hasattr(discord.Color, "brand_pink") else discord.Color.magenta(),
    )
    gif = fetch_gif(kind)
    if gif:
        embed.set_image(url=gif)
    await ctx.send(embed=embed)


@bot.command(name="hug")
async def hug(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "hug", member, "hugs")


@bot.command(name="pat")
async def pat(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "pat", member, "pats")


@bot.command(name="boop")
async def boop(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "boop", member, "boops")


@bot.command(name="kill")
async def kill(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "kill", member, "dramatically ends")


@bot.command(name="kiss")
async def kiss(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "kiss", member, "kisses")


@bot.command(name="slap")
async def slap(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "slap", member, "slaps")


@bot.command(name="cuddle")
async def cuddle(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "cuddle", member, "cuddles")


@bot.command(name="bite")
async def bite(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "bite", member, "bites")


@bot.command(name="lick")
async def lick(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "lick", member, "licks")


@bot.command(name="punch")
async def punch(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "punch", member, "punches")


@bot.command(name="kick")
async def kick(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "kick", member, "kicks")


@bot.command(name="wave")
async def wave(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "wave", member, "waves at")


@bot.command(name="highfive", aliases=["hf"])
async def highfive(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "highfive", member, "high-fives")


@bot.command(name="handhold", aliases=["holdhands"])
async def handhold(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "handhold", member, "holds hands with")


@bot.command(name="feed")
async def feed(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "feed", member, "feeds")


@bot.command(name="baka")
async def baka(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "baka", member, "calls a baka:")


@bot.command(name="pout")
async def pout(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "pout", member, "pouts at")


@bot.command(name="stare")
async def stare(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "stare", member, "stares at")


@bot.command(name="pouty")
async def pouty(ctx: commands.Context, member: discord.Member):
    await _action(ctx, "pouty", member, "gets pouty at")


@bot.command(name="blush")
async def blush(ctx: commands.Context):
    await _action_self(ctx, "blush", "blushes")


@bot.command(name="smug")
async def smug(ctx: commands.Context):
    await _action_self(ctx, "smug", "looks smug")


@bot.command(name="dance")
async def dance(ctx: commands.Context):
    await _action_self(ctx, "dance", "dances")


@bot.command(name="cry")
async def cry(ctx: commands.Context):
    await _action_self(ctx, "cry", "cries")


@bot.command(name="smile")
async def smile(ctx: commands.Context):
    await _action_self(ctx, "smile", "smiles")


@bot.command(name="think")
async def think(ctx: commands.Context):
    await _action_self(ctx, "think", "thinks")


@bot.command(name="thumbsup", aliases=["thumbs"])
async def thumbsup(ctx: commands.Context):
    await _action_self(ctx, "thumbsup", "gives a thumbs up")


@bot.command(name="nom")
async def nom(ctx: commands.Context):
    await _action_self(ctx, "nom", "noms")


@bot.command(name="yawn")
async def yawn(ctx: commands.Context):
    await _action_self(ctx, "yawn", "yawns")


@bot.command(name="sleepy", aliases=["sleep"])
async def sleepy(ctx: commands.Context):
    await _action_self(ctx, "sleepy", "looks sleepy")


@bot.command(name="shy")
async def shy(ctx: commands.Context):
    await _action_self(ctx, "shy", "acts shy")


@bot.command(name="ship")
async def ship(ctx: commands.Context, first: discord.Member, second: discord.Member = None):
    second = second or ctx.author
    pair = sorted([str(first.id), str(second.id)])
    percent = daily_seed("ship", pair[0], pair[1]) % 101
    if percent >= 90:
        verdict = "A match made in heaven."
    elif percent >= 70:
        verdict = "Very promising."
    elif percent >= 50:
        verdict = "Could work, with effort."
    elif percent >= 25:
        verdict = "Rough seas ahead."
    else:
        verdict = "Please do not."
    embed = discord.Embed(
        title=f"{first.display_name} \U00002764 {second.display_name}",
        description=f"**{percent}%** {bar(percent // 10)}\n{verdict}",
        color=discord.Color.magenta(),
    )
    gif = fetch_gif("ship")
    if gif:
        embed.set_image(url=gif)
    await ctx.send(embed=embed)


@bot.command(name="marry")
async def marry(ctx: commands.Context, member: discord.Member):
    """The partner has to accept — a marriage is not a one-sided command."""
    if member.id == ctx.author.id:
        return await ctx.send("> You cannot marry yourself. (Legally, and emotionally.)")

    guild_map = marriage_map(ctx.guild.id)
    mine = guild_map.get(str(ctx.author.id))
    theirs = guild_map.get(str(member.id))

    if mine:
        partner = ctx.guild.get_member(int(mine)) if str(mine).isdigit() else None
        name = partner.display_name if partner else "someone"
        return await ctx.send(f"> You are already married to **{name}**. Run `{COMMAND_PREFIX}divorce @{name}` first.")
    if theirs:
        partner = ctx.guild.get_member(int(theirs)) if str(theirs).isdigit() else None
        name = partner.display_name if partner else "someone"
        return await ctx.send(f"> **{member.display_name}** is already married to **{name}**.")

    embed = discord.Embed(
        description=f"**{ctx.author.display_name}** is proposing to **{member.display_name}**…",
        color=discord.Color.magenta(),
    )
    message = await ctx.send(embed=embed)
    await message.add_reaction("\U00002764")
    await message.add_reaction("\U0001F494")

    answer = await await_reaction(message, member.id, ("\U00002764", "\U0001F494"), timeout=60.0)
    if answer is None:
        return await ctx.send(f"> {member.display_name} did not answer. Awkward.")
    if answer == "\U00002764":
        guild_map[str(ctx.author.id)] = str(member.id)
        guild_map[str(member.id)] = str(ctx.author.id)
        marriages.save()
        pair = tuple(sorted([str(ctx.author.id), str(member.id)]))
        if pair in MARRIAGE_REWARDED:
            await ctx.send(f"> \U0001F389 **{member.display_name}** said yes! (No bonus — you two have married before.)")
        else:
            MARRIAGE_REWARDED.add(pair)
            add_coins(ctx.author.id, 50)
            await ctx.send(f"> \U0001F389 **{member.display_name}** said yes! (+50 {CURRENCY} for the happy couple)")
    else:
        await ctx.send(f"> **{member.display_name}** said no.")


@bot.command(name="divorce")
async def divorce(ctx: commands.Context, member: discord.Member):
    guild_map = marriage_map(ctx.guild.id)
    mine = guild_map.get(str(ctx.author.id))
    theirs = guild_map.get(str(member.id))
    if mine != str(member.id) or theirs != str(ctx.author.id):
        return await ctx.send(f"> You two are not married. Run `{COMMAND_PREFIX}marry @{member.display_name}` first.")
    guild_map.pop(str(ctx.author.id), None)
    guild_map.pop(str(member.id), None)
    marriages.save()
    embed = discord.Embed(
        description=f"**{ctx.author.display_name}** has divorced **{member.display_name}**.",
        color=discord.Color.dark_grey(),
    )
    gif = fetch_gif("divorce")
    if gif:
        embed.set_image(url=gif)
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# AFK
# ---------------------------------------------------------------------------


@bot.command(name="afk", aliases=["away"])
async def afk_cmd(ctx: commands.Context, *, reason: str = None):
    """Mark yourself away in this guild. Purely informational."""
    guild_map = afk_map(ctx.guild.id)
    guild_map[str(ctx.author.id)] = {"reason": (reason or "AFK")[:200], "since": time.time()}
    afk.save()
    await ctx.send(f"> You are now AFK: {guild_map[str(ctx.author.id)]['reason']}")


# ---------------------------------------------------------------------------
# TRIVIA
# ---------------------------------------------------------------------------


async def _delete_later(message, delay: int = CLEANUP_DELAY) -> None:
    """Background delete for a finished interactive message."""
    await asyncio.sleep(delay)
    await try_delete(message)


def cleanup_after(message, delay: int = CLEANUP_DELAY) -> None:
    if message is not None:
        asyncio.create_task(_delete_later(message, delay))


class ChoiceView(discord.ui.View):
    """A four-button multiple choice used by trivia and flags."""

    def __init__(self, options, correct_index: int, author_id: int, on_result=None):
        super().__init__(timeout=30)
        self.correct_index = correct_index
        self.author_id = author_id
        self.on_result = on_result
        self.answered = False
        self.message = None
        for index, label in enumerate(options):
            button = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary, row=0)
            button.callback = self._make_callback(index)
            self.add_item(button)

    async def on_timeout(self) -> None:
        cleanup_after(self.message)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author_id:
                return await interaction.response.send_message("This one is someone else's — start your own.", ephemeral=True)
            if self.answered:
                return await interaction.response.defer()
            self.answered = True
            correct = index == self.correct_index
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)
            if self.on_result:
                await self.on_result(interaction, correct)
            cleanup_after(interaction.message)
            self.stop()
        return callback


@bot.command(name="trivia")
async def trivia(ctx: commands.Context):
    question, options, correct = random.choice(TRIVIA)

    async def on_result(interaction, correct_answer: bool):
        if correct_answer:
            add_coins(interaction.user.id, 75)
            trivia_store.data.setdefault("scores", {})
            key = str(interaction.user.id)
            trivia_store.data["scores"][key] = trivia_store.data["scores"].get(key, 0) + 1
            trivia_store.save()
            await interaction.followup.send(f"Correct! +75 {CURRENCY}")
        else:
            await interaction.followup.send(f"Not quite — it was **{options[correct]}**.")

    view = ChoiceView(options, correct, ctx.author.id, on_result)
    view.message = await ctx.send(embed=discord.Embed(title=question, color=discord.Color.blurple()), view=view)


@bot.command(name="flags")
async def flags(ctx: commands.Context):
    flag, country = random.choice(FLAGS)
    wrong = random.sample([c for _, c in FLAGS if c != country], 3)
    options = [country] + wrong
    random.shuffle(options)
    correct = options.index(country)

    async def on_result(interaction, correct_answer: bool):
        if correct_answer:
            add_coins(interaction.user.id, 50)
            await interaction.followup.send(f"Correct — **{country}**! +50 {CURRENCY}")
        else:
            await interaction.followup.send(f"That was **{country}**.")

    view = ChoiceView(options, correct, ctx.author.id, on_result)
    view.message = await ctx.send(
        embed=discord.Embed(title=flag, description="Which country is this?", color=discord.Color.blurple()),
        view=view,
    )


@bot.command(name="scramble")
async def scramble(ctx: commands.Context):
    word = random.choice(SCRAMBLE_WORDS)
    letters = list(word)
    shuffled = letters[:]
    while shuffled == letters:
        random.shuffle(shuffled)
    await ctx.send(f"> Unscramble this word: **{''.join(shuffled).upper()}** — you have 30 seconds.")

    def check(message):
        return message.channel.id == ctx.channel.id and not message.author.bot

    try:
        message = await bot.wait_for("message", timeout=30.0, check=check)
    except asyncio.TimeoutError:
        return await ctx.send(f"> Time's up. The word was **{word}**.")
    if message.content.strip().lower() == word:
        add_coins(message.author.id, 60)
        return await ctx.send(f"> {message.author.mention} got it! +60 {CURRENCY}")
    await ctx.send(f"> Nope — it was **{word}**.")


@bot.command(name="wordchain")
async def wordchain(ctx: commands.Context):
    """Start a chain; each reply has to begin with the previous word's last letter."""
    state = WORDCHAIN.setdefault(ctx.channel.id, {"last": None, "used": set(), "touch": time.time()})
    state["last"] = None
    state["used"] = set()
    state["touch"] = time.time()
    await ctx.send("> Word chain started! I'll give a word, then you reply with one starting with its last letter.")

    def check(message):
        return message.channel.id == ctx.channel.id and not message.author.bot

    word = random.choice(SCRAMBLE_WORDS)
    state["last"] = word
    state["used"].add(word)
    await ctx.send(f"> Start: **{word.upper()}**")

    for _ in range(15):
        try:
            message = await bot.wait_for("message", timeout=45.0, check=check)
        except asyncio.TimeoutError:
            return await ctx.send("> Chain broke — nobody replied in time.")
        reply = message.content.strip().lower()
        if not re.fullmatch(r"[a-z]+", reply):
            continue
        if reply[0] != state["last"][-1]:
            return await ctx.send(f"> Chain broken! **{reply}** doesn't start with **{state['last'][-1].upper()}**.")
        if reply in state["used"]:
            return await ctx.send(f"> Chain broken! **{reply}** was already used.")
        state["used"].add(reply)
        state["last"] = reply
        state["touch"] = time.time()
        add_coins(message.author.id, 10)
        await message.add_reaction("\u2705")


@bot.command(name="hangman")
async def hangman(ctx: commands.Context):
    word = random.choice(HANGMAN_WORDS)
    state = {"word": word, "guessed": set(), "wrong": 0, "touch": time.time()}
    HANGMAN[ctx.channel.id] = state
    view = HangmanView(ctx.channel.id, ctx.author.id)
    view.message = await ctx.send(f"> Hangman! 6 misses allowed.\n> " + hangman_display(state), view=view)


def hangman_display(state) -> str:
    shown = " ".join(ch if ch in state["guessed"] else "_" for ch in state["word"])
    tried = sorted(ch.upper() for ch in state["guessed"] if ch not in state["word"])
    text = f"`{shown}`  ·  misses {state['wrong']}/6"
    if tried:
        text += "\nTried: " + " ".join(tried)
    return text


class HangmanView(discord.ui.View):
    """Letter menus for hangman.

    Two string selects, not 26 buttons: Discord allows five buttons per action
    row and five rows per view, so an A-Z button grid needs six rows and is
    rejected outright. A guessed letter is *removed* from its menu rather than
    greyed out, because SelectOption has no `disabled` flag — the menus are
    rebuilt after every guess instead.
    """

    LETTER_ROWS = ("ABCDEFGHIJKLM", "NOPQRSTUVWXYZ")

    def __init__(self, channel_id: int, author_id: int):
        super().__init__(timeout=180)
        self.channel_id = channel_id
        self.author_id = author_id
        self.message = None
        # Redraw from whatever the game has already had guessed, so a view
        # rebuilt for an in-progress game matches the state instead of offering
        # every letter again.
        live = HANGMAN.get(channel_id) or {}
        self._rebuild(set(live.get("guessed", set())))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This is someone else's game — start your own with `>hangman`.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        cleanup_after(self.message)

    def _rebuild(self, used: set) -> None:
        """Redraw both menus, dropping letters that have already been tried."""
        self.clear_items()
        for row, letters in enumerate(self.LETTER_ROWS):
            remaining = [ch for ch in letters if ch.lower() not in used]
            select = discord.ui.Select(
                placeholder=f"Guess {letters[0]}-{letters[-1]}",
                # A select needs at least one option, so an exhausted row keeps a
                # dead placeholder and is disabled instead.
                options=[discord.SelectOption(label=ch) for ch in remaining]
                or [discord.SelectOption(label="\u2014", value="-")],
                row=row,
                min_values=1,
                max_values=1,
            )
            select.disabled = not remaining
            select.callback = self._on_guess
            self.add_item(select)

    async def _on_guess(self, interaction: discord.Interaction):
        state = HANGMAN.get(self.channel_id)
        if not state:
            return await interaction.response.send_message("That game is over.", ephemeral=True)

        letter = str((interaction.data or {}).get("values", ["-"])[0]).lower()
        if letter == "-" or letter in state["guessed"]:
            return await interaction.response.send_message("Already tried that one.", ephemeral=True)

        state["guessed"].add(letter)
        state["touch"] = time.time()
        if letter not in state["word"]:
            state["wrong"] += 1

        solved = all(ch in state["guessed"] for ch in state["word"])
        if solved:
            HANGMAN.pop(self.channel_id, None)
            add_coins(interaction.user.id, 100)
            for child in self.children:
                child.disabled = True
            cleanup_after(interaction.message)
            self.stop()
            return await interaction.response.edit_message(
                content=f"> Solved! The word was **{state['word']}**. +100 {CURRENCY}", view=self)

        if state["wrong"] >= 6:
            HANGMAN.pop(self.channel_id, None)
            for child in self.children:
                child.disabled = True
            cleanup_after(interaction.message)
            self.stop()
            return await interaction.response.edit_message(
                content=f"> Out of misses. The word was **{state['word']}**.", view=self)

        self._rebuild(state["guessed"])
        await interaction.response.edit_message(content=f"> " + hangman_display(state), view=self)


@bot.command(name="guess")
async def guess(ctx: commands.Context):
    number = random.randint(1, 100)
    GUESSING[ctx.channel.id] = {"number": number, "tries": 0, "touch": time.time()}
    await ctx.send("> I picked a number between 1 and 100. Type your guess!")

    def check(message):
        return message.channel.id == ctx.channel.id and not message.author.bot and message.content.strip().isdigit()

    for _ in range(10):
        try:
            message = await bot.wait_for("message", timeout=60.0, check=check)
        except asyncio.TimeoutError:
            GUESSING.pop(ctx.channel.id, None)
            return await ctx.send(f"> Nobody guessed it. It was **{number}**.")
        attempt = int(message.content.strip())
        GUESSING[ctx.channel.id]["tries"] += 1
        GUESSING[ctx.channel.id]["touch"] = time.time()
        if attempt == number:
            GUESSING.pop(ctx.channel.id, None)
            add_coins(message.author.id, 80)
            return await ctx.send(f"> {message.author.mention} got it — **{number}**! +80 {CURRENCY}")
        await message.add_reaction("\U0001F53C" if attempt < number else "\U0001F53D")
    GUESSING.pop(ctx.channel.id, None)
    await ctx.send(f"> Out of guesses. It was **{number}**.")


# ---------------------------------------------------------------------------
# ECONOMY
# ---------------------------------------------------------------------------


@bot.command(name="balance", aliases=["bal"])
async def balance(ctx: commands.Context, member: discord.Member = None):
    own = member is None
    member = member or ctx.author
    rec = acct(member.id)
    level, into, needed = level_progress(rec)
    embed = discord.Embed(title=f"{member.display_name}'s balance", color=discord.Color.gold())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Wallet", value=f"{rec['balance']:,} {CURRENCY}")
    embed.add_field(name="Bank", value=f"{rec['bank']:,} {CURRENCY}")
    embed.add_field(name="Level", value=f"{level} · {into}/{needed} xp\n{bar(int(10 * into / needed))}")
    embed.add_field(name="Record", value=f"{rec['wins']}W / {rec['losses']}L")
    if own:
        await try_delete(ctx.message)
        await send_clean(ctx, embed=embed, delete_after=15)
    else:
        await ctx.send(embed=embed)


@bot.command(name="daily")
async def daily(ctx: commands.Context):
    rec = acct(ctx.author.id)
    now = time.time()
    if now - rec["last_daily"] < 86400:
        wait = human_delta(86400 - (now - rec["last_daily"]))
        await try_delete(ctx.message)
        return await send_clean(ctx, content=f"> Already claimed. Come back in **{wait}**.", delete_after=15)

    rec["streak"] = int(rec.get("streak", 0)) + 1 if now - rec["last_daily"] <= 172800 else 1
    rec["best_streak"] = max(int(rec.get("best_streak", 0)), rec["streak"])
    multiplier = streak_multiplier(rec["streak"])

    base = 250
    bonus = random.randint(0, 150)
    total = int((base + bonus) * multiplier)
    rec["balance"] += total
    rec["last_daily"] = now
    add_xp(ctx.author.id, 20)
    economy.save()

    await try_delete(ctx.message)
    await send_clean(
        ctx,
        content=(f"> Daily claimed: **{total}** {CURRENCY} (base {base} + bonus {bonus}, "
                 f"streak {rec['streak']} · x{multiplier:g})."),
        delete_after=15,
    )


@bot.command(name="streak")
async def streak(ctx: commands.Context):
    rec = acct(ctx.author.id)
    current = int(rec.get("streak", 0))
    mine = (str(ctx.author.id), current)
    ranked = sorted(
        ((uid, int(r.get("streak", 0))) for uid, r in economy.data.items() if isinstance(r, dict)),
        key=lambda pair: pair[1],
        reverse=True,
    )
    rank = next((index for index, pair in enumerate(ranked, start=1) if pair[0] == mine[0]), len(ranked))
    embed = discord.Embed(title=f"{ctx.author.display_name}'s streak", color=discord.Color.orange())
    embed.add_field(name="Current", value=f"{current} day{'s' if current != 1 else ''}")
    embed.add_field(name="Best", value=str(int(rec.get("best_streak", 0))))
    embed.add_field(name="Multiplier", value=f"x{streak_multiplier(current):g}")
    embed.add_field(name="Rank", value=f"#{rank} of {len(ranked)}")
    await ctx.send(embed=embed)


@bot.command(name="bank")
async def bank(ctx: commands.Context, action: str = None, amount: str = None):
    """`>bank deposit 500` / `>bank withdraw all`"""
    rec = acct(ctx.author.id)
    verb = (action or "").strip().lower()
    if verb not in ("deposit", "withdraw", "dep", "with", "w"):
        return await ctx.send(f"> Use `{COMMAND_PREFIX}bank deposit <amount>` or `{COMMAND_PREFIX}bank withdraw <amount>`.")
    deposit = verb in ("deposit", "dep")
    available = int(rec["balance"] if deposit else rec["bank"])
    if amount is None:
        return await ctx.send("> How much? A number, `all` or `half`.")
    value = parse_amount(amount, available)
    if value is None:
        return await ctx.send("> That amount doesn't work. Try a number, `all` or `half`.")
    if deposit:
        rec["balance"] -= value
        rec["bank"] += value
    else:
        rec["bank"] -= value
        rec["balance"] += value
    economy.save()
    await try_delete(ctx.message)
    await send_clean(
        ctx,
        content=(f"> {'Deposited' if deposit else 'Withdrew'} **{value:,}** {CURRENCY}. "
                 f"Wallet {rec['balance']:,} · Bank {rec['bank']:,}"),
        delete_after=15,
    )


@bot.command(name="richest", aliases=["rich"])
async def richest(ctx: commands.Context):
    recs = [(uid, rec) for uid, rec in economy.data.items() if isinstance(rec, dict)]
    if not recs:
        return await ctx.send("> Nobody has any coins yet.")
    uid, rec = max(recs, key=lambda pair: total_coins(pair[1]))
    try:
        user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
        name = user.display_name
    except Exception:
        name = f"user {uid}"
    await ctx.send(f"> **{name}** is the richest right now with **{total_coins(rec):,}** {CURRENCY}.")


@bot.command(name="work", aliases=["swork", "startwork", "stopwork", "sw"])
async def work(ctx: commands.Context):
    """Starts a passive job, or stops and cashes out the active one."""
    uid = ctx.author.id
    key = str(uid)
    entry = worker.data.get(key)
    rec = acct(uid)
    now = time.time()

    if not isinstance(entry, dict):
        qwait = QWORK_COOLDOWN - (now - float(rec.get("last_qwork", 0)))
        if qwait > 0:
            await try_delete(ctx.message)
            return await send_clean(ctx, content=f"> Your quick work cooldown has **{human_delta(qwait)}** left.", delete_after=15)
        wait = work_cooldown_left(uid)
        if wait > 0:
            await try_delete(ctx.message)
            return await send_clean(ctx, content=f"> You are still resting from your last job. Try again in **{human_delta(wait)}**.", delete_after=15)
        worker.data[key] = {"kind": "passive", "started_at": now, "last_tick": now}
        worker.save()
        await try_delete(ctx.message)
        return await send_clean(
            ctx,
            content=f"> You started your shift as a **{job_title(uid)}**. Run `{COMMAND_PREFIX}work` again to stop and cash out.",
            delete_after=15,
        )

    if entry.get("kind") == "quick":
        left = max(0, QWORK_SECONDS - (now - float(entry.get("started_at", now))))
        await try_delete(ctx.message)
        return await send_clean(
            ctx,
            content=f"> Your quick work is still in flight — it pays out in **{human_delta(left)}**.",
            delete_after=15,
        )

    await try_delete(ctx.message)
    await _cash_out_passive(ctx, key)


async def _cash_out_passive(ctx: commands.Context, key: str, silent: bool = False):
    """Stop a passive job and pay it out. Always removes the entry."""
    lines = await _finish_passive(key, announce=not silent)
    if silent:
        return lines
    await send_clean(ctx, content="\n".join(lines), delete_after=15)
    return lines


async def _finish_passive(key: str, announce: bool = True):
    """Pay out a passive job, set the cooldown, drop the entry."""
    entry = worker.data.get(key) or {}
    started = float(entry.get("started_at", time.time()))
    elapsed = time.time() - started
    coins, minutes = passive_payout(elapsed)

    if minutes < 1:
        if announce:
            return ["> Not even a minute yet. Try again in a bit."]
        return []

    lucky = random.random() < LUCKY_CHANCE
    if lucky:
        coins *= 2

    rec = acct(key)
    rec["balance"] = int(rec.get("balance", 0)) + coins
    rec["last_work"] = time.time()
    WORK_COOLDOWN_UNTIL[key] = time.time() + work_cooldown_for(minutes)
    economy.save()
    worker.data.pop(key, None)
    worker.save()
    LAST_PASSIVE_PAYOUT[key] = (coins, minutes)

    lines = [f"> Clocked out after **{minutes}** minute{'s' if minutes != 1 else ''} — you earned **{coins}** {CURRENCY}."]
    if lucky:
        lines.append("> Lucky bonus! Your payout was doubled.")
    return lines


@bot.command(name="qwork", aliases=["quickwork", "qw"])
async def qwork(ctx: commands.Context):
    """Quick work: pays after 2 minutes, 5-minute cooldown from the start."""
    uid = ctx.author.id
    key = str(uid)
    now = time.time()
    rec = acct(uid)

    if key in worker.data:
        entry = worker.data[key] or {}
        if entry.get("kind") == "quick":
            left = max(0, QWORK_SECONDS - (now - float(entry.get("started_at", now))))
            message = f"> You already have quick work in flight — it pays out in **{human_delta(left)}**."
        else:
            message = f"> You already have a passive job running. Stop it with `{COMMAND_PREFIX}work` first."
        await try_delete(ctx.message)
        return await send_clean(ctx, content=message, delete_after=15)

    qwait = QWORK_COOLDOWN - (now - float(rec.get("last_qwork", 0)))
    if qwait > 0:
        await try_delete(ctx.message)
        return await send_clean(ctx, content=f"> Quick work is on cooldown for another **{human_delta(qwait)}**.", delete_after=15)

    wait = work_cooldown_left(uid)
    if wait > 0:
        await try_delete(ctx.message)
        return await send_clean(ctx, content=f"> You are still resting from your last job. Try again in **{human_delta(wait)}**.", delete_after=15)

    run = int(rec.get("work_count", 0)) + 1
    payout = qwork_tier(run)
    lucky = random.random() < LUCKY_CHANCE
    if lucky:
        payout *= 2

    rec["work_count"] = run
    rec["last_qwork"] = now
    welcomed = not rec.get("qwork_welcomed", False)
    if welcomed:
        rec["balance"] = int(rec.get("balance", 0)) + 150
        rec["qwork_welcomed"] = True
    economy.save()

    worker.data[key] = {"kind": "quick", "started_at": now, "last_tick": now, "payout": payout}
    worker.save()

    lines = [f"> Your shift as a **{job_title(uid)}** has started. You will be paid **{payout}** {CURRENCY} in 2 minutes."]
    if lucky:
        lines.append("> Lucky bonus! Your payout was doubled.")
    if welcomed:
        lines.append(f"> Welcome to the job — here is **150** {CURRENCY} to get you started.")
    await try_delete(ctx.message)
    await send_clean(ctx, content="\n".join(lines), delete_after=15)


@bot.command(name="slots")
async def slots(ctx: commands.Context, bet: str = "10"):
    rec = acct(ctx.author.id)
    amount = parse_bet(bet, rec)
    if amount is None:
        return await ctx.send("> That bet doesn't work. Try `10`, `half` or `all`.")
    reel = ["\U0001F352", "\U0001F34B", "\U0001F514", "\U0001F48E", "7\ufe0f\u20e3"]
    result = [random.choice(reel) for _ in range(3)]
    if result[0] == result[1] == result[2]:
        multiplier = 10 if result[0] == "\U0001F48E" else (7 if result[0] == "7\ufe0f\u20e3" else 5)
        payout = amount * multiplier
        rec["balance"] += payout - amount
        rec["wins"] += 1
        economy.save()
        return await ctx.send(f"> [ {' | '.join(result)} ]\n> **Jackpot!** x{multiplier} — you win **{payout - amount}** {CURRENCY}.")
    if len(set(result)) == 2:
        payout = int(amount * 1.5)
        rec["balance"] += payout - amount
        economy.save()
        return await ctx.send(f"> [ {' | '.join(result)} ]\n> Two of a kind. x1.5 — you win **{payout - amount}** {CURRENCY}.")
    rec["balance"] -= amount
    rec["losses"] += 1
    economy.save()
    await ctx.send(f"> [ {' | '.join(result)} ]\n> No match. You lose **{amount}** {CURRENCY}.")


def hand_total(cards) -> int:
    total = sum(cards)
    aces = cards.count(11)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


class BlackjackView(discord.ui.View):
    """Hit or stand. The dealer draws to 17 on stand."""

    def __init__(self, author_id: int, bet: int):
        super().__init__(timeout=90)
        self.author_id = author_id
        self.bet = bet
        self.message = None
        self.finished = False
        deck = [2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 11] * 4
        random.shuffle(deck)
        self.player = [deck.pop(), deck.pop()]
        self.dealer = [deck.pop(), deck.pop()]
        self.deck = deck

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This hand belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        cleanup_after(self.message)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.append(self.deck.pop())
        await self._refresh(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        while hand_total(self.dealer) < 17:
            self.dealer.append(self.deck.pop())
        rec = acct(self.author_id)
        player_total = hand_total(self.player)
        dealer_total = hand_total(self.dealer)
        if dealer_total > 21 or player_total > dealer_total:
            rec["balance"] += self.bet
            rec["wins"] += 1
            outcome = f"You win **{self.bet}** {CURRENCY}."
        elif player_total == dealer_total:
            outcome = f"Push — your **{self.bet}** {CURRENCY} comes back."
        else:
            rec["balance"] -= self.bet
            rec["losses"] += 1
            outcome = f"You lose **{self.bet}** {CURRENCY}."
        economy.save()
        for child in self.children:
            child.disabled = True
        self.finished = True
        await interaction.response.edit_message(content=self._text(reveal=True) + f"\n> {outcome}", view=self)
        cleanup_after(interaction.message)
        self.stop()

    def _text(self, reveal: bool = False) -> str:
        dealer = self.dealer if reveal else [self.dealer[0]]
        hidden = "" if reveal else " `?`"
        return (f"> **Dealer:** `{dealer}` ({hand_total(dealer)}){hidden}\n"
                f"> **You:** `{self.player}` ({hand_total(self.player)})")

    async def _refresh(self, interaction: discord.Interaction):
        if hand_total(self.player) > 21:
            rec = acct(self.author_id)
            rec["balance"] -= self.bet
            rec["losses"] += 1
            economy.save()
            for child in self.children:
                child.disabled = True
            self.finished = True
            await interaction.response.edit_message(
                content=self._text(reveal=True) + f"\n> Bust — you lose **{self.bet}** {CURRENCY}.", view=self)
            cleanup_after(interaction.message)
            self.stop()
            return
        await interaction.response.edit_message(content=self._text(), view=self)


@bot.command(name="blackjack", aliases=["bj"])
async def blackjack(ctx: commands.Context, bet: str = "10"):
    rec = acct(ctx.author.id)
    amount = parse_bet(bet, rec)
    if amount is None:
        return await ctx.send("> That bet doesn't work. Try `10`, `half` or `all`.")
    view = BlackjackView(ctx.author.id, amount)
    view.message = await ctx.send(view._text(), view=view)


@bot.command(name="rob")
async def rob(ctx: commands.Context, member: discord.Member):
    if member.bot or member.id == ctx.author.id:
        return await ctx.send("> Pick a real person who isn't you.")
    robber = acct(ctx.author.id)
    victim = acct(member.id)
    if time.time() - robber["last_rob"] < 300:
        return await ctx.send(f"> Lay low for a bit — try again in **{human_delta(300 - (time.time() - robber['last_rob']))}**.")
    if victim["balance"] < 50:
        return await ctx.send("> They're broke. Not worth the effort.")
    robber["last_rob"] = time.time()
    if random.random() < 0.45:
        stolen = random.randint(20, min(300, victim["balance"]))
        victim["balance"] -= stolen
        robber["balance"] += stolen
        economy.save()
        return await ctx.send(f"> Got away with **{stolen}** {CURRENCY} from {member.display_name}!")
    fine = min(robber["balance"], random.randint(25, 150))
    robber["balance"] -= fine
    economy.save()
    await ctx.send(f"> Caught! You paid a **{fine}** {CURRENCY} fine.")


@bot.command(name="shop")
async def shop(ctx: commands.Context, action: str = None, item: str = None):
    if action and action.lower() == "buy":
        if not item:
            return await ctx.send("[X] Which item? `" + COMMAND_PREFIX + "shop buy lucky_coin`")
        entry = SHOP_ITEMS.get(item.lower())
        if not entry:
            return await ctx.send("[X] No such item. Run `" + COMMAND_PREFIX + "shop` to see the list.")
        rec = acct(ctx.author.id)
        if rec["balance"] < entry["price"]:
            return await ctx.send(f"> You need **{entry['price']}** {CURRENCY} for that.")
        rec["balance"] -= entry["price"]
        rec["inventory"][item.lower()] = rec["inventory"].get(item.lower(), 0) + 1
        if item.lower() == "mystery_box":
            prize = random.choice([100, 300, 500, 800, 2000])
            rec["balance"] += prize
            economy.save()
            return await ctx.send(f"> You bought a Mystery Box and it contained **{prize}** {CURRENCY}.")
        economy.save()
        return await ctx.send(f"> Bought **{entry['name']}** for {entry['price']} {CURRENCY}.")

    embed = discord.Embed(title="Shop", description=f"Buy with `{COMMAND_PREFIX}shop buy <item>`", color=discord.Color.gold())
    for key, entry in SHOP_ITEMS.items():
        embed.add_field(name=f"{entry['name']} — {entry['price']} {CURRENCY}", value=f"`{key}` · {entry['desc']}", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="gift")
async def gift(ctx: commands.Context, member: discord.Member, amount: int):
    if member.bot or member.id == ctx.author.id:
        return await ctx.send("> Pick a real person who isn't you.")
    if amount < 1:
        return await ctx.send("> Send at least 1 coin.")
    rec = acct(ctx.author.id)
    if rec["balance"] < amount:
        return await ctx.send("> You don't have that much in your wallet.")
    rec["balance"] -= amount
    add_coins(member.id, amount)
    economy.save()
    await ctx.send(f"> Sent **{amount}** {CURRENCY} to {member.display_name}.")


# ---------------------------------------------------------------------------
# SOCIAL
# ---------------------------------------------------------------------------


async def _render_board(title: str, value, colour, formatter):
    """Shared body for the leaderboard family: a numbered top-10 embed."""
    recs = [(uid, rec) for uid, rec in economy.data.items() if isinstance(rec, dict)]
    recs.sort(key=lambda pair: value(pair[1]), reverse=True)
    lines = []
    for index, (uid, rec) in enumerate(recs[:10], start=1):
        try:
            user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
            name = user.display_name
        except Exception:
            name = f"user {uid}"
        lines.append(f"`{index:>2}.` **{name}** — {formatter(value(rec), rec)}")
    embed = discord.Embed(title=title, description="\n".join(lines) or "Nobody is on the board yet.", color=colour)
    return embed


@bot.command(name="baltop", aliases=["bt", "topbal", "leaderboard"])
async def baltop(ctx: commands.Context):
    embed = await _render_board(
        "Richest members", total_coins, discord.Color.gold(), lambda amount, rec: f"{amount:,} {CURRENCY}"
    )
    await ctx.send(embed=embed)


@bot.command(name="banktop")
async def banktop(ctx: commands.Context):
    embed = await _render_board(
        "Largest banks", lambda rec: int(rec.get("bank", 0)), discord.Color.blurple(),
        lambda amount, rec: f"{amount:,} {CURRENCY}"
    )
    await ctx.send(embed=embed)


@bot.command(name="leveltop", aliases=["lt", "toplevel"])
async def leveltop(ctx: commands.Context):
    embed = await _render_board(
        "Highest levels", level_of, discord.Color.purple(),
        lambda amount, rec: f"Level {amount} · {int(rec.get('xp', 0)):,} xp"
    )
    await ctx.send(embed=embed)


@bot.command(name="wintop", aliases=["wt", "topwins"])
async def wintop(ctx: commands.Context):
    embed = await _render_board(
        "Most casino wins", lambda rec: int(rec.get("wins", 0)), discord.Color.green(),
        lambda amount, rec: f"{amount:,} wins"
    )
    await ctx.send(embed=embed)


@bot.command(name="lbstats", aliases=["lb", "statslb"])
async def lbstats(ctx: commands.Context):
    recs = [(uid, rec) for uid, rec in economy.data.items() if isinstance(rec, dict)]
    embed = discord.Embed(title="Leaderboard summary", color=discord.Color.gold())

    async def name_of(uid):
        try:
            user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
            return user.display_name
        except Exception:
            return f"user {uid}"

    if recs:
        rich_uid, rich_rec = max(recs, key=lambda pair: total_coins(pair[1]))
        level_uid, level_rec = max(recs, key=lambda pair: level_of(pair[1]))
        win_uid, win_rec = max(recs, key=lambda pair: int(pair[1].get("wins", 0)))
        embed.add_field(name="Richest", value=f"**{await name_of(rich_uid)}** — {total_coins(rich_rec):,} {CURRENCY}")
        embed.add_field(name="Highest level", value=f"**{await name_of(level_uid)}** — Level {level_of(level_rec)}")
        embed.add_field(name="Most wins", value=f"**{await name_of(win_uid)}** — {int(win_rec.get('wins', 0)):,} wins")
    else:
        embed.description = "No economy records yet."
    embed.set_footer(text=f"{len(recs):,} tracked members")
    await ctx.send(embed=embed)


@bot.command(name="profile")
async def profile(ctx: commands.Context, member: discord.Member = None):
    own = member is None
    member = member or ctx.author
    rec = acct(member.id)
    level, into, needed = level_progress(rec)
    embed = discord.Embed(title=member.display_name, color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=f"{level}")
    embed.add_field(name="Coins", value=f"{total_coins(rec):,} {CURRENCY}")
    embed.add_field(name="Record", value=f"{rec['wins']}W / {rec['losses']}L")
    embed.add_field(name="Streak", value=f"{int(rec.get('streak', 0))} days (best {int(rec.get('best_streak', 0))})")
    inventory = rec.get("inventory") or {}
    if inventory:
        items = [f"{SHOP_ITEMS.get(k, {}).get('name', k)} x{v}" for k, v in inventory.items()]
        embed.add_field(name="Inventory", value=", ".join(items)[:1020], inline=False)
    embed.add_field(name="Progress", value=f"{into}/{needed} xp {bar(int(10 * into / needed))}", inline=False)
    if own:
        await try_delete(ctx.message)
        await send_clean(ctx, embed=embed, delete_after=15)
    else:
        await ctx.send(embed=embed)


@bot.command(name="compare")
async def compare(ctx: commands.Context, first: discord.Member, second: discord.Member = None):
    second = second or ctx.author
    a, b = acct(first.id), acct(second.id)
    embed = discord.Embed(title=f"{first.display_name} vs {second.display_name}", color=discord.Color.blurple())
    rows = [
        ("Coins", total_coins(a), total_coins(b)),
        ("Level", level_of(a), level_of(b)),
        ("Wins", int(a["wins"]), int(b["wins"])),
    ]
    for label, left, right in rows:
        winner_left = left >= right
        embed.add_field(name=label, value=f"{'> ' if winner_left else ''}{left:,}\n{'< ' if not winner_left else ''}{right:,}", inline=True)
    await try_delete(ctx.message)
    await send_clean(ctx, embed=embed, delete_after=15)


@bot.command(name="stats")
async def stats(ctx: commands.Context):
    embed = discord.Embed(title="Bot statistics", color=discord.Color.blurple())
    embed.add_field(name="Servers", value=str(len(bot.guilds)))
    embed.add_field(name="Members", value=f"{sum(g.member_count or 0 for g in bot.guilds):,}")
    embed.add_field(name="Latency", value=f"{latency_ms()}ms")
    embed.add_field(name="Uptime", value=uptime_text())
    embed.add_field(name="Commands", value=str(len(bot.commands)))
    if COMMAND_USES:
        name, count = COMMAND_USES.most_common(1)[0]
        embed.add_field(name="Most used command", value=f"`{COMMAND_PREFIX}{name}` — {count:,} run{'s' if count != 1 else ''}")
    else:
        embed.add_field(name="Most used command", value="No commands run yet this session")
    embed.add_field(name="Version", value=BOT_VERSION)
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# REMINDERS
# ---------------------------------------------------------------------------


@bot.command(name="reminder", aliases=["remind"])
async def reminder(ctx: commands.Context, duration: str = None, *, text: str = None):
    if not duration or not text:
        return await ctx.send(f"> Format: `{COMMAND_PREFIX}reminder <duration> <text>` — for example `{COMMAND_PREFIX}reminder 10m check the oven`.")
    key = str(ctx.author.id)
    if key in reminders.data:
        return await ctx.send(f"> You already have a pending reminder. Run `{COMMAND_PREFIX}remindercancel` first.")
    seconds = parse_duration(duration)
    if seconds is None:
        return await ctx.send("> Duration must be a number plus `s`, `m`, `h` or `d`, and no longer than 30 days.")
    reminders.data[key] = {"text": text[:500], "due_at": time.time() + seconds}
    reminders.save()
    await ctx.send(f"> Reminder set for **{human_delta(seconds)}** from now.")


@bot.command(name="reminders", aliases=["reminderlist"])
async def reminders_cmd(ctx: commands.Context):
    entry = reminders.data.get(str(ctx.author.id))
    if not isinstance(entry, dict):
        return await ctx.send("> You have no pending reminder.")
    left = max(0, float(entry.get("due_at", 0)) - time.time())
    await ctx.send(f"> **{entry.get('text', '')}** — due in **{human_delta(left)}**.")


@bot.command(name="remindercancel", aliases=["rcancel"])
async def remindercancel(ctx: commands.Context):
    if reminders.data.pop(str(ctx.author.id), None) is None:
        return await ctx.send("> You have no pending reminder to cancel.")
    reminders.save()
    await ctx.send("> Reminder cancelled.")


# ---------------------------------------------------------------------------
# TIME-WASTERS
# ---------------------------------------------------------------------------


@bot.command(name="meme")
async def meme(ctx: commands.Context):
    data = http_json("https://meme-api.com/gimme")
    url = (data or {}).get("url") or random.choice(FALLBACK_MEMES)
    title = (data or {}).get("title") or "Meme"
    embed = discord.Embed(title=title[:256], color=discord.Color.blurple())
    embed.set_image(url=url)
    await ctx.send(embed=embed)


@bot.command(name="catfact")
async def catfact(ctx: commands.Context):
    data = http_json("https://some-random-api.com/animal/cat")
    fact = (data or {}).get("fact") or random.choice(FALLBACK_CAT_FACTS)
    image = (data or {}).get("image")
    embed = discord.Embed(description=fact, color=discord.Color.blurple())
    if image:
        embed.set_image(url=image)
    await ctx.send(embed=embed)


@bot.command(name="dogfact")
async def dogfact(ctx: commands.Context):
    data = http_json("https://some-random-api.com/animal/dog")
    fact = (data or {}).get("fact") or random.choice(FALLBACK_DOG_FACTS)
    image = (data or {}).get("image")
    embed = discord.Embed(description=fact, color=discord.Color.blurple())
    if image:
        embed.set_image(url=image)
    await ctx.send(embed=embed)


@bot.command(name="foxfact")
async def foxfact(ctx: commands.Context):
    data = http_json("https://some-random-api.com/animal/fox")
    fact = (data or {}).get("fact") or random.choice(FALLBACK_FOX_FACTS)
    image = (data or {}).get("image")
    embed = discord.Embed(description=fact, color=discord.Color.orange())
    if image:
        embed.set_image(url=image)
    await ctx.send(embed=embed)


@bot.command(name="joke")
async def joke(ctx: commands.Context):
    data = http_json("https://official-joke-api.appspot.com/random_joke")
    if data and data.get("setup"):
        setup, punchline = data["setup"], data.get("punchline", "")
    else:
        setup, punchline = random.choice(FALLBACK_JOKES)
    embed = discord.Embed(title=setup[:256], description=f"||{punchline}||", color=discord.Color.blurple())
    await ctx.send(embed=embed)


@bot.command(name="randomquote", aliases=["quoterandom", "rquote"])
async def randomquote(ctx: commands.Context):
    data = http_json("https://api.quotable.io/random")
    if data and data.get("content"):
        text, author = data["content"], data.get("author", "Unknown")
    else:
        text, author = random.choice(FALLBACK_QUOTES)
    embed = discord.Embed(description=f"*{text}*", color=discord.Color.blurple())
    embed.set_footer(text=f"— {author}")
    await ctx.send(embed=embed)


@bot.command(name="urban", aliases=["ud"])
async def urban(ctx: commands.Context, *, term: str):
    query = urllib.parse.quote(str(term).strip()[:120], safe="")
    data = http_json(f"https://api.urbandictionary.com/v0/define?term={query}")
    entries = (data or {}).get("list") or []
    if not entries:
        return await ctx.send("> Urban Dictionary had nothing for that. Try a different term.")
    top = entries[0]
    definition = re.sub(r"[\[\]]", "", str(top.get("definition") or "")).strip() or "No definition."
    embed = discord.Embed(
        title=str(top.get("word") or term)[:256],
        description=definition[:1000],
        color=discord.Color.dark_theme(),
    )
    embed.set_footer(text="Source: Urban Dictionary — content may be NSFW")
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# QUOTE CARD
# ---------------------------------------------------------------------------


def _timestamp_of(message: discord.Message) -> str:
    try:
        return discord.utils.format_dt(message.created_at, "f")
    except Exception:
        return ""


def _top_role_colour(member):
    """The quoted author's top role colour, blurple when it is the default."""
    try:
        if isinstance(member, discord.Member):
            role = getattr(member, "top_role", None)
            if role is not None and role.color.value:
                return (role.color.r, role.color.g, role.color.b)
        elif getattr(member, "color", None) is not None and member.color.value:
            return (member.color.r, member.color.g, member.color.b)
    except Exception:
        pass
    return QUOTE_ACCENT


@bot.command(name="quote", aliases=["q"])
async def quote(ctx: commands.Context):
    """Turn the message you replied to into a PNG card."""
    reference = ctx.message.reference
    if reference is None:
        return await ctx.send(f"> Reply to the message you want to quote, then run `{COMMAND_PREFIX}quote`.")

    target = reference.resolved
    if target is None:
        try:
            target = await ctx.channel.fetch_message(reference.message_id)
        except Exception:
            return await ctx.send("> I could not fetch that message — it may be too old or in a channel I cannot read.")

    if not isinstance(target, discord.Message):
        return await ctx.send("> I could not fetch that message — it may be too old or in a channel I cannot read.")

    author = target.author
    name = getattr(author, "display_name", None) or getattr(author, "name", "Unknown")
    body = (target.content or "").strip()
    if not body:
        body = target.system_content or "[no text in that message]"
    stamp = _timestamp_of(target)

    await try_delete(ctx.message)

    try:
        avatar_bytes = await author.display_avatar.read()
        bot_icon = None
        if bot.user:
            try:
                bot_icon = await bot.user.display_avatar.read()
            except Exception:
                bot_icon = None
        png = await asyncio.to_thread(
            render_quote_card, body, name, stamp, _top_role_colour(author), avatar_bytes,
            bot.user.name if bot.user else "wispcord", bot_icon,
        )
        await ctx.send(file=discord.File(io.BytesIO(png), filename="quote.png"))
    except Exception as exc:
        log(f"Quote card render failed: {type(exc).__name__}: {exc}")
        await ctx.send(f"**{name}** said:\n>>> {body[:QUOTE_MAX_CHARS]}")


# ---------------------------------------------------------------------------
# TICKET PANEL — the persistent "Create Ticket" button
# ---------------------------------------------------------------------------

TICKET_CUSTOM_ID = "ticket_creation_button"


class TicketConfirmView(discord.ui.View):
    """The one-shot confirmation shown after the panel button is clicked.

    Per-interaction and short-lived, unlike the persistent panel itself: the
    link button opens the browser without ever calling back, and Cancel just
    rewrites this ephemeral message.
    """

    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link,
            url=SUPPORT_PAGE_URL,
            label="Open support page",
        ))
        cancel = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Cancel")
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _cancel(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Cancelled. You can reopen the panel any time.",
            view=self,
        )
        self.stop()


class TicketCreationView(discord.ui.View):
    """Persistent panel button.

    Clicking it does NOT create a ticket. It sends the member to the support
    form on the member site, which is where tickets are actually opened — that
    way the web flow stays the single source of truth, and this bot never needs
    write access to the ticket tables.

    Registered as a persistent view (timeout=None, fixed custom_id), so the
    button keeps working after the bot restarts without redeploying the panel.
    """

    def __init__(self, label: str = "Create Ticket", emoji=None, style=None):
        super().__init__(timeout=None)
        # The single button is declared below; this only restyles it, so the
        # view can never end up with two children sharing one custom_id.
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == TICKET_CUSTOM_ID:
                child.label = (label or "Create Ticket")[:80]
                child.emoji = emoji
                child.style = style or discord.ButtonStyle.success

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.success, custom_id=TICKET_CUSTOM_ID)
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not SUPPORT_PAGE_URL:
            return await interaction.response.send_message(
                "Click the support page link to open a ticket.", ephemeral=True
            )
        await interaction.response.send_message(
            "You are about to be redirected to our support page.",
            view=TicketConfirmView(),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# BACKGROUND TASKS
# ---------------------------------------------------------------------------


@tasks.loop(minutes=10)
async def sweep_game_state():
    """Drop the state of games a channel started and then walked away from.

    HANGMAN, WORDCHAIN and GUESSING are keyed by channel and are otherwise only
    cleaned up when a game finishes normally, so an abandoned game would sit in
    memory for the life of the process.
    """
    cutoff = time.time() - GAME_STATE_TTL
    for store in (HANGMAN, WORDCHAIN, GUESSING):
        stale = [key for key, value in list(store.items())
                 if not isinstance(value, dict) or value.get("touch", 0) < cutoff]
        for key in stale:
            store.pop(key, None)


@sweep_game_state.before_loop
async def before_sweep_game_state():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def work_sweep():
    """One pass over the worker store: pay finished quick jobs, clock out
    passive jobs that hit the 8-hour cap."""
    now = time.time()
    changed = False
    for key, entry in list(worker.data.items()):
        if not isinstance(entry, dict):
            worker.data.pop(key, None)
            changed = True
            continue

        kind = entry.get("kind")
        started = float(entry.get("started_at", now))

        if kind == "quick" and now - started >= QWORK_SECONDS:
            payout = int(entry.get("payout", 0))
            rec = acct(key)
            rec["balance"] = int(rec.get("balance", 0)) + payout
            economy.save()
            worker.data.pop(key, None)
            changed = True
            try:
                user = bot.get_user(int(key)) or await bot.fetch_user(int(key))
                await user.send(f"Your {job_title(key)} paid out: **{payout}** {CURRENCY}.")
            except Exception as exc:
                log(f"Quick work DM failed for {key}: {type(exc).__name__}: {exc}")

        elif kind == "passive" and now - started >= WORK_CAP_SECONDS:
            lines = await _finish_passive(key, announce=False)
            changed = True
            coins = 0
            minutes = 0
            if lines:
                payload = worker_cap_summary(key)
                coins, minutes = payload
            try:
                user = bot.get_user(int(key)) or await bot.fetch_user(int(key))
                await user.send(f"Clocked out after 8 hours. Total: {coins} coins for {minutes} minutes.")
            except Exception as exc:
                log(f"Clock-out DM failed for {key}: {type(exc).__name__}: {exc}")

    if changed:
        worker.save()


def worker_cap_summary(key: str):
    """(coins, minutes) for the passive job that just capped out.

    Read after `_finish_passive`, which stashes the last payment here so the
    clock-out DM can quote the exact numbers that were paid.
    """
    return LAST_PASSIVE_PAYOUT.get(key, (0, 0))


@work_sweep.before_loop
async def before_work_sweep():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def reminder_sweep():
    """DM any reminder whose time has come, then drop it."""
    now = time.time()
    changed = False
    for key, entry in list(reminders.data.items()):
        if not isinstance(entry, dict) or float(entry.get("due_at", 0)) > now:
            continue
        reminders.data.pop(key, None)
        changed = True
        try:
            user = bot.get_user(int(key)) or await bot.fetch_user(int(key))
            await user.send(f"**Reminder:** {entry.get('text', '')}")
        except Exception as exc:
            log(f"Reminder DM failed for {key}: {type(exc).__name__}: {exc}")
    if changed:
        reminders.save()


@reminder_sweep.before_loop
async def before_reminder_sweep():
    await bot.wait_until_ready()


@tasks.loop(seconds=60)
async def heartbeat():
    """Upsert the `member` row in `bot_status`, then push the economy and state.

    The member site reads the row with the anon key (RLS allows SELECT) and
    considers the bot online while the timestamp is under three minutes old.
    Any failure is logged once and then ignored — a heartbeat must never take
    the process down. `mirror_state()` rides along here because this is the
    only timer the bot always has: it is what keeps `bot_state` current.
    """
    row = {
        "id": "member",
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "version": BOT_VERSION,
        "guild_count": len(bot.guilds),
        "member_count": sum(g.member_count or 0 for g in bot.guilds),
        "latency_ms": latency_ms(),
        "uptime_seconds": int(time.time() - START_TIME),
    }
    if not supa_upsert("bot_status", row, "id"):
        # `uptime_seconds` only exists if the schema has been migrated. Rather
        # than let the whole heartbeat fail on a missing optional column, drop
        # it and try the write that has always worked.
        row.pop("uptime_seconds", None)
        if supa_upsert("bot_status", row, "id"):
            log("Heartbeat saved without uptime — add the column to bot_status "
                "(alter table bot_status add column if not exists uptime_seconds integer default 0;) "
                "to get real uptime on the site.")
        else:
            log(f"Heartbeat failed — {_supabase_diagnosis()}")

    mirror_economy()
    mirror_state()


def _supabase_diagnosis() -> str:
    """A short, specific reason the last Supabase write did not land.

    The body is trimmed to a line and the status code is always included: with
    the raw body alone, a successful read and a rejected write can look alike.
    """
    if not SUPABASE_URL:
        return "SUPABASE_URL is not set."
    if not SUPABASE_SERVICE_KEY:
        return "SUPABASE_SERVICE_KEY is not set."

    status, raw = supa("GET", "bot_status?select=id&limit=1")
    body = (raw or "").strip().replace("\n", " ")[:300]

    if status == 0:
        return f"the request never completed ({body or 'network error'})."
    if status in (401, 403):
        return (f"Supabase rejected the key (HTTP {status}). Check "
                f"SUPABASE_SERVICE_KEY is the service_role key from "
                f"Project Settings > API, not the anon key. {body}")
    if status == 404:
        return (f"the bot_status table was not found (HTTP 404). Run "
                f"wispbyte_schema.sql in the Supabase SQL editor. {body}")
    if status >= 400:
        return f"HTTP {status}. {body}"
    # A readable table means the read works, so the failure is in the write —
    # almost always a column that has not been added yet.
    return (f"the write was rejected while reads still work (probe HTTP {status}), which usually "
            f"means a column is missing from bot_status. Run the ALTER TABLE statements from "
            f"pages/schema.sql. Read probe: {body}")


def _economy_row(uid, rec, stamp=None) -> dict:
    """The `economy` table row for one member. `stamp` is only for pushes."""
    row = {
        "user_id": str(uid),
        "balance": int(rec.get("balance", 0)),
        "bank": int(rec.get("bank", 0)),
        "xp": int(rec.get("xp", 0)),
        "level": level_of(rec),
        "wins": int(rec.get("wins", 0)),
        "losses": int(rec.get("losses", 0)),
        "streak": int(rec.get("streak", 0)),
    }
    if stamp:
        row["updated_at"] = stamp
    return row


def _economy_digest(row) -> str:
    """The mirrored fields, checksummed: identical means nothing to send."""
    def num(key):
        try:
            return int(row.get(key) or 0)
        except Exception:
            return 0

    return hashlib.sha256(
        f"{num('balance')}|{num('bank')}|{num('xp')}|{num('level')}|"
        f"{num('wins')}|{num('losses')}|{num('streak')}".encode("utf-8")
    ).hexdigest()


def adopt_economy_edits(force_all: bool = False) -> int:
    """Take currency changes made outside this bot from the `economy` table.

    The staff panel's Currency button and the admin bot's money commands both
    write that table, because it is the one place two separate bots can reach.
    A row whose numbers differ from what this bot last pushed was changed by
    someone else, and that change wins: it is what the site is already showing,
    and the next push would otherwise flatten it back.

    Reads are incremental on `updated_at`, which our own writes set — a row
    edited by hand in Supabase does not touch that column, so every
    ECONOMY_SWEEP_SECONDS the whole table is read to catch those too.

    Returns how many records changed.
    """
    global _ECONOMY_LAST_SEEN, _ECONOMY_LAST_SWEEP

    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return 0

    now = time.time()
    full = (force_all or not _ECONOMY_LAST_SEEN
            or (now - _ECONOMY_LAST_SWEEP) >= ECONOMY_SWEEP_SECONDS)
    query = ("economy?select=user_id,balance,bank,xp,level,wins,losses,streak,updated_at"
             "&order=updated_at.desc&limit=1000")
    if not full and _ECONOMY_LAST_SEEN:
        query += f"&updated_at=gt.{urllib.parse.quote(_ECONOMY_LAST_SEEN)}"

    status, raw = supa("GET", query)
    if status != 200:
        return 0
    try:
        rows = json.loads(raw)
    except Exception:
        return 0
    if not isinstance(rows, list):
        return 0

    _ECONOMY_LAST_SWEEP = now
    changed_any = False
    example = ""

    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = str(row.get("updated_at") or "")
        if stamp > _ECONOMY_LAST_SEEN:
            _ECONOMY_LAST_SEEN = stamp
        uid = str(row.get("user_id") or "")
        if not uid.isdigit():
            continue

        digest = _economy_digest(row)
        if _ECONOMY_DIRTY.get(uid) == digest:
            continue                      # our own last write, nothing to adopt
        _ECONOMY_DIRTY[uid] = digest      # so the next pass does not repeat this

        rec = acct(uid)
        fields = ("balance", "bank", "xp", "wins", "losses", "streak")
        before = {field: int(rec.get(field, 0)) for field in fields}
        try:
            wanted = {field: int(row.get(field) or 0) for field in fields}
        except Exception:
            continue
        if before == wanted:
            continue

        rec.update(wanted)
        changed_any = True
        if not example:
            example = (f"{uid} {before['balance']:,} -> {wanted['balance']:,}"
                       if before["balance"] != wanted["balance"] else f"{uid} xp/level")

    if changed_any:
        economy.save()
        log(f"Economy: adopted a change made outside the bot ({example}).")
        return 1
    return 0


def mirror_economy(force: bool = False) -> None:
    """Adopt outside changes, then push economy records that changed here.

    The JSON file is the source of truth for everything the bot itself awards;
    the table is the source of truth for what an operator changed by hand (see
    adopt_economy_edits). A failure is logged and skipped, because the bot has
    to keep running even when the site cannot be reached.

    Rows are checksummed and skipped when unchanged, so a quiet server costs one
    small request per pass instead of one row per member every time.
    """
    global _ECONOMY_DIRTY

    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        return

    adopt_economy_edits()

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows = []
    seen = set()

    for uid, rec in list(economy.data.items()):
        if not isinstance(rec, dict) or not str(uid).isdigit():
            continue
        key = str(uid)
        seen.add(key)
        row = _economy_row(key, rec, now)
        digest = _economy_digest(row)
        if not force and _ECONOMY_DIRTY.get(key) == digest:
            continue
        _ECONOMY_DIRTY[key] = digest
        rows.append(row)

    # Forget records for people who no longer have an entry, so the cache cannot
    # grow forever.
    for gone in [k for k in _ECONOMY_DIRTY if k not in seen]:
        _ECONOMY_DIRTY.pop(gone, None)

    if not rows:
        return

    # Batched, so one enormous guild cannot build a single oversized request.
    for start in range(0, len(rows), 200):
        chunk = rows[start:start + 200]
        status, raw = supa(
            "POST",
            "economy?on_conflict=user_id",
            data=chunk,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if not (200 <= status < 300):
            log(f"Economy mirror failed ({status}): {raw[:200]}")
            return


@tasks.loop(seconds=ECONOMY_PULL_SECONDS)
async def economy_sync():
    """Notice currency changed from the panel or the admin bot, quickly.

    Deliberately on the event loop rather than a thread: it edits the same
    in-memory records the commands do, and one small request every fifteen
    seconds is not worth a lock.
    """
    mirror_economy()


@economy_sync.before_loop
async def before_economy_sync():
    await bot.wait_until_ready()


@heartbeat.before_loop
async def before_heartbeat():
    await bot.wait_until_ready()


@bot.event
async def setup_hook():
    """Register the persistent ticket button before the gateway connects."""
    # Before anything can read or write state: rebuild any data file this host
    # does not have (a fresh VM, or a wiped data directory) from Supabase.
    restore_stores()

    try:
        bot.add_view(TicketCreationView())
        log("Ticket panel button registered as a persistent view.")
    except Exception as exc:
        log(f"Could not register the ticket panel view: {type(exc).__name__}: {exc}")

    sweep_game_state.start()
    work_sweep.start()
    reminder_sweep.start()

    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        heartbeat.start()
        economy_sync.start()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def main():
    if not TOKEN:
        log("MEMBER_BOT_TOKEN is missing. Put it in .env next to this file.")
        raise SystemExit(1)

    os.makedirs(DATA_DIR, exist_ok=True)
    log("wispcord member bot starting…")
    log(f"Data directory: {DATA_DIR}")
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        log("Discord rejected MEMBER_BOT_TOKEN. Check you copied the member bot's token, not the admin bot's.")
        raise SystemExit(1)
    except Exception as exc:
        log(f"Fatal: {type(exc).__name__}: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

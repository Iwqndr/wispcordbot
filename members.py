import asyncio
import datetime
import hashlib
import json
import math
import os
import random
import re
import time
import urllib.error
import urllib.request

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

TOKEN = (os.getenv("MEMBER_BOT_TOKEN") or "").strip()
COMMAND_PREFIX = (os.getenv("COMMAND_PREFIX") or ">").strip() or ">"
MEMBER_SITE_URL = (os.getenv("MEMBER_SITE_URL") or "").strip().rstrip("/")

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()

# Bumped by hand when the deployed build changes; lands in the heartbeat row.
BOT_VERSION = "1.0.0"

# Where every JSON file of persistent state lives. Created on first run and
# deliberately gitignored — this is runtime data, not source.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memberbot_data")

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
# ---------------------------------------------------------------------------


class JsonStore:
    """A JSON file that behaves like a dict and can be saved on demand.

    Every write goes to a `.tmp` next to the target and is then replaced, so a
    crash mid-write can never leave a half-written file behind.
    """

    def __init__(self, filename: str, default=None):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.path = os.path.join(DATA_DIR, filename)
        self.default = default if default is not None else {}
        self.data = self._read()

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return json.loads(json.dumps(self.default))
        except Exception as exc:  # corrupt file: keep going on a fresh copy
            log(f"Could not read {os.path.basename(self.path)}: {exc}")
            return json.loads(json.dumps(self.default))

    def save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            log(f"Could not write {os.path.basename(self.path)}: {exc}")


economy = JsonStore("economy.json")            # uid -> balance/bank/xp/inventory
lb_store = JsonStore("leaderboards.json", {"wins": {}, "games": {}, "trivia": {}, "hangman": {}})
trivia_store = JsonStore("trivia.json", {"used": [], "scores": {}})

# Live game state that does not need to survive a restart. Each entry carries a
# `touch` timestamp so abandoned games can be swept — see sweep_game_state().
HANGMAN = {}      # channel_id -> {word, guessed:set, wrong:int, touch:float}
WORDCHAIN = {}    # channel_id -> {"last": word, "used": set, "touch": float}
GUESSING = {}     # channel_id -> {"number": int, "tries": int, "touch": float}

# How long an untouched game is allowed to sit in memory before it is dropped.
GAME_STATE_TTL = 1800.0


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
        rec = {"balance": 0, "bank": 0, "xp": 0, "wins": 0, "losses": 0, "inventory": {}, "last_daily": 0, "last_work": 0, "last_rob": 0}
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

# Nekos.best categories for the GIF commands. `boop` and `kill` are not real
# categories there, so they borrow the closest fitting one.
GIF_KINDS = {
    "hug": "hug",
    "pat": "pat",
    "boop": "poke",
    "kill": "kick",
    "kiss": "kiss",
    "marry": "kiss",
    "divorce": "cry",
    "ship": "cuddle",
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
    ]),
    ("Actions", [
        ("hug @user", "Hug someone"),
        ("pat @user", "Pat someone"),
        ("boop @user", "Boop someone"),
        ("kill @user", "Dramatically end someone"),
        ("kiss @user", "Kiss someone"),
        ("ship @user1 @user2", "Ship two people"),
        ("marry @user", "Marry someone"),
        ("divorce @user", "Divorce someone"),
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
        ("balance", "Show your coins"),
        ("work", "Work for coins"),
        ("slots <bet>", "Play the slot machine"),
        ("blackjack <bet>", "Play blackjack"),
        ("rob @user", "Try to rob someone"),
        ("shop", "Browse the shop"),
        ("shop buy <item>", "Buy an item"),
        ("gift @user <amount>", "Give coins to someone"),
    ]),
    ("Social", [
        ("leaderboard", "Richest members"),
        ("profile [@user]", "Your profile card"),
        ("compare @a @b", "Compare two members"),
        ("stats", "Bot statistics"),
    ]),
    ("Time-wasters", [
        ("meme", "Random meme"),
        ("catfact", "A cat fact"),
        ("dogfact", "A dog fact"),
        ("foxfact", "A fox fact"),
        ("joke", "A random joke"),
        ("quote", "A random quote"),
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
        log("Supabase heartbeat: configured")
    else:
        log("Supabase heartbeat: disabled (SUPABASE_URL / SUPABASE_SERVICE_KEY missing)")


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
# MEMBER COMMANDS — ported from the admin bot's bot.py, behaviour unchanged
# ---------------------------------------------------------------------------


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.send(f"> Latency: **{latency_ms()}ms**")


@bot.command(name="serverinfo")
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


@bot.command(name="userinfo")
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


@bot.command(name="avatar")
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


@bot.command(name="roleinfo")
async def roleinfo(ctx: commands.Context, *, role: discord.Role):
    embed = discord.Embed(title=role.name, color=role.color)
    embed.add_field(name="ID", value=str(role.id))
    embed.add_field(name="Color", value=str(role.color))
    embed.add_field(name="Members", value=str(len(role.members)))
    embed.add_field(name="Mentionable", value=str(role.mentionable))
    embed.add_field(name="Hoisted", value=str(role.hoist))
    embed.add_field(name="Position", value=str(role.position))
    await ctx.send(embed=embed)


@bot.command(name="channelinfo")
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
    try:
        await ctx.send(embed=embed, delete_after=45)
    except Exception:
        await ctx.send(embed=embed)


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


@bot.command(name="coinflip")
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
    embed = discord.Embed(
        description=f"**{ctx.author.display_name}** is proposing to **{member.display_name}**…",
        color=discord.Color.magenta(),
    )
    message = await ctx.send(embed=embed)
    await message.add_reaction("\U00002764")
    await message.add_reaction("\U0001F494")

    def check(reaction, user):
        return user.id == member.id and str(reaction.emoji) in ("\U00002764", "\U0001F494") and reaction.message.id == message.id

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=60.0, check=check)
    except asyncio.TimeoutError:
        return await ctx.send(f"> {member.display_name} did not answer. Awkward.")
    if str(reaction.emoji) == "\U00002764":
        add_coins(ctx.author.id, 50)
        await ctx.send(f"> \U0001F389 **{member.display_name}** said yes! (+50 {CURRENCY} for the happy couple)")
    else:
        await ctx.send(f"> **{member.display_name}** said no.")


@bot.command(name="divorce")
async def divorce(ctx: commands.Context, member: discord.Member):
    embed = discord.Embed(
        description=f"**{ctx.author.display_name}** has divorced **{member.display_name}**.",
        color=discord.Color.dark_grey(),
    )
    gif = fetch_gif("divorce")
    if gif:
        embed.set_image(url=gif)
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# TRIVIA
# ---------------------------------------------------------------------------


class ChoiceView(discord.ui.View):
    """A four-button multiple choice used by trivia and flags."""

    def __init__(self, options, correct_index: int, author_id: int, on_result=None):
        super().__init__(timeout=30)
        self.correct_index = correct_index
        self.author_id = author_id
        self.on_result = on_result
        self.answered = False
        for index, label in enumerate(options):
            button = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary, row=0)
            button.callback = self._make_callback(index)
            self.add_item(button)

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

    await ctx.send(embed=discord.Embed(title=question, color=discord.Color.blurple()), view=ChoiceView(options, correct, ctx.author.id, on_result))


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

    await ctx.send(embed=discord.Embed(title=flag, description="Which country is this?", color=discord.Color.blurple()),
                   view=ChoiceView(options, correct, ctx.author.id, on_result))


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
    await ctx.send(f"> Hangman! 6 misses allowed.\n> " + hangman_display(state), view=HangmanView(ctx.channel.id))


def hangman_display(state) -> str:
    shown = " ".join(ch if ch in state["guessed"] else "_" for ch in state["word"])
    wrong = state["wrong"]
    return f"`{shown}`  ·  misses {wrong}/6"


class HangmanView(discord.ui.View):
    """Letter menus for hangman.

    Two string selects, not 26 buttons: Discord allows five buttons per action
    row and five rows per view, so an A-Z button grid needs six rows and is
    rejected outright. A guessed letter is *removed* from its menu rather than
    greyed out, because SelectOption has no `disabled` flag — the menus are
    rebuilt after every guess instead.
    """

    LETTER_ROWS = ("ABCDEFGHIJKLM", "NOPQRSTUVWXYZ")

    def __init__(self, channel_id: int):
        super().__init__(timeout=180)
        self.channel_id = channel_id
        # Redraw from whatever the game has already had guessed, so a view
        # rebuilt for an in-progress game matches the state instead of offering
        # every letter again.
        live = HANGMAN.get(channel_id) or {}
        self._rebuild(set(live.get("guessed", set())))

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
            return await interaction.response.edit_message(
                content=f"> Solved! The word was **{state['word']}**. +100 {CURRENCY}", view=self)

        if state["wrong"] >= 6:
            HANGMAN.pop(self.channel_id, None)
            for child in self.children:
                child.disabled = True
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
    member = member or ctx.author
    rec = acct(member.id)
    level, into, needed = level_progress(rec)
    embed = discord.Embed(title=f"{member.display_name}'s balance", color=discord.Color.gold())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Wallet", value=f"{rec['balance']:,} {CURRENCY}")
    embed.add_field(name="Bank", value=f"{rec['bank']:,} {CURRENCY}")
    embed.add_field(name="Level", value=f"{level} · {into}/{needed} xp\n{bar(int(10 * into / needed))}")
    embed.add_field(name="Record", value=f"{rec['wins']}W / {rec['losses']}L")
    await ctx.send(embed=embed)


@bot.command(name="daily")
async def daily(ctx: commands.Context):
    rec = acct(ctx.author.id)
    now = time.time()
    if now - rec["last_daily"] < 86400:
        wait = human_delta(86400 - (now - rec["last_daily"]))
        return await ctx.send(f"> Already claimed. Come back in **{wait}**.")
    base = 250
    bonus = random.randint(0, 150)
    total = base + bonus
    rec["balance"] += total
    rec["last_daily"] = now
    add_xp(ctx.author.id, 20)
    economy.save()
    await ctx.send(f"> Daily claimed: **{total}** {CURRENCY} (base {base} + bonus {bonus}).")


@bot.command(name="work")
@commands.cooldown(rate=1, per=60, type=commands.BucketType.user)
async def work(ctx: commands.Context):
    jobs = ["washed dishes", "walked a dog", "wrote some code", "stacked shelves", "drove a bus", "fixed a printer"]
    earned = random.randint(40, 120)
    add_coins(ctx.author.id, earned)
    add_xp(ctx.author.id, 5)
    await ctx.send(f"> You {random.choice(jobs)} and earned **{earned}** {CURRENCY}.")


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
        await interaction.response.edit_message(content=self._text(reveal=True) + f"\n> {outcome}", view=self)
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
            return await interaction.response.edit_message(
                content=self._text(reveal=True) + f"\n> Bust — you lose **{self.bet}** {CURRENCY}.", view=self)
        await interaction.response.edit_message(content=self._text(), view=self)


@bot.command(name="blackjack")
async def blackjack(ctx: commands.Context, bet: str = "10"):
    rec = acct(ctx.author.id)
    amount = parse_bet(bet, rec)
    if amount is None:
        return await ctx.send("> That bet doesn't work. Try `10`, `half` or `all`.")
    view = BlackjackView(ctx.author.id, amount)
    await ctx.send(view._text(), view=view)


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


@bot.command(name="leaderboard", aliases=["lb"])
async def leaderboard(ctx: commands.Context):
    recs = [(uid, rec) for uid, rec in economy.data.items() if isinstance(rec, dict)]
    recs.sort(key=lambda pair: int(pair[1].get("balance", 0)) + int(pair[1].get("bank", 0)), reverse=True)
    lines = []
    for index, (uid, rec) in enumerate(recs[:10], start=1):
        try:
            user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
            name = user.display_name
        except Exception:
            name = f"user {uid}"
        total = int(rec.get("balance", 0)) + int(rec.get("bank", 0))
        lines.append(f"`{index:>2}.` **{name}** — {total:,} {CURRENCY}")
    embed = discord.Embed(title="Richest members", description="\n".join(lines) or "Nobody has any coins yet.",
                          color=discord.Color.gold())
    await ctx.send(embed=embed)


@bot.command(name="profile")
async def profile(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    rec = acct(member.id)
    level, into, needed = level_progress(rec)
    embed = discord.Embed(title=member.display_name, color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Level", value=f"{level}")
    embed.add_field(name="Coins", value=f"{int(rec['balance']) + int(rec['bank']):,} {CURRENCY}")
    embed.add_field(name="Record", value=f"{rec['wins']}W / {rec['losses']}L")
    inventory = rec.get("inventory") or {}
    if inventory:
        items = [f"{SHOP_ITEMS.get(k, {}).get('name', k)} x{v}" for k, v in inventory.items()]
        embed.add_field(name="Inventory", value=", ".join(items)[:1020], inline=False)
    embed.add_field(name="Progress", value=f"{into}/{needed} xp {bar(int(10 * into / needed))}", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="compare")
async def compare(ctx: commands.Context, first: discord.Member, second: discord.Member = None):
    second = second or ctx.author
    a, b = acct(first.id), acct(second.id)
    embed = discord.Embed(title=f"{first.display_name} vs {second.display_name}", color=discord.Color.blurple())
    rows = [
        ("Coins", int(a["balance"]) + int(a["bank"]), int(b["balance"]) + int(b["bank"])),
        ("Level", level_of(a), level_of(b)),
        ("Wins", int(a["wins"]), int(b["wins"])),
    ]
    for label, left, right in rows:
        winner_left = left >= right
        embed.add_field(name=label, value=f"{'> ' if winner_left else ''}{left:,}\n{'< ' if not winner_left else ''}{right:,}", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="stats")
async def stats(ctx: commands.Context):
    embed = discord.Embed(title="Bot statistics", color=discord.Color.blurple())
    embed.add_field(name="Servers", value=str(len(bot.guilds)))
    embed.add_field(name="Members", value=f"{sum(g.member_count or 0 for g in bot.guilds):,}")
    embed.add_field(name="Latency", value=f"{latency_ms()}ms")
    embed.add_field(name="Uptime", value=uptime_text())
    embed.add_field(name="Commands", value=str(len(bot.commands)))
    embed.add_field(name="Version", value=BOT_VERSION)
    await ctx.send(embed=embed)


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


@bot.command(name="quote")
async def quote(ctx: commands.Context):
    data = http_json("https://api.quotable.io/random")
    if data and data.get("content"):
        text, author = data["content"], data.get("author", "Unknown")
    else:
        text, author = random.choice(FALLBACK_QUOTES)
    embed = discord.Embed(description=f"*{text}*", color=discord.Color.blurple())
    embed.set_footer(text=f"— {author}")
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# TICKET PANEL — the persistent "Create Ticket" button
# ---------------------------------------------------------------------------

TICKET_CUSTOM_ID = "ticket_creation_button"


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
        if SUPPORT_PAGE_URL:
            message = f"Head to our [support page]({SUPPORT_PAGE_URL}) to open a Ticket."
        else:
            message = "Head to our support page to open a Ticket."
        await interaction.response.send_message(message, ephemeral=True)


# ---------------------------------------------------------------------------
# HEARTBEAT — lets the member page show this bot's live status
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


@tasks.loop(seconds=60)
async def heartbeat():
    """Upsert the `member` row in `bot_status` once a minute.

    The member page reads this row with the anon key (RLS allows SELECT), and
    considers the bot online while the timestamp is under three minutes old.
    Any failure is logged once and then ignored — a heartbeat must never take
    the process down.
    """
    row = {
        "id": "member",
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "version": BOT_VERSION,
        "guild_count": len(bot.guilds),
        "member_count": sum(g.member_count or 0 for g in bot.guilds),
        "latency_ms": latency_ms(),
    }
    if not supa_upsert("bot_status", row, "id"):
        log("Heartbeat failed — check SUPABASE_URL / SUPABASE_SERVICE_KEY and the bot_status table.")


@heartbeat.before_loop
async def before_heartbeat():
    await bot.wait_until_ready()


@bot.event
async def setup_hook():
    """Register the persistent ticket button before the gateway connects."""
    try:
        bot.add_view(TicketCreationView())
        log("Ticket panel button registered as a persistent view.")
    except Exception as exc:
        log(f"Could not register the ticket panel view: {type(exc).__name__}: {exc}")

    sweep_game_state.start()

    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        heartbeat.start()


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

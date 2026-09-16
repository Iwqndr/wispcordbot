# wispcord — the member bot

The member-facing half of the Discord bot. It runs **on its own** as a second
Discord application, deployed to Wispbyte, and shares no code with the admin bot
that stays on your PC.

What it does:

- every member-facing command (`>ping`, `>serverinfo`, `>help`, `>roll`, …) so
  members never see moderation commands exist
- 40+ minigames — trivia, economy, gambling, socials, action GIFs
- the persistent **Create Ticket** panel button, which sends members to the
  support form on your member page
- a 60-second Supabase heartbeat so the member page can show live bot status

**Nothing in this folder is required for the admin bot, and nothing in the admin
bot's folder is required for this one.**

---

## Before you start

You need all of these already in place:

| Thing | Why |
|---|---|
| A Discord account | to create the second application |
| A GitHub account | to host this folder |
| A Supabase account with a **project created but empty** | tables + heartbeat |
| A Cloudflare account | to host the member page |
| Python 3.11+ on Windows | to run the admin bot (already working) |
| One working admin bot on your PC | the other half of the system |

Throughout this guide, replace anything in `<angle brackets>` with your own
value. Never paste a token or a service key into a public chat.

---

## 1. Create the second Discord application (the member bot)

### 1.1 Make the application

1. Open **https://discord.com/developers/applications**
2. Click **New Application** (top right).
3. Name it — e.g. `wispcord` or `members` — and tick the policy checkbox.
4. Click **Create**.

### 1.2 Enable the intents it needs

1. In the left sidebar click **Bot**.
2. Scroll to **Privileged Gateway Intents**.
3. Turn **ON** each of these three:

   - **Presence Intent**
   - **Server Members Intent**
   - **Message Content Intent**

   `Message Content` is what lets it read `>commands`. `Server Members` is what
   lets `>userinfo @someone` and the join/leave behaviour work. Discord shows a
   verification notice for the first two on large bots — that only matters at
   100+ servers.

4. Click **Save Changes**.

### 1.3 Copy the token

1. Still on the **Bot** page, click **Reset Token** → **Yes, do it!**
2. Click **Copy**.

   > This is `MEMBER_BOT_TOKEN`. It is **not** the admin bot's token. If you paste
   > your admin token here, the member bot will start up as the admin bot — two
   > processes logged in as the same application, which fights itself.

3. Paste it somewhere safe for now (you will use it in step 5).

### 1.4 Build the invite URL

1. In the left sidebar click **OAuth2** → **URL Generator**.
2. Under **Scopes**, tick exactly these two:

   - `bot`
   - `applications.commands`

3. Under **Bot Permissions**, tick these checkboxes:

   **General Server Permissions**
   - View Channels
   - Send Messages
   - Send Messages in Threads
   - Embed Links
   - Attach Files
   - Read Message History
   - Add Reactions
   - Use External Emojis
   - Manage Messages *(only if you want it to delete its own help messages — optional)*

   That set produces the permission integer **117824**. If you would rather paste
   the integer, scroll to the bottom of the page and read the generated URL — it
   ends in `&permissions=117824`. You can also just swap the number in.

4. Copy the generated URL.
5. Open it in a new browser tab, pick your server, and click **Authorize**.
6. Confirm the member bot shows up in your member list (it will be offline until
   you finish step 5).

---

## 2. Create the GitHub repository

The repository is called `wispcordbot`, which gives you this clone URL:

```
https://github.com/Iwqndr/wispcordbot.git
```

### 2.1 Create it on GitHub

1. Open **https://github.com/new**
2. **Repository name**: `wispcordbot`
3. **Description**: optional.
4. Set it to **Public** or **Private** — either works with Wispbyte.
5. Do **not** tick "Add a README file", "Add .gitignore" or "Choose a license".
   This folder already has all three.
6. Click **Create repository**.

### 2.2 Push this folder to it

**The easy way.** Open **PowerShell**, go into this folder, and run one command:

```powershell
cd "C:\path\to\wispcord"
python push.py
```

That is all of step 2.2. If the folder is not a git repository yet, `push.py`
creates one on the `main` branch, adds the `origin` remote, sets a commit
identity for that repository only (derived from the repository owner, so your
global git config is never touched), commits everything, and pushes.

Useful options:

| Command | What it does |
|---|---|
| `python push.py` | set up if needed, then push |
| `python push.py -m "message"` | use your own commit message |
| `python push.py <url>` | push to a different repository |
| `python push.py --force` | allow setup inside another git repo |
| `python push.py --help` | show the usage |

It refuses to run when the folder sits inside another git repository, because
that would nest one repo inside another and leave the outer one holding a broken
embedded-repo reference. Move this folder out first, then run it again.

**The manual way,** if you would rather do it by hand:

```powershell
cd "C:\path\to\wispcord"
```

```powershell
git init
```

```powershell
git add .
git commit -m "Initial commit"
```

```powershell
git branch -M main
```

```powershell
git remote add origin https://github.com/Iwqndr/wispcordbot.git
```

```powershell
git push -u origin main
```

When it asks you to sign in, use your GitHub account and a **personal access
token** as the password (GitHub no longer accepts your account password). Create
one at **https://github.com/settings/tokens** → *Tokens (classic)* → *Generate
new token (classic)* → tick **repo** → *Generate*.

Confirm it worked: refresh the repository page and you should see `members.py`,
`README.md`, `requirements.txt`, `.env.example`, `.gitignore` and
`wispbyte_schema.sql`. You should **not** see `.env` or `memberbot_data/` — the
`.gitignore` keeps them out.

---

## 3. Create the Wispbyte server

1. Open **https://wispbyte.com** and click **Register** / **Sign Up**.
2. Confirm your email, then log in.
3. On the dashboard click **Create Server** (sometimes labelled *New Server*).
4. Fill it in like this:

   | Field | Value |
   |---|---|
   | **Server Name** | `wispcord` (anything you like) |
   | **Server Type / Egg** | **Discord Bot** — if there is a plain **Python** option instead, pick that |
   | **Python Version** | **3.11** (or the highest 3.11/3.x offered) |
   | **RAM** | the **smallest tier** that includes Python 3.11 — 512 MB is plenty for a bot with no database |
   | **Region** | the one closest to your players |
   | **Disk** | the default |

5. Scroll to **Deployment** / **GitHub Options** and paste:

   | Field | Value |
   |---|---|
   | **Repository URL** | `https://github.com/Iwqndr/wispcordbot.git` |
   | **Branch** | `main` |
   | **Auto Update on Startup** | **ON** — this way a `git push` is enough to deploy |
   | **Start Command** | `python members.py` |

6. Click **Create Server** and wait for it to provision (usually under a minute).

> If your panel has no GitHub options, leave them blank: you can create the files
> through the panel's own file manager or SFTP instead. The repository URL is a
> convenience, not a requirement.

---

## 4. Install the requirements

Open your server and find the **Console** (or **Terminal**) tab.

Type this and press Enter:

```
pip install -r requirements.txt
```

If Wispbyte complains that `pip` is not a recognised command, try:

```
python -m pip install -r requirements.txt
```

If the GitHub clone did not happen automatically, clone by hand first:

```
git clone https://github.com/Iwqndr/wispcordbot.git .
```

The `.` on the end matters — it clones *into* the current directory instead of a
subfolder.

You can also use the panel's **Startup** / **egg** field for a python package
list, but the `pip install` above is the one to trust.

---

## 5. Set every environment variable

You have two ways to do this. Pick one.

### Option A — the panel (recommended on Wispbyte)

1. Open your server.
2. Click the **Startup** tab.
3. Find the **Environment Variables** / **Variables** section.
4. Add each row below with **Add Variable**.

### Option B — a `.env` file

1. Open the **Files** tab.
2. Open (or create) `.env` in the server's root folder, next to `members.py`.
3. Paste the block from `.env.example` and fill in your values.

### The variables

| Variable | What goes in it | Where the value comes from |
|---|---|---|
| `MEMBER_BOT_TOKEN` | the token you copied in step 1.3 | Discord → your member app → **Bot** → Reset Token → Copy |
| `COMMAND_PREFIX` | `>` (or anything) | your choice; leave it blank for `>` |
| `MEMBER_SITE_URL` | `https://your-site.pages.dev` — no trailing slash | your Cloudflare Pages URL, from step 8 |
| `SUPABASE_URL` | `https://xxxxxxxxxxxx.supabase.co` | Supabase → **Project Settings** → **API** → *Project URL* |
| `SUPABASE_SERVICE_KEY` | the long `service_role` secret | Supabase → **Project Settings** → **API** → *Project API keys* → `service_role` |

> ⚠️ `SUPABASE_SERVICE_KEY` bypasses every row-level security policy. It lives on
> the server only. Never put it in the member page, never commit it, never paste
> it into Discord.

Save the variables, then **Restart** the server.

---

## 6. Run the SQL in Supabase

1. Open **https://supabase.com/dashboard** and click your project.
2. In the left sidebar click **SQL Editor**.
3. Click **+ New query**.
4. Open `wispbyte_schema.sql` from this folder, select all of it, and paste it
   into the editor.
5. Click **Run** (or press `Ctrl` + `Enter`).
6. You should see **Success. No rows returned.** That is correct — the script
   creates tables, it does not return data.

Verify: click **Table Editor** in the left sidebar. You should see six tables:
`bot_status`, `media_only_channels`, `pending_uploads`, `selfpromo_channels`,
`ticket_queue`, `ticket_reply_queue`.

The script is safe to run again if you are unsure — every statement checks first.

---

## 7. Test that the member bot connects

1. In Wispbyte, open the **Console** tab.
2. Click **Start** (or **Restart**) if it is not already running.
3. Within a few seconds you should see lines like this:

   ```
   [13:04:02] wispcord member bot starting…
   [13:04:02] Data directory: /home/container/memberbot_data
   [13:04:03] Ticket panel button registered as a persistent view.
   [13:04:05] Logged in as wispcord#1234 (ID: 1234567890123456789)
   [13:04:05] Prefix: '>' · serving 1 guild(s)
   [13:04:05] Ticket panel link: https://your-site.pages.dev?open=support
   [13:04:05] Supabase heartbeat: configured
   ```

4. Now test in Discord:
   - Type `>ping` — you should get `Latency: **42ms**`.
   - Type `>help` — you should get the member command list, with **no moderation
     commands in it**.
   - Type `>daily` — you should get coins.
   - Make sure the member bot shows as **online** in your member list.

**If it says `MEMBER_BOT_TOKEN is missing`** — the variable did not save. Re-check
step 5 and restart.

**If it says `Discord rejected MEMBER_BOT_TOKEN`** — you pasted the admin bot's
token, or there is a stray space. Copy it again from the member application.

**If it says `Heartbeat failed`** — either the SQL from step 6 has not run, or the
service key is wrong. The bot still works; only the status dot will be wrong.

---

## 8. Cloudflare setup for the member page

The member page is a single static file (`templates/member.html` from the admin
project) plus two small Workers.

### 8.1 Publish the page

1. Open **https://dash.cloudflare.com** and click **Workers & Pages**.
2. Click **Create** → **Pages** → **Upload assets**.
3. Name the project, e.g. `wispcord-member`.
4. Drag in a folder containing your member page **renamed to `index.html`**.

   > Cloudflare serves `index.html` at `/`. This is why the admin project has no
   > `templates/index.html` any more — the member page *is* the landing page.

5. Click **Deploy**. Cloudflare gives you a URL like
   `https://wispcord-member.pages.dev`.

6. Copy that URL into the admin bot's stop step — put `MEMBER_SITE_URL` in the
   admin project's `.env` too, then restart `main.py`. That is what the ticket
   panel editor uses to build the `?open=support` link, so both halves agree on
   the same address.

7. Also paste it into Wispbyte as `MEMBER_SITE_URL` (step 5) and restart.

### 8.2 Point the page at Supabase

The member page has the Supabase URL and **anon** key near the top of its script
block:

```js
const SUPABASE_URL = "https://xxxxxxxxxxxx.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOi...";
```

Both are safe in a public page — the anon key is designed to be public, and the
policies from step 6 are what actually restrict it. Get them from Supabase →
**Project Settings** → **API** → *Project URL* and *anon public*.

The heartbeat row (`bot_status` where `id = 'member'`) is readable with the anon
key because of the `anon can read bot status` policy. The page treats the member
bot as **online** while that row's `updated_at` is under three minutes old.

### 8.3 The Worker that signs Supabase uploads

Attachments go straight from the browser to storage, so the browser needs a
short-lived signed URL rather than your service key.

1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Worker**.
2. Name it `wispcord-upload-signer` and **Deploy**.
3. Click **Edit code**, replace the contents with:

   ```js
   export default {
     async fetch(request, env) {
       if (request.method !== "POST") {
         return new Response("Method not allowed", { status: 405 });
       }
       // Only your own site may ask for a signature.
       const origin = request.headers.get("Origin") || "";
       const allowed = (env.ALLOWED_ORIGIN || "").split(",").map(s => s.trim()).filter(Boolean);
       if (!allowed.some(a => origin === a)) {
         return new Response("Forbidden", { status: 403 });
       }

       const { filename } = await request.json().catch(() => ({}));
       if (!filename) return new Response("filename required", { status: 400 });

       const path = `uploads/${crypto.randomUUID()}-${filename}`;
       const uploadRes = await fetch(
         `${env.SUPABASE_URL}/storage/v1/object/upload/sign/attachments/${path}`,
         {
           method: "POST",
           headers: {
             apikey: env.SUPABASE_SERVICE_KEY,
             Authorization: `Bearer ${env.SUPABASE_SERVICE_KEY}`,
             "Content-Type": "application/json",
           },
           body: JSON.stringify({ expiresIn: 300 }),
         }
       );
       if (!uploadRes.ok) {
         return new Response(await uploadRes.text(), { status: uploadRes.status });
       }
       const { signedURL } = await uploadRes.json();
       const publicUrl = `${env.SUPABASE_URL}/storage/v1/object/public/attachments/${path}`;
       return Response.json({ uploadUrl: `${env.SUPABASE_URL}/storage/v1${signedURL}`, publicUrl });
     },
   };
   ```

4. In the Worker's **Settings** → **Variables and Secrets**, add:

   | Name | Value | Type |
   |---|---|---|
   | `SUPABASE_URL` | your project URL | Text |
   | `SUPABASE_SERVICE_KEY` | the `service_role` key | **Secret** |
   | `ALLOWED_ORIGIN` | `https://wispcord-member.pages.dev` | Text |

5. Create a **public** Supabase Storage bucket named `attachments` (Storage →
   New bucket → name it `attachments` → tick *Public bucket*).
6. Copy the Worker's URL (`https://wispcord-upload-signer.<you>.workers.dev`) and
   set it as the upload-signing endpoint in the member page's upload code.

### 8.4 The cron that keeps the free project awake

Supabase free projects **pause after 7 days with no activity**. A paused project
means a dead heartbeat and a member page that says the bot is offline.

1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Cron Trigger**.
   (If your dashboard only offers a Worker, create one and add the trigger under
   its **Settings** → **Triggers** → **Cron Triggers**.)
2. Name it `wispcord-keepalive`.
3. Add two cron expressions so it can never drift past the window:

   ```
   */15 * * * *
   0 9 * * *
   ```

4. Paste this as the Worker body:

   ```js
   export default {
     async scheduled(event, env, ctx) {
       // Any read counts as activity. A single row is the cheapest query.
       await fetch(`${env.SUPABASE_URL}/rest/v1/bot_status?select=id&limit=1`, {
         headers: {
           apikey: env.SUPABASE_SERVICE_KEY,
           Authorization: `Bearer ${env.SUPABASE_SERVICE_KEY}`,
         },
       });
     },
   };
   ```

5. Add the same `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` variables to it (service
   key as a **Secret**), then **Save** and **Deploy**.

---

## 9. Everything else needed to make the stack live

### 9.1 Deploy the ticket panel to a channel

The panel button has to be posted once, from the **admin** bot:

1. Start the admin bot on your PC (`python main.py`).
2. Open the admin panel at `http://localhost:5000/admin`.
3. Go to the **Owner Panel** tab and click the **Ticket Panel** button.
4. Pick the channel in **Deploy / Update The Panel** and click **Deploy Panel**.
4. The button now appears in that channel. It keeps working across restarts of
   **both** bots — the admin bot re-registers the panel message, and the member
   bot re-registers the button handler.

Click the button in Discord: you should get an **ephemeral** reply that says
*"Head to our support page to open a Ticket."* with "support page" as a blue
link. Click the link — the member page opens with the support form already up.

If the reply has no link, `MEMBER_SITE_URL` is empty on the **member bot**
(Wispbyte) — set it and restart.

### 9.2 Redeploying after a change

1. Make your change in this folder.
2. Commit and push — either by hand, or with the helper that ships in this
   folder:

   ```powershell
   python push.py
   ```

   It checks the remote is set, shows what changed, stages, commits as
   `new update`, and pushes. By hand that is:

   ```powershell
   git add .
   git commit -m "Update"
   git push
   ```

3. In Wispbyte, click **Restart** (with *Auto Update on Startup* on, restarting
   pulls the new commit).

### 9.3 Where the data lives

| Data | Location | Survives a restart? |
|---|---|---|
| Balances, xp, inventory | `memberbot_data/economy.json` | yes |
| Win/loss and trivia tallies | `memberbot_data/leaderboards.json` | yes |
| Trivia scores | `memberbot_data/trivia.json` | yes |
| Hangman / word-chain / guess games in progress | memory | no — per-channel, and swept 30 minutes after the last move |
| Bot status row | Supabase `bot_status` | yes |

Back up `memberbot_data/` before wiping or rebuilding the Wispbyte server — the
panel calls it "reinstall" and it deletes everything.

### 9.4 A checklist before you call it done

- [ ] The member bot shows online in Discord
- [ ] `>help` lists member commands and **no** moderation commands
- [ ] `>serverinfo`, `>roll 4d6+2`, `>daily` and `>trivia` all answer
- [ ] The Create Ticket button replies with a working support-page link
- [ ] Opening that link lands on the support form automatically
- [ ] The member page's status widget says the member bot is online
- [ ] `.env` is **not** in the GitHub repository
- [ ] The keepalive cron is enabled so Supabase does not pause

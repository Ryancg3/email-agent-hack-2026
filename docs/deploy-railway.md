# Deploy to Railway (always on)

This runs the web app and the 5-minute scheduler as one Railway service, so the agent
keeps working when your laptop is off. The SQLite database lives on a 1 GB volume
that survives deploys and restarts.

**Cost:** the Hobby plan is $5 a month and includes $5 of usage. This app idles at
around 250 to 300 MB of memory and very little CPU, which should fit inside that. If
it ever goes over, you pay the difference. You can set a hard limit under
**Workspace > Usage**. Anthropic API usage is billed separately.

## What gets deployed

| File | What it does |
| --- | --- |
| `Dockerfile` | Python 3.13 image with the dependencies and collected static files. Debug is off and the database path is `/data/app.db` |
| `deploy/start.sh` | Runs migrations, then gunicorn and `run_scheduler` side by side. If either stops, the container exits and Railway restarts it |
| `railway.json` | Build from the Dockerfile, health check on `/health`, always restart, one replica |

The volume and the environment variables are set in Railway's dashboard, below.
`APP_HOST` defaults to your Railway domain, so you only set it for a custom domain.

## 1. Create the project

1. Sign in at [railway.com](https://railway.com) with GitHub and choose the **Hobby** plan.
2. Click **New Project > Deploy from GitHub repo** and pick `email-agent-hack-2026`.
   If it isn't listed, click **Configure GitHub App** and give Railway access to it.
3. The first build starts right away and fails because the variables are missing.
   That's expected.
4. To deploy from a branch other than `main`, open the service, then
   **Settings > Source > Branch**.

## 2. Add the volume

Press **Cmd+K** (or **Ctrl+K**) on the project canvas, choose **Add Volume**, pick
the service, and set the mount path to:

```
/data
```

## 3. Set the variables

Open the service's **Variables** tab, click **Raw Editor**, and paste:

```
ANTHROPIC_API_KEY=...
TOKEN_KEY=...
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
DJANGO_SECRET_KEY=...
```

Copy the values from your `.env`. If you need new keys:

- `DJANGO_SECRET_KEY`: `python -c "import secrets; print(secrets.token_urlsafe(50))"`
- `TOKEN_KEY`: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  If you ever change it later, you'll need to reconnect Google.

Click **Update Variables**, then **Deploy** to apply them.

## 4. Get a public URL

Open **Settings > Networking** and click **Generate Domain**. If it asks for a port,
leave the suggested one: the app listens on whatever `PORT` Railway gives it. You get
a URL such as `https://email-agent-production-1a2b.up.railway.app`.

Check that `https://<your-domain>/health` returns `{"status": "ok"}`. Use your real
domain in the steps below.

## 5. Update your Google OAuth client

In [Google Cloud Console](https://console.cloud.google.com/apis/credentials), open your
**Web application** OAuth client and add this to **Authorized redirect URIs**, next to
the localhost ones:

```
https://<your-domain>/google/callback/
```

The trailing slash matters. Save it. It can take a few minutes to apply.

Then go to **Google Auth Platform > Audience** (the OAuth consent screen) and check
the **Publishing status**:

- **Testing:** Google expires refresh tokens after 7 days, so the agent would stop
  every week until you reconnect. Click **Publish app** to move it to **In production**.
  If you stay in Testing, the app helps you keep up (see
  [Staying connected in Testing mode](#staying-connected-in-testing-mode)).
- **In production** without verification is fine for your own use. Google shows a
  "Google hasn't verified this app" screen when you connect. Click **Advanced**, then
  **Go to ... (unsafe)**, once.

## 6. Set up your account

1. Open `https://<your-domain>`, sign up and connect Google.
2. Edit your labels on the **Labels** tab and turn the agent on.
3. Close sign-ups so nobody else can run the agent on your Anthropic key: add the
   variable `SIGNUP_CLOSED=1` and deploy. Your account stays.

The hosted app starts with an empty database, separate from your local `app.db`, so
you sign up and connect Google once more there. It only handles mail that arrives
after you connect.

Then **stop `make dev` on your laptop**, or turn the agent off in your local
dashboard. Otherwise both copies label each email and draft duplicate replies.

## Everyday use

| To | Do |
| --- | --- |
| Ship new code | Push to the connected branch. Railway redeploys automatically |
| See what the agent is doing | The service's **Deploy Logs**. Look for `running for <email>` every 5 minutes |
| Open a shell | Install the CLI, then `railway link` and `railway ssh` |
| Create an admin login for `/admin/` | `railway ssh`, then `python manage.py createsuperuser` |
| Restart | The deployment's menu > **Restart** |

Notes:

- Keep it at one replica. A second one would run its own scheduler.
- A service with a volume has a few seconds of downtime on each deploy. The
  scheduler picks up any missed emails on its next run.
- Back up the database now and then from the volume's **Backups** tab.

## Staying connected in Testing mode

While the Google app is in Testing, Google signs the agent out 7 days after each
connect or reconnect. Two things cover that, with no setup:

- **Day 6 reminder email.** Six days after you last connected, the agent puts an email
  titled "Reconnect Google to keep Inbox Agent running" in your inbox, with a link that
  reconnects you. Reconnecting starts a new 7 days. The email is inserted into your
  mailbox through the agent's own Gmail access, not sent, so it works while that access
  is still valid. Deploy Logs show `[reminder] put a reconnect reminder in ...`.
- **Red dashboard banner.** If Google signs the agent out anyway (you missed the
  reminder, or revoked access), the dashboard shows "Google is disconnected" with a
  **Reconnect Google** button. The agent skips you until you reconnect.

Once the app is **In production** the 7-day limit no longer applies, but the reminder
still arrives once per connect. Ignore it.

## Troubleshooting

- **`Bad Request (400)`:** the host isn't allowed. With a custom domain, add the
  variable `APP_HOST` with that domain.
- **Google says `redirect_uri_mismatch`:** the URI in step 5 must match exactly,
  including `https://` and the trailing slash.
- **The agent stops after a week:** your OAuth app is still in Testing. Reconnect from
  the link in the day 6 reminder email or the dashboard banner, or publish the app
  (step 5) to stop it for good.
- **Deploy fails with `KeyError: 'TOKEN_KEY'`** (or another name): a variable from
  step 3 is missing.
- **Data disappears after a deploy:** the volume isn't mounted at `/data`. See step 2.

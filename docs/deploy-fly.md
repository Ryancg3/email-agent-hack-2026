# Deploy to Fly.io (always on)

This runs the web app and the 5-minute scheduler on one small Fly.io machine, so the
agent keeps working when your laptop is off. The SQLite database lives on a 1 GB
volume that survives deploys and restarts.

**Cost:** about $3 to $4 a month (a `shared-cpu-1x` machine with 512 MB, plus a 1 GB
volume). Fly needs a credit card on file. Anthropic API usage is billed separately,
the same as when you run it locally.

## What gets deployed

| File | What it does |
| --- | --- |
| `Dockerfile` | Python 3.13 image with the dependencies and collected static files |
| `deploy/start.sh` | Runs migrations, then gunicorn and `run_scheduler` side by side. If either stops, the container exits and Fly restarts it |
| `fly.toml` | One always-on machine, HTTPS, and the `agent_data` volume mounted at `/data` |

Settings read from the environment:

| Variable | Where it's set | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `DJANGO_SECRET_KEY`, `TOKEN_KEY` | `fly secrets` | Same as in `.env` |
| `SIGNUP_CLOSED` | `fly secrets` | `1` blocks new sign-ups after you've made your account |
| `DJANGO_DEBUG`, `DATABASE_PATH` | `fly.toml` | Debug off, database on the volume |
| `APP_HOST` | automatic | Defaults to `<app>.fly.dev`. Set it only for a custom domain |

## 1. Install the Fly CLI and log in

```bash
brew install flyctl        # or: curl -L https://fly.io/install.sh | sh
fly auth signup            # or: fly auth login
```

## 2. Create the app

App names are global, so pick your own. Replace `my-email-agent` below and in later steps.

```bash
fly launch --no-deploy --copy-config --name my-email-agent --region fra
```

Pick the region closest to you (`fly platform regions` lists them). If it asks to
tweak settings or add a database, say no. This writes your app name into `fly.toml`.

## 3. Create the volume

```bash
fly volumes create agent_data --size 1 --region fra
```

Fly warns that one volume means no redundancy. That's fine here: say yes. Fly takes
a daily snapshot of the volume and keeps it for 5 days.

## 4. Set the secrets

From the repo folder, copy every value from your `.env` in one go:

```bash
grep -E '^[A-Z_]+=.+' .env | fly secrets import
```

Then check that all five are listed:

```bash
fly secrets list
```

Need fresh keys? `DJANGO_SECRET_KEY`: `python -c "import secrets; print(secrets.token_urlsafe(50))"`.
`TOKEN_KEY`: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
If you ever change `TOKEN_KEY` later, everyone has to reconnect Google.

## 5. Update your Google OAuth client

In [Google Cloud Console](https://console.cloud.google.com/apis/credentials), open your
**Web application** OAuth client and add this to **Authorized redirect URIs**, next to
the localhost ones:

```
https://my-email-agent.fly.dev/google/callback/
```

The trailing slash matters. Save it. It can take a few minutes to apply.

Then go to **Google Auth Platform > Audience** (the OAuth consent screen) and check
the **Publishing status**:

- **Testing:** Google expires refresh tokens after 7 days, so the agent would stop
  every week until you reconnect. Click **Publish app** to move it to **In production**.
- **In production** without verification is fine for your own use. Google shows a
  "Google hasn't verified this app" screen when you connect. Click **Advanced**, then
  **Go to ... (unsafe)**, once.

## 6. Deploy

```bash
fly deploy
fly scale count 1    # exactly one machine: a second one would run its own scheduler and database
```

Watch it start:

```bash
fly logs
```

You should see the migrations, then gunicorn listening on port 8000. Check
`https://my-email-agent.fly.dev/health` returns `{"status": "ok"}`.

## 7. Set up your account

1. Open `https://my-email-agent.fly.dev`, sign up and connect Google.
2. Edit your labels on the **Labels** tab and turn the agent on.
3. Close sign-ups so nobody else can run the agent on your Anthropic key:

   ```bash
   fly secrets set SIGNUP_CLOSED=1
   ```

   Setting a secret restarts the app. Your account stays.

The hosted app starts with an empty database, separate from your local `app.db`, so
you sign up and connect Google once more there. It only handles mail that arrives
after you connect.

Then **stop `make dev` on your laptop**, or turn the agent off in your local
dashboard. Otherwise both copies label each email and draft duplicate replies.

## Everyday use

| To | Run |
| --- | --- |
| Ship new code | `git pull && fly deploy` |
| See what the agent is doing | `fly logs` |
| Open a shell on the machine | `fly ssh console` |
| Restart | `fly apps restart` |
| Pause it (stops billing for the machine, keeps the volume) | `fly scale count 0` |
| Create an admin login for `/admin/` | `fly ssh console -C "python manage.py createsuperuser"` |

## Troubleshooting

- **`Bad Request (400)`:** the host isn't allowed. If you use a custom domain, set
  `fly secrets set APP_HOST=mail.example.com`.
- **Google says `redirect_uri_mismatch`:** the URI in step 5 must match exactly,
  including `https://` and the trailing slash.
- **The agent stops after a week:** your OAuth app is still in Testing. See step 5,
  then reconnect Google from the dashboard.
- **Nothing happens every 5 minutes:** check `fly logs` for `running for <email>`.
  The user needs Google connected and the agent turned on.

# Gmail push: run the agent when an email arrives

Without this, the scheduler checks every inbox every 5 minutes. With it, Gmail tells
the app about a new email within seconds, and the agent runs on the scheduler's next
10-second tick. The 5-minute poll stays on as a fallback: users with a working push
are still polled every 30 minutes, and anyone whose push isn't working is polled every
5 minutes as before.

## How it works

1. For every enabled, connected user the scheduler calls Gmail's `users.watch` on the
   inbox, naming a Google Cloud Pub/Sub topic. A watch lasts 7 days; the scheduler
   renews it daily (retrying hourly if it fails, with the error shown on the dashboard).
2. When the inbox changes, Gmail publishes `{emailAddress, historyId}` to the topic,
   and a push subscription POSTs it to `https://<your host>/gmail/push/?token=<secret>`.
3. The webhook checks the secret, marks that user's connection, and answers 204.
   It does no Gmail or Claude work itself.
4. The scheduler sees the mark and runs the agent as usual. The run finds new
   emails the same way a poll does (newest inbox emails not processed yet), so a lost
   or duplicate notification can't skip or double-process an email.

Inbox changes that aren't new emails (the agent adding labels, you archiving) also
send a notification. Those runs find nothing new and stop after two cheap Gmail calls,
without calling Claude.

## What you need

- The app reachable over public HTTPS (Render or Railway, or a Cloudflare tunnel when
  testing locally), with its host in `ALLOWED_HOSTS`. Render and Railway set this up
  themselves. For a tunnel, set `APP_HOST` in `.env` to the tunnel's hostname and open
  the app through the tunnel URL.
- The Google Cloud project that owns your OAuth client. No new OAuth scope is needed:
  `gmail.modify` already allows `watch`, so users don't reconnect.

## Google Cloud setup (once)

Pick a secret for the webhook first:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

With the gcloud CLI (or do the same in the Cloud Console, steps below):

```bash
PROJECT=your-project-id            # the project with your OAuth client
HOST=your-app.onrender.com         # where the app is reachable
TOKEN=the-secret-from-above

gcloud config set project $PROJECT
gcloud services enable pubsub.googleapis.com

# The topic Gmail publishes to.
gcloud pubsub topics create gmail-push

# Let Gmail publish to it.
gcloud pubsub topics add-iam-policy-binding gmail-push \
  --member=serviceAccount:gmail-api-push@system.gserviceaccount.com \
  --role=roles/pubsub.publisher

# Forward each message to the app.
gcloud pubsub subscriptions create gmail-push-webhook \
  --topic=gmail-push \
  --push-endpoint="https://$HOST/gmail/push/?token=$TOKEN" \
  --ack-deadline=10 \
  --expiration-period=never
```

In the Cloud Console instead:

1. **APIs & Services → Library**: enable **Cloud Pub/Sub API**.
2. **Pub/Sub → Topics → Create topic**: ID `gmail-push`, untick "Add a default
   subscription".
3. Open the topic → **Permissions** (side panel) → **Add principal**:
   `gmail-api-push@system.gserviceaccount.com`, role **Pub/Sub Publisher**.
4. **Pub/Sub → Subscriptions → Create subscription**: ID `gmail-push-webhook`, topic
   `gmail-push`, delivery type **Push**, endpoint
   `https://<your host>/gmail/push/?token=<your secret>`, expiration **Never expire**.

## App settings

Add to the app's environment (`.env` locally, the service's env vars when hosted):

```
GMAIL_PUBSUB_TOPIC=projects/<your-project-id>/topics/gmail-push
GMAIL_PUSH_TOKEN=<your secret>
```

Restart the app. Within a few seconds the scheduler starts the watch, and the
dashboard's Google account card says "New emails: Handled within seconds". If it
shows an error instead, the usual causes are the topic name (it must be the full
`projects/.../topics/...` path) or the missing Publisher grant in step 3.

To check the webhook from outside, a POST with a wrong token should answer 403:

```bash
curl -i -X POST "https://$HOST/gmail/push/?token=wrong"
```

## Turning it off

Remove the two settings and restart: the app goes back to polling every 5 minutes,
and Gmail stops sending within 7 days as the watch expires. Disconnecting Google on the
dashboard stops that user's watch right away. Changing the secret means updating the
subscription's endpoint too (`gcloud pubsub subscriptions modify-push-config
gmail-push-webhook --push-endpoint=...`).

Pub/Sub's free tier (10 GB a month) covers this many times over.

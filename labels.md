# Labels

What each Gmail label is for. The classifier reads this to pick labels and sub-labels.
These are the default labels every new user starts with; each user edits their own copy on the Labels tab.

- One `## <label name>` section per Gmail label. The emoji is optional; names match by letters only.
- `Sub-labels:` says when the label gets sub-labels. `never` is enforced in code; anything else guides the classifier.
- `Cleanup:` (optional) says when a sub-label is done and can be deleted. `never` is enforced in code; anything else guides the cleanup model.
- The rest of the section says what belongs under the label and what doesn't.
- The labels themselves come from Gmail. A Gmail label missing here still works, without a description.

## 💼 Work
Sub-labels: one per project or client
Cleanup: once the project is finished or silent for two months
Your job: colleagues, managers, clients, meetings and projects.
Not code notifications (Coding), not newsletters from work tools (Newsletters).

## 🏠 Personal
Sub-labels: never
Emails written to you by a real person you know: friends, family and personal conversations.
Not anything automated or sent to a big list (Newsletters).

## 💻 Coding
Sub-labels: one per repository or project
Cleanup: once the repository or project has been quiet for two months
GitHub, GitLab and other developer tools: pull requests, code reviews, issues, mentions,
CI and build results, deploys, releases, and dependency or security alerts for your repositories.
Not sign-in codes (Authorisations), not billing for developer tools (Finance),
not product news from developer tools (Newsletters).

## 🔐 Authorisations
Sub-labels: never
Things you act on to get or grant access: sign-in codes, 2FA codes, password resets,
account verification, invitations to join an account, team or project.
Not warnings about your account (Alerts).

## 🔔 Alerts
Sub-labels: never
Security alerts and account notices: new sign-ins, suspicious activity, password or
account changes, service and policy notices from companies you have an account with.
Not job alerts (Newsletters), not codes you need to sign in (Authorisations).

## ✈️ Travel
Sub-labels: one per trip
Cleanup: after the trip is over
Flights, trains, hotels, bookings and itineraries.
Not events in another city, unless the email is about booking the travel.

## 🛍️ Shopping
Sub-labels: never
Orders, shipping, receipts and returns.
Not store marketing (Newsletters), not invoices or bills you have to pay (Finance).

## 📰 Newsletters
Sub-labels: never
Subscribed content, digests, product updates, marketing and job alerts.

## 📅 Events
Sub-labels: one per event
Cleanup: after the event date
Invitations, tickets and schedules for things you attend: meetups, conferences,
talks, concerts, parties.
Not regular work meetings (Work).

## 💳 Finance
Sub-labels: never
Money: invoices and bills to pay, payment confirmations, PayPal and bank emails about
money sent or received, statements, refunds, taxes, rent, contracts and subscription billing.
Not the receipt for an online order (Shopping), not security warnings from your bank (Alerts),
not health insurance (Medical).

## 🩺 Medical
Sub-labels: one per doctor or practice
Cleanup: never
Doctors, dentists, appointments, prescriptions, test results, health insurance and
medical bills.

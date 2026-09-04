# My Block

A tiny web app for lending tools and equipment with neighbours you trust. Real accounts
(name + password, gated by a shared street invite code), photo uploads, and karma points
for people who lend a lot. No monetization, no third-party auth service.

## Stack (all free-tier)

- **Hosting:** [Vercel](https://vercel.com) (Hobby plan — free, deploys Flask natively)
- **Database:** Postgres via [Neon](https://neon.tech), added from Vercel's Storage tab
- **Photo storage:** [Vercel Blob](https://vercel.com/docs/vercel-blob), added the same way

For a group of ~10–30 neighbours this comfortably fits inside every free tier involved —
Vercel Hobby (1M function calls/month, 1GB Blob storage/month included), Neon free tier
(0.5GB storage, 100 compute-hours/month). You will not need a credit card unless you
outgrow this by a lot.

## What it does

- **Real accounts** — sign up with your name and a password, gated behind a shared street
  invite code so randoms can't join. After that, everyone logs in with their own password.
- **List items** — name, category, description, and an actual photo upload (stored in
  Vercel Blob, not just a pasted link).
- **Browse & request** — neighbours browse what's available and tap "Request to borrow."
- **Approve / decline / return** — the owner approves or declines, then marks an item
  returned when it comes back.
- **Karma** — every time an item is marked returned, the *lender* gets +1 karma point.
  Leaderboard badges: Newcomer → Helpful Neighbour → Generous Neighbour → Tool Shed Hero
  → Block Legend.

## Deploying it (step by step)

### 1. Get the code into a Git repo

Vercel deploys from a Git repository (GitHub, GitLab, or Bitbucket). Create a new repo and
push this folder to it:

```bash
cd my-block
git init
git add .
git commit -m "My Block v1"
# create an empty repo on GitHub first, then:
git remote add origin https://github.com/<you>/my-block.git
git push -u origin main
```

### 2. Import the project on Vercel

1. Go to [vercel.com/new](https://vercel.com/new) and import the repo you just pushed.
2. Vercel auto-detects it as a Python/Flask app. Deploy it as-is — it'll be broken until
   you add storage in the next step, and that's fine.

### 3. Add a Postgres database

1. In the project, open the **Storage** tab → **Create Database** → **Neon (Postgres)**.
2. Follow the prompts. Vercel automatically sets a `DATABASE_URL` environment variable
   on your project — the app is already coded to read that exact name, so there's nothing
   else to configure.

### 4. Add Blob storage (for item photos)

1. Same **Storage** tab → **Create Database** → **Blob**.
2. Name it (e.g. "Images"), create it. Vercel automatically sets `BLOB_READ_WRITE_TOKEN`
   on your project — again, the app already reads that name.

### 5. Set the two remaining environment variables

In **Project Settings → Environment Variables**, add:

| Name | Value |
|---|---|
| `MY_BLOCK_INVITE_CODE` | Whatever code you'll share with your street, e.g. `oaktree2026` |
| `MY_BLOCK_SECRET` | Any long random string (used to sign login sessions) — e.g. run `python -c "import secrets; print(secrets.token_hex(32))"` and paste the result |

### 6. Redeploy

Trigger a redeploy (Vercel's dashboard has a **Redeploy** button, or just push a new commit)
so the new environment variables take effect. Your app is now live at the `*.vercel.app`
URL Vercel gives you — share that link and the invite code with your neighbours.

## Local development

```bash
pip install -r requirements.txt
export DATABASE_URL="postgres://...(a Neon connection string, or any local Postgres)"
export MY_BLOCK_INVITE_CODE="testinvite"
export MY_BLOCK_SECRET="dev-secret"
# BLOB_READ_WRITE_TOKEN is optional locally — without it, items save fine but skip the photo
python app.py
```

Tables are created automatically on first request — no separate migration step.

## Deliberately left out of v1

- Notifications (no email/SMS/push — people check "My items" for pending requests)
- Ratings/reviews beyond the karma count
- Multiple "blocks"/groups — one shared invite code = one trust group
- Password reset (small trusted group — for now, whoever manages the deploy can reset a
  password directly in the database if someone forgets)

All easy to add later if the core loop (list → request → approve → return → karma) turns
out to actually get used.

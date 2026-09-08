# HS Property Management — Deployment

Two environments run on one Contabo VPS, isolated from each other.

| | Live | Test |
|---|---|---|
| URL | https://haspm.com (and www) | https://test.haspm.com |
| Database | `data/live/data/hs_property.db` | `data/test/data/hs_property.db` |
| Uploads | `data/live/…` | `data/test/…` |
| Scheduler | runs (`sched-live`) | **off** |
| Outbound email | enabled once SMTP is configured | **hard-disabled** |
| Deploys on | manual approval | every push to `main` |
| Search engines | indexed | `noindex` header |

Server: `161.97.80.66` · app root `/opt/haspm` · Ubuntu 24.04

---

## Layout

```
/opt/haspm/                  git checkout, deployed by deploy.sh
├── docker-compose.yml
├── .env.live  .env.test     secrets, chmod 600, never in git
├── nginx/conf.d/haspm.conf  routing + TLS
├── data/
│   ├── live/{data,static-uploads,doc-uploads}
│   └── test/{data,static-uploads,doc-uploads}
└── backups/{live,test}
```

Containers: `nginx` (80/443) → `web-live` and `web-test` (gunicorn, 3 workers each),
plus `sched-live` (daily jobs) and `certbot` (renewals). Only nginx is published;
the app containers are reachable only on the internal Docker network.

---

## Routine work

**Ship to test** — push to `main`. GitHub Actions deploys and verifies it.

**Promote to live** — Actions → *Deploy to live* → Run workflow, type `deploy`
to confirm. It backs up the live database first, then deploys.

**By hand on the server:**

```bash
cd /opt/haspm
./deploy/deploy.sh test      # test only
./deploy/deploy.sh live      # live only
./deploy/deploy.sh all       # everything, including nginx
REF=some-branch ./deploy/deploy.sh test
```

---

## First-time setup (already done, kept for rebuilds)

```bash
# 1. provision: docker, ufw, fail2ban, dirs, generated SECRET_KEYs
curl -fsSL https://raw.githubusercontent.com/princewizl/haspm/main/deploy/bootstrap-server.sh | bash

# 2. certificates (needs DNS pointing at the box first)
/opt/haspm/deploy/issue-certs.sh

# 3. start everything
/opt/haspm/deploy/deploy.sh all
```

### GitHub secrets

Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `SSH_HOST` | `161.97.80.66` |
| `SSH_USER` | `deploy` |
| `SSH_KEY` | contents of the private key (see below) |

For an approval gate on production, add required reviewers to the `live`
environment under Settings → Environments.

---

## Operations

```bash
cd /opt/haspm

docker compose ps                        # what is running
docker compose logs -f web-live          # live app logs
docker compose logs -f sched-live        # scheduled job logs
docker compose logs -f nginx

./deploy/backup.sh live                  # snapshot db + uploads + config
./deploy/backup.sh test
```

Backups land in `/opt/haspm/backups/<env>/`, 30-day retention. The live
workflow takes one automatically before every production deploy.

### Restore

```bash
cd /opt/haspm
docker compose stop web-live sched-live
gunzip -c backups/live/hs_property-<stamp>.db.gz > data/live/data/hs_property.db
tar xzf backups/live/uploads-<stamp>.tar.gz -C data/live
chown -R 10001:10001 data/live
docker compose start web-live sched-live
```

### Certificates

One certificate covers `haspm.com`, `www.haspm.com` and `test.haspm.com`.
The `certbot` container checks twice daily and renews inside the 30-day
window over the webroot challenge — no downtime, no cron needed.

```bash
docker compose exec certbot certbot certificates    # check expiry
docker compose exec nginx nginx -s reload           # after a manual renewal
```

If a name is ever added or removed, re-run `./deploy/issue-certs.sh`; it
skips any domain that does not resolve to this server.

---

## Editing nginx

`nginx/conf.d` is bind-mounted as a **directory**, not a single file, which
avoids the stale-inode trap where a running container keeps serving the old
config after the file is replaced. Even so:

1. Validate before applying, in a throwaway container:
   ```bash
   docker run --rm -v /opt/haspm/nginx/conf.d:/etc/nginx/conf.d:ro \
     nginx:1.27-alpine nginx -t
   ```
2. A failed `nginx -t` is a hard stop. Do not continue.
3. Apply with `docker compose up -d --force-recreate nginx`.
4. Verify with page **content**, not just status codes — a misrouted host
   still returns 200 while serving the wrong site:
   ```bash
   curl -s https://test.haspm.com/ | grep -o '<title>[^<]*'
   ```

---

## Notes and gotchas

**The scheduler must never run in a web container.** It emails tenants
invoices, overdue reminders and lease-expiry notices. APScheduler runs
in-process, so with 3 gunicorn workers every tenant would get 3 copies of
every mail. `web-live` and `web-test` set `ENABLE_SCHEDULER=0`; the single
`sched-live` container owns the schedule. `scheduler_worker.py` also forces
the flag off before importing the app, so it cannot double-start.

**Test cannot email anyone.** `MAIL_SUPPRESS_SEND=1` blanks `MAIL_SERVER` at
startup, so even configuring SMTP through the staging Settings page sends
nothing.

**SMTP and Paystack credentials** are entered in-app and persist to
`data/<env>/data/*.json`. They are not in git and not in the image, so they
survive rebuilds and must be set once per environment.

**SQLite** suits the current load and keeps operations simple. Back it up
with `deploy/backup.sh` (which uses `.backup`, not `cp` — copying a database
mid-write can restore corrupt). If concurrent writes ever become a
bottleneck, move to Postgres by setting `DATABASE_URL` in the env file.

**Uploads must stay under `static/`.** Property images are served through
Flask's `/static` route, so `data/<env>/static-uploads` mounts at
`/app/static/uploads`. Tenant documents go through `send_file` and mount at
`/app/uploads`.

**Passwords** are hashed with pbkdf2:sha256. Accounts created before hashing
existed verify once against their old plain-text value and are re-hashed on
that login, so nobody is locked out during the transition.

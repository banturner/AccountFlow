# Deploying AccountFlow to Hostinger KVM2

Written against the box as measured on 31 Jul 2026:

```
RAM   7.8 GB total, 1.1 GB used, 6.6 GB available
Swap  0 B                                   ← fix this first
CPU   2 vCPU
Disk  78 GB free of 96 GB
Ports 80 / 443 / 8000 / 5432 / 6379 all free
Also running: kitchen-bot-1, kitchen-bot-2, token-manager,
              portainer, agent-api, backup, ollama (hermes3, 4.7 GB)
```

AccountFlow adds six containers at roughly **0.8–1.0 GB** steady with the
production overlay. That fits — with one caveat you must handle first.

---

## 0. The Ollama collision (do this before anything else)

`hermes3` is a 4.7 GB model. It is currently **unloaded** — the `hermes`
process is holding only ~240 MB — which is why 6.6 GB looks free. When
something invokes it:

```
6.6 GB available  −  ~5.5 GB hermes3 loaded  −  ~1.0 GB AccountFlow  ≈  0.1 GB
```

With **no swap**, that is an OOM kill, and the kernel chooses the victim.

**Add swap.** You have 78 GB of disk; this is the cheapest insurance on the box:

```bash
ssh kvm2 'fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo "/swapfile none swap sw 0 0" >> /etc/fstab && free -h'
```

**Make Ollama let go faster.** Default keep-alive holds the model in RAM for
5 minutes after each call:

```bash
ssh kvm2 'systemctl edit ollama'
```

Add, then `systemctl restart ollama`:

```ini
[Service]
Environment="OLLAMA_KEEP_ALIVE=60s"
```

If hermes3 is not actually earning its keep, `ollama rm hermes3` returns
4.7 GB outright and removes the problem entirely.

---

## 1. Point a domain at the box

Required, not cosmetic. Without HTTPS on a real hostname:

- Google/Microsoft will not accept the OAuth redirect URI for a public host
- the draft-review links go to a clinic owner's phone and must not be plaintext
- `docker-compose.yml` mounts `/etc/letsencrypt`, **which does not exist yet**,
  so nginx will fail to start as shipped

Create a DNS **A record**: `app.yourdomain.com → 156.67.220.241`. Verify:

```bash
dig +short app.yourdomain.com
```

## 2. Firewall

```bash
ssh kvm2 'ufw allow 80/tcp && ufw allow 443/tcp && ufw status'
```

## 3. Certificate

Nothing is listening on 80 yet, so use standalone mode:

```bash
ssh kvm2 'apt-get update && apt-get install -y certbot && certbot certonly --standalone -d app.yourdomain.com --agree-tos -m you@yourdomain.com --non-interactive'
```

Renewal needs port 80 free, so stop nginx around it:

```bash
ssh kvm2 'certbot renew --pre-hook "docker stop accountflow-nginx" --post-hook "docker start accountflow-nginx" --dry-run'
```

If the dry run passes, the systemd timer certbot installs handles renewals.

## 4. Get the code onto the server

**Its own directory and its own Compose project — never merged into
`/opt/bots/docker-compose.yml`.** Compose namespaces containers, volumes and
networks per project, so nothing collides; more importantly, nobody running
`docker compose down` for AccountFlow can take down the kitchen bots, which
have been up nine days healthy.

```bash
ssh kvm2 'mkdir -p /opt/accountflow'
scp -r ./accountflow/. kvm2:/opt/accountflow/
ssh kvm2 'rm -f /opt/accountflow/docker-compose.override.yml'
```

Two details that matter: `./accountflow/.` (not `*`) so the dotfiles —
`.env.example`, `.dockerignore` — actually copy; and the override file is
removed on arrival, because a bare `docker compose up` on the server would
otherwise mount the host tree, `.env` included, over the built image. Once
the GitHub remote exists, prefer `git clone` / `git pull` on the server over
`scp` — the override removal still applies.

## 5. Configure

```bash
ssh kvm2 'cd /opt/accountflow && cp .env.example .env && nano .env'
```

Generate the secrets on the server:

```bash
ssh kvm2 'python3 -c "import secrets; print(\"JWT_SECRET_KEY=\" + secrets.token_urlsafe(64))"'
ssh kvm2 'python3 -c "from cryptography.fernet import Fernet; print(\"FERNET_KEY=\" + Fernet.generate_key().decode())"'
```

Must be right or things fail quietly:

| Variable | Value |
|---|---|
| `APP_BASE_URL` | `https://app.yourdomain.com` — **every review link is built from this** |
| `GOOGLE_REDIRECT_URI` | `https://app.yourdomain.com/api/auth/google/callback`, character-for-character identical to the console |
| `ENVIRONMENT` | `production` — disables `/api/docs` and forces secure cookies |
| `POSTGRES_PASSWORD` / `REDIS_PASSWORD` | fresh, not the examples |
| `DATABASE_URL` | the **runtime** DSN: `postgresql+asyncpg://accountflow_app:<app password>@db:5432/accountflow`. The API and worker connect as `accountflow_app`, a non-owner role that row-level security applies to (`docs/adr/001-row-level-security.md`). Pick a fresh password, different from `POSTGRES_PASSWORD` |
| `MIGRATION_DATABASE_URL` | the **owner** DSN: `postgresql+asyncpg://accountflow:<POSTGRES_PASSWORD>@db:5432/accountflow`. Alembic only. If the two URLs name the same role, `alembic upgrade head` refuses to run — that configuration would silently disable RLS |

**Rotating the app password later.** Migration `010` creates `accountflow_app`
only on its first run. Once it is stamped, changing the password in
`DATABASE_URL` alone leaves the role's real password untouched, and every API
and worker connection then fails authentication — a total outage whose cause
points nowhere near the role. Change the role first, from a shell on the box:

```bash
ssh kvm2
cd /opt/accountflow
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec db psql -U accountflow -d accountflow
```

At the `psql` prompt run `ALTER ROLE accountflow_app PASSWORD '<new password>';`,
then put the same value into `DATABASE_URL` and restart `api` and `worker`.
| `SENDGRID_*` | working, or no owner ever gets a review email |

Also set the server name in `nginx/nginx.conf` and the certificate paths to
`/etc/letsencrypt/live/app.yourdomain.com/`.

## 6. Deploy

```bash
ssh kvm2 'cd /opt/accountflow && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build'
```

```bash
ssh kvm2 'cd /opt/accountflow && docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api alembic upgrade head'
```

The first `alembic upgrade head` also creates the `accountflow_app` role
(migration `010`) from `DATABASE_URL`, so a fresh deploy needs no separate
`CREATE ROLE` step. A **restore** does — see `scripts/backup_db.sh`.

**Upgrading an instance that has already processed mail.** Every Celery task
signature gained a leading `tenant_id` argument (ADR-001), so a message queued
by the old code cannot run on the new worker: it raises `TypeError`, and the
thread it belonged to sits in `processing` until the stuck-thread sweeper
re-dispatches it. Nothing is lost, but the clean order is to drain the queue
before swapping the image — stop `beat` so no new fan-outs are queued, give
in-flight tasks a minute, then purge what is left:

```bash
ssh kvm2 'cd /opt/accountflow && docker compose -f docker-compose.yml -f docker-compose.prod.yml stop beat'
```

```bash
ssh kvm2 'cd /opt/accountflow && docker compose -f docker-compose.yml -f docker-compose.prod.yml exec worker celery -A app.worker.celery_app purge -f'
```

A first deploy has an empty queue and skips this.

## 7. Verify

```bash
ssh kvm2 'cd /opt/accountflow && docker compose -f docker-compose.yml -f docker-compose.prod.yml ps && curl -s localhost:8000/api/health'
```

Then, in order:

- [ ] `curl -sI https://app.yourdomain.com/api/health` → `200`, valid cert
- [ ] `free -h` → still comfortable, swap present
- [ ] `docker stats --no-stream` → nothing pinned at its `mem_limit`
- [ ] **kitchen-bot-1 and kitchen-bot-2 still `Up (healthy)`** — the box's
      existing job matters more than the new one
- [ ] create a tenant (`ONBOARDING_RUNBOOK.md` §1), connect a mailbox, send a
      test email, confirm the "Reply ready" notification arrives and the link
      opens over HTTPS

## 8. Two things that will bite later

**Backups.** The existing `backup` container knows nothing about
`accountflow_postgres_data`. Until it does, pilot data is unprotected.
`scripts/backup_db.sh` does a nightly `pg_dump` with 35-day retention (the
PDPA pack's backup-ageing promise). Install it as part of the first deploy,
not after:

```bash
ssh kvm2 'install -m 750 /opt/accountflow/scripts/backup_db.sh /usr/local/bin/accountflow-backup && (crontab -l 2>/dev/null; echo "15 3 * * * /usr/local/bin/accountflow-backup >> /var/log/accountflow-backup.log 2>&1") | crontab - && /usr/local/bin/accountflow-backup'
```

The last command runs one backup immediately so you see it work. Confirm the
file also lands wherever the weekly GitHub backup goes.

**The monitor bot.** Six unfamiliar containers plus a RAM step-change may set
off KVM2 health alerts. Expect noise on the first day and tune thresholds
rather than ignoring it.

---

## Rolling back

Self-contained by design — nothing here touches `/opt/bots`:

```bash
ssh kvm2 'cd /opt/accountflow && docker compose -f docker-compose.yml -f docker-compose.prod.yml down'
```

Add `-v` only if you also want the database destroyed.

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
scp -r ./accountflow/* kvm2:/opt/accountflow/
```

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
`accountflow_postgres_data`. Until it does, pilot data is unprotected:

```bash
ssh kvm2 'docker exec accountflow-db pg_dump -U accountflow accountflow | gzip > /root/accountflow_$(date +%F).sql.gz'
```

Put that on cron, and confirm it lands wherever the weekly GitHub backup goes.

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

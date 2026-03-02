# MeshHall Database Backups

The database (`meshhall.db`) stores everything: user registry, bulletins,
check-in history, frequency directory, message log, and weather alert cache.
Back it up regularly.

## The backup script

`tools/backup_db.sh` uses SQLite's native online backup API — it creates an
atomic, consistent snapshot **while the bot is running**. You don't need to
stop MeshHall to take a backup.

---

## Ad-hoc backups

**Quick backup with defaults** (saves to `/opt/meshhall/data/backups/`):

```bash
sudo -u meshhall bash /opt/meshhall/tools/backup_db.sh
```

**Backup to a specific directory:**

```bash
sudo -u meshhall bash /opt/meshhall/tools/backup_db.sh --dest /mnt/usb/backups
```

**Backup before a risky change** (e.g. before upgrading):

```bash
sudo -u meshhall bash /opt/meshhall/tools/backup_db.sh \
    --dest /opt/meshhall/data/backups \
    --keep 0   # keep=0 means never prune — good for before-upgrade snapshots
```

**Verify a backup manually:**

```bash
sqlite3 /opt/meshhall/data/backups/meshhall-20260228-140000.db "PRAGMA integrity_check;"
# Should return: ok
```

---

## Scheduled backups with cron

Edit the cron table for the `meshhall` service account:

```bash
sudo crontab -u meshhall -e
```

### Daily backup, keep 30 days

```cron
0 2 * * * bash /opt/meshhall/tools/backup_db.sh \
               --db   /opt/meshhall/data/meshhall.db \
               --dest /opt/meshhall/data/backups \
               --keep 30 \
               >> /opt/meshhall/data/meshhall.log 2>&1
```

### Weekly backup to external storage, keep 12 weeks

```cron
0 3 * * 0 bash /opt/meshhall/tools/backup_db.sh \
               --db   /opt/meshhall/data/meshhall.db \
               --dest /mnt/nas/meshhall-backups \
               --keep 84 \
               >> /opt/meshhall/data/meshhall.log 2>&1
```

### Both: daily local + weekly offsite

```cron
# Daily local backup, keep 30 days
0 2 * * * bash /opt/meshhall/tools/backup_db.sh \
               --dest /opt/meshhall/data/backups --keep 30 \
               >> /opt/meshhall/data/meshhall.log 2>&1

# Weekly backup to USB/NAS, keep 12 weeks
0 3 * * 0 bash /opt/meshhall/tools/backup_db.sh \
               --dest /mnt/usb/meshhall-backups --keep 84 \
               >> /opt/meshhall/data/meshhall.log 2>&1
```

---

## Restoring from backup

Stop the bot, replace the database, restart:

```bash
sudo systemctl stop meshhall meshhall-same

# Replace with your chosen backup
sudo -u meshhall cp /opt/meshhall/data/backups/meshhall-20260228-020000.db \
                    /opt/meshhall/data/meshhall.db

# Verify before starting
sqlite3 /opt/meshhall/data/meshhall.db "PRAGMA integrity_check;"

sudo systemctl start meshhall
```

---

## Options reference

| Flag | Default | Description |
|------|---------|-------------|
| `--db PATH` | `/opt/meshhall/data/meshhall.db` | Source database |
| `--dest DIR` | `/opt/meshhall/data/backups` | Backup destination directory |
| `--keep DAYS` | `30` | Days of backups to keep (`0` = keep all) |

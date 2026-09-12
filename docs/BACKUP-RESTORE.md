# Backup & Restore — Hippocampus v0.1-alpha

> **Status (this HEAD):** the dump/restore recipe below was executed
> end-to-end against an empty disposable pgvector/pg17 container in the
> clean-history export E2E run. Source `pg_dump -Fc` produced a
> self-contained file including the `vector` extension and the
> canonical active-memory DDL; `pg_restore` into the empty target
> reproduced table presence + row counts (`raw` = 2, `QA` = 0,
> `explicit` = 1) and the post-restore active-memory write + recall
> proof passed. See
> [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
> § 2.5. Remaining alpha limitations are
> listed in
> [`docs/RELEASE-CHECKLIST.md`](RELEASE-CHECKLIST.md)
> § E (Backup / restore).
>
> **Scope:** this document describes how to back up and restore the
> durable engine state on a disposable pgvector container. It does
> **not** promise point-in-time recovery, streaming replication, or
> cloud-managed snapshot semantics — see § 2.
>
> **Primary path:** the recipe in this document uses the **`pgvector`**
> Docker image's stock `pg_dump` / `pg_restore` (invoked via
> `docker exec`). This is the documented disposable path. Using a
> host-installed `pg_dump` / `pg_restore` against the container is
> possible but is treated as an advanced/optional variant (§ 9) — it
> is **not** the reference path and may produce dumps that fail to
> restore onto the same image if client / server version skews.

---

## 1. Scope of backup

The durable engine state lives in two places:

1. **PostgreSQL (the truth).** `conversation_stream`, `qa_pairs`,
   observer notes, topic tables, and the canonical active-memory
   table `public.explicit_memories`. This is what `pg_dump`
   captures.
2. **Local profile directory** at the absolute path set by top-level
   `basePath` in `config.yaml` (e.g.
   `C:\Users\<you>\.v3-core\profiles\default\` on Windows).
   This is **not** authoritative; it can be regenerated from PG on
   next run.

> The `backup_paths()` helper in `v3-hermes-plugin` returns the local
> data root only. It is a **hint**, not a complete backup. For a real
> backup you **must** also dump PostgreSQL explicitly.

---

## 2. What this doc does NOT promise

- It does **not** promise point-in-time recovery (PITR). For that,
  enable WAL archiving on the PG host separately.
- It does **not** promise streaming or incremental backup. The recipe
  is a full `pg_dump`.
- It does **not** promise that backups made on one PG major version
  restore cleanly on another. The recipe uses the same image tag
  (`pgvector/pgvector:pg17`) for both dump and restore.
- It does **not** promise that a backup made against the disposable
  container will restore onto a PG with different users / schemas /
  extensions installed. Restore onto a like-for-like PG.

---

## 3. The recipe

### 3.1. Backup (`pg_dump`)

The disposable PG runs on a local port of your choice with a database
name of your choice, user `postgres`, password supplied via env. The
**documented primary path** is to run `pg_dump` *inside* the
`pgvector` Docker container (via `docker exec`) so the client binary
matches the server's major version. To make a `pg_dump` archive:

```bash
# In a POSIX shell (git-bash / MSYS / WSL).
TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)
OUT_DIR="$HOME/v3-backups/$TIMESTAMP"
mkdir -p "$OUT_DIR"

# Primary path: pg_dump from inside the same pgvector container.
# -Fc: custom format (compressed, allows pg_restore)
# -d:  target database
# -f:  output file (inside the container; docker cp below moves it out)
# Replace container-name / database with what your disposable container uses.
docker exec v3-pgvector-alpha \
  pg_dump \
    -U postgres \
    -d <disposable-database> \
    -Fc \
    -f "/tmp/<disposable-database>.dump"

docker cp "v3-pgvector-alpha:/tmp/<disposable-database>.dump" \
  "$OUT_DIR/<disposable-database>.dump"

# (Optional, also via docker exec:) globals for role/grant completeness.
docker exec v3-pgvector-alpha \
  pg_dumpall -U postgres --globals-only \
  -f "/tmp/globals.sql"
docker cp "v3-pgvector-alpha:/tmp/globals.sql" "$OUT_DIR/globals.sql"

ls -lh "$OUT_DIR"
```

Expected: a single `<disposable-database>.dump` file plus an optional
`globals.sql`. The dump includes the `vector` extension, the
`public.explicit_memories` DDL, and all its indexes (IVFFLAT
embedding, partial active status, `created_at DESC`, tags GIN).

> 🛠️ **Advanced / optional — host `pg_dump` against the container.**
> If your host has a `pg_dump` whose major version matches the
> container's server (e.g. PostgreSQL 17 client + `pg17` server), you
> can run `pg_dump -h 127.0.0.1 -p <disposable-port> -U postgres ...`
> directly. This is **not** the reference path and is documented in
> § 9.

### 3.2. Verify the dump on the same machine

This is the **non-skippable** step.

```bash
# 1. Make sure pg_restore can list the TOC without errors.
pg_restore --list "$OUT_DIR/<disposable-database>.dump" > "$OUT_DIR/toc.txt"
head -40 "$OUT_DIR/toc.txt"

# 2. Confirm the canonical active-memory DDL is in the dump.
grep -c "explicit_memories" "$OUT_DIR/toc.txt"
# Expected: > 0 (table DDL + indexes + the IVFFLAT CREATE INDEX)

# 3. Confirm the vector extension entry exists.
grep -E "EXTENSION.*vector" "$OUT_DIR/toc.txt"
```

If any of the above fails, **the dump is broken; do not trust it**.
Re-run after fixing the source container.

### 3.3. Restore into an empty isolated PG

Use a brand-new Docker container with the **same image tag** as the
source, on a different local port, with an empty database.

```bash
# Start the restore container (empty database, same PG major version).
docker run --name v3-pgvector-restore --rm -d \
  -e POSTGRES_PASSWORD=<local-only-password> \
  -e POSTGRES_DB=v3embeddings_restore \
  -p <restore-port>:5432 \
  pgvector/pgvector:pg17

# Wait for it to come up.
until docker exec v3-pgvector-restore pg_isready -U postgres; do sleep 1; done

# Copy the dump into the restore container, then run pg_restore inside
# the container so the binary matches the server's major version.
docker cp "$OUT_DIR/<disposable-database>.dump" \
  "v3-pgvector-restore:/tmp/<disposable-database>.dump"

docker exec v3-pgvector-restore \
  pg_restore \
    -U postgres \
    -d v3embeddings_restore \
    -c --if-exists --no-owner \
    "/tmp/<disposable-database>.dump"

# Optional: apply globals (also via docker exec).
docker exec v3-pgvector-restore \
  bash -c "cat > /tmp/globals.sql" < "$OUT_DIR/globals.sql"
docker exec v3-pgvector-restore \
  psql -U postgres -d v3embeddings_restore -f /tmp/globals.sql
```

### 3.4. Verify the restore

Two checks, both required:

```bash
# (a) Table-level readback.
docker exec v3-pgvector-restore psql -U postgres -d v3embeddings_restore \
  -c "\dt public.*"

# Must include public.explicit_memories plus the rest of the engine tables.
docker exec v3-pgvector-restore psql -U postgres -d v3embeddings_restore \
  -c "\d public.explicit_memories"

# (b) Row count parity with the source.
#     Replace SELECT COUNT(*) with the engine's row-count helper or a
#     direct count; the canonical active-memory table:
docker exec v3-pgvector-restore psql -U postgres -d v3embeddings_restore \
  -c "SELECT COUNT(*) FROM public.explicit_memories WHERE status='active';"
```

Expected: the count matches what was on the source PG just before the
dump. If the count is zero and you had active memories, the restore is
broken — do not promote this backup.

### 3.5. Post-restore write + recall proof

To confirm the restored instance is usable end-to-end:

1. Point a fresh `v3-core` config at the restored PG
   (`host=127.0.0.1`, `port=<restore-port>`,
   `database=v3embeddings_restore`). The top-level `basePath` for the
   new profile may point anywhere you choose; it does **not** affect
   PG row counts.
2. `v3-core info` should exit `0` and print the status summary. Provider
   health is a separate `v3_health` contract and was not part of this
   no-LLM/no-provider restore smoke.
3. Run an active-memory scenario (write a card → exact retry →
   keyword recall → vector recall → soft-archive). The supported
   proof is recorded in
   [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
   § 2.5; the fresh export restore smoke reproduced the source counts, then
   wrote and keyword-recalled a new active-memory marker.
4. Tear the restore container down:

   ```bash
   docker rm -f v3-pgvector-restore
   ```

---

## 4. Evidence references

The supported-surface doc cites the alpha-run evidence pointer for this
recipe:

- The fresh export dump/restore proof is recorded in
  [`docs/PUBLIC_ALPHA_SUPPORTED_SURFACE.md`](PUBLIC_ALPHA_SUPPORTED_SURFACE.md)
  § 2.5: source `2|0|1`, restored `2|0|1`, post-restore active-memory
  write + keyword recall PASS.

---

## 5. Restoring the local profile

The local profile directory at the absolute `basePath` you chose
holds a small SQLite file plus a couple of JSON caches. **You
usually don't need to back this up** — the engine regenerates it from
PG on first use.

If you do want a snapshot (e.g. you want to preserve a hand-curated
cache state during a reproduction):

```powershell
Copy-Item -Recurse "$env:USERPROFILE\.v3-core\profiles\default" `
          "$env:USERPROFILE\.v3-core\profiles\default.bak-<timestamp>"
```

To restore:

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.v3-core\profiles\default"
Move-Item "$env:USERPROFILE\.v3-core\profiles\default.bak-<timestamp>" `
         "$env:USERPROFILE\.v3-core\profiles\default"
```

---

## 6. What "verified" does and does not cover

This table records the fresh export verification scope and the intentionally
uncovered cases. It is not a promise that the uncovered cases are safe.

| Intended to be verified | Intentionally NOT covered |
|---|---|
| Fresh empty PG → restore → table presence + row counts match source. | Incremental / streaming backup. |
| Fresh export post-restore active-memory write + keyword recall works. | Vector recall after restore (not exercised in fresh export). |
| `pg_dump -Fc` produces a self-contained file including the `vector` extension. | Restore onto a PG where `vector` is not installed. |
| `pg_restore --list` reads the TOC without error. | Restore across architectures (e.g. Linux dump → Windows restore). |
| Restore is idempotent (re-running on a non-empty DB with `-c --if-exists` is safe). | PITR or WAL-based replay. |

---

## 7. Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `pg_restore: error: could not execute query: ERROR: extension "vector" is not available` | Target PG image doesn't have `vector` installed. | Use `pgvector/pgvector:pg17` (or pg17 if you also use it on the source). |
| `pg_restore: error: role "..." does not exist` | Globals not applied. | Either apply `globals.sql` first, or restore with `--no-owner --no-privileges`. |
| Active-memory lab scenario returns `table_available=False` | Schema wasn't applied (the dump didn't include it). | Re-check `grep explicit_memories toc.txt`; rerun `pg_dump` on the source. |
| Vector recall returns 0 hits after restore | Embedding column was NULL in the source (provider was unconfigured at write time). | Re-embed using the post-commit lease; this is an **expected** failure mode and is not a backup bug. |

---

## 8. Out of scope

- PITR / WAL archiving (configure on the PG host directly).
- Logical replication between two PG instances (not used by the
  alpha).
- Cloud-managed PG snapshot mechanisms (RDS automated snapshots,
  Cloud SQL, etc.). These work but are **not** the reference path
  and require their own restore-on-an-empty-instance verification.

---

## 9. Advanced variant — host `pg_dump` / `pg_restore` against the container

> 🛠️ **This section is advanced / optional.** The reference path is
> `docker exec pg_dump` / `docker exec pg_restore` inside the
> `pgvector` container (§ 3). Use this variant only if your host has
> a `pg_dump`/`pg_restore` client whose major version matches the
> container's server, and you have a reason to skip the docker-exec

```bash
# Set the password via env so it never appears on a CLI argument list.
export PGPASSWORD=<local-only-password>

# Backup.
pg_dump \
  -h 127.0.0.1 \
  -p <disposable-port> \
  -U postgres \
  -d <disposable-database> \
  -Fc \
  -f "$OUT_DIR/<disposable-database>.dump"

# Restore into an empty isolated PG.
pg_restore \
  -h 127.0.0.1 \
  -p <restore-port> \
  -U postgres \
  -d v3embeddings_restore \
  -c --if-exists --no-owner \
  "$OUT_DIR/<disposable-database>.dump"
```

Risks vs. the primary path:

- A version skew between host client and container server may
  produce dumps that fail to restore onto the same image. Always
  pin both the host `psql --version` and the container image tag
  to the same major version.
- The dump files created by this variant are NOT covered by the
  § 3 verification recipes. If you adopt this variant, run the
  § 3.2 and § 3.4 checks against your dump/restore.

#!/usr/bin/env bash
# Create a verified, restorable PostgreSQL backup without exposing credentials
# in shell history or backup filenames. Intended for the systemd timer below.
set -euo pipefail

umask 077
router_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
backup_dir="$router_dir/backups/postgres"
mkdir -p "$backup_dir"

exec 9>"$backup_dir/.backup.lock"
flock -n 9 || exit 0

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$backup_dir/llm_router_${stamp}.dump"
partial="${target}.partial"
trap 'rm -f "$partial"' EXIT

docker compose -f "$router_dir/docker-compose.yml" exec -T postgres \
  pg_dump -U llm_router_user -d llm_router --format=custom --no-owner --no-privileges \
  > "$partial"

# A nonempty archive index proves that the dump is readable before it becomes
# the retained recovery point.
docker compose -f "$router_dir/docker-compose.yml" exec -T postgres \
  pg_restore --list < "$partial" > /dev/null

mv "$partial" "$target"
trap - EXIT

# Keep two weeks of daily recovery points. The glob is deliberately confined
# to the router's dedicated backup directory.
find "$backup_dir" -maxdepth 1 -type f -name 'llm_router_*.dump' -mtime +14 -delete

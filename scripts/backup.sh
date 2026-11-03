#!/usr/bin/env bash
# Usage: bash scripts/backup.sh /secure/backups age1PUBLIC_RECIPIENT
# Stops application writers briefly for a consistent DB + file backup; restarts them.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
out_dir=${1:?Usage: backup.sh OUTPUT_DIRECTORY AGE_PUBLIC_RECIPIENT}
recipient=${2:?Supply an age public recipient}
command -v age >/dev/null || { echo 'Install age first (sudo apt install age).' >&2; exit 1; }
[[ -f .env ]] || { echo '.env is missing' >&2; exit 1; }
mkdir -p "$out_dir"
out_dir=$(cd "$out_dir" && pwd)
stage=$(mktemp -d)
restart_services=()
while IFS= read -r service; do
  case "$service" in bot|worker_chat|worker_service|worker_notifications|scheduler|app) restart_services+=("$service");; esac
done < <(docker compose ps --status running --services)
cleanup() {
  rc=$?
  if (( ${#restart_services[@]} )); then
    if ! docker compose start "${restart_services[@]}" >/dev/null; then
      echo 'Backup finished, but restarting application processes failed. Check docker compose ps.' >&2
      rc=1
    fi
  fi
  rm -rf -- "$stage"
  exit "$rc"
}
trap cleanup EXIT
if (( ${#restart_services[@]} )); then docker compose stop "${restart_services[@]}"; fi
docker compose exec -T postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$stage/db.dump"
docker compose run --rm -T --no-deps --entrypoint tar app -C /data -czf - media > "$stage/media.tar.gz"
cp .env "$stage/.env"
printf 'Family Assistant backup\nUTC: %s\nFormat: 1\n' "$(date -u +%FT%TZ)" > "$stage/BACKUP.txt"
target="$out_dir/family-assistant-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.age"
tar -C "$stage" -czf - . | age -r "$recipient" > "$target.partial"
mv "$target.partial" "$target"
echo "Encrypted backup: $target"

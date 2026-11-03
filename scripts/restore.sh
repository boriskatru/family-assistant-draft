#!/usr/bin/env bash
# Restore to a NEW instance with no existing .env or data volumes.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
archive=${1:?Usage: restore.sh BACKUP.age AGE_IDENTITY --confirm-new-instance}
identity=${2:?Supply the age identity key file}
[[ ${3:-} == --confirm-new-instance ]] || { echo 'This restores data. Add --confirm-new-instance in a new project directory.' >&2; exit 1; }
[[ ! -e .env ]] || { echo 'Refusing to overwrite existing .env. Restore in a new directory/host.' >&2; exit 1; }
command -v age >/dev/null || { echo 'Install age first.' >&2; exit 1; }
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
age -d -i "$identity" "$archive" > "$stage/backup.tar.gz"
python3 - "$stage" <<'PY'
import pathlib, sys, tarfile
stage = pathlib.Path(sys.argv[1])
allowed = {'db.dump', 'media.tar.gz', '.env', 'BACKUP.txt'}
with tarfile.open(stage/'backup.tar.gz', 'r:gz') as archive:
    members = archive.getmembers()
    for member in members:
        name = member.name.removeprefix('./')
        if name in ('', '.') and member.isdir():
            continue
        if name not in allowed or not member.isfile():
            raise SystemExit('Unexpected backup archive member')
    for member in members:
        name = member.name.removeprefix('./')
        if name in allowed:
            with archive.extractfile(member) as src, (stage/name).open('wb') as dst:
                dst.write(src.read())
    if not all((stage/n).is_file() for n in allowed):
        raise SystemExit('Incomplete backup')
with tarfile.open(stage/'media.tar.gz', 'r:gz') as media:
    for member in media.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'media' or not (member.isfile() or member.isdir()):
            raise SystemExit('Unsafe media archive member')
PY
project=$(python3 - "$stage/.env" <<'PY'
import pathlib, re, sys
values=dict(line.split('=',1) for line in pathlib.Path(sys.argv[1]).read_text().splitlines() if '=' in line and not line.lstrip().startswith('#'))
name=values.get('COMPOSE_PROJECT_NAME','family-assistant')
if not re.fullmatch('[a-z0-9][a-z0-9_-]*',name):
    raise SystemExit('Invalid Compose project name')
print(name)
PY
)
# Ensure volume checks and Compose target the same project, even if the caller
# has an unrelated COMPOSE_PROJECT_NAME in the shell environment.
export COMPOSE_PROJECT_NAME="$project"
for suffix in postgres_data media_data; do
  if docker volume inspect "${project}_${suffix}" >/dev/null 2>&1; then
    echo "Refusing to overwrite existing volume ${project}_${suffix}. Use a new host." >&2
    exit 1
  fi
done
cp "$stage/.env" .env
chmod 600 .env
docker compose build
docker compose up -d --wait postgres
docker compose exec -T postgres sh -c 'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' < "$stage/db.dump"
docker compose run --rm -T --no-deps --entrypoint tar app -C /data -xzf - < "$stage/media.tar.gz"
docker compose up -d
echo 'Restored. For Google HTTPS also run: docker compose --profile web up -d'
echo 'Check the PUBLIC_HOST DNS record before enabling the web profile.'

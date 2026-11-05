#!/usr/bin/env python3
"""Create the VPS-only empty profile without replacing existing content."""
import os
from pathlib import Path


def initialise(project_dir: Path) -> tuple[Path, bool]:
    directory = project_dir / 'private'
    directory.mkdir(mode=0o750, exist_ok=True)
    path = directory / 'personal_context.md'
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    except FileExistsError:
        created = False
    else:
        os.close(descriptor)
        created = True
    # Dockerfile runs the application as UID:GID 10001:10001. The host owner
    # remains root and its editor can write; the container gets read access.
    os.chown(directory, 0, 10001)
    os.chmod(directory, 0o750)
    os.chown(path, 0, 10001)
    os.chmod(path, 0o640)
    return path, created


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit('Запустите: sudo python3 scripts/init_personal_context.py')
    path, created = initialise(Path(__file__).resolve().parent.parent)
    print(('Создан пустой файл: ' if created else 'Существующий файл сохранён: ') + str(path))


if __name__ == '__main__':
    main()

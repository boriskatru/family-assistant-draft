#!/usr/bin/env python3
"""Generate local deployment secrets without printing them; never overwrite .env."""
import argparse
import base64
import os
from pathlib import Path
import re
import secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.env'))
    args = parser.parse_args()
    template = Path(__file__).resolve().parents[1] / '.env.example'
    text = template.read_text(encoding='utf-8')
    replacements = {
        'DJANGO_SECRET_KEY': secrets.token_urlsafe(64),
        'TOKEN_ENCRYPTION_KEY': base64.urlsafe_b64encode(os.urandom(32)).decode('ascii'),
        'POSTGRES_PASSWORD': secrets.token_hex(32),
        'TELEGRAM_WEBHOOK_SECRET': secrets.token_urlsafe(32),
    }
    for key, value in replacements.items():
        text = re.sub(rf'(?m)^{re.escape(key)}=.*$', f'{key}={value}', text)
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error(f'{args.output} already exists; edit it, do not regenerate secrets')
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write(text)
    print(f'Created {args.output}. Fill Telegram token and IDs; optional Google/LLM settings.')


if __name__ == '__main__':
    main()

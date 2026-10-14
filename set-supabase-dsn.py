"""Write SUPABASE_DB_URL into .env, correctly percent-encoded.

Run it, paste the password when prompted (it is not echoed and never leaves this
machine), and it writes the one line gitscout needs:

    python set-supabase-dsn.py

Supabase's own dialog warns that a Postgres connection string needs the password
percent-encoded. Doing it by hand is where this usually goes wrong: a generated
password containing '@', '/', ':' or '#' silently produces a URI that parses into
the wrong host.
"""
from __future__ import annotations

import getpass
import pathlib
import re
from urllib.parse import quote

PROJECT_REF = "nqnocdkwdnvczxuvhcvr"
DEFAULT_HOST = "aws-0-us-west-1.pooler.supabase.com"
ENV = pathlib.Path(__file__).with_name(".env")


def main() -> None:
    host = input(f"Pooler host [{DEFAULT_HOST}]: ").strip() or DEFAULT_HOST
    port = input("Port [6543]: ").strip() or "6543"
    password = getpass.getpass("Database password (not shown): ")
    if not password:
        raise SystemExit("no password given, nothing written")

    # safe="" so every reserved character is encoded, '@' and '/' included.
    dsn = (
        f"postgresql://postgres.{PROJECT_REF}:{quote(password, safe='')}"
        f"@{host}:{port}/postgres"
    )

    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    line = f"SUPABASE_DB_URL={dsn}"
    if re.search(r"^SUPABASE_DB_URL=", text, flags=re.M):
        text = re.sub(r"^SUPABASE_DB_URL=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text.strip() else "") + line + "\n"
    ENV.write_text(text, encoding="utf-8")

    encoded = quote(password, safe="") != password
    print(f"\nWrote SUPABASE_DB_URL to {ENV}")
    print(f"  host     {host}:{port}")
    print(f"  password {len(password)} chars, " + ("percent-encoded" if encoded else "no encoding needed"))
    print("\nNow tell Claude 'done'.")


if __name__ == "__main__":
    main()

"""Run the WHOLE project check: lint, type-check, every unit and database test.

Reads the superuser url the project already keeps in backend/.env (BIDREADY_LOCAL_DATABASE_URL),
creates `bidready_test` beside the working database if it is missing, points the suite at it
and runs pytest. The working database is never touched.
"""
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BACKEND = Path(r"D:\rfpproject\backend")
OUT = Path(r"D:\rfpproject\review\r24_full_checks_output.txt")
TEST_DB = "bidready_test"

url = None
for line in (BACKEND / ".env").read_text(encoding="utf-8").splitlines():
    if line.startswith("BIDREADY_LOCAL_DATABASE_URL="):
        url = line.split("=", 1)[1].strip()
if not url:
    sys.exit("BIDREADY_LOCAL_DATABASE_URL is not set in backend/.env")

parts = urlsplit(url)
admin_url = urlunsplit(parts._replace(path="/postgres")).replace("postgresql+psycopg", "postgresql")
test_url = urlunsplit(parts._replace(path="/" + TEST_DB)).replace("postgresql+psycopg", "postgresql")

import psycopg

with psycopg.connect(admin_url, autocommit=True) as conn:
    exists = conn.execute("select 1 from pg_database where datname = %s", (TEST_DB,)).fetchone()
    if not exists:
        conn.execute(f'create database "{TEST_DB}"')
        print("created database", TEST_DB)
    else:
        print("database", TEST_DB, "already exists")

env = dict(os.environ, BIDREADY_TEST_DATABASE_URL=test_url)
steps = [
    ("install the declared development dependencies", [sys.executable, "-m", "pip", "install", "-q", "-e", ".[dev]"]),
    ("ruff check app tests alembic", [sys.executable, "-m", "ruff", "check", "app", "tests", "alembic"]),
    ("mypy app", [sys.executable, "-m", "mypy", "app"]),
    ("pytest (all unit and database tests)", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short", "-rs", "tests"]),
]
summary = []
with OUT.open("w", encoding="utf-8") as fh:
    for name, cmd in steps:
        fh.write(f"\n===== {name} =====\n")
        fh.flush()
        result = subprocess.run(cmd, cwd=BACKEND, env=env, stdout=fh, stderr=subprocess.STDOUT)
        summary.append((name, result.returncode))
    fh.write("\n===== SUMMARY =====\n")
    for name, code in summary:
        fh.write(f"{'PASS' if code == 0 else 'FAIL'}  {name}  (exit {code})\n")
text = OUT.read_text(encoding="utf-8")
print(text[-3000:])

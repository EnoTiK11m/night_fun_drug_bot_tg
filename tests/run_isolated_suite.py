"""Run the entire unittest suite without letting default paths touch the user DB."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix='not_rules_suite_') as directory:
        env = dict(os.environ, DB_PATH=str(Path(directory) / 'isolated.db'))
        initialized = subprocess.run(
            [sys.executable, '-c', 'import asyncio, app.storage.database as database; asyncio.run(database.init_db())'],
            cwd=root, env=env, check=False,
        )
        if initialized.returncode:
            return initialized.returncode
        return subprocess.run(
            [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
            cwd=root, env=env, check=False,
        ).returncode


if __name__ == '__main__':
    raise SystemExit(main())

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()

from config import DB_PATH  # noqa: E402


def backup_database(source_path: Path, output_dir: Path) -> Path:
    if not source_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = output_dir / f"{source_path.stem}_{timestamp}.db"

    with sqlite3.connect(source_path) as source:
        with sqlite3.connect(backup_path) as target:
            source.backup(target)

    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a consistent SQLite backup.")
    parser.add_argument("--db", default=DB_PATH, help="Source SQLite database path.")
    parser.add_argument(
        "--output-dir",
        default="backups",
        help="Directory where backup files are written.",
    )
    args = parser.parse_args()

    backup_path = backup_database(Path(args.db), Path(args.output_dir))
    print(f"Backup written: {backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

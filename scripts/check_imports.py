"""Import every application module without starting polling or opening the database."""
import importlib
from pathlib import Path
import pkgutil
import sys


def main():
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    import app
    names = sorted(item.name for item in pkgutil.walk_packages(app.__path__, app.__name__ + '.'))
    for name in names:
        importlib.import_module(name)
    print(f'Import smoke: {len(names)} modules OK; polling was not started')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

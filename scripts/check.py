#!/usr/bin/env python3
"""The one check for boring-pirates: a fresh environment from the hash lock, then the tests.

    python scripts/check.py --common-wheels /path/to/boring-pond-common/dist [--python /usr/bin/python3.12]

Installs the locked dependencies (--require-hashes) into a new virtual
environment, then boring-pirates from a copy of its tracked files, and runs the
tests (with optional extra checks in a local pond/checks folder when that directory
exists; a published copy has none) and the scoped strict mypy gate there.
Exit status 0 means every step passed.
"""
import sys
import tempfile
from pathlib import Path

from _develop import load

_, develop = load()

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ["pytest==9.1.1", "mypy==1.18.2"]


def main():
    args = develop.arguments(__doc__).parse_args()
    with tempfile.TemporaryDirectory(prefix="pirates-check-") as tmp:
        tmp = Path(tmp)
        python = develop.environment(develop.uv(), args.python, tmp / "venv", [ROOT / "requirements.lock"],
                                     args.common_wheels, [develop.package(ROOT, tmp / "app")],
                                     ROOT / "scripts/build-constraints.txt",
                                     [*TOOLS, *map(str, sorted(args.common_wheels.glob("boring_deploy-*.whl")))])
        suites = ["tests"]
        if (ROOT / "pond/checks").is_dir():
            suites.append("pond/checks")
        else:
            print("pond/checks absent: skipped")
        develop.run(python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *suites, cwd=ROOT)
        develop.run(python, "-m", "mypy", cwd=ROOT)
    print("all boring-pirates checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

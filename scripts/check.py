#!/usr/bin/env python3
"""The one check for boring-pirates: a fresh environment from the hash lock, then the tests.

    python scripts/check.py --common-wheels /path/to/boring-pond-common/dist [--python /usr/bin/python3.12]

Installs the locked dependencies (--require-hashes) into a new virtual
environment, with the test tools of requirements-dev.txt, then boring-pirates from
a copy of its tracked files, and runs the tests and the scoped strict mypy gate there.
Exit status 0 means every step passed.
"""
import sys
import tempfile
from pathlib import Path

from _develop import load

_, develop = load()

ROOT = Path(__file__).resolve().parents[1]


def main():
    args = develop.arguments(__doc__).parse_args()
    with tempfile.TemporaryDirectory(prefix="pirates-check-") as tmp:
        tmp = Path(tmp)
        python = develop.environment(develop.uv(), args.python, tmp / "venv", [ROOT / "requirements.lock"],
                                     args.common_wheels, [develop.package(ROOT, tmp / "app")],
                                     ROOT / "scripts/build-constraints.txt",
                                     ["-r", str(ROOT / "requirements-dev.txt"),
                                      *map(str, sorted(args.common_wheels.glob("boring_deploy-*.whl")))])
        develop.run(python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests", cwd=ROOT)
        develop.run(python, "-m", "mypy", cwd=ROOT)
    print("all boring-pirates checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

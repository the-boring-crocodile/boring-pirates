#!/usr/bin/env python3
"""Build the Pirates release artifact for one target Python from a clean checkout.

    python scripts/build.py --common-wheels /path/to/boring-pond-common/dist --out NEW_DIR [--python-version 3.14]

OUT/bundle/ is the offline wheel bundle: the boring-pirates wheel, every locked
dependency wheel for the target interpreter (linux x86_64), a requirements.lock
that adds the Pirate wheel's own hash, SHA256SUMS and MANIFEST.json. Install it
with

    python -m pip install --no-index --only-binary=:all: --require-hashes \\
        --find-links BUNDLE -r BUNDLE/requirements.lock

OUT/boring-pirates-<version>-linux-x86_64.tar.gz is the release (bundle,
standalone CPython, deploy; see boring_deploy) that `sudo <unpacked>/deploy`
installs into /opt/boring-pirates.
"""
import hashlib
import sys
from pathlib import Path

from _develop import load

artifact, develop = load()

ROOT = Path(__file__).resolve().parent.parent


def main():
    args = develop.arguments(__doc__, build=True).parse_args()
    develop.clean_tree(ROOT, args.allow_dirty)
    top = develop.new_directory(args.out)
    bundle = top / "bundle"
    bundle.mkdir()
    develop.wheelhouse(ROOT, bundle, sources=["."], lock_file=ROOT / "requirements.lock",
                       common_wheels=args.common_wheels, python_version=args.python_version,
                       constraints=ROOT / "scripts/build-constraints.txt")
    (pirate,) = bundle.glob("boring_pirates-*.whl")
    version = pirate.name.split("-")[1]
    lock = (ROOT / "requirements.lock").read_text()
    lock += f"boring-pirates=={version} \\\n    --hash=sha256:{hashlib.sha256(pirate.read_bytes()).hexdigest()}\n"
    (bundle / "requirements.lock").write_text(lock)
    wheels = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(bundle.glob("*.whl"))}
    develop.record(ROOT, bundle, wheels, bundle / "requirements.lock", args.python_version)
    print(f"{len(wheels)} wheels in {bundle}")
    python = artifact.standalone_python(develop.uv(), develop.PYTHONS[args.python_version], top / "python.tar.gz")
    files = {f"wheels/{p.name}": p for p in sorted(bundle.iterdir()) if p.suffix == ".whl" or p.name == "requirements.lock"}
    files["python.tar.gz"] = python
    files.update({name: ROOT / name for name in ("LICENSE", "COPYING.md")})
    files.update({f"install/{p.name}": p for p in sorted((ROOT / "deploy" / "install").iterdir())})
    release = {"component": "boring-pirates", "version": version, "runtime": "venv",
               "default_home": "/opt/boring-pirates", "python": "python.tar.gz",
               "commit": develop.git(ROOT, "rev-parse", "HEAD").strip()}
    archive = artifact.assemble(top, release, ROOT / "deploy" / "steps.py", files,
                                install=ROOT / "deploy" / "install.py")
    print(f"{archive} {hashlib.sha256(archive.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

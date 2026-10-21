"""A flavour's file roots, served by the worker kit's shared file tools."""
from __future__ import annotations
import os
import stat
from pathlib import Path
from boring_worker_kit.files import FileTools
from boring_worker_kit.file_policy import FilePolicy

class CheckoutFiles:
    """The flavour's policy (parse_profiles always sets one) over the checkout's locks."""

    def __init__(self, root: Path, policy: FilePolicy, workdir: Path | None = None) -> None:
        selected = root if workdir is None else workdir
        info = selected.stat()
        if selected.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("checkout ownership or write permissions are unsafe")
        self.tools = FileTools(policy, root/".pirate-locks", reserved_prefixes=(".pirate-",))

    def close(self) -> None:
        self.tools.pool.shutdown(wait=True)

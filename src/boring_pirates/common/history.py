"""One append-only JSONL transcript per run, named by the worker's job_id."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .limits import MAX_HISTORY_RECORD

JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
TRANSCRIPT = re.compile(r"[0-9a-f]{32}\.jsonl\Z")


class FileHistory:
    def __init__(self, directory: Path, job_id: str, flavour: str, client_uid: int, provider: str, model: str):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ValueError("job_id must be 32 lowercase hex characters")
        self.path = directory / (job_id + ".jsonl")
        self.header = {"job_id": job_id, "flavour": flavour, "client_uid": client_uid,
                       "provider": provider, "model": model}
        # O_EXCL: a job_id names exactly one run; an existing file is never reused.
        self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        self.sequence = 0

    def append(self, kind: str, data: dict) -> None:
        record = dict(version=2, sequence=self.sequence, **self.header,
                      time=datetime.now(timezone.utc).isoformat(), kind=kind, data=data)
        encoded = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        if len(encoded) > MAX_HISTORY_RECORD:
            raise ValueError("history record too large")
        # A short write is explicit corruption; the file is then inspectable, never replayed.
        if os.write(self.fd, encoded) != len(encoded):
            raise OSError("short history write")
        os.fsync(self.fd)
        self.sequence += 1

    def close(self) -> None:
        os.close(self.fd)


def prune(directory: Path, retention_seconds: int) -> list[str]:
    """Delete transcripts not written for retention_seconds; other files stay."""
    cutoff = time.time() - retention_seconds
    removed = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if (TRANSCRIPT.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff):
                os.unlink(entry.path)
                removed.append(entry.name)
    return removed

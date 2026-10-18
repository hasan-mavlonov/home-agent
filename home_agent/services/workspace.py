"""The agent's file area. Every path is resolved and confined to the workspace root."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from pathlib import Path

TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".tsv", ".yaml", ".yml", ".html", ".xml", ".log"}
WRITE_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".tsv"}
UPLOAD_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".png", ".jpg", ".jpeg", ".docx"}
MAX_WRITE_BYTES = 1_000_000
MAX_UPLOAD_BYTES = 15_000_000


class WorkspaceError(Exception):
    pass


class Workspace:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.root = root.resolve()

    def resolve(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative.strip():
            raise WorkspaceError("A file path is required")
        relative = relative.strip().replace("\\", "/")
        if "\x00" in relative or relative.startswith("/") or re.match(r"^[A-Za-z]:", relative) or relative.startswith("~"):
            raise WorkspaceError("Use a path relative to the workspace, like notes/plan.md")
        if any(part in ("..",) for part in relative.split("/")):
            raise WorkspaceError("Paths may not contain '..'")
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root):
            raise WorkspaceError("That path is outside the workspace")
        if any(part.startswith(".") for part in candidate.relative_to(self.root).parts):
            raise WorkspaceError("Hidden files are not accessible")
        return candidate

    def list(self) -> list[dict]:
        out = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(self.root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            stat = path.stat()
            out.append({"path": rel.as_posix(), "size": stat.st_size, "modified": int(stat.st_mtime)})
        return out

    def read_text(self, relative: str, start: int = 0, max_chars: int = 20000) -> tuple[str, int]:
        path = self.resolve(relative)
        if not path.is_file():
            raise WorkspaceError(f"No such file: {relative}")
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            from ..net.extract import pdf_to_text

            _, text = pdf_to_text(path.read_bytes())
        elif suffix in TEXT_EXTENSIONS:
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            raise WorkspaceError(f"Cannot read {suffix or 'this'} files as text")
        return text[start : start + max_chars], len(text)

    def write_text(self, relative: str, content: str) -> dict:
        path = self.resolve(relative)
        if path.suffix.lower() not in WRITE_EXTENSIONS:
            raise WorkspaceError(f"Only {', '.join(sorted(WRITE_EXTENSIONS))} files can be written")
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise WorkspaceError("File is larger than 1 MB")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.resolve().is_relative_to(self.root):
            raise WorkspaceError("That path is outside the workspace")
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return {"path": path.relative_to(self.root).as_posix(), "size": len(data)}

    def save_upload(self, filename: str, data: bytes) -> dict:
        name = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(filename or "upload").name).strip(" .") or "upload"
        if Path(name).suffix.lower() not in UPLOAD_EXTENSIONS:
            raise WorkspaceError(f"Allowed upload types: {', '.join(sorted(UPLOAD_EXTENSIONS))}")
        if len(data) > MAX_UPLOAD_BYTES:
            raise WorkspaceError("Uploads are limited to 15 MB")
        target = self.resolve(f"uploads/{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        stem, suffix, counter = target.stem, target.suffix, 1
        while target.exists():
            target = target.with_name(f"{stem}-{counter}{suffix}")
            counter += 1
        target.write_bytes(data)
        return {"path": target.relative_to(self.root).as_posix(), "size": len(data)}

    def read_bytes(self, relative: str) -> bytes:
        path = self.resolve(relative)
        if not path.is_file():
            raise WorkspaceError(f"No such file: {relative}")
        return path.read_bytes()

    def file_meta(self, relative: str) -> dict:
        data = self.read_bytes(relative)
        mime = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        return {"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mime": mime}

    def delete(self, relative: str) -> bool:
        path = self.resolve(relative)
        if path.is_file():
            path.unlink()
            return True
        return False

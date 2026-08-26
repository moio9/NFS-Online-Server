#!/usr/bin/env python3
"""Create a deterministic sanitized source tar.xz and SHA-256 sidecar."""

from __future__ import annotations

import argparse
import hashlib
import io
import lzma
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def parse_version(path: Path) -> str:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    try:
        return values["package"]
    except KeyError as exc:
        raise ValueError(f"{path} has no package= version") from exc


def parse_args() -> argparse.Namespace:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--output-dir", type=Path, default=default_root / "dist")
    parser.add_argument("--name", default="NFS-Online-Server")
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        default=int(os.environ.get("SOURCE_DATE_EPOCH", "0")),
        help="normalized archive timestamp (default: SOURCE_DATE_EPOCH or 0)",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def included_files(root: Path, output_dir: Path) -> list[Path]:
    result: list[Path] = []
    output_dir = output_dir.resolve()
    for path in root.rglob("*"):
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            raise ValueError(f"symbolic link is not allowed: {path.relative_to(root)}")
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved == output_dir or output_dir in resolved.parents:
            continue
        if path.name == "MANIFEST.sha256":
            continue
        result.append(path)
    return sorted(result, key=lambda item: item.relative_to(root).as_posix())


def normalized_info(name: str, *, mode: int, size: int, mtime: int, is_dir: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = mtime
    info.mode = mode
    if is_dir:
        info.type = tarfile.DIRTYPE
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        info.size = size
    return info


def add_directory_tree(archive: tarfile.TarFile, root_name: str, files: list[Path], root: Path, mtime: int) -> None:
    directories: set[PurePosixPath] = {PurePosixPath(root_name)}
    for path in files:
        relative = PurePosixPath(path.relative_to(root).as_posix())
        parent = relative.parent
        while str(parent) != ".":
            directories.add(PurePosixPath(root_name) / parent)
            parent = parent.parent
    for directory in sorted(directories, key=lambda item: (len(item.parts), item.as_posix())):
        archive.addfile(normalized_info(directory.as_posix(), mode=0o755, size=0, mtime=mtime, is_dir=True))


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    output_dir = args.output_dir.resolve()
    checker = root / "tools/check_public_tree.py"
    if not checker.is_file():
        print("error: tools/check_public_tree.py is missing", file=sys.stderr)
        return 2

    result = subprocess.run([sys.executable, str(checker), str(root)], check=False)
    if result.returncode:
        return result.returncode

    version = parse_version(root / "VERSION")
    root_name = f"{args.name}-{version}-source"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{root_name}.tar.xz"
    sidecar = output.with_suffix(output.suffix + ".sha256")
    files = included_files(root, output_dir)

    manifest_lines: list[str] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        manifest_lines.append(f"{sha256_file(path)}  {relative}\n")
    manifest = "".join(manifest_lines).encode("utf-8")

    with tempfile.NamedTemporaryFile(prefix=output.name + ".", dir=output_dir, delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        with lzma.open(temp_path, "wb", preset=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                add_directory_tree(archive, root_name, files, root, args.source_date_epoch)
                for path in files:
                    relative = path.relative_to(root).as_posix()
                    arcname = f"{root_name}/{relative}"
                    stat = path.stat()
                    mode = 0o755 if stat.st_mode & 0o111 else 0o644
                    info = normalized_info(
                        arcname,
                        mode=mode,
                        size=stat.st_size,
                        mtime=args.source_date_epoch,
                    )
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
                manifest_name = f"{root_name}/MANIFEST.sha256"
                manifest_info = normalized_info(
                    manifest_name,
                    mode=0o644,
                    size=len(manifest),
                    mtime=args.source_date_epoch,
                )
                archive.addfile(manifest_info, io.BytesIO(manifest))
        temp_path.replace(output)
    finally:
        temp_path.unlink(missing_ok=True)

    digest = sha256_file(output)
    sidecar.write_text(f"{digest}  {output.name}\n", encoding="utf-8", newline="\n")
    print(f"Created: {output}")
    print(f"SHA-256: {digest}")
    print(f"Files: {len(files)} + MANIFEST.sha256")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

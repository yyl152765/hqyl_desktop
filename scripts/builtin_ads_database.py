"""Select an explicitly supplied, untracked connection resource for a private build."""
from pathlib import Path
import subprocess

from backend.ads_database import builtin_connection_settings


def release_connection_data(source_path: str, project_root: Path) -> list[tuple[str, str]]:
    if not source_path:
        return []
    source = Path(source_path)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise ValueError("The authorized database resource must be an existing absolute file path")
    source = source.resolve()
    if source.name != "ads_database.json" or source.is_relative_to(project_root.resolve()):
        raise ValueError("The private database resource must be outside the exported source directory")
    repository = subprocess.run(
        ["git", "-C", str(source.parent), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if repository.returncode and any((parent / ".git").exists() for parent in source.parents):
        raise ValueError("Unable to verify the private database resource's Git status")
    if repository.returncode == 0:
        root = Path(repository.stdout.strip()).resolve()
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", source.relative_to(root).as_posix()],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if tracked.returncode == 0:
            raise ValueError("A database connection resource must not be tracked in Git")
        if tracked.returncode != 1:
            raise ValueError("Unable to verify the private database resource's Git status")
    if not builtin_connection_settings(source):
        raise ValueError("The authorized database resource is missing")
    return [(str(source), "backend/resources")]

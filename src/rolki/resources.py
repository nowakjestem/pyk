from __future__ import annotations

import shutil
from pathlib import Path

from .config import Config
from .errors import ResourceWait


def available_memory() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except FileNotFoundError:
        return None  # Production is Linux; macOS CLI still works.
    return None


def working_set(used: int, root: Path, *, v1=False) -> int:
    """Exclude clean inactive file cache; retain anonymous and dirty memory."""
    try:
        stats = dict(line.split() for line in (root / "memory.stat").read_text().splitlines())
        inactive = int(stats.get("total_inactive_file" if v1 else "inactive_file", "0"))
        dirty = int(stats.get("total_dirty" if v1 else "file_dirty", "0"))
        writeback = int(stats.get("total_writeback" if v1 else "file_writeback", "0"))
        reclaimable = max(0, inactive - max(0, dirty) - max(0, writeback))
        return max(0, used - reclaimable)
    except (OSError, ValueError):
        return used  # Missing/malformed statistics must not overestimate headroom.


def container_headroom() -> int | None:
    try:
        root = Path("/sys/fs/cgroup")
        limit = (root / "memory.max").read_text().strip()
        used = int((root / "memory.current").read_text())
        return None if limit == "max" else max(0, int(limit) - working_set(used, root))
    except (FileNotFoundError, ValueError):
        try:
            root = Path("/sys/fs/cgroup/memory")
            limit = int((root / "memory.limit_in_bytes").read_text())
            used = int((root / "memory.usage_in_bytes").read_text())
            return None if limit > 2**60 else max(0, limit - working_set(used, root, v1=True))
        except (FileNotFoundError, ValueError):
            return None


def tree_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except FileNotFoundError:
            # Render/upload cleanup can remove a file during the disk guard's scan.
            continue
    return total


def check_resources(config: Config, *, memory=True):
    root = config.paths.work_dir
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < config.limits.min_disk_free_bytes:
        raise ResourceWait("Za mało wolnego dysku. Zadanie czeka na dostępne zasoby.")
    if tree_size(root) >= config.limits.max_work_bytes:
        raise ResourceWait("Katalog roboczy osiągnął limit miejsca. Zadanie czeka.")
    if memory:
        available = available_memory()
        if available is not None and available < config.limits.min_available_memory_bytes:
            raise ResourceWait("Za mało dostępnego RAM. Zadanie czeka na dostępne zasoby.")
        headroom = container_headroom()
        if headroom is not None and headroom < config.limits.min_container_headroom_bytes:
            raise ResourceWait("Za mało pamięci w kontenerze. Zadanie czeka na dostępne zasoby.")

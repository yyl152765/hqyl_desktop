"""Keep delivery filenames stable when ERP warehouse display names change."""
from pathlib import Path
import os


def inventory_file_stems(warehouse_mapping, overrides=None):
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict) or set(overrides) - set(warehouse_mapping):
        raise ValueError("库存文件名配置包含未知仓库")
    stems = {name: overrides.get(name, name) for name in warehouse_mapping}
    for name, stem in stems.items():
        if (not isinstance(stem, str) or not stem.strip() or stem != stem.strip()
                or stem in {".", ".."} or stem.endswith(".")
                or any(c in stem for c in '<>:"/\\|?*') or not stem.isprintable()):
            raise ValueError("库存文件名必须是不含目录的有效名称")
        if stem != name and stem in warehouse_mapping:
            raise ValueError("库存文件名不能覆盖另一仓库的原始导出")
    if len({stem.casefold() for stem in stems.values()}) != len(stems):
        raise ValueError("库存文件名配置重复")
    return stems


def preserve_inventory_filenames(paths, stems):
    """Rename completed local exports without rewriting the ERP data inside."""
    result = []
    for path in map(Path, paths):
        target = path.with_name(stems[path.stem] + path.suffix)
        if path != target:
            os.replace(path, target)
        result.append(target)
    return result

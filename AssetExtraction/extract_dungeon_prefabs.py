#!/usr/bin/env python3
"""Print the dungeon map prefab paths for ``static.DUNGEON_PREFAB_PATHS``.

DungeonTemplate records name each room's map node but not the paths between
nodes; those live in the client's ``campaign/<zone>/<dungeon>/prefabs/nodes``
prefabs (``Nodes/Paths/Path_<A>_<B>``).  This reads them from the client's
``globalgamemanagers`` resource index and ``resources.assets``.

Requires UnityPy (optional extraction dependency, not a server requirement):

    python3 AssetExtraction/extract_dungeon_prefabs.py \\
        "/path/to/HEX SHARDS OF FATE/Hex.app/Contents/Resources/Data"
"""

from __future__ import annotations

import sys
from pathlib import Path

import UnityPy  # pyright: ignore[reportMissingImports] -- optional asset extraction dependency


def _transform(game_object):
    for pair in game_object.m_Component:
        component = pair.component.deref() if hasattr(pair, "component") else pair.deref()
        if component is not None and component.type.name == "Transform":
            return component.read()
    return None


def dungeon_prefab_paths(data_dir: Path) -> dict[str, list[str]]:
    env = UnityPy.load(str(data_dir / "globalgamemanagers"),
                       str(data_dir / "resources.assets"))
    manager = next(obj.read() for obj in env.objects
                   if obj.type.name == "ResourceManager")
    result: dict[str, list[str]] = {}
    for path, pointer in manager.m_Container:
        path = path.lower()
        if not (path.startswith("campaign/") and path.endswith("/prefabs/nodes")):
            continue
        root = _transform(pointer.read())
        if root is None:
            continue
        names = []
        for child in root.m_Children:
            child = child.read()
            if child.m_GameObject.read().m_Name != "Paths":
                continue
            names.extend(grandchild.read().m_GameObject.read().m_Name
                         for grandchild in child.m_Children)
        if names:
            result[path[:-len("/prefabs/nodes")]] = names
    return dict(sorted(result.items()))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    for root, names in dungeon_prefab_paths(Path(sys.argv[1])).items():
        print(f'    "{root}": """')
        line = []
        for name in names:
            if len(" ".join(line + [name])) > 78:
                print(" ".join(line))
                line = []
            line.append(name)
        if line:
            print(" ".join(line))
        print('""".split(),')


if __name__ == "__main__":
    main()

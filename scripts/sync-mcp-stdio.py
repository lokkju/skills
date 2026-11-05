#!/usr/bin/env python3
"""Keep the plugins' copies of mcp_stdio.py identical.

Each plugin installs on its own (a marketplace entry each, and `npx skills add` copies only the
skill directory), so the MCP protocol module can't be shared at runtime; each plugin carries a
copy. forge-tracker's is the source.

    scripts/sync-mcp-stdio.py           copy forge-tracker's to the other plugins
    scripts/sync-mcp-stdio.py --check   exit 1, naming each copy, when one differs
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "plugins/forge-tracker/skills/forge-tracker/scripts/forge_tracker/mcp_stdio.py"
COPIES = [
    ROOT / "plugins/term-recorder/skills/term-recorder/scripts/term_recorder/mcp_stdio.py",
]


def main(argv: list[str]) -> int:
    check = argv == ["--check"]
    if argv and not check:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    want = SOURCE.read_bytes()
    stale = [c for c in COPIES if not c.is_file() or c.read_bytes() != want]
    if check:
        for copy in stale:
            print(
                f"{copy.relative_to(ROOT)} differs from {SOURCE.relative_to(ROOT)}; "
                "run scripts/sync-mcp-stdio.py",
                file=sys.stderr,
            )
        return 1 if stale else 0
    for copy in stale:
        copy.write_bytes(want)
        print(f"updated {copy.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

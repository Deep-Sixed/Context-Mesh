"""``python -m contextmesh_mcp`` — write or inspect a session directory.

Deliberately not the server. Serving needs the MCP SDK and Python 3.10+;
writing a session needs neither, so this entry point works everywhere the core
does and can be tested on every supported version.

    python -m contextmesh_mcp --demo --rounds 8 --save ./session
    python -m contextmesh_mcp --session ./session

To serve one, install the extra and use ``contextmesh-mcp --session ./session``.
"""

from __future__ import annotations

from .session import main

if __name__ == "__main__":
    raise SystemExit(main())

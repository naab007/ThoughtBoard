"""Launcher for the ThoughtBoard MCP server.

Claude Code spawns MCP servers without a cwd we control, so put our folder on
sys.path before importing the package.

    python run_server.py                # MCP (stdio) + portal thread
    python run_server.py --portal-only  # portal only, foreground (no MCP)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    if "--portal-only" in sys.argv:
        import os

        from thoughtboard.portal import PORT_DEFAULT, run_portal
        port = int(os.environ.get("THOUGHTBOARD_PORT", PORT_DEFAULT))
        print(f"ThoughtBoard portal (standalone) at http://127.0.0.1:{port}/", file=sys.stderr)
        run_portal(port)
    else:
        from thoughtboard.server import main as server_main
        server_main()


if __name__ == "__main__":
    main()

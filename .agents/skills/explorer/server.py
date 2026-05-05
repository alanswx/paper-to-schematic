"""Schematic Explorer — stdlib HTTP server.

Run: python3 .agents/skills/explorer/server.py
Then open http://127.0.0.1:8765/

Read-mostly viewer. The agent (Claude) drives the workflow via the librarian
and schematic-graph skills; this UI exists for HITL operations the human
does with a mouse: drawing component bboxes, refining pin positions, and
visually verifying what the agent has produced.
"""
import http.server
import json
import mimetypes
import os
import sys
import urllib.parse
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SKILL_DIR.parent.parent.parent
STATIC_DIR = SKILL_DIR / "static"
LIBRARIAN_DIR = PROJECT_ROOT / ".agents" / "skills" / "librarian"
DEFAULT_BOARD = os.environ.get("VIEWER_BOARD", "exidy_440")


def board_dir(board_id: str) -> Path:
    return PROJECT_ROOT / "boards" / board_id


def load_board(board_id: str) -> dict:
    return json.loads((board_dir(board_id) / "board.json").read_text())


def load_chips() -> dict:
    return json.loads((LIBRARIAN_DIR / "chips.json").read_text())


def graph_path(board_id: str) -> Path:
    return board_dir(board_id) / "graph.json"


def empty_graph(board: dict) -> dict:
    return {
        "board": {
            "id": board["id"],
            "drawing_number": board["drawing_number"],
            "title": board.get("title", ""),
            "manufacturer": board.get("manufacturer", ""),
            "year": board.get("year"),
        },
        "sheets": [
            {
                "index": s["index"],
                "title": s["title"],
                "scan_path": s["scan_path"],
                "scan_pixel_size": s.get("scan_pixel_size"),
            }
            for s in board["sheets"]
        ],
        "components": [],
        "nets": [],
    }


def load_graph(board_id: str) -> dict:
    p = graph_path(board_id)
    if p.exists():
        return json.loads(p.read_text())
    return empty_graph(load_board(board_id))


def save_graph(board_id: str, graph: dict) -> None:
    graph_path(board_id).write_text(json.dumps(graph, indent=2))


def list_boards() -> list:
    """Enumerate boards/<id>/board.json — small subset for the picker."""
    out = []
    boards_root = PROJECT_ROOT / "boards"
    if not boards_root.exists():
        return out
    for entry in sorted(boards_root.iterdir()):
        if not entry.is_dir():
            continue
        bf = entry / "board.json"
        if not bf.exists():
            continue
        try:
            b = json.loads(bf.read_text())
        except Exception:
            continue
        out.append({
            "id": b.get("id", entry.name),
            "title": b.get("title", entry.name),
            "drawing_number": b.get("drawing_number", ""),
            "n_sheets": len(b.get("sheets", [])),
        })
    return out


def resolve_scan(board_id: str, sheet_index: int) -> Path:
    board = load_board(board_id)
    for s in board["sheets"]:
        if s["index"] == sheet_index:
            scan = (board_dir(board_id) / s["scan_path"]).resolve()
            if not str(scan).startswith(str(PROJECT_ROOT) + os.sep) and str(scan) != str(PROJECT_ROOT):
                raise PermissionError(f"scan path escapes project root: {scan}")
            return scan
    raise FileNotFoundError(f"sheet {sheet_index} not in board {board_id}")


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404, f"not found: {path}")
            return
        ctype, _ = mimetypes.guess_type(str(path))
        ctype = ctype or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _board_id(self, url) -> str:
        """Extract ?board=<id> query, falling back to DEFAULT_BOARD."""
        q = urllib.parse.parse_qs(url.query)
        bid = (q.get("board") or [DEFAULT_BOARD])[0]
        # Sanity: id must match a directory name pattern.
        if not bid.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"invalid board id: {bid!r}")
        return bid

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                return self._serve_file(STATIC_DIR / "index.html")
            if path.startswith("/static/"):
                rel = path[len("/static/"):]
                f = (STATIC_DIR / rel).resolve()
                if not str(f).startswith(str(STATIC_DIR) + os.sep):
                    self.send_error(403)
                    return
                return self._serve_file(f)
            if path == "/api/boards":
                return self._json(200, list_boards())
            if path == "/api/board":
                return self._json(200, load_board(self._board_id(url)))
            if path == "/api/chips":
                return self._json(200, load_chips())
            if path == "/api/graph":
                return self._json(200, load_graph(self._board_id(url)))
            if path.startswith("/api/sheet/"):
                rest = path[len("/api/sheet/"):]
                idx = int(rest.split(".")[0])
                return self._serve_file(resolve_scan(self._board_id(url), idx))
            self.send_error(404)
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_PUT(self):
        url = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            data = json.loads(body)
            if url.path == "/api/graph":
                bid = self._board_id(url)
                save_graph(bid, data)
                return self._json(200, {"saved": True, "path": str(graph_path(bid))})
            self.send_error(404)
        except Exception as e:
            self._json(500, {"error": str(e)})

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[viewer] {fmt % args}\n")


def main():
    port = int(os.environ.get("VIEWER_PORT", "8765"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"viewer:  http://127.0.0.1:{port}/")
    print(f"  root:  {PROJECT_ROOT}")
    print(f"  board: {DEFAULT_BOARD}")
    server.serve_forever()


if __name__ == "__main__":
    main()

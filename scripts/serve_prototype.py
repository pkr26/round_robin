"""Serve the static fictional prototype on loopback for browser verification."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class PreviewServer(ThreadingHTTPServer):
    # Browser test pages load several chunks concurrently. The stdlib default
    # backlog of five can reset local connections during a parallel cold load.
    request_queue_size = 128
    daemon_threads = True


class PreviewHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        from urllib.parse import urlsplit
        path = urlsplit(self.path).path
        if path in {"/live", "/design"}:
            self.path = path + ".html"
        return super().do_GET()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    output = Path(__file__).resolve().parents[1] / "frontend/out"
    if not (output / "index.html").is_file():
        raise SystemExit("Build the fictional prototype first with pnpm build.")
    with PreviewServer(("127.0.0.1", 3000), partial(PreviewHandler, directory=str(output))) as server:
        print("Fictional static prototype: http://127.0.0.1:3000", flush=True)
        server.serve_forever()

"""
Defender Octa — production entrypoint (waitress WSGI server).

Use this instead of `python api_server.py` for any real deployment.
`api_server.py` runs Flask's development server, which is single-threaded
under load, has no request queueing, and prints a warning telling you not
to use it in production.

Run:
    python serve.py
    python serve.py --threads 32 --port 8080

Host/port default to site_config.json ("server": {...}); flags override.

--------------------------------------------------------------------------
IMPORTANT — do not switch this to a multi-process server (gunicorn -w N,
uwsgi with workers, waitress-serve with multiple processes).

api_server.py keeps live state in module-level globals (vehicle_stats,
gate_state, entry_times) and runs background threads (score watchdog, USB
camera capture, RTSP readers). Multiple worker processes would each get
their OWN copy of that state and their own duplicate camera threads, so
gate status would differ depending on which worker served the request.

Waitress is the right fit: ONE process, many threads, shared globals.
--------------------------------------------------------------------------
"""

import argparse
import sys


def _force_utf8_stdout():
    """Make stdout/stderr UTF-8 before anything prints.

    The startup banners contain non-ASCII characters (→, —). When this
    process runs under a service manager (NSSM, Task Scheduler) its output
    is redirected to a file, and on Windows that makes Python fall back to
    the cp1252 codec — which cannot encode them. The result is a
    UnicodeEncodeError inside db.init_db() and the server dies before it
    ever binds the port.

    This must run BEFORE importing api_server, which prints while importing.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # already wrapped, or not a real stream — nothing to do


_force_utf8_stdout()

try:
    from waitress import serve
except ImportError:
    sys.exit(
        "waitress is not installed.\n"
        "    pip install waitress\n"
        "(or: .venv\\Scripts\\pip install waitress)"
    )

from api_server import app, bootstrap, CONFIG


def main():
    ap = argparse.ArgumentParser(description="Run Defender Octa via waitress.")
    ap.add_argument("--host", default=None, help="bind address (default: site_config.json)")
    ap.add_argument("--port", type=int, default=None, help="bind port (default: site_config.json)")
    ap.add_argument("--threads", type=int, default=12,
                    help="worker threads (default: 12). Raise if MJPEG camera "
                         "streams starve the dashboard — each open stream holds "
                         "a thread for as long as the browser tab is open.")
    args = ap.parse_args()

    host = args.host if args.host is not None else CONFIG.host
    port = args.port if args.port is not None else CONFIG.port

    # Same startup sequence the dev server runs: DB, watchdog, cameras, etc.
    bootstrap()

    print(f"{'=' * 50}")
    print("  Server    : waitress (production)")
    print(f"  Threads   : {args.threads}")
    print(f"  Listening : http://{host}:{port}")
    if host in ("0.0.0.0", "::"):
        print("  Reachable on every network interface — restrict with a")
        print("  firewall rule if this box is not on a trusted LAN.")
    print(f"{'=' * 50}\n", flush=True)

    serve(
        app,
        host=host,
        port=port,
        threads=args.threads,
        # Camera/MJPEG responses are long-lived and unbounded. waitress
        # defaults to closing an idle channel after 120s, which would cut
        # off a live camera feed mid-stream, so effectively disable it
        # (it wants an int — None is rejected).
        channel_timeout=86400,
        outbuf_overflow=1 << 30,
        # The banner above already reports the real bind address.
        ident="DefenderOcta",
    )


if __name__ == "__main__":
    main()

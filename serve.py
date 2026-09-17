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
WHY THE THREAD AND TIMEOUT NUMBERS BELOW MATTER

Waitress serves every request from a fixed thread pool. A normal request
borrows a thread for a few milliseconds and gives it back. A STREAM does
not: /api/stream (Server-Sent Events) and /video_feed (MJPEG) are infinite
by design, so each open stream holds one thread for the entire life of the
connection.

That makes the thread count a hard limit on concurrent viewers, not a
performance dial. With the old settings — 12 threads and a 24-hour idle
timeout — the demo site reached this state on 17 Sep 2026:

    18 established connections
    task queue depth 86 and climbing
    CPU 0.93%                 <- threads blocked, not busy
    "total open connections reached the connection limit,
     no longer accepting new connections"

A login over localhost then timed out at 30 seconds. Nothing had crashed.
Every thread was simply held by a stream belonging to a browser tab that
was asleep, or to a connection that had dropped without closing cleanly,
and the 24-hour timeout meant none of them would be reclaimed that day.

Two numbers fix it, and they work together:

  threads          — how many concurrent streams the site can carry at all.
                     Each guard tablet, manager laptop and open dashboard
                     tab is one. 32 is headroom for a real site; it is not
                     a substitute for the timeout below.

  channel_timeout  — how long a channel may sit with NO data moving before
                     waitress reclaims it. This is the one that actually
                     recovers leaked threads. It measures inactivity, not
                     connection age, so it does not cut off a stream that
                     is alive and sending.

The SSE endpoint sends a keepalive every 20 seconds:

    except _queue.Empty:
        yield ": ping\\n\\n"   # keepalive for proxies/tunnels

so at 90 seconds a live stream has roughly four pings of margin, while a
dead one is reclaimed in a minute and a half instead of a day.

THE TRADE-OFF, STATED PLAINLY: a camera feed that stalls for more than
90 seconds will now be closed. EventSource reconnects on its own, so SSE
recovers silently — but an MJPEG feed in an <img> tag does NOT reconnect;
the picture simply stops. If you run cameras, /video_feed should emit a
keepalive when no frame is available rather than yielding nothing, and
should return 503 outright on a site with no camera configured. Until it
does, a camera-less site's /video_feed request is an idle channel, which
is exactly what this timeout is here to reclaim.
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


# Keepalive interval of the SSE endpoint in api_server.py. channel_timeout
# must stay comfortably above this or live streams get cut off. If you ever
# change the ping interval there, change this.
SSE_PING_SECONDS = 20


def main():
    ap = argparse.ArgumentParser(description="Run Defender Octa via waitress.")
    ap.add_argument("--host", default=None,
                    help="bind address (default: site_config.json)")
    ap.add_argument("--port", type=int, default=None,
                    help="bind port (default: site_config.json)")
    ap.add_argument("--threads", type=int, default=32,
                    help="worker threads (default: 32). Each open SSE or "
                         "MJPEG stream holds one thread for the life of the "
                         "connection, so this is the ceiling on concurrent "
                         "dashboard tabs and camera views, not a speed dial.")
    ap.add_argument("--channel-timeout", type=int, default=90,
                    help="seconds a connection may sit idle before waitress "
                         "reclaims it (default: 90). This is what recovers "
                         "threads held by dropped or sleeping clients. Must "
                         f"stay above the {SSE_PING_SECONDS}s SSE keepalive.")
    ap.add_argument("--connection-limit", type=int, default=200,
                    help="total simultaneous connections before waitress "
                         "stops accepting new ones (default: 200).")
    args = ap.parse_args()

    host = args.host if args.host is not None else CONFIG.host
    port = args.port if args.port is not None else CONFIG.port

    # A channel_timeout at or below the keepalive interval kills every live
    # stream on a timer. Refuse to start rather than ship a site whose
    # camera feeds drop every minute for no visible reason.
    if args.channel_timeout <= SSE_PING_SECONDS * 2:
        sys.exit(
            f"--channel-timeout {args.channel_timeout} is too low: the SSE "
            f"keepalive fires every {SSE_PING_SECONDS}s, so live streams "
            f"would be closed mid-flight. Use at least "
            f"{SSE_PING_SECONDS * 3}."
        )

    if args.threads < 8:
        print(f"  WARNING: only {args.threads} worker threads. Each open "
              f"dashboard tab holds one for its event stream.", flush=True)

    # Same startup sequence the dev server runs: DB, watchdog, cameras, etc.
    bootstrap()

    print(f"{'=' * 50}")
    print("  Server    : waitress (production)")
    print(f"  Threads   : {args.threads}")
    print(f"  Idle close: {args.channel_timeout}s "
          f"(SSE pings every {SSE_PING_SECONDS}s)")
    print(f"  Max conns : {args.connection_limit}")
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
        # See the header. Long-lived streams hold a thread each; this is the
        # setting that reclaims the ones whose client has gone away without
        # closing. It measures inactivity, so it does not interrupt a stream
        # that is actually sending.
        channel_timeout=args.channel_timeout,
        connection_limit=args.connection_limit,
        outbuf_overflow=1 << 30,
        # The banner above already reports the real bind address.
        ident="DefenderOcta",
    )


if __name__ == "__main__":
    main()

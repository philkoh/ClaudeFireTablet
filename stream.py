#!/usr/bin/env python3
"""
Read H.264 elementary stream from the Fire tablet over adb-forwarded TCP,
buffer 5 seconds, then pipe to ffplay for display.

Usage:
    adb forward tcp:7777 tcp:7777
    python3 stream.py [--delay SECS] [--port PORT]
"""

import argparse
import collections
import socket
import subprocess
import sys
import threading
import time

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--delay", type=float, default=5.0,
                    help="seconds of buffering before display")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    print(f"connecting to {args.host}:{args.port} (delay={args.delay}s)…", flush=True)

    while True:
        try:
            sock = socket.create_connection((args.host, args.port), timeout=5)
            break
        except (ConnectionRefusedError, socket.timeout):
            print("  not ready, retrying…", flush=True)
            time.sleep(1)
    print("connected — buffering…", flush=True)

    ffplay = subprocess.Popen(
        ["ffplay",
         "-loglevel", "warning",
         "-fflags", "nobuffer",
         "-flags", "low_delay",
         "-probesize", "32",
         "-analyzeduration", "0",
         "-sync", "ext",
         "-f", "h264",
         "-window_title", f"Fire tablet — delayed {args.delay}s",
         "-i", "-"],
        stdin=subprocess.PIPE,
    )

    queue = collections.deque()
    queue_lock = threading.Lock()
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                chunk = sock.recv(65536)
                if not chunk:
                    break
                with queue_lock:
                    queue.append((time.monotonic(), chunk))
        except Exception as e:
            print(f"reader: {e}", file=sys.stderr)
        finally:
            stop.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    last_status = 0
    total_bytes = 0
    try:
        while not stop.is_set():
            now = time.monotonic()
            release_before = now - args.delay
            sendable = []
            with queue_lock:
                while queue and queue[0][0] <= release_before:
                    sendable.append(queue.popleft()[1])
                pending_chunks = len(queue)

            try:
                for chunk in sendable:
                    ffplay.stdin.write(chunk)
                    total_bytes += len(chunk)
                if sendable:
                    ffplay.stdin.flush()
            except (BrokenPipeError, OSError):
                print("player window closed", flush=True)
                stop.set()
                break

            if now - last_status > 2:
                last_status = now
                print(f"  streamed {total_bytes/1024:.0f} KB, "
                      f"{pending_chunks} chunks buffered", flush=True)

            if not sendable:
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try: sock.close()
        except Exception: pass
        try: ffplay.stdin.close()
        except Exception: pass
        ffplay.wait()


if __name__ == "__main__":
    main()

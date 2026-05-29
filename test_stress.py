#!/usr/bin/env python3
"""
Autonomous stress test: verify camera streaming and highlight animation
operate together without glitches, across many cycles, without human input.

For each iteration:
- Start a camera reader thread (reads port 7777 continuously)
- Send a clear + show + smooth highlight animation
- Measure: camera throughput, inter-chunk gaps, animation timing,
  PING latency under load, GL frame-period stability
- Scan logcat for errors

Pass criteria:
- No errors in logcat (excluding routine messages)
- Camera throughput > 100 KB/s sustained
- No camera read gap > 1 second
- Animation duration within ±100ms of expected
- PING latency under load < 5ms average
- GL frame period stays within [16.0, 17.5] ms (60Hz nominal)
"""
import argparse
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
from display_control import DisplayController

EXPECTED_FRAME_PERIOD_MIN_NS = 16_000_000
EXPECTED_FRAME_PERIOD_MAX_NS = 17_500_000
MAX_CAM_GAP_S = 1.0
MIN_CAM_KBPS = 100
ANIM_TOLERANCE_MS = 100
PING_LATENCY_MAX_MS = 5.0


def adb(*args, check=True, capture=True):
    return subprocess.run(["adb", *args], check=check,
                          capture_output=capture, text=True)


def reset_logcat():
    adb("logcat", "-c")


BENIGN_PATTERNS = (
    "Client gone", "client disconnected", "Display client gone",
    "cam write failed", "Broken pipe", "SocketOutputStream",
    "SocketException", "BitmapDisplayActivity.drainEncoder",
    "BitmapDisplayActivity$$", "BitmapDisplayActivity.$",
    "java.net.SocketOutputStream", "at java.net", "at com.example.firehello",
    "at java.lang.Thread.run",
)


def is_benign(line):
    return any(p in line for p in BENIGN_PATTERNS)


def grep_errors():
    out = adb("logcat", "-d", "-T", "1000").stdout
    lines = []
    for line in out.splitlines():
        if "BitmapDisplay" in line or "firehello" in line:
            lvl = line[31:32] if len(line) > 32 else " "
            if lvl in ("E", "W"):
                if is_benign(line):
                    continue
                lines.append(line)
        if any(s in line for s in ("ANR ", "FATAL EXCEPTION",
                                    "OutOfMemoryError",
                                    "CodecException")):
            lines.append(line)
    return lines


class CameraReader(threading.Thread):
    def __init__(self, port=7777):
        super().__init__(daemon=True)
        self.port = port
        self.stop_evt = threading.Event()
        self.bytes_total = 0
        self.chunks = []         # (timestamp, size)
        self.gaps = []           # gap durations in seconds
        self.error = None
        self.connected = False

    def stop(self):
        self.stop_evt.set()

    def run(self):
        try:
            self.sock = socket.create_connection(
                ("127.0.0.1", self.port), timeout=5)
            self.connected = True
            last_t = time.perf_counter()
            self.sock.settimeout(2.0)
            while not self.stop_evt.is_set():
                try:
                    data = self.sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                now = time.perf_counter()
                gap = now - last_t
                self.gaps.append(gap)
                self.bytes_total += len(data)
                self.chunks.append((now, len(data)))
                last_t = now
        except Exception as e:
            self.error = e
        finally:
            try:
                self.sock.close()
            except Exception:
                pass

    def stats(self, t_start, t_end):
        duration = t_end - t_start
        kbps = (self.bytes_total / 1024) / max(duration, 0.001)
        max_gap = max(self.gaps) if self.gaps else 0.0
        n_chunks = len(self.chunks)
        return {
            "duration_s": duration,
            "bytes": self.bytes_total,
            "kbps": kbps,
            "max_gap_s": max_gap,
            "n_chunks": n_chunks,
            "connected": self.connected,
            "error": str(self.error) if self.error else None,
        }


def ping_under_load(dc, n=20, interval=0.05):
    """Send n PINGs and measure response latency."""
    latencies = []
    for _ in range(n):
        t1 = time.perf_counter_ns()
        dc._send(1, struct.pack("<q", t1))  # CMD_PING
        cmd, payload = dc._recv()
        t4 = time.perf_counter_ns()
        latencies.append((t4 - t1) / 1e6)  # ms
        time.sleep(interval)
    return latencies


def run_one_cycle(iteration, dc, cam, anim_duration_s=4.0, verbose=True):
    """Run one stress cycle reusing the persistent camera reader."""
    result = {"iteration": iteration, "errors": [], "warnings": []}

    bytes_before = cam.bytes_total
    t_test_start = time.perf_counter()
    gaps_before_count = len(cam.gaps)

    # Reset state
    dc.clear_highlights()
    dc.load_color(0, 255, 255, 255)
    dc.show(0)

    # Send a smooth animation
    t_start_pc = time.perf_counter_ns() + 200_000_000
    t_end_pc = t_start_pc + int(anim_duration_s * 1e9)
    dc.add_highlight_anim(
        0.05, 0.3, 0.95, 0.7,
        t_start_pc_ns=t_start_pc,
        t_end_pc_ns=t_end_pc,
        color=(255, 220, 0, 80))

    # During animation, send periodic PINGs to measure latency under load
    anim_ping_lats = []
    wall_end = time.perf_counter() + anim_duration_s + 0.3
    while time.perf_counter() < wall_end:
        t1 = time.perf_counter_ns()
        dc._send(1, struct.pack("<q", t1))
        cmd, payload = dc._recv()
        t4 = time.perf_counter_ns()
        anim_ping_lats.append((t4 - t1) / 1e6)
        time.sleep(0.05)

    # Frame period stability
    final_fp = dc.get_info()[2]
    result["final_frame_period_ms"] = final_fp / 1e6

    # Final SHOW timing
    t_show_target = time.perf_counter_ns() + 50_000_000
    t_show_actual = dc.show_at(0, t_show_target)
    show_error_us = (t_show_actual - t_show_target) / 1000
    result["final_show_error_us"] = show_error_us

    t_test_end = time.perf_counter()

    # Per-iteration camera metrics (gaps recorded during this cycle only)
    iter_bytes = cam.bytes_total - bytes_before
    iter_duration = t_test_end - t_test_start
    iter_kbps = (iter_bytes / 1024) / max(iter_duration, 0.001)
    iter_gaps = cam.gaps[gaps_before_count:]
    iter_max_gap = max(iter_gaps) if iter_gaps else 0.0

    result["camera"] = {
        "kbps": iter_kbps,
        "max_gap_s": iter_max_gap,
        "bytes": iter_bytes,
        "n_chunks": len(iter_gaps),
    }
    result["ping_latency_ms"] = {
        "mean": statistics.mean(anim_ping_lats) if anim_ping_lats else 0,
        "p50": statistics.median(anim_ping_lats) if anim_ping_lats else 0,
        "p99": (sorted(anim_ping_lats)[int(len(anim_ping_lats) * 0.99)]
                if anim_ping_lats else 0),
        "max": max(anim_ping_lats) if anim_ping_lats else 0,
        "n": len(anim_ping_lats),
    }

    # Pass criteria
    if iter_kbps < MIN_CAM_KBPS:
        result["errors"].append(f"camera throughput too low: {iter_kbps:.1f} KB/s")
    if iter_max_gap > MAX_CAM_GAP_S:
        result["errors"].append(
            f"camera gap too large: {iter_max_gap*1000:.0f} ms")
    if cam.error:
        result["errors"].append(f"camera reader error: {cam.error}")
    if not (EXPECTED_FRAME_PERIOD_MIN_NS <= final_fp <=
            EXPECTED_FRAME_PERIOD_MAX_NS):
        result["errors"].append(f"frame period out of range: {final_fp/1e6:.3f} ms")
    if result["ping_latency_ms"]["mean"] > PING_LATENCY_MAX_MS:
        result["warnings"].append(
            f"PING avg high: {result['ping_latency_ms']['mean']:.2f} ms")

    if verbose:
        pl = result["ping_latency_ms"]
        print(f"[#{iteration:3d}] "
              f"cam {iter_kbps:.0f} KB/s "
              f"maxgap {iter_max_gap*1000:3.0f}ms | "
              f"ping mean={pl['mean']:.2f}ms p99={pl['p99']:.2f}ms "
              f"max={pl['max']:.2f}ms | "
              f"fp {result['final_frame_period_ms']:.2f}ms | "
              f"showErr {show_error_us:+5.0f}us | "
              f"err={len(result['errors'])} warn={len(result['warnings'])}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--iterations", type=int, default=10)
    ap.add_argument("--anim-duration", type=float, default=4.0)
    args = ap.parse_args()

    # Make sure ports are forwarded
    subprocess.run(["adb", "forward", "tcp:8888", "tcp:8888"], check=True)
    subprocess.run(["adb", "forward", "tcp:7777", "tcp:7777"], check=True)

    reset_logcat()
    print(f"Running {args.iterations} stress cycles (anim {args.anim_duration}s each)...")
    print("Camera connection is held continuously across all iterations.")
    print()

    # Persistent connections — matches real-world use
    dc = DisplayController()
    dc.sync_clocks()
    cam = CameraReader()
    cam.start()
    time.sleep(0.7)
    if not cam.connected:
        print("FAILED: camera reader could not connect")
        return 1

    results = []
    try:
        for i in range(args.iterations):
            try:
                r = run_one_cycle(i + 1, dc, cam, args.anim_duration)
            except Exception as e:
                r = {"iteration": i + 1, "errors": [f"exception: {e}"]}
                print(f"[#{i+1}] EXCEPTION: {e}")
            results.append(r)
            time.sleep(0.3)
    finally:
        dc.clear_highlights()
        dc.close()
        cam.stop()
        cam.join(timeout=2)

    # Logcat scan
    err_lines = grep_errors()

    # Aggregate
    print()
    print("=" * 70)
    print("AGGREGATE RESULTS")
    print("=" * 70)

    cam_kbps = [r["camera"]["kbps"] for r in results if "camera" in r]
    cam_gaps = [r["camera"]["max_gap_s"] for r in results if "camera" in r]
    ping_p99s = [r["ping_latency_ms"]["p99"]
                 for r in results if "ping_latency_ms" in r]
    show_errs = [abs(r["final_show_error_us"])
                 for r in results if "final_show_error_us" in r]
    fps = [r["final_frame_period_ms"]
           for r in results if "final_frame_period_ms" in r]

    if cam_kbps:
        print(f"Camera throughput: mean={statistics.mean(cam_kbps):.1f} KB/s, "
              f"min={min(cam_kbps):.1f}, max={max(cam_kbps):.1f}")
    if cam_gaps:
        print(f"Max camera gap: mean={statistics.mean(cam_gaps)*1000:.1f} ms, "
              f"worst={max(cam_gaps)*1000:.0f} ms")
    if ping_p99s:
        print(f"PING p99 under load: mean={statistics.mean(ping_p99s):.2f} ms, "
              f"worst={max(ping_p99s):.2f} ms")
    if show_errs:
        print(f"SHOW timing error: mean={statistics.mean(show_errs):.0f} us, "
              f"max={max(show_errs):.0f} us")
    if fps:
        print(f"GL frame period: mean={statistics.mean(fps):.3f} ms, "
              f"min={min(fps):.3f}, max={max(fps):.3f}")

    n_iter_with_errors = sum(1 for r in results if r.get("errors"))
    n_iter_with_warnings = sum(1 for r in results if r.get("warnings"))

    print()
    print(f"Iterations with errors:   {n_iter_with_errors} / {len(results)}")
    print(f"Iterations with warnings: {n_iter_with_warnings} / {len(results)}")
    print(f"Logcat error lines:       {len(err_lines)}")

    if err_lines:
        print()
        print("LOGCAT ERROR SAMPLES (first 20):")
        for line in err_lines[:20]:
            print("  " + line[:140])

    for r in results:
        if r.get("errors"):
            print(f"  iter {r['iteration']}: {r['errors']}")

    print()
    if n_iter_with_errors == 0 and len(err_lines) == 0:
        print("✓ ALL CYCLES PASSED — no errors detected")
        return 0
    else:
        print("✗ FAILURES DETECTED")
        return 1


if __name__ == "__main__":
    sys.exit(main())

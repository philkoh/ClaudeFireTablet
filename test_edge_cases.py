#!/usr/bin/env python3
"""
Edge-case stress tests for risks identified in research:
  - Large texture upload (glTexImage2D 9.2 MB) — does it stall a frame?
  - Memory leak across many cycles — does process RSS grow?
  - Long continuous animation — does timing drift?
  - Many queued animations — does the renderer handle them?
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


def adb(*args, capture=True):
    return subprocess.run(["adb", *args], check=True,
                          capture_output=capture, text=True)


def get_meminfo_pss_kb():
    """Read process PSS (proportional set size) in KB from dumpsys."""
    try:
        out = adb("shell", "dumpsys", "meminfo", "com.example.firehello").stdout
        # Look for "TOTAL PSS:" line
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("TOTAL PSS:"):
                # "TOTAL PSS:    49386    TOTAL RSS:    52344    ..."
                parts = line.split()
                return int(parts[2])
        # Fallback: older format
        for line in out.splitlines():
            if "TOTAL" in line and "PSS" not in line.upper():
                parts = line.split()
                for p in parts:
                    if p.isdigit():
                        return int(p)
    except Exception as e:
        print(f"meminfo parse error: {e}")
    return None


# ─── Test 1: Large texture upload stalls? ─────────────────────────────────

class CameraReader(threading.Thread):
    def __init__(self, port=7777):
        super().__init__(daemon=True)
        self.stop_evt = threading.Event()
        self.chunks = []  # (timestamp, size)
        self.bytes_total = 0
        self.connected = False
        self.port = port

    def stop(self):
        self.stop_evt.set()

    def run(self):
        try:
            self.sock = socket.create_connection(
                ("127.0.0.1", self.port), timeout=5)
            self.connected = True
            self.sock.settimeout(1.0)
            while not self.stop_evt.is_set():
                try:
                    data = self.sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                now = time.perf_counter()
                self.bytes_total += len(data)
                self.chunks.append((now, len(data)))
        except Exception as e:
            print(f"camera reader: {e}")
        finally:
            try: self.sock.close()
            except Exception: pass


def test_texture_upload_stall():
    """During an active animation, upload a 9.2 MB texture and see
    if the camera/anim glitches."""
    print("\n── TEST 1: Large texture upload during active animation ──")

    dc = DisplayController()
    dc.sync_clocks()
    w, h, fp = dc.get_info()

    cam = CameraReader()
    cam.start()
    time.sleep(0.7)

    # Pre-build the large texture data
    large_rgba = bytes([255, 240, 200, 255]) * (w * h)
    print(f"  Texture: {w}x{h} = {len(large_rgba)/1024/1024:.1f} MB")

    # Set up base display
    dc.clear_highlights()
    dc.load_color(0, 250, 250, 250)
    dc.show(0)
    time.sleep(0.3)

    # Start a long animation
    t_start = time.perf_counter_ns() + 200_000_000
    t_end = t_start + 8_000_000_000  # 8 seconds
    dc.add_highlight_anim(0.05, 0.3, 0.95, 0.7,
                          t_start_pc_ns=t_start, t_end_pc_ns=t_end,
                          color=(255, 220, 0, 80))
    print(f"  Animation duration: 8.0 s")

    # Wait for animation to begin
    time.sleep(1.5)

    # During animation: upload large texture, measure how long it takes
    bytes_before = cam.bytes_total
    chunks_before = len(cam.chunks)
    t_upload_start = time.perf_counter_ns()
    dc.load_rgba(1, w, h, large_rgba)
    t_upload_end = time.perf_counter_ns()
    bytes_after = cam.bytes_total
    chunks_after = len(cam.chunks)

    upload_ms = (t_upload_end - t_upload_start) / 1e6
    bytes_during_upload = bytes_after - bytes_before
    chunks_during_upload = chunks_after - chunks_before
    cam_kbps_during_upload = (bytes_during_upload / 1024) / max(upload_ms / 1000, 0.001)

    print(f"  Texture upload took: {upload_ms:.1f} ms")
    print(f"  Camera bytes during upload: {bytes_during_upload/1024:.0f} KB "
          f"({chunks_during_upload} chunks, {cam_kbps_during_upload:.0f} KB/s)")

    # Now measure camera gap right at the time of upload
    # The largest gap in the chunks list near upload time tells us if the camera stalled
    t_upload_start_s = t_upload_start / 1e9 - (
        time.time_ns() / 1e9 - time.perf_counter())  # approximate wall time
    # easier: just find largest gap in last 2 seconds
    recent = [c for c in cam.chunks if c[0] > time.perf_counter() - 2.0]
    gaps = [recent[i+1][0] - recent[i][0] for i in range(len(recent)-1)]
    max_recent_gap = max(gaps) if gaps else 0
    print(f"  Max camera gap in last 2s: {max_recent_gap*1000:.0f} ms "
          f"(expected ~33 ms at 30 fps)")

    # Continue animation to completion
    time.sleep(7)

    # Verify final state
    target = time.perf_counter_ns() + 50_000_000
    t_show = dc.show_at(0, target)
    print(f"  Post-upload SHOW timing error: {(t_show - target)/1000:+.0f} us")

    cam.stop()
    cam.join(timeout=2)
    dc.clear_highlights()
    dc.close()

    # Pass criteria
    passed = max_recent_gap < 0.5  # less than 500ms gap
    print(f"  {'✓ PASS' if passed else '✗ FAIL'}: "
          f"max camera gap during upload {max_recent_gap*1000:.0f} ms")
    return passed


# ─── Test 2: Memory leak ──────────────────────────────────────────────────

def test_memory_leak(n_cycles=30):
    """Run many short cycles, check if process memory grows."""
    print(f"\n── TEST 2: Memory leak check ({n_cycles} cycles) ──")

    dc = DisplayController()
    dc.sync_clocks()

    pss_samples = []

    pss0 = get_meminfo_pss_kb()
    print(f"  Initial PSS: {pss0} KB" if pss0 else "  (could not read PSS)")
    pss_samples.append((0, pss0))

    for i in range(n_cycles):
        # Load + animate + clear
        dc.clear_highlights()
        dc.load_color(0, 200, 200, 200)
        dc.show(0)
        t_start = time.perf_counter_ns() + 100_000_000
        t_end = t_start + 500_000_000
        dc.add_highlight_anim(0.1, 0.2, 0.9, 0.8,
                              t_start_pc_ns=t_start, t_end_pc_ns=t_end)
        time.sleep(0.7)

        if (i + 1) % 5 == 0:
            pss = get_meminfo_pss_kb()
            pss_samples.append((i + 1, pss))
            print(f"  After {i+1:2d} cycles: PSS = {pss} KB"
                  f" (Δ {pss - pss0:+d} KB)" if pss and pss0 else
                  f"  After {i+1:2d} cycles: PSS unavailable")

    dc.clear_highlights()
    dc.close()

    # Compute growth rate
    valid = [(i, pss) for i, pss in pss_samples if pss is not None]
    if len(valid) < 2:
        print("  ✗ FAIL: not enough memory samples")
        return False
    growth = valid[-1][1] - valid[0][1]
    growth_per_cycle = growth / max(valid[-1][0], 1)
    print(f"  Total PSS growth: {growth:+d} KB over {valid[-1][0]} cycles "
          f"({growth_per_cycle:.1f} KB/cycle)")
    passed = growth < 5000  # less than 5MB total growth is OK
    print(f"  {'✓ PASS' if passed else '✗ FAIL'}: growth {growth} KB")
    return passed


# ─── Test 3: Long continuous animation ──────────────────────────────────

def test_long_animation():
    """Single long animation (30s). Verify timing precision at start and end."""
    print("\n── TEST 3: Long-duration animation (30 seconds) ──")

    dc = DisplayController()
    dc.sync_clocks()

    cam = CameraReader()
    cam.start()
    time.sleep(0.5)

    dc.clear_highlights()
    dc.load_color(0, 220, 220, 220)
    dc.show(0)

    duration_s = 30.0
    t_start = time.perf_counter_ns() + 500_000_000
    t_end = t_start + int(duration_s * 1e9)
    dc.add_highlight_anim(0.05, 0.2, 0.95, 0.8,
                          t_start_pc_ns=t_start, t_end_pc_ns=t_end)

    # Sample PING latency every second
    ping_latencies = []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < duration_s + 1:
        t1 = time.perf_counter_ns()
        dc._send(1, struct.pack("<q", t1))
        cmd, payload = dc._recv()
        t4 = time.perf_counter_ns()
        ping_latencies.append((t4 - t1) / 1e6)
        time.sleep(0.5)

    # Verify camera ran smoothly throughout
    cam_gaps = [cam.chunks[i+1][0] - cam.chunks[i][0]
                for i in range(len(cam.chunks)-1)]
    max_cam_gap = max(cam_gaps) if cam_gaps else 0
    cam_kbps = (cam.bytes_total / 1024) / (time.perf_counter() - t0)

    # Frame period should be stable
    fp_final = dc.get_info()[2]

    cam.stop(); cam.join(timeout=2)
    dc.clear_highlights()
    dc.close()

    p99 = sorted(ping_latencies)[int(len(ping_latencies) * 0.99)]
    p_max = max(ping_latencies)
    p_mean = statistics.mean(ping_latencies)
    print(f"  PING during anim: n={len(ping_latencies)} "
          f"mean={p_mean:.2f}ms p99={p99:.2f}ms max={p_max:.2f}ms")
    print(f"  Camera during anim: {cam_kbps:.0f} KB/s, "
          f"max gap {max_cam_gap*1000:.0f} ms")
    print(f"  Final frame period: {fp_final/1e6:.3f} ms")

    passed = (max_cam_gap < 0.5 and p_max < 50.0 and
              16_000_000 < fp_final < 17_500_000)
    print(f"  {'✓ PASS' if passed else '✗ FAIL'}")
    return passed


# ─── Test 4: Many queued animations ───────────────────────────────────────

def test_many_animations():
    """Queue 50 animations at once, verify the renderer handles them."""
    print("\n── TEST 4: 50 concurrent animations ──")

    dc = DisplayController()
    dc.sync_clocks()
    dc.clear_highlights()
    dc.load_color(0, 240, 240, 240)
    dc.show(0)
    time.sleep(0.3)

    n = 50
    t_now = time.perf_counter_ns()
    t_send_start = t_now
    for i in range(n):
        y_top = i / n
        y_bot = (i + 1) / n
        t_start = t_now + i * 50_000_000  # staggered starts
        t_end = t_start + 1_000_000_000   # 1s each
        dc.add_highlight_anim(0.05, y_top, 0.95, y_bot,
                              t_start_pc_ns=t_start, t_end_pc_ns=t_end,
                              color=(255, 220, 0, 50))
    t_send_end = time.perf_counter_ns()
    elapsed_ms = (t_send_end - t_send_start) / 1e6
    print(f"  Queued {n} animations in {elapsed_ms:.0f} ms "
          f"({n * 1000 / elapsed_ms:.0f} cmd/s)")

    # Wait for them all to complete + see them
    time.sleep(4.0)

    # Verify still healthy
    target = time.perf_counter_ns() + 50_000_000
    t_show = dc.show_at(0, target)
    show_err = (t_show - target) / 1000
    fp = dc.get_info()[2]
    print(f"  Post-batch SHOW err: {show_err:+.0f} us, frame period: {fp/1e6:.3f} ms")

    dc.clear_highlights()
    dc.close()

    passed = abs(show_err) < 30000 and 16_000_000 < fp < 17_500_000
    print(f"  {'✓ PASS' if passed else '✗ FAIL'}")
    return passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-mem", action="store_true")
    ap.add_argument("--skip-long", action="store_true")
    args = ap.parse_args()

    subprocess.run(["adb", "forward", "tcp:8888", "tcp:8888"], check=True)
    subprocess.run(["adb", "forward", "tcp:7777", "tcp:7777"], check=True)

    results = {}
    results["texture_stall"] = test_texture_upload_stall()
    if not args.skip_mem:
        results["memory_leak"] = test_memory_leak()
    if not args.skip_long:
        results["long_anim"] = test_long_animation()
    results["many_anims"] = test_many_animations()

    print()
    print("=" * 60)
    print("EDGE CASE TEST SUMMARY")
    print("=" * 60)
    for name, passed in results.items():
        print(f"  {'✓' if passed else '✗'} {name}")
    n_pass = sum(1 for p in results.values() if p)
    print(f"\n  {n_pass}/{len(results)} tests passed")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

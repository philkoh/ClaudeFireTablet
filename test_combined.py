#!/usr/bin/env python3
"""
Final combined stress: everything happening at once, for a long time.

For 60 seconds straight:
  - Camera continuously streaming
  - Score animation running in a loop
  - Periodic ADB touch events (simulating menu taps)
  - Periodic PING under load
  - Periodic SHOW timing measurement

Measures:
  - Camera continuity (max gap, throughput)
  - Highlight precision (frame period stability)
  - Network responsiveness (PING latency distribution)
  - GL pipeline health (SHOW errors stay bounded)
"""
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
from display_control import DisplayController


def adb_shell(*args):
    subprocess.run(["adb", "shell", *args], capture_output=True)


class CameraReader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_evt = threading.Event()
        self.chunks = []
        self.bytes_total = 0
        self.connected = False

    def stop(self): self.stop_evt.set()

    def run(self):
        try:
            s = socket.create_connection(("127.0.0.1", 7777), timeout=5)
            self.connected = True
            s.settimeout(1.0)
            while not self.stop_evt.is_set():
                try:
                    d = s.recv(65536)
                except socket.timeout:
                    continue
                if not d: break
                self.bytes_total += len(d)
                self.chunks.append((time.perf_counter(), len(d)))
            s.close()
        except Exception as e:
            print(f"cam: {e}")


def main():
    subprocess.run(["adb", "forward", "tcp:8888", "tcp:8888"], check=True)
    subprocess.run(["adb", "forward", "tcp:7777", "tcp:7777"], check=True)
    subprocess.run(["adb", "logcat", "-c"], check=False)

    duration_s = 60.0
    print(f"Combined stress test — {duration_s:.0f} seconds")
    print()

    dc = DisplayController()
    dc.sync_clocks()
    cam = CameraReader()
    cam.start()
    time.sleep(0.7)
    if not cam.connected:
        print("FAIL: camera could not connect")
        return 1

    # Set up display
    dc.clear_highlights()
    dc.load_color(0, 230, 230, 230)
    dc.show(0)

    t0 = time.perf_counter()
    next_anim_t = t0
    next_ping_t = t0 + 0.05
    next_show_t = t0 + 1.0
    next_touch_t = t0 + 5.0

    ping_lats = []
    show_errs = []
    anim_count = 0
    touch_count = 0

    while time.perf_counter() - t0 < duration_s:
        now = time.perf_counter()

        # Queue a new animation every 3s
        if now >= next_anim_t:
            t_start = time.perf_counter_ns() + 100_000_000
            t_end = t_start + 2_800_000_000  # 2.8s
            dc.clear_highlights(target_pc_ns=t_start)
            dc.add_highlight_anim(0.05, 0.3, 0.95, 0.7,
                                  t_start_pc_ns=t_start, t_end_pc_ns=t_end,
                                  color=(255, 220, 0, 80))
            anim_count += 1
            next_anim_t = now + 3.0

        # PING every 100 ms
        if now >= next_ping_t:
            t1 = time.perf_counter_ns()
            dc._send(1, struct.pack("<q", t1))
            cmd, payload = dc._recv()
            t4 = time.perf_counter_ns()
            ping_lats.append((t4 - t1) / 1e6)
            next_ping_t = now + 0.1

        # SHOW timing check every 2s (uses static color so doesn't disrupt anim)
        if now >= next_show_t:
            target = time.perf_counter_ns() + 100_000_000
            t_actual = dc.show_at(0, target)
            show_errs.append((t_actual - target) / 1000)
            next_show_t = now + 2.0

        # Simulate menu tap every 15s
        if now >= next_touch_t:
            # tap MENU button, wait, tap Close
            adb_shell("input", "tap", "60", "30")
            time.sleep(0.5)
            adb_shell("input", "tap", "1680", "1050")  # Close
            touch_count += 1
            next_touch_t = now + 15.0

        time.sleep(0.01)

    cam.stop(); cam.join(timeout=3)

    # Analyze
    t_end_test = time.perf_counter()
    total = t_end_test - t0

    cam_kbps = (cam.bytes_total / 1024) / total
    cam_gaps = [cam.chunks[i+1][0] - cam.chunks[i][0]
                for i in range(len(cam.chunks)-1)]
    cam_max_gap = max(cam_gaps) if cam_gaps else 0
    cam_p99_gap = (sorted(cam_gaps)[int(len(cam_gaps)*0.99)]
                    if cam_gaps else 0)

    print(f"=== RESULTS ({total:.0f} seconds) ===")
    print(f"Animations queued:   {anim_count}")
    print(f"Touch events:        {touch_count}")
    print(f"PINGs sent:          {len(ping_lats)}")
    print(f"SHOWs measured:      {len(show_errs)}")
    print()
    print(f"Camera:")
    print(f"  bytes:             {cam.bytes_total/1024:.0f} KB "
          f"({cam_kbps:.0f} KB/s)")
    print(f"  chunks:            {len(cam.chunks)}")
    print(f"  inter-chunk gap:   median={statistics.median(cam_gaps)*1000:.1f}ms "
          f"p99={cam_p99_gap*1000:.0f}ms max={cam_max_gap*1000:.0f}ms")
    print()
    print(f"PING latency (ms):")
    print(f"  mean={statistics.mean(ping_lats):.2f} "
          f"median={statistics.median(ping_lats):.2f} "
          f"p99={sorted(ping_lats)[int(len(ping_lats)*0.99)]:.2f} "
          f"max={max(ping_lats):.2f}")
    print()
    print(f"SHOW timing error (us, VSYNC-quantized, expected |x| < 17000):")
    print(f"  mean={statistics.mean(show_errs):.0f} "
          f"|mean|={statistics.mean(abs(e) for e in show_errs):.0f} "
          f"min={min(show_errs):+.0f} max={max(show_errs):+.0f}")

    final_fp = dc.get_info()[2]
    print()
    print(f"Final GL frame period: {final_fp/1e6:.3f} ms (expect 16.65–16.75)")

    dc.clear_highlights()
    dc.close()

    # Pass criteria
    fails = []
    if cam_max_gap > 0.5:
        fails.append(f"camera max gap {cam_max_gap*1000:.0f}ms > 500ms")
    if cam_kbps < 100:
        fails.append(f"camera throughput {cam_kbps:.0f} KB/s too low")
    if max(ping_lats) > 100:
        fails.append(f"PING max {max(ping_lats):.0f}ms > 100ms")
    if max(abs(e) for e in show_errs) > 20000:
        fails.append(f"SHOW worst error {max(abs(e) for e in show_errs):.0f}us > 20000")
    if not (16_000_000 < final_fp < 17_500_000):
        fails.append(f"frame period {final_fp/1e6:.3f}ms out of range")

    # Scan logcat for real errors
    out = subprocess.run(["adb", "logcat", "-d"], capture_output=True,
                         text=True).stdout
    BENIGN = ("Client gone", "client disconnected", "Display client gone",
              "cam write failed", "Broken pipe", "SocketOutputStream",
              "SocketException", "at java", "at com.example")
    err_lines = []
    for line in out.splitlines():
        if "BitmapDisplay" in line or "firehello" in line:
            if len(line) > 32 and line[31] in ("E", "W"):
                if not any(b in line for b in BENIGN):
                    err_lines.append(line)
        elif any(s in line for s in ("ANR ", "FATAL EXCEPTION",
                                      "OutOfMemoryError")):
            err_lines.append(line)
    print()
    print(f"Non-benign logcat lines: {len(err_lines)}")
    for line in err_lines[:10]:
        print(f"  {line[:140]}")

    print()
    if fails or err_lines:
        for f in fails: print(f"  ✗ {f}")
        return 1
    print("✓ ALL CRITERIA MET — no glitches detected over 60-second combined stress")
    return 0


if __name__ == "__main__":
    sys.exit(main())

---
name: project-fire-tablet
description: "Android app at /home/phil/ClaudeFireTablet — VSYNC-accurate fullscreen bitmap display + simultaneous front-camera H.264 streaming, controlled from PC via binary TCP protocol"
metadata: 
  node_type: memory
  type: project
  originSessionId: 0128266c-c532-425f-a6ed-d8b6e53fe567
---

User's project at `/home/phil/ClaudeFireTablet` does interactive Android development on a Fire HD 10 Plus 11th gen connected via USB. Package: `com.example.firehello`. **Current build v0.3** — fullscreen bitmap display with sub-ms timing coordination, simultaneous camera streaming confirmed working.

## What it does

**Display (port 8888):** PC sends bitmaps/colors to tablet via binary protocol. Tablet renders fullscreen via OpenGL ES 2.0 with VSYNC-accurate timing. NTP-like clock sync achieves <1ms PC-tablet offset. Transitions reported at exact VSYNC boundary timestamps.

**Camera (port 7777):** Front camera → Camera2 → MediaCodec H.264 (640×480, 30fps, 2Mbps) → TCP stream. Runs simultaneously with display. PC can view via `stream.py` or raw socket.

**Use case:** Musical scores displayed on tablet timed exactly to music on PC. Camera streams performer for observation.

## File layout
```
/home/phil/ClaudeFireTablet/
├── AndroidManifest.xml         # v0.3 — BitmapDisplayActivity is launcher
├── build.sh                    # gradle-free build, preserves keystore
├── display_control.py          # PC-side DisplayController library + demo
├── stream.py                   # PC-side camera viewer (5s buffer → ffplay)
├── CLAUDE.md                   # standing orders, project docs
├── restore-memories.sh         # restore Claude memories after clone
├── .claude-memory/             # tracked copies of Claude Code memory files
├── res/layout/main.xml
├── src/com/example/firehello/
│   ├── BitmapDisplayActivity.java  # main: GL display + camera + TCP servers
│   └── MainActivity.java           # legacy camera-only (still launchable)
└── build/
    ├── app-debug.apk
    └── debug.keystore          # preserved across builds
```

## Run loop
```bash
./build.sh && adb install -r build/app-debug.apk
adb shell am start -n com.example.firehello/.BitmapDisplayActivity
adb forward tcp:8888 tcp:8888   # display control
adb forward tcp:7777 tcp:7777   # camera stream
python3 display_control.py --demo   # test timing
python3 stream.py                    # view camera
```

## Display protocol (binary, little-endian, port 8888)
Message: `[1B cmd][4B LE payload_len][payload]`

| Cmd | Name | Payload | Response |
|-----|------|---------|----------|
| 0x01 | PING | 8B pc_time_ns | PONG: 8B echo + 8B tablet_time |
| 0x02 | LOAD_RGBA | 4B id + 4B w + 4B h + RGBA data | LOADED |
| 0x03 | SHOW | 4B id + 8B target_tablet_ns (0=now) | SHOWN: 4B id + 8B sched + 8B actual |
| 0x04 | LOAD_COLOR | 4B id + R + G + B + A | LOADED |
| 0x05 | INFO | (none) | 4B width + 4B height + 8B frame_period_ns |

## Timing characteristics
- Frame period: 16.696 ms (59.9 Hz measured)
- Display pipeline: 2 frames (draw → SurfaceFlinger → scanout)
- VSYNC quantization: appearance locked to nearest VSYNC boundary (max error: ±16.7ms from arbitrary target, but reported time is sub-ms accurate)
- Clock sync RTT over USB: ~1.2ms, offset accuracy: <0.7ms

## Known gotchas
- **Keystore**: `build.sh` now preserves keystore. If you get sig mismatch: `adb uninstall com.example.firehello` then `adb install`.
- **Camera permission**: must be granted on first run. Use `adb shell pm grant com.example.firehello android.permission.CAMERA`.
- **Camera releases on background**: activity must stay in foreground.
- **VSYNC phase matters**: for musical sync, align targets to VSYNC boundaries for minimal error.

See [[project-github-repo]], [[reference-android-sdk]], [[user-fire-tablet]], [[feedback-auto-commit]]

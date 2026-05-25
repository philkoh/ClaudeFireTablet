#!/usr/bin/env python3
"""
Generate a test piano score image and simulate playback on the tablet
with a yellow highlight sweeping across in time with the music.

Usage:
    adb forward tcp:8888 tcp:8888
    python3 score_demo.py
"""

import struct
import time
from PIL import Image, ImageDraw, ImageFont
from display_control import DisplayController

# ─── Score layout constants ───────────────────────────────────────────

WIDTH, HEIGHT = 1920, 1200
BG_COLOR = (255, 255, 252)  # warm white
STAFF_COLOR = (40, 40, 40)
NOTE_COLOR = (20, 20, 20)
BAR_COLOR = (40, 40, 40)
TITLE_COLOR = (30, 30, 30)

LINE_SP = 22  # pixels between staff lines
LEFT_MARGIN = 180  # space for clef + time sig
RIGHT_MARGIN = 50
TOP_MARGIN = 180  # space for title
TREBLE_TOP_Y = 340  # y of top treble staff line
BASS_TOP_Y = TREBLE_TOP_Y + 5 * LINE_SP + 100  # gap between staves

NUM_BARS = 8
BEATS_PER_BAR = 4

MUSIC_WIDTH = WIDTH - LEFT_MARGIN - RIGHT_MARGIN
BAR_WIDTH = MUSIC_WIDTH / NUM_BARS
BEAT_WIDTH = BAR_WIDTH / BEATS_PER_BAR

# ─── Music data: Ode to Joy (Beethoven), C major, 4/4 ────────────────

MELODY = [
    # Bar 1-4
    'E4', 'E4', 'F4', 'G4',
    'G4', 'F4', 'E4', 'D4',
    'C4', 'C4', 'D4', 'E4',
    'E4', 'D4', 'D4', None,
    # Bar 5-8
    'E4', 'E4', 'F4', 'G4',
    'G4', 'F4', 'E4', 'D4',
    'C4', 'C4', 'D4', 'E4',
    'D4', 'C4', 'C4', None,
]

BASS_NOTES = ['C3', 'G2', 'C3', 'G2', 'C3', 'G2', 'C3', 'C3']

# Map note names to staff positions (0 = bottom line, each step = 0.5 spacing)
TREBLE_NOTES = {
    'C4': -1, 'D4': -0.5, 'E4': 0, 'F4': 0.5, 'G4': 1,
    'A4': 1.5, 'B4': 2, 'C5': 2.5, 'D5': 3, 'E5': 3.5, 'F5': 4,
}
BASS_NOTES_POS = {
    'G2': 0, 'A2': 0.5, 'B2': 1, 'C3': 1.5, 'D3': 2,
    'E3': 2.5, 'F3': 3, 'G3': 3.5, 'A3': 4,
}


def note_y_treble(note):
    pos = TREBLE_NOTES[note]
    return int(TREBLE_TOP_Y + (4 - pos) * LINE_SP)


def note_y_bass(note):
    pos = BASS_NOTES_POS[note]
    return int(BASS_TOP_Y + (4 - pos) * LINE_SP)


def beat_x(bar, beat):
    return int(LEFT_MARGIN + bar * BAR_WIDTH + (beat + 0.5) * BEAT_WIDTH)


# ─── Drawing helpers ──────────────────────────────────────────────────

def draw_staff_lines(draw, top_y):
    for i in range(5):
        y = top_y + i * LINE_SP
        draw.line([(LEFT_MARGIN - 40, y), (WIDTH - RIGHT_MARGIN, y)],
                  fill=STAFF_COLOR, width=2)


def draw_barlines(draw):
    for bar in range(NUM_BARS + 1):
        x = int(LEFT_MARGIN + bar * BAR_WIDTH)
        y_top = TREBLE_TOP_Y
        y_bot = BASS_TOP_Y + 4 * LINE_SP
        w = 3 if bar == 0 or bar == NUM_BARS else 2
        draw.line([(x, y_top), (x, y_bot)], fill=BAR_COLOR, width=w)
    # double bar at end
    x = int(LEFT_MARGIN + NUM_BARS * BAR_WIDTH)
    draw.line([(x - 6, TREBLE_TOP_Y), (x - 6, BASS_TOP_Y + 4 * LINE_SP)],
              fill=BAR_COLOR, width=2)
    draw.line([(x, TREBLE_TOP_Y), (x, BASS_TOP_Y + 4 * LINE_SP)],
              fill=BAR_COLOR, width=4)


def draw_brace(draw):
    x = LEFT_MARGIN - 45
    y_top = TREBLE_TOP_Y
    y_bot = BASS_TOP_Y + 4 * LINE_SP
    mid = (y_top + y_bot) // 2
    for y in range(y_top, y_bot + 1):
        dist = abs(y - mid) / (y_bot - y_top) * 2
        offset = int(8 * (1 - dist * dist))
        draw.line([(x - offset, y), (x - offset + 3, y)], fill=STAFF_COLOR)


def draw_treble_clef(draw, font_large):
    x, y = LEFT_MARGIN - 25, TREBLE_TOP_Y - 10
    # Simplified treble clef using a large "G" styled rendering
    # Draw a vertical line with curves suggesting the clef shape
    cy = TREBLE_TOP_Y + 2 * LINE_SP  # center on B4 line
    draw.ellipse([x - 8, cy - 30, x + 18, cy + 30], outline=NOTE_COLOR, width=3)
    draw.ellipse([x - 2, cy + 5, x + 12, cy + 35], outline=NOTE_COLOR, width=3)
    draw.line([(x + 8, cy - 40), (x + 8, cy + 55)], fill=NOTE_COLOR, width=3)


def draw_bass_clef(draw, font_large):
    x = LEFT_MARGIN - 22
    cy = BASS_TOP_Y + 1.5 * LINE_SP
    # Bass clef: C-shaped curve with two dots
    draw.arc([x - 5, int(cy - 22), x + 25, int(cy + 22)],
             start=270, end=90, fill=NOTE_COLOR, width=3)
    dot_x = x + 28
    dot_y1 = int(BASS_TOP_Y + 1 * LINE_SP)
    dot_y2 = int(BASS_TOP_Y + 2 * LINE_SP)
    draw.ellipse([dot_x, dot_y1 - 4, dot_x + 8, dot_y1 + 4], fill=NOTE_COLOR)
    draw.ellipse([dot_x, dot_y2 - 4, dot_x + 8, dot_y2 + 4], fill=NOTE_COLOR)


def draw_time_sig(draw, font):
    x = LEFT_MARGIN + 15
    draw.text((x, TREBLE_TOP_Y + LINE_SP // 2), "4", font=font, fill=NOTE_COLOR)
    draw.text((x, TREBLE_TOP_Y + 2 * LINE_SP + LINE_SP // 2), "4",
              font=font, fill=NOTE_COLOR)
    draw.text((x, BASS_TOP_Y + LINE_SP // 2), "4", font=font, fill=NOTE_COLOR)
    draw.text((x, BASS_TOP_Y + 2 * LINE_SP + LINE_SP // 2), "4",
              font=font, fill=NOTE_COLOR)


def draw_note(draw, x, y, filled=True, stem_up=True):
    # Note head (filled ellipse)
    rx, ry = 9, 7
    if filled:
        draw.ellipse([x - rx, y - ry, x + rx, y + ry], fill=NOTE_COLOR)
    else:
        draw.ellipse([x - rx, y - ry, x + rx, y + ry],
                     outline=NOTE_COLOR, width=2)
    # Stem
    stem_len = int(3 * LINE_SP)
    if stem_up:
        draw.line([(x + rx - 1, y), (x + rx - 1, y - stem_len)],
                  fill=NOTE_COLOR, width=2)
    else:
        draw.line([(x - rx + 1, y), (x - rx + 1, y + stem_len)],
                  fill=NOTE_COLOR, width=2)


def draw_whole_note(draw, x, y):
    rx, ry = 11, 8
    draw.ellipse([x - rx, y - ry, x + rx, y + ry],
                 outline=NOTE_COLOR, width=3)
    # inner hollow
    draw.ellipse([x - rx + 4, y - ry + 3, x + rx - 4, y + ry - 3],
                 fill=BG_COLOR)


def draw_ledger_lines(draw, x, y, staff_top, staff_bot):
    if y < staff_top:
        for ly in range(staff_top - LINE_SP, y - LINE_SP // 2, -LINE_SP):
            draw.line([(x - 14, ly), (x + 14, ly)], fill=STAFF_COLOR, width=2)
    elif y > staff_bot:
        for ly in range(staff_bot + LINE_SP, y + LINE_SP // 2, LINE_SP):
            draw.line([(x - 14, ly), (x + 14, ly)], fill=STAFF_COLOR, width=2)


def draw_quarter_rest(draw, x, staff_top):
    y = staff_top + 2 * LINE_SP
    # Simplified rest: zigzag shape
    pts = [(x, y - 20), (x + 8, y - 10), (x - 4, y),
           (x + 8, y + 10), (x, y + 20)]
    draw.line(pts, fill=NOTE_COLOR, width=3)


# ─── Generate the score image ────────────────────────────────────────

def generate_score():
    img = Image.new('RGBA', (WIDTH, HEIGHT), BG_COLOR + (255,))
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", 48)
        font_subtitle = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", 28)
        font_timesig = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
    except OSError:
        font_title = ImageFont.load_default()
        font_subtitle = font_title
        font_timesig = font_title

    # Title
    draw.text((WIDTH // 2, 40), "Ode to Joy", font=font_title,
              fill=TITLE_COLOR, anchor="mt")
    draw.text((WIDTH // 2, 100), "Ludwig van Beethoven",
              font=font_subtitle, fill=(100, 100, 100), anchor="mt")
    draw.text((WIDTH // 2, 140), "Moderato ♩= 100",
              font=font_subtitle, fill=(80, 80, 80), anchor="mt")

    # Staff lines
    draw_staff_lines(draw, TREBLE_TOP_Y)
    draw_staff_lines(draw, BASS_TOP_Y)

    # Brace, barlines, clefs
    draw_brace(draw)
    draw_barlines(draw)
    draw_treble_clef(draw, font_timesig)
    draw_bass_clef(draw, font_timesig)
    draw_time_sig(draw, font_timesig)

    # Melody notes
    treble_bot = TREBLE_TOP_Y + 4 * LINE_SP
    for i, note in enumerate(MELODY):
        bar = i // BEATS_PER_BAR
        beat = i % BEATS_PER_BAR
        x = beat_x(bar, beat)
        if note is None:
            draw_quarter_rest(draw, x, TREBLE_TOP_Y)
        else:
            y = note_y_treble(note)
            stem_up = y > TREBLE_TOP_Y + 2 * LINE_SP
            draw_note(draw, x, y, filled=True, stem_up=stem_up)
            draw_ledger_lines(draw, x, y, TREBLE_TOP_Y, treble_bot)

    # Bass notes (whole notes, one per bar)
    for bar, note in enumerate(BASS_NOTES):
        x = int(LEFT_MARGIN + bar * BAR_WIDTH + BAR_WIDTH * 0.5)
        y = note_y_bass(note)
        draw_whole_note(draw, x, y)

    return img


# ─── Playback simulation ─────────────────────────────────────────────

def simulate_playback():
    print("Generating score image...")
    img = generate_score()
    img.save("/tmp/score_ode_to_joy.png")
    rgba_data = img.tobytes()
    print(f"  Score image: {WIDTH}x{HEIGHT}, {len(rgba_data)} bytes")

    print("Connecting to tablet...")
    dc = DisplayController()
    w, h, fp = dc.get_info()
    print(f"  Screen: {w}x{h}")

    print("Synchronizing clocks...")
    offset, rtt = dc.sync_clocks()
    print(f"  RTT: {rtt/1e6:.2f} ms")

    print("Uploading score image...")
    dc.load_rgba(0, WIDTH, HEIGHT, rgba_data)
    dc.show(0)
    print("  Score displayed!")
    time.sleep(1.0)

    # Playback parameters
    bpm = 100  # moderate tempo
    beat_duration_ns = int(60 / bpm * 1_000_000_000)  # ns per quarter note
    total_beats = NUM_BARS * BEATS_PER_BAR

    # Highlight boundaries (normalized)
    x_start = LEFT_MARGIN / WIDTH
    x_end = (LEFT_MARGIN + NUM_BARS * BAR_WIDTH) / WIDTH
    hl_top = (TREBLE_TOP_Y - 15) / HEIGHT
    hl_bot = (BASS_TOP_Y + 4 * LINE_SP + 15) / HEIGHT

    total_duration = total_beats * beat_duration_ns
    start_time = time.perf_counter_ns() + 500_000_000  # start in 500ms
    end_time = start_time + total_duration

    print(f"\nPlaying at {bpm} BPM ({beat_duration_ns/1e6:.0f} ms/beat)...")
    print(f"  Total duration: {total_duration / 1e9:.1f} seconds")
    print()

    # One animation command — tablet interpolates at 60fps, zero USB traffic
    dc.add_highlight_anim(
        x_start, hl_top,
        x_end, hl_bot,
        t_start_pc_ns=start_time,
        t_end_pc_ns=end_time,
        color=(255, 220, 0, 60)
    )
    print("  Single animation queued — tablet renders pixel-smooth sweep")
    bar_names = ["bar 1: E E F G", "bar 2: G F E D",
                 "bar 3: C C D E", "bar 4: E D D .",
                 "bar 5: E E F G", "bar 6: G F E D",
                 "bar 7: C C D E", "bar 8: D C C ."]

    print("  Highlight sweeping across score...")
    current_bar = -1
    while time.perf_counter_ns() < end_time:
        elapsed = time.perf_counter_ns() - start_time
        if elapsed < 0:
            time.sleep(0.05)
            continue
        bar = int(elapsed / beat_duration_ns) // BEATS_PER_BAR
        if bar != current_bar and bar < NUM_BARS:
            current_bar = bar
            print(f"    ♪ {bar_names[bar]}")
        time.sleep(0.1)

    time.sleep(0.5)

    # Final state: full highlight
    print("\n  Playback complete!")
    time.sleep(2.0)

    # Clean up
    dc.clear_highlights()
    dc.close()
    print("Done.")


if __name__ == "__main__":
    simulate_playback()

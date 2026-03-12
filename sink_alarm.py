#!/usr/bin/env python3
"""
SinkAlarm - An alarm that only stops when you scan a sink (or type "I'm awake" after 3 mins)
Requirements: pip install anthropic opencv-python pillow pygame schedule
"""

import anthropic
import base64
import threading
import time
import datetime
import sys
import os
import io

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    print("⚠️  opencv-python not installed. Camera scanning disabled.")
    print("   Install with: pip install opencv-python")

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import pygame
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False
    print("⚠️  pygame not installed. Using system beep fallback.")
    print("   Install with: pip install pygame")


# ── ASCII art banner ────────────────────────────────────────────────────────
BANNER = r"""
  ____  _       _        _    _
 / ___|(_)_ __ | | __   / \  | | __ _ _ __ _ __ ___
 \___ \| | '_ \| |/ /  / _ \ | |/ _` | '__| '_ ` _ \
  ___) | | | | |   <  / ___ \| | (_| | |  | | | | | |
 |____/|_|_| |_|_|\_\/_/   \_\_|\__,_|_|  |_| |_| |_|

  Wake up. Walk to the sink. Prove you're awake.
"""

DISMISS_GRACE_SECONDS = 180  # 3 minutes before "I'm awake" is allowed


# ── Audio ────────────────────────────────────────────────────────────────────
def generate_alarm_tone():
    """Generate a simple alarm sound using pygame."""
    if not PYGAME_AVAILABLE:
        return None
    pygame.mixer.init(frequency=44100, size=-16, channels=1, buffer=512)
    sample_rate = 44100
    duration = 0.5  # seconds per beep
    samples = int(sample_rate * duration)
    import array, math
    buf = array.array('h', [0] * samples)
    for i in range(samples):
        t = i / sample_rate
        # Two-tone alarm: 880Hz and 1100Hz alternating
        freq = 880 if (i // (sample_rate // 4)) % 2 == 0 else 1100
        val = int(32767 * 0.5 * math.sin(2 * math.pi * freq * t))
        # Apply fade in/out to avoid clicks
        fade = min(i, samples - i) / (samples * 0.1)
        fade = min(fade, 1.0)
        buf[i] = int(val * fade)
    sound = pygame.sndarray.make_sound(__import__('numpy').array(buf, dtype='int16') if False else buf)
    return pygame.mixer.Sound(buffer=bytes(buf))


class AlarmSound:
    def __init__(self):
        self.running = False
        self.thread = None
        self._sound = None
        if PYGAME_AVAILABLE:
            try:
                self._sound = generate_alarm_tone()
            except Exception as e:
                print(f"  Audio init warning: {e}")

    def _beep_loop(self):
        while self.running:
            if PYGAME_AVAILABLE and self._sound:
                try:
                    self._sound.play()
                    time.sleep(0.6)
                    self._sound.stop()
                    time.sleep(0.3)
                except Exception:
                    self._fallback_beep()
            else:
                self._fallback_beep()

    def _fallback_beep(self):
        print('\a', end='', flush=True)
        time.sleep(1)

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._beep_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if PYGAME_AVAILABLE and self._sound:
            try:
                self._sound.stop()
            except Exception:
                pass


# ── Vision: Sink Detection ───────────────────────────────────────────────────
def capture_image_from_camera():
    """Capture a single frame from the webcam. Returns JPEG bytes or None."""
    if not CV2_AVAILABLE:
        return None
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("  ⚠️  Could not open camera.")
        return None
    time.sleep(0.5)  # warm up
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return None
    _, buf = cv2.imencode('.jpg', frame)
    return buf.tobytes()


def is_sink_in_image(image_bytes: bytes) -> tuple[bool, str]:
    """Use Claude claude-sonnet-4-20250514 vision to check if image contains a sink."""
    client = anthropic.Anthropic()
    b64 = base64.standard_b64encode(image_bytes).decode('utf-8')
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    }
                },
                {
                    "type": "text",
                    "text": (
                        "Does this image clearly show a sink (bathroom sink, kitchen sink, etc.)? "
                        "Answer ONLY with: YES if a sink is clearly visible, NO if not. "
                        "Then on the next line add a very brief reason (max 10 words)."
                    )
                }
            ]
        }]
    )
    text = response.content[0].text.strip()
    lines = text.split('\n', 1)
    detected = lines[0].strip().upper().startswith('YES')
    reason = lines[1].strip() if len(lines) > 1 else text
    return detected, reason


# ── Alarm Logic ──────────────────────────────────────────────────────────────
class SinkAlarm:
    def __init__(self):
        self.alarm_time: datetime.time | None = None
        self.alarm_active = False
        self.alarm_start: datetime.datetime | None = None
        self.sound = AlarmSound()
        self.dismissed = False

    def set_alarm(self, alarm_time: datetime.time):
        self.alarm_time = alarm_time
        print(f"\n  ✅  Alarm set for {alarm_time.strftime('%H:%M')}")
        print("  💡  To dismiss: walk to a sink and press ENTER to scan it.")
        print(f"  💡  After {DISMISS_GRACE_SECONDS//60} minutes, you may type \"I'm awake\" instead.\n")

    def _wait_for_alarm(self):
        print("  ⏳  Waiting for alarm time...")
        while True:
            now = datetime.datetime.now().time()
            # Compare hours and minutes only
            if now.hour == self.alarm_time.hour and now.minute == self.alarm_time.minute:
                self._trigger_alarm()
                break
            time.sleep(10)

    def _trigger_alarm(self):
        self.alarm_active = True
        self.alarm_start = datetime.datetime.now()
        self.sound.start()
        print("\n" + "🚨 " * 20)
        print("  🔔  WAKE UP! ALARM RINGING! 🔔")
        print("🚨 " * 20)
        print(f"\n  ⏰  Alarm triggered at {self.alarm_start.strftime('%H:%M:%S')}")
        self._dismissal_loop()

    def _dismissal_loop(self):
        print("\n  👉  Press ENTER to scan your sink and dismiss the alarm.")
        if not CV2_AVAILABLE:
            print("  ⚠️  Camera unavailable — you can type \"I'm awake\" immediately.\n")

        while self.alarm_active and not self.dismissed:
            elapsed = (datetime.datetime.now() - self.alarm_start).total_seconds()
            grace_unlocked = elapsed >= DISMISS_GRACE_SECONDS or not CV2_AVAILABLE

            if grace_unlocked and not CV2_AVAILABLE:
                prompt = '  > Type "I\'m awake" to dismiss (no camera): '
            elif grace_unlocked:
                prompt = '  > Press ENTER to scan sink  OR  type "I\'m awake" to dismiss: '
            else:
                mins_left = int((DISMISS_GRACE_SECONDS - elapsed) / 60) + 1
                prompt = f'  > Press ENTER to scan sink (text dismiss unlocks in ~{mins_left}m): '

            try:
                user_input = input(prompt).strip()
            except EOFError:
                time.sleep(2)
                continue

            if user_input.lower() in ("i'm awake", "im awake", "i am awake"):
                if grace_unlocked:
                    print("\n  😴  Text dismissal accepted. Get up properly next time!")
                    self._dismiss("text")
                else:
                    remaining = int(DISMISS_GRACE_SECONDS - elapsed)
                    print(f"  ❌  Text dismissal not available yet. {remaining}s remaining. Go find a sink!")
            elif user_input == "" or user_input.lower() == "scan":
                self._attempt_scan()
            else:
                print('  ❓  Unknown input. Press ENTER to scan or type "I\'m awake".')

    def _attempt_scan(self):
        if not CV2_AVAILABLE:
            print("  ⚠️  Camera not available. Install opencv-python to use sink scanning.")
            return

        print("\n  📷  Point your camera at the sink and hold still...")
        image = capture_image_from_camera()
        if image is None:
            print("  ❌  Could not capture image. Try again.")
            return

        print("  🔍  Analyzing image with AI vision...")
        try:
            found, reason = is_sink_in_image(image)
        except Exception as e:
            print(f"  ❌  Vision check failed: {e}")
            print("  💡  Make sure ANTHROPIC_API_KEY is set.")
            return

        if found:
            print(f"\n  ✅  Sink confirmed! ({reason})")
            print("  🎉  ALARM DISMISSED — Good morning! Have a great day!")
            self._dismiss("sink_scan")
        else:
            print(f"\n  ❌  No sink detected. ({reason})")
            print("  🚿  Please point the camera at a sink and try again.\n")

    def _dismiss(self, method: str):
        self.alarm_active = False
        self.dismissed = True
        self.sound.stop()
        elapsed = (datetime.datetime.now() - self.alarm_start).total_seconds()
        print(f"\n  ⏱️  You were awake for {int(elapsed)}s before dismissing (method: {method}).")

    def run(self):
        print(BANNER)

        # Check for API key
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("  ⚠️  WARNING: ANTHROPIC_API_KEY not set.")
            print("  Sink scanning requires it. Set it with:")
            print("    export ANTHROPIC_API_KEY=your_key_here\n")

        # Get alarm time from user
        while True:
            raw = input("  Enter alarm time (HH:MM, 24h format, e.g. 07:30): ").strip()
            try:
                hour, minute = map(int, raw.split(':'))
                alarm_time = datetime.time(hour, minute)
                break
            except (ValueError, AttributeError):
                print("  ❌  Invalid format. Please use HH:MM (e.g. 07:30)")

        self.set_alarm(alarm_time)

        # Check if alarm time is in the past today → schedule for tomorrow
        now = datetime.datetime.now()
        alarm_dt = datetime.datetime.combine(now.date(), alarm_time)
        if alarm_dt <= now:
            alarm_dt += datetime.timedelta(days=1)
            print(f"  📅  That time has passed today — alarm set for tomorrow ({alarm_dt.strftime('%Y-%m-%d %H:%M')}).")

        wait_secs = (alarm_dt - now).total_seconds()
        print(f"  ⏳  Alarm will ring in {int(wait_secs // 3600)}h {int((wait_secs % 3600) // 60)}m {int(wait_secs % 60)}s")
        print("  (Press Ctrl+C to cancel)\n")

        try:
            self._wait_for_alarm()
        except KeyboardInterrupt:
            self.sound.stop()
            print("\n\n  🛑  Alarm cancelled.")
            sys.exit(0)


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Quick test mode: --test triggers alarm immediately
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        print(BANNER)
        print("  🧪  TEST MODE: Triggering alarm immediately...\n")
        alarm = SinkAlarm()
        alarm.alarm_start = datetime.datetime.now()
        alarm.alarm_active = True
        alarm.sound.start()
        alarm._dismissal_loop()
    else:
        SinkAlarm().run()

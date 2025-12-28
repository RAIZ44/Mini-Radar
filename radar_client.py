"""
PC-side radar visualizer (Pygame) for Raspberry Pi servo + ultrasonic streamer.

What it does
------------
- Listens for UDP broadcast packets on PORT (default: 5005).
- Each packet is a CSV line: epoch_seconds, servo_angle_degrees, distance_cm_or_nan
- Draws a semi-circular "radar" (0..180°) with:
    - range rings (25/50/75% of MAX_CM)
    - angle spokes (every 15°, labels every 30°)
    - a sweep arm showing the latest servo angle
    - fading blips for recent distance measurements

Networking assumptions
----------------------
- The Pi sends packets to 255.255.255.255:PORT (broadcast).
- This PC should be on the same LAN/subnet and allow inbound UDP on PORT.
- If you see nothing:
    - check firewall rules
    - confirm both devices are on the same network
    - confirm the Pi is actually broadcasting
"""

import math
import socket
import time
from collections import deque

import pygame

# -----------------------------
# UDP stream configuration
# -----------------------------
PORT = 5005      # Must match the Pi sender's PORT
MAX_CM = 200.0   # Max range used for scaling/labels (should match Pi-side MAX_CM)

# -----------------------------
# Window / rendering settings
# -----------------------------
WIDTH, HEIGHT = 900, 650
FPS = 60

# Colors (RGB)
BG = (0, 0, 0)
GREEN = (0, 255, 80)
DIM_GREEN = (0, 120, 40)

# Radar geometry:
# - Semi-circle spans 0..180 degrees along the bottom of the window
# - Center is near the bottom so the arc is visible above it
RADAR_RADIUS = 280
CENTER = (WIDTH // 2, HEIGHT - 40)

# -----------------------------
# Blip history / fading
# -----------------------------
BLIP_LIFETIME_S = 2.0  # how long a measurement stays visible
MAX_BLIPS = 600        # cap to prevent unbounded memory growth


def polar_to_xy(center, radius_px, display_angle_deg):
    """
    Convert polar coordinates to screen coordinates for a 0..180° radar.

    Coordinate system notes:
    - Pygame's +x is right, +y is down.
    - We want 0° at the left horizon, 180° at the right horizon, and 90° straight up.
      This matches a semi-circle arc sitting on the baseline through CENTER.
    """
    theta = math.radians(180.0 - display_angle_deg)
    x = center[0] + radius_px * math.cos(theta)
    y = center[1] - radius_px * math.sin(theta)
    return int(x), int(y)


def draw_grid(screen, font):
    """
    Draw the radar frame:
    - outer arc
    - inner range rings
    - angle spokes with labels
    - baseline diameter
    """
    cx, cy = CENTER

    # Outer boundary arc (full circle outline; only the top half is visible)
    pygame.draw.circle(screen, GREEN, CENTER, RADAR_RADIUS, 2)

    # Range rings at 25/50/75% of the max radius with distance labels
    for frac in (0.25, 0.5, 0.75):
        r = int(RADAR_RADIUS * frac)
        pygame.draw.circle(screen, DIM_GREEN, CENTER, r, 1)

        label_cm = int(MAX_CM * frac)
        txt = font.render(f"{label_cm} cm", True, DIM_GREEN)
        screen.blit(txt, (cx + 8, cy - r - 18))

    # Spokes every 15 degrees; brighter every 30 degrees
    for a in range(0, 181, 15):
        end = polar_to_xy(CENTER, RADAR_RADIUS, a)
        col = DIM_GREEN if a % 30 else GREEN
        pygame.draw.line(screen, col, CENTER, end, 1)

        # Angle labels every 30 degrees
        if a % 30 == 0:
            lx, ly = polar_to_xy(CENTER, RADAR_RADIUS + 18, a)
            t = font.render(str(a), True, DIM_GREEN)
            screen.blit(t, (lx - t.get_width() // 2, ly - t.get_height() // 2))

    # Baseline diameter (ground line)
    pygame.draw.line(screen, GREEN, (cx - RADAR_RADIUS, cy), (cx + RADAR_RADIUS, cy), 2)


def draw_sweep(screen, display_angle_deg):
    """
    Draw the current sweep arm plus a simple trailing effect.

    The trailing lines are just slightly older angles behind the main sweep
    to give motion feel without requiring a full alpha-blended surface.
    """
    end = polar_to_xy(CENTER, RADAR_RADIUS, display_angle_deg)
    pygame.draw.line(screen, GREEN, CENTER, end, 3)

    # Trail behind the sweep (dimmer, slightly offset)
    for k in range(1, 10):
        a2 = max(0, min(180, display_angle_deg - k * 2))
        e2 = polar_to_xy(CENTER, RADAR_RADIUS, a2)
        pygame.draw.line(screen, DIM_GREEN, CENTER, e2, 1)


def draw_blips(screen, blips, now):
    """
    Draw measurement points (blips) with time-based fade.

    Each blip is (timestamp, display_angle_deg, distance_cm).
    Newer blips are brighter; older blips fade to black and are skipped.
    """
    for ts, a, d in blips:
        age = now - ts
        if age < 0 or age > BLIP_LIFETIME_S:
            continue

        # Linear fade 1.0 -> 0.0 across the lifetime
        fade = 1.0 - (age / BLIP_LIFETIME_S)

        # Dynamic color based on fade (keeps "radar green" feel)
        col = (0, int(255 * fade), int(80 * fade))

        # Convert distance in cm to pixels within the radar radius
        r_px = int((min(d, MAX_CM) / MAX_CM) * RADAR_RADIUS)
        x, y = polar_to_xy(CENTER, r_px, a)

        # Blip core + ring for readability
        pygame.draw.circle(screen, col, (x, y), 4)
        pygame.draw.circle(screen, col, (x, y), 10, 1)


def servo_to_display_angle(servo_angle):
    """
    Map Pi servo angles (-90..+90) into display angles (0..180).

    Pi-side sender sweeps -90..+90 degrees, which is convenient for servos.
    The radar display expects 0..180 degrees for a semi-circle.
    """
    return servo_angle + 90.0


def main():
    """Initialize Pygame, bind UDP socket, and run the render loop."""
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("Radar (GUI on PC, sensor on Pi)")
    clock = pygame.time.Clock()

    # Fonts (choose monospace for stable alignment)
    font = pygame.font.SysFont("consolas", 18)
    big = pygame.font.SysFont("consolas", 24, bold=True)

    # UDP listener:
    # - bind to all interfaces so it works on Wi-Fi/Ethernet without changing code
    # - non-blocking so the GUI stays responsive even if no packets arrive
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    sock.setblocking(False)

    # Blip history is bounded (deque auto-drops oldest entries)
    blips = deque(maxlen=MAX_BLIPS)

    # Track the latest received values so the sweep arm keeps moving even if
    # packets arrive intermittently.
    latest_angle = 0.0
    latest_dist = float("nan")

    running = True
    while running:
        # -----------------------------
        # Event handling (quit controls)
        # -----------------------------
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                running = False

        # -----------------------------
        # Drain all available UDP packets
        # -----------------------------
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except BlockingIOError:
                break

            # Parse expected CSV payload from Pi:
            # epoch_seconds, servo_angle, distance_cm_or_nan
            try:
                ts_s, angle_s, dist_s = data.decode("ascii").strip().split(",")
                latest_angle = float(angle_s)
                latest_dist = float(dist_s)

                # Only record blips when distance is valid (not NaN)
                if not math.isnan(latest_dist):
                    disp_angle = servo_to_display_angle(latest_angle)
                    blips.append((time.time(), disp_angle, latest_dist))
            except Exception:
                # Ignore malformed packets to keep UI robust
                pass

        # -----------------------------
        # Rendering
        # -----------------------------
        screen.fill(BG)
        draw_grid(screen, font)

        disp_angle = servo_to_display_angle(latest_angle)
        draw_blips(screen, blips, time.time())
        draw_sweep(screen, disp_angle)

        # HUD / labels
        title = big.render("RADAR SCAN (PC GUI)", True, GREEN)
        screen.blit(title, (20, 20))

        dist_str = "None" if math.isnan(latest_dist) else f"{latest_dist:6.1f} cm"
        info = font.render(
            f"Servo angle: {latest_angle:6.1f}°   Display: {disp_angle:6.1f}°   Distance: {dist_str}",
            True,
            GREEN,
        )
        screen.blit(info, (20, 55))

        hint = font.render("ESC or Q to quit", True, DIM_GREEN)
        screen.blit(hint, (20, HEIGHT - 30))

        pygame.display.flip()
        clock.tick(FPS)

    # Cleanup
    sock.close()
    pygame.quit()


if __name__ == "__main__":
    main()

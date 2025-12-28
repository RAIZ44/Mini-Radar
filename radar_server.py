"""
Raspberry Pi "radar" sweep: servo + ultrasonic distance + UDP broadcast stream.

What it does
------------
- Sweeps a servo from -90° to +90° and back in small steps.
- At each angle, reads distance from an HC-SR04-style ultrasonic sensor.
- Broadcasts a CSV line over UDP so a PC/receiver on the LAN can visualize it.

UDP message format (ASCII CSV)
------------------------------
epoch_seconds,servo_angle_degrees,distance_cm_or_nan

Example:
1700000000.123456,-42.00,37.50

Notes / Requirements
--------------------
- Uses gpiozero + pigpio for stable servo PWM:
    sudo apt install -y pigpio python3-gpiozero
    sudo systemctl enable --now pigpio
- Uses lgpio for fast ultrasonic timing.
- TRIG/ECHO pins assume 3.3V logic; many ultrasonic modules output 5V on ECHO.
  Use a voltage divider/level shifter to protect the Pi (very important).
"""

import math
import socket
import time
import threading

import lgpio as GPIO
from gpiozero import AngularServo, Device
from gpiozero.pins.pigpio import PiGPIOFactory

# -----------------------------
# GPIO pin configuration (BCM)
# -----------------------------
SERVO_GPIO = 18  # Hardware PWM-capable pin (recommended for servo control)
TRIG = 23        # Ultrasonic trigger output pin
ECHO = 24        # Ultrasonic echo input pin

# -----------------------------
# Servo calibration parameters
# -----------------------------
# Pulse widths may need tuning for your specific servo to reach full travel
# without buzzing or stalling at the end stops.
SERVO_MIN_PW = 0.0006  # seconds (600 µs)
SERVO_MAX_PW = 0.0023  # seconds (2300 µs)

# -----------------------------
# Ultrasonic measurement params
# -----------------------------
SPEED_CM_S = 34300.0       # Speed of sound at ~20°C in cm/s
ECHO_TIMEOUT_S = 0.020     # Fail-safe timeout waiting for echo edges (20 ms)
TRIGGER_PULSE_S = 0.000010 # Trigger pulse width (10 µs)
MAX_CM = 200.0             # Max distance we consider "valid" for this app

# -----------------------------
# UDP streaming configuration
# -----------------------------
HOST = "0.0.0.0"  # Informational only; receiver binds on its own machine
PORT = 5005       # Port the receiver listens on


class Ultrasonic:
    """
    HC-SR04-like ultrasonic range sensor driver using lgpio.

    Timing model:
    - TRIG goes high for ~10 µs to initiate a ping.
    - ECHO goes high for a duration proportional to round-trip travel time.
    - Distance = (duration * speed_of_sound) / 2.
    """

    def __init__(self, trig_pin: int, echo_pin: int):
        """
        Initialize GPIO chip and claim TRIG/ECHO pins.

        Args:
            trig_pin: BCM pin number used for TRIG (output).
            echo_pin: BCM pin number used for ECHO (input).
        """
        self.trig = trig_pin
        self.echo = echo_pin

        # Open GPIO chip 0 (the main GPIO controller on Raspberry Pi)
        self.h = GPIO.gpiochip_open(0)

        # Claim TRIG as output, ECHO as input
        GPIO.gpio_claim_output(self.h, self.trig)
        GPIO.gpio_claim_input(self.h, self.echo)

        # Ensure TRIG starts low and give sensor time to settle
        GPIO.gpio_write(self.h, self.trig, 0)
        time.sleep(0.05)

    def close(self):
        """Release the GPIO chip handle (safe to call multiple times)."""
        try:
            GPIO.gpiochip_close(self.h)
        except Exception:
            pass

    def get_distance_cm(self):
        """
        Measure distance once.

        Returns:
            float distance in centimeters, or None if the reading times out
            or is outside reasonable bounds.
        """
        # Ensure a clean low pulse before triggering
        GPIO.gpio_write(self.h, self.trig, 0)
        time.sleep(0.0002)

        # Send the trigger pulse (10 µs high)
        GPIO.gpio_write(self.h, self.trig, 1)
        time.sleep(TRIGGER_PULSE_S)
        GPIO.gpio_write(self.h, self.trig, 0)

        # Wait for ECHO to go high (start of the pulse)
        t0 = time.perf_counter()
        while GPIO.gpio_read(self.h, self.echo) == 0:
            if (time.perf_counter() - t0) > ECHO_TIMEOUT_S:
                return None

        pulse_start = time.perf_counter()

        # Wait for ECHO to go low (end of the pulse)
        while GPIO.gpio_read(self.h, self.echo) == 1:
            if (time.perf_counter() - pulse_start) > ECHO_TIMEOUT_S:
                return None

        pulse_end = time.perf_counter()
        dur = pulse_end - pulse_start

        # Convert round-trip time to one-way distance
        d_cm = (dur * SPEED_CM_S) / 2.0

        # Basic sanity check (sensor glitches can produce 0 or huge values)
        if d_cm <= 0 or d_cm > 1000:
            return None

        return d_cm


def main():
    """
    Entry point:
    - Configure pigpio-backed GPIO for servo PWM.
    - Sweep servo and read distance.
    - Broadcast readings over UDP for a LAN receiver to consume.
    """
    # Use pigpio for precise PWM timing (much smoother servo control)
    Device.pin_factory = PiGPIOFactory()

    # AngularServo maps -90..+90 degrees to configured pulse widths
    servo = AngularServo(
        SERVO_GPIO,
        min_pulse_width=SERVO_MIN_PW,
        max_pulse_width=SERVO_MAX_PW,
    )
    ultra = Ultrasonic(TRIG, ECHO)

    # Set an initial known position before starting the sweep
    servo.angle = -90.0

    # UDP socket for streaming measurements
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Sweep state
    angle = -90.0     # current servo angle in degrees
    direction = +1.0  # +1 sweeping toward +90, -1 sweeping back toward -90
    step = 2.0        # degrees per update (smaller = smoother, slower)
    settle = 0.015    # seconds to let servo physically settle before measuring

    print(f"Streaming UDP to {HOST}:{PORT} (bind on receiver).")
    print("Tip: Receiver should listen on PORT 5005. Ctrl+C to stop.")

    try:
        while True:
            # Move servo to the next angle and allow vibration/overshoot to settle
            servo.angle = angle
            time.sleep(settle)

            # Take a distance reading
            dist = ultra.get_distance_cm()

            # Apply application-level max range filtering (treat as "no reading")
            if dist is not None and dist > MAX_CM:
                dist = None

            # Encode payload as CSV.
            # Use NaN for "no reading" so the receiver can detect missing values.
            dist_out = dist if dist is not None else float("nan")
            msg = f"{time.time():.6f},{angle:.2f},{dist_out:.2f}".encode("ascii")

            # Broadcast to the LAN: receiver can be any machine on the subnet
            # listening on UDP port 5005 (no hard-coded IP required).
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(msg, ("255.255.255.255", PORT))

            # Advance sweep angle and reverse at endpoints
            angle += direction * step
            if angle >= 90.0:
                angle = 90.0
                direction = -1.0
            elif angle <= -90.0:
                angle = -90.0
                direction = +1.0

    except KeyboardInterrupt:
        # Graceful shutdown on Ctrl+C
        print("\nStopping...")
    finally:
        # Always release hardware resources
        try:
            servo.detach()  # stop sending PWM
        except Exception:
            pass
        ultra.close()
        sock.close()


if __name__ == "__main__":
    main()

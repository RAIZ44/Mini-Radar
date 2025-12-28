# Pi Radar Sweep (Headless Pi + PC GUI)

A simple “radar” scanner:

- **Raspberry Pi (headless)** moves a **servo** and reads an **HC-SR04** distance sensor  
- The Pi **broadcasts UDP** packets on your LAN  
- **Windows PC** runs a **pygame GUI** to display the sweep + blips  

![GUI](Radar%20GUI.png)
<img src="Radar%20GUI.png" alt="GUI" width="450">
---

## Files

- `radar_server.py` — run on the **Pi**
- `radar_client_gui.py` — run on **Windows**
- `Pin Layout.png` — wiring screenshot

---

## Wiring (BCM)

![Pin Layout](Pin%20Layout.png)

| Part | Signal | Pi GPIO |
|---|---|---|
| Servo | PWM | **GPIO18** |
| HC-SR04 | TRIG | **GPIO23** |
| HC-SR04 | ECHO | **GPIO24** |

Also connect **5V** and **GND** for the servo + sensor (**common ground required**).

### IMPORTANT: HC-SR04 ECHO is 5V
Raspberry Pi GPIO is **3.3V only**. Use a **voltage divider / level shifter** on the ECHO pin.

Example divider:
- **1kΩ** from HC-SR04 **ECHO → GPIO24**
- **2kΩ** from **GPIO24 → GND**

---

## Run on the Pi

Install dependencies:

```bash
sudo apt update
sudo apt install -y pigpio python3-gpiozero python3-lgpio
sudo systemctl enable --now pigpiod
```

Start streaming:

```bash
python3 radar_server.py
```

The Pi broadcasts UDP on port **5005**.

---

## Run on Windows

Install pygame:

```powershell
py -m pip install pygame
```

Start the GUI:

```powershell
py radar_client_gui.py
```

---

## Network notes

- Pi sends to `255.255.255.255:5005` (UDP broadcast)
- PC and Pi must be on the **same network**
- If you see nothing, allow **UDP 5005** in Windows Firewall

---

## Data format

Each packet is ASCII CSV:

```
timestamp,servo_angle_deg,distance_cm_or_nan
```

Example:

```
1700000000.123456,-42.00,37.50
```

---

## Quick troubleshooting

- **Servo jitters / Pi resets:** power servo from a separate 5V supply (share GND)
- **Distance always NaN:** check TRIG/ECHO wiring + make sure ECHO is level-shifted
- **GUI opens but no data:** firewall / broadcast blocked / not on same subnet

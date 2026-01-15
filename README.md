# PTZ11 Broadcast Controller v6.1

**VISCA Protocol over UDP** - Interactive Web-based PTZ Camera Controller with Live RTSP Streaming

## Features

✨ **Live RTSP Video Streaming** - Real-time camera feed at 640x360
🎮 **Interactive Joystick Control** - 8-direction Pan/Tilt with variable speed
🔍 **Zoom & Focus Control** - Motorized zoom and autofocus
💾 **Preset Memory** - Save and recall up to 5 camera positions
📡 **VISCA Protocol** - Full VISCA command support over UDP
🖥️ **Dark Mode UI** - Modern GitHub-inspired dark theme
📱 **Responsive Design** - Works on desktop, tablet, and mobile

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/tedmark678-afk/ptz11-broadcast-controller.git
cd ptz11-broadcast-controller
```

### 2. Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate  # macOS/Linux
# OR
venv\Scripts\activate  # Windows
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Camera IP

Edit `ptz11_controller.py` line 20-26:

```python
CONFIG = {
    'camera': {
        'ip': '192.168.1.11',          # Your PTZ11 camera IP
        'port': 52381,                  # VISCA over UDP port
        'rtsp': 'rtsp://192.168.1.11/1/h264major',
        'device_id': '3301432581P2107',
        'firmware_version': 'V1.3.81',
    },
    ...
}
```

### 5. Run the Controller

```bash
python3 ptz11_controller.py
```

Output:
```
╔═══════════════════════════════════════════════════════════════╗
║     PTZ11 Enhanced Controller v6.1 - COMPLETE VERSION        ║
║  Based on firmware: 3301432581P2107-V1.3.81                 ║
║  VISCA Protocol over UDP - Full Joystick Control            ║
╠═══════════════════════════════════════════════════════════════╣
║  Device: 192.168.1.11:52381                                  ║
║  Web UI: http://127.0.0.1:5007                              ║
║  Features: Live RTSP, Joystick PTZ, Zoom, Focus, Presets    ║
║  Press Ctrl+C to stop                                        ║
╚═══════════════════════════════════════════════════════════════╝
```

### 6. Open Web UI

Navigate to: **http://127.0.0.1:5007**

## UI Tabs

### 🎮 CONTROLLER
- **Central Joystick** - Pan/Tilt with 8 directions
- **Left Slider** - Focus control (near/far)
- **Right Slider** - Zoom control (in/out)
- **AUTO Button** - Enable autofocus
- **P1-P5 Buttons** - Preset memory recall

### 💻 HEX TERMINAL
- Send raw VISCA commands in HEX format
- Example: `81 01 06 01 0A 0A 02 02` (Pan right, tilt up)
- Real-time command log

### 🔧 DEBUG
- Network diagnostics
- Camera ping test
- UDP connectivity check
- Device info display

## VISCA Protocol Commands

### Pan/Tilt
```
81 01 06 01 [pan_speed] [tilt_speed] [pan_dir] [tilt_dir] FF

Pan Direction:
  01 = Right
  02 = Left
  03 = Stop

Tilt Direction:
  01 = Up
  02 = Down
  03 = Stop

Speed: 01-24 (decimal)
```

### Zoom
```
81 01 04 07 [byte] FF

Zoom In: 20 + speed (2x-2F)
Zoom Out: 30 + speed (3x-3F)
Stop: 00
```

### Focus
```
81 01 04 08 [byte] FF

Focus Near: 20 + speed (2x-2F)
Focus Far: 30 + speed (3x-3F)
Auto Focus On: 81 01 04 38 02 FF
Auto Focus Off: 81 01 04 38 03 FF
```

### Preset Memory
```
Save Preset 1: 81 01 04 3F 01 00 FF
Recall Preset 1: 81 01 04 3F 02 00 FF

Preset numbers: 00-FE (0-254)
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web UI |
| `/video_feed` | GET | MJPEG video stream |
| `/move` | GET | Pan/tilt control `?p=[01/02/03]&t=[01/02/03]&s=[1-24]` |
| `/stop` | GET | Stop all motion |
| `/zoom/move` | GET | Zoom control `?dir=in/out&spd=[1-7]` |
| `/focus/move` | GET | Focus control `?dir=near/far&spd=[1-8]` |
| `/focus/auto` | GET | Autofocus `?enable=true/false` |
| `/preset/call/<num>` | GET | Recall preset (1-5) |
| `/preset/save/<num>` | GET | Save preset (1-5) |
| `/status` | GET | JSON status |
| `/test` | GET | Network diagnostics |

## System Requirements

- **Python** 3.8+
- **macOS** / Linux / Windows with WSL2
- **Network** - Camera on same LAN
- **Ports** - 5007 (Flask), 52381 (VISCA UDP)

## Troubleshooting

### Camera not reachable
```bash
# Test ping
ping 192.168.1.11

# Check VISCA port (should respond to UDP)
nc -u 192.168.1.11 52381
```

### RTSP stream not working
- Verify camera RTSP URL is correct
- Check firewall allows outbound RTSP (554)
- Try `ffmpeg -rtsp_transport tcp -i rtsp://192.168.1.11/1/h264major -t 10 -f null -`

### Web UI not loading
- Ensure port 5007 is not in use: `lsof -i :5007`
- Check Python version: `python3 --version`
- Reinstall Flask: `pip install --upgrade Flask`

## Architecture

```
┌─────────────────────────────────────────┐
│      Web Browser (HTML/JS/CSS)          │
│   - Joystick via nipplejs library       │
│   - Dark mode UI with CSS variables     │
│   - Real-time status updates            │
└──────────────┬──────────────────────────┘
               │ HTTP (port 5007)
┌──────────────▼──────────────────────────┐
│       Flask Web Server (Python)         │
│   - Route handling & MJPEG stream       │
│   - VISCA command builder               │
│   - Camera status management            │
└──────────────┬──────────────────────────┘
               │ RTSP (554) & VISCA UDP (52381)
┌──────────────▼──────────────────────────┐
│      PTZ11 IP Camera (192.168.1.11)     │
│   - RTSP video streaming                │
│   - VISCA protocol control              │
│   - H.264 encoding                      │
└─────────────────────────────────────────┘
```

## Development

### Adding Custom Commands

```python
# In ptz11_controller.py, add new route:

@app.route('/custom/command')
def custom_command():
    payload_hex = "81 01 XX XX XX"
    status, msg = send_visca_command(payload_hex)
    return msg
```

### Extending UI

Edit the `HTML_TEMPLATE` variable to add buttons, sliders, or displays.

## License

MIT License - Feel free to modify and distribute

## Author

Tedmark678 - Broadcast Systems Engineer

---

**Repository**: https://github.com/tedmark678-afk/ptz11-broadcast-controller

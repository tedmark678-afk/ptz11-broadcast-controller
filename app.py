#!/usr/bin/env python3
"""
PTZ11 Broadcast Controller - FRESH START
Minimal implementation: VISCA over UDP + RTSP stream + Web UI
"""

import cv2, threading, socket, time, logging, json, os
from flask import Flask, render_template_string, request, Response, jsonify
import subprocess, numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# Camera config
CAM_IP = '192.168.1.11'
CAM_PORT = 52381
RTSP_URL = 'rtsp://192.168.1.11/1/h264major'
CONFIG_FILE = 'ptz_config.json'

# Global state
state = {
    'pan': '03', 'tilt': '03', 'zoom': 'stop', 'focus': 'stop',
    'preset': 0, 'reachable': False, 'stream_fps': 0, 'stream_status': 'init'
}
lock = threading.Lock()
seq = 0

# Counters for FPS
stream_frame_count = 0
stream_last_time = time.time()

def load_config():
    global CAM_IP, CAM_PORT, RTSP_URL
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
                CAM_IP = cfg.get('cam_ip', CAM_IP)
                CAM_PORT = cfg.get('cam_port', CAM_PORT)
                RTSP_URL = cfg.get('rtsp_url', RTSP_URL)
                logger.info(f"Config loaded: {CAM_IP}:{CAM_PORT}")
        except Exception as e:
            logger.error(f"Config load error: {e}")

def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump({
                'cam_ip': CAM_IP,
                'cam_port': CAM_PORT,
                'rtsp_url': RTSP_URL
            }, f, indent=2)
    except Exception as e:
        logger.error(f"Config save error: {e}")

def get_seq():
    global seq
    seq = (seq + 1) & 0xFFFFFFFF
    return seq

def visca_packet(payload_hex):
    """Create VISCA UDP packet"""
    try:
        payload = bytearray.fromhex(payload_hex.replace(' ', '').upper())
        s = get_seq()
        length = len(payload) + 1
        header = bytearray([0x01, 0x00, 0x00, length & 0xFF])
        header.extend(s.to_bytes(4, 'big'))
        return header + payload + b'\xFF'
    except Exception as e:
        logger.error(f"Packet error: {e}")
        return None

def send_cmd(payload_hex):
    """Send VISCA command to camera"""
    with lock:
        try:
            pkt = visca_packet(payload_hex)
            if not pkt:
                return False
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            sock.sendto(pkt, (CAM_IP, CAM_PORT))
            try:
                sock.recvfrom(1024)
            except:
                pass
            sock.close()
            return True
        except Exception as e:
            logger.error(f"Send error: {e}")
            return False

def pan_tilt(pan_byte, tilt_byte, speed=10):
    """Send pan/tilt command"""
    speed = max(1, min(24, speed))
    cmd = f"81 01 06 01 {speed:02X} {speed:02X} {pan_byte} {tilt_byte}"
    send_cmd(cmd)
    state['pan'] = pan_byte
    state['tilt'] = tilt_byte

def zoom(direction, speed=1):
    """Zoom in/out"""
    speed = max(0, min(7, speed))
    if direction == 'in':
        byte = 0x20 + speed
    elif direction == 'out':
        byte = 0x30 + speed
    else:
        byte = 0x00
    send_cmd(f"81 01 04 07 {byte:02X}")
    state['zoom'] = direction

def focus(direction, speed=1):
    """Focus near/far"""
    speed = max(0, min(8, speed))
    if direction == 'near':
        byte = 0x20 + speed
    elif direction == 'far':
        byte = 0x30 + speed
    else:
        byte = 0x00
    send_cmd(f"81 01 04 08 {byte:02X}")
    state['focus'] = direction

def stop_movement():
    """Stop all movement"""
    pan_tilt('03', '03', 0)
    zoom('stop', 0)
    focus('stop', 0)

def preset_call(num):
    """Recall preset"""
    if 1 <= num <= 255:
        send_cmd(f"81 01 04 3F 02 {num:02X}")
        state['preset'] = num

def preset_set(num):
    """Save preset"""
    if 1 <= num <= 255:
        send_cmd(f"81 01 04 3F 00 {num:02X}")
        state['preset'] = num

def check_camera():
    """Check if camera is reachable"""
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', '1', CAM_IP],
            capture_output=True, timeout=2
        )
        state['reachable'] = (result.returncode == 0)
    except:
        state['reachable'] = False

def gen_frames():
    """Generate video frames"""
    global stream_frame_count, stream_last_time
    cap = None
    error_count = 0
    
    while True:
        try:
            if cap is None or not cap.isOpened():
                logger.info(f"Opening RTSP: {RTSP_URL}")
                cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                cap.set(cv2.CAP_PROP_FPS, 30)
                error_count = 0
            
            ret, frame = cap.read()
            
            if not ret:
                error_count += 1
                if error_count > 15:
                    frame = np.zeros((360, 640, 3), dtype=np.uint8)
                    cv2.putText(frame, 'OFFLINE', (240, 180), 
                              cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 2)
                    state['stream_status'] = 'offline'
                else:
                    state['stream_status'] = 'buffering'
                    time.sleep(0.1)
                    continue
            else:
                error_count = 0
                state['stream_status'] = 'live'
                stream_frame_count += 1
                now = time.time()
                if now - stream_last_time >= 1.0:
                    state['stream_fps'] = stream_frame_count
                    stream_frame_count = 0
                    stream_last_time = now
            
            frame = cv2.resize(frame, (640, 360))
            ret, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ret:
                yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'
        
        except Exception as e:
            logger.error(f"Stream error: {e}")
            time.sleep(1)

# ROUTES
@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/video')
def video():
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/move')
def api_move():
    p = request.args.get('p', '03')
    t = request.args.get('t', '03')
    s = int(request.args.get('s', '10'))
    logger.info(f"MOVE p={p} t={t} s={s}")
    pan_tilt(p, t, s)
    return 'OK'

@app.route('/api/stop')
def api_stop():
    logger.info("STOP")
    stop_movement()
    return 'OK'

@app.route('/api/zoom')
def api_zoom():
    d = request.args.get('dir', 'stop')
    s = int(request.args.get('s', '1'))
    logger.info(f"ZOOM {d} {s}")
    zoom(d, s)
    return 'OK'

@app.route('/api/focus')
def api_focus():
    d = request.args.get('dir', 'stop')
    s = int(request.args.get('s', '1'))
    logger.info(f"FOCUS {d} {s}")
    focus(d, s)
    return 'OK'

@app.route('/api/preset/call')
def api_preset_call():
    num = int(request.args.get('num', 1))
    logger.info(f"PRESET CALL {num}")
    preset_call(num)
    return jsonify({'ok': True})

@app.route('/api/preset/set')
def api_preset_set():
    num = int(request.args.get('num', 1))
    logger.info(f"PRESET SET {num}")
    preset_set(num)
    return jsonify({'ok': True})

@app.route('/api/status')
def api_status():
    return jsonify(state)

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    global CAM_IP, CAM_PORT, RTSP_URL
    if request.method == 'POST':
        try:
            data = request.json
            CAM_IP = data.get('cam_ip', CAM_IP)
            CAM_PORT = int(data.get('cam_port', CAM_PORT))
            RTSP_URL = data.get('rtsp_url', RTSP_URL)
            save_config()
            logger.info(f"Config saved: {CAM_IP}:{CAM_PORT}")
            return jsonify({'ok': True})
        except Exception as e:
            logger.error(f"Config error: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 400
    
    return jsonify({'cam_ip': CAM_IP, 'cam_port': CAM_PORT, 'rtsp_url': RTSP_URL})

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PTZ11 Controller</title>
<style>
:root { --primary: #ff9800; --bg: #1a1a1a; --text: #fff; --border: #404040; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body { width: 100%; height: 100vh; background: var(--bg); color: var(--text); font-family: Segoe UI, sans-serif; overflow: hidden; }
.container { display: flex; height: 100vh; flex-direction: column; }
.header { background: #000; border-bottom: 2px solid var(--primary); padding: 10px 15px; display: flex; justify-content: space-between; align-items: center; }
.header h1 { font-size: 16px; color: var(--primary); }
.content { display: flex; flex: 1; gap: 10px; padding: 10px; }
.video { flex: 1; background: #000; border: 2px solid var(--border); position: relative; }
.video img { width: 100%; height: 100%; object-fit: contain; }
.video-info { position: absolute; top: 8px; right: 8px; background: rgba(0,0,0,0.7); padding: 5px 10px; border-radius: 3px; font-size: 11px; }
.panel { width: 300px; background: #2d2d2d; border: 2px solid var(--border); display: flex; flex-direction: column; overflow: hidden; }
.panel-title { background: #1a1a1a; padding: 10px; font-weight: bold; font-size: 12px; border-bottom: 2px solid var(--border); color: var(--primary); }
.panel-body { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 12px; }
.control-section { display: flex; flex-direction: column; gap: 8px; }
.control-label { font-size: 11px; font-weight: bold; text-transform: uppercase; color: var(--primary); }
.joystick { width: 150px; height: 150px; background: radial-gradient(circle at 35% 35%, #3d3d3d, #1a1a1a); border: 2px solid var(--border); border-radius: 50%; position: relative; cursor: crosshair; display: flex; align-items: center; justify-content: center; margin: 0 auto; }
.joystick-knob { width: 40px; height: 40px; background: radial-gradient(circle at 30% 30%, #666, #222); border: 2px solid var(--border); border-radius: 50%; position: absolute; cursor: grab; }
.joystick-knob.active { cursor: grabbing; }
.slider { width: 100%; height: 5px; background: linear-gradient(90deg, #ff6b00, var(--primary), #ffb700); border-radius: 3px; cursor: pointer; }
.slider-group { display: flex; gap: 8px; }
.slider-item { flex: 1; display: flex; flex-direction: column; gap: 5px; }
.slider-value { text-align: center; font-size: 10px; background: rgba(255, 152, 0, 0.1); padding: 3px; border-radius: 2px; }
.btn { padding: 8px 12px; background: linear-gradient(135deg, #444, #333); border: 2px solid var(--border); color: var(--text); border-radius: 3px; cursor: pointer; font-weight: 600; font-size: 11px; text-transform: uppercase; transition: 0.2s; }
.btn:hover { background: linear-gradient(135deg, #555, #444); border-color: var(--primary); color: var(--primary); }
.btn:active { transform: scale(0.97); }
.btn.primary { background: linear-gradient(135deg, var(--primary), #ff9800); border-color: var(--primary); color: #000; }
.btn-group { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.btn-group.full { grid-template-columns: 1fr; }
.presets { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }
.preset-btn { aspect-ratio: 1; padding: 4px; background: linear-gradient(135deg, #444, #333); border: 2px solid var(--border); color: var(--text); border-radius: 3px; cursor: pointer; font-weight: bold; font-size: 10px; transition: 0.2s; }
.preset-btn:hover { border-color: var(--primary); }
.preset-btn.active { background: linear-gradient(135deg, var(--primary), #ff9800); color: #000; }
@media (max-width: 1024px) { .content { flex-direction: column; } .panel { width: 100%; height: auto; } }
</style>
</head>
<body>
<div class="container">
<div class="header">
<div>
<h1>[PTZ11] Controller</h1>
</div>
<div>
<span>Camera: <span id="cam-status" style="color: #aaa;">●</span></span>
<span style="margin-left: 20px;">Stream: <span id="stream-status" style="color: #aaa;">●</span></span>
</div>
</div>
<div class="content">
<div class="video">
<img src="/video" alt="RTSP Stream">
<div class="video-info" id="video-info">INIT</div>
</div>
<div class="panel">
<div class="panel-title">CONTROLLER</div>
<div class="panel-body">
<div class="control-section">
<div class="control-label">Pan/Tilt</div>
<div style="margin: 0 auto;">
<div class="joystick" id="joypad">
<div class="joystick-knob" id="joystick-knob"></div>
</div>
</div>
<input type="range" id="joy-speed" min="1" max="24" value="10" class="slider" style="margin-top: 5px;">
<div class="slider-value" id="speed-label">Speed: 10</div>
</div>
<div class="control-section">
<div class="control-label">Zoom</div>
<div class="slider-group">
<input type="range" id="zoom" min="-7" max="7" value="0" class="slider">
</div>
<div class="slider-value" id="zoom-label">STOP</div>
</div>
<div class="control-section">
<div class="control-label">Focus</div>
<div class="slider-group">
<input type="range" id="focus" min="-8" max="8" value="0" class="slider">
</div>
<div class="slider-value" id="focus-label">AUTO</div>
</div>
<div class="btn-group full">
<button class="btn primary" id="stop-btn">STOP ALL</button>
</div>
<div class="btn-group">
<button class="btn" id="home-btn">HOME</button>
<button class="btn" id="focus-auto-btn">FOCUS AUTO</button>
</div>
<div class="control-section">
<div class="control-label">Presets</div>
<div class="presets" id="presets"></div>
</div>
</div>
</div>
</div>
</div>

<script>
const joypad = document.getElementById('joypad');
const joyKnob = document.getElementById('joystick-knob');
const joySpeed = document.getElementById('joy-speed');
const stopBtn = document.getElementById('stop-btn');
const homeBtn = document.getElementById('home-btn');
const focusAutoBtn = document.getElementById('focus-auto-btn');
const zoomSlider = document.getElementById('zoom');
const focusSlider = document.getElementById('focus');
const videoInfo = document.getElementById('video-info');
const camStatus = document.getElementById('cam-status');
const streamStatus = document.getElementById('stream-status');

let joyActive = false;
let joyPointerId = null;
let lastCmd = null;
let speedMult = 0.5;

// Joystick
joypad.addEventListener('pointerdown', (e) => {
    joyActive = true;
    joyPointerId = e.pointerId;
    joyKnob.classList.add('active');
    joypad.setPointerCapture(e.pointerId);
});

document.addEventListener('pointermove', (e) => {
    if (!joyActive || e.pointerId !== joyPointerId) return;
    
    const rect = joypad.getBoundingClientRect();
    const cx = rect.width / 2;
    const cy = rect.height / 2;
    let x = e.clientX - rect.left - cx;
    let y = e.clientY - rect.top - cy;
    
    const dist = Math.sqrt(x * x + y * y);
    const maxDist = 60;
    
    if (dist > maxDist) {
        const angle = Math.atan2(y, x);
        x = Math.cos(angle) * maxDist;
        y = Math.sin(angle) * maxDist;
    }
    
    joyKnob.style.transform = `translate(calc(-50% + ${x}px), calc(-50% + ${y}px))`;
    
    // Calculate direction
    const angle = Math.atan2(y, x) * (180 / Math.PI);
    const speed = Math.max(1, Math.min(24, Math.floor((dist / maxDist) * 24 * speedMult)));
    
    let p = '03', t = '03';
    if (angle > -45 && angle <= 45) { p = '02'; t = (angle > 0) ? '02' : (angle < 0) ? '01' : '03'; }
    else if (angle > 45 && angle <= 135) { t = '02'; p = (angle < 90) ? '02' : '01'; }
    else if (angle > 135 || angle <= -135) { p = '01'; t = (angle > 0) ? '02' : '01'; }
    else if (angle > -135 && angle <= -45) { t = '01'; p = (angle > -90) ? '02' : '01'; }
    
    const url = `/api/move?p=${p}&t=${t}&s=${speed}`;
    if (url !== lastCmd) {
        fetch(url).catch(e => console.error('Move error:', e));
        lastCmd = url;
    }
});

document.addEventListener('pointerup', (e) => {
    if (!joyActive || e.pointerId !== joyPointerId) return;
    joyActive = false;
    joyPointerId = null;
    joyKnob.classList.remove('active');
    joyKnob.style.transform = 'translate(-50%, -50%)';
    fetch('/api/stop').catch(e => console.error('Stop error:', e));
    lastCmd = null;
});

joySpeed.addEventListener('input', (e) => {
    speedMult = parseInt(e.target.value) / 24;
    document.getElementById('speed-label').textContent = `Speed: ${e.target.value}`;
});

stopBtn.addEventListener('click', () => {
    fetch('/api/stop').catch(e => console.error('Stop error:', e));
    joyKnob.style.transform = 'translate(-50%, -50%)';
    zoomSlider.value = 0;
    focusSlider.value = 0;
    updateLabels();
});

homeBtn.addEventListener('click', () => {
    fetch('/api/preset/call?num=1').catch(e => console.error('Home error:', e));
});

focusAutoBtn.addEventListener('click', () => {
    fetch('/api/focus?dir=stop').catch(e => console.error('Focus error:', e));
    focusSlider.value = 0;
    updateLabels();
});

zoomSlider.addEventListener('input', (e) => {
    const val = parseInt(e.target.value);
    if (val === 0) {
        fetch('/api/zoom?dir=stop').catch(e => console.error('Zoom error:', e));
    } else {
        const dir = val > 0 ? 'in' : 'out';
        const speed = Math.abs(val);
        fetch(`/api/zoom?dir=${dir}&s=${speed}`).catch(e => console.error('Zoom error:', e));
    }
    updateLabels();
});

focusSlider.addEventListener('input', (e) => {
    const val = parseInt(e.target.value);
    if (val === 0) {
        fetch('/api/focus?dir=stop').catch(e => console.error('Focus error:', e));
    } else {
        const dir = val > 0 ? 'near' : 'far';
        const speed = Math.abs(val);
        fetch(`/api/focus?dir=${dir}&s=${speed}`).catch(e => console.error('Focus error:', e));
    }
    updateLabels();
});

function updateLabels() {
    const z = parseInt(zoomSlider.value);
    document.getElementById('zoom-label').textContent = z === 0 ? 'STOP' : (z > 0 ? 'IN ' : 'OUT ') + Math.abs(z);
    
    const f = parseInt(focusSlider.value);
    document.getElementById('focus-label').textContent = f === 0 ? 'AUTO' : (f > 0 ? 'NEAR ' : 'FAR ') + Math.abs(f);
}

function generatePresets() {
    const container = document.getElementById('presets');
    for (let i = 1; i <= 16; i++) {
        const btn = document.createElement('button');
        btn.className = 'preset-btn';
        btn.textContent = 'P' + i;
        btn.addEventListener('click', () => {
            fetch(`/api/preset/call?num=${i}`).catch(e => console.error('Preset error:', e));
        });
        btn.addEventListener('dblclick', () => {
            if (confirm(`Save Preset ${i}?`)) {
                fetch(`/api/preset/set?num=${i}`).catch(e => console.error('Preset save error:', e));
            }
        });
        container.appendChild(btn);
    }
}

function updateStatus() {
    fetch('/api/status')
        .then(r => r.json())
        .then(d => {
            camStatus.textContent = d.reachable ? '🟢' : '🔴';
            if (d.stream_status === 'live') {
                streamStatus.textContent = '🟢';
                videoInfo.textContent = `LIVE ${d.stream_fps} FPS`;
                videoInfo.style.color = '#4CAF50';
            } else if (d.stream_status === 'buffering') {
                streamStatus.textContent = '🟡';
                videoInfo.textContent = 'BUFFERING';
            } else {
                streamStatus.textContent = '🔴';
                videoInfo.textContent = 'OFFLINE';
            }
        })
        .catch(e => console.error('Status error:', e));
}

generatePr esets();
updateLabels();
updateStatus();
setInterval(updateStatus, 2000);
</script>
</body>
</html>
"""

if __name__ == '__main__':
    load_config()
    check_camera()
    
    # Background camera check
    def bg_check():
        while True:
            time.sleep(5)
            check_camera()
    
    t = threading.Thread(target=bg_check, daemon=True)
    t.start()
    
    logger.info("\n" + "="*60)
    logger.info("PTZ11 CONTROLLER - FRESH START")
    logger.info("="*60)
    logger.info(f"Camera: {CAM_IP}:{CAM_PORT}")
    logger.info(f"RTSP: {RTSP_URL}")
    logger.info(f"URL: http://127.0.0.1:5007")
    logger.info("="*60 + "\n")
    
    app.run(host='127.0.0.1', port=5007, threaded=True, debug=False)

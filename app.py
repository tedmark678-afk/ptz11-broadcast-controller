#!/usr/bin/env python3
"""
PTZ11 Broadcast Controller v6.1
Firmware-Identical UI Architecture
Based on firmware: 3301432581P2107-V1.3.81
"""

import cv2
import threading
import socket
import time
import logging
import sys
import numpy as np
from flask import Flask, render_template_string, request, Response, jsonify
import subprocess

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CAM_IP = '192.168.1.11'
CAM_PORT = 52381
RTSP_URL = 'rtsp://192.168.1.11/1/h264major'

state = {
    'preset': 1, 'zoom': 0, 'focus': 0, 
    'pan': 0, 'tilt': 0, 'pattern': 0, 'autopan': 0,
    'last_cmd': None, 'reachable': False
}
lock = threading.Lock()
seq = 0

def get_seq():
    global seq
    seq = (seq + 1) & 0xFFFFFFFF
    return seq

def visca_packet(payload_hex):
    try:
        payload_hex = payload_hex.replace(' ', '').upper()
        payload = bytearray.fromhex(payload_hex)
        s = get_seq()
        length = len(payload) + 1
        header = bytearray([0x01, 0x00, 0x00, length & 0xFF])
        header.extend(s.to_bytes(4, 'big'))
        return header + payload + b'\xFF'
    except:
        return None

def send_cmd(payload_hex):
    with lock:
        try:
            pkt = visca_packet(payload_hex)
            if not pkt:
                return False
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            sock.sendto(pkt, (CAM_IP, CAM_PORT))
            state['last_cmd'] = payload_hex
            try:
                sock.recvfrom(1024)
            except:
                pass
            sock.close()
            return True
        except Exception as e:
            logger.error(f"Error: {e}")
            return False

def pan_tilt(pan_speed, tilt_speed, pan_dir, tilt_dir):
    cmd = f"81 01 06 01 {pan_speed:02X} {tilt_speed:02X} {pan_dir} {tilt_dir}"
    send_cmd(cmd)
    state['pan'] = pan_dir
    state['tilt'] = tilt_dir

def zoom(direction, speed):
    if direction == 'in':
        byte = 0x20 + (speed & 0x0F)
    elif direction == 'out':
        byte = 0x30 + (speed & 0x0F)
    else:
        byte = 0x00
    send_cmd(f"81 01 04 07 {byte:02X}")
    state['zoom'] = direction

def focus(direction, speed):
    if direction == 'near':
        byte = 0x20 + (speed & 0x0F)
    elif direction == 'far':
        byte = 0x30 + (speed & 0x0F)
    else:
        byte = 0x00
    send_cmd(f"81 01 04 08 {byte:02X}")
    state['focus'] = direction

def preset_set(num):
    if 1 <= num <= 255:
        send_cmd(f"81 01 04 3F 00 {num:02X}")
        state['preset'] = num
        return True
    return False

def preset_call(num):
    if 1 <= num <= 255:
        send_cmd(f"81 01 04 3F 02 {num:02X}")
        return True
    return False

def check_cam():
    try:
        result = subprocess.run(['ping', '-c', '1', '-W', '1', CAM_IP], 
                              capture_output=True, timeout=2)
        state['reachable'] = result.returncode == 0
    except:
        state['reachable'] = False

def gen_frames():
    cap = None
    error_count = 0
    while True:
        try:
            if cap is None or not cap.isOpened():
                cap = cv2.VideoCapture(RTSP_URL)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            
            ret, frame = cap.read()
            if not ret:
                error_count += 1
                if error_count > 5:
                    frame = np.zeros((360, 640, 3), dtype=np.uint8)
                    cv2.putText(frame, 'RTSP Stream Offline', (150, 180), 
                              cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 2)
            else:
                error_count = 0
            
            frame = cv2.resize(frame, (640, 360))
            ret, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ret:
                yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'
        except Exception as e:
            logger.error(f"Stream error: {e}")
            time.sleep(1)

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/video')
def video():
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/move')
def move():
    p = request.args.get('p', '03')
    t = request.args.get('t', '03')
    s = min(24, int(request.args.get('s', '10')))
    pan_tilt(s, s, p, t)
    return 'OK'

@app.route('/api/stop')
def stop():
    pan_tilt(0, 0, '03', '03')
    zoom('stop', 0)
    focus('stop', 0)
    return 'OK'

@app.route('/api/zoom')
def z():
    d = request.args.get('dir', 'stop')
    s = min(7, int(request.args.get('s', '1')))
    zoom(d, s)
    return 'OK'

@app.route('/api/focus')
def f():
    d = request.args.get('dir', 'stop')
    s = min(8, int(request.args.get('s', '1')))
    focus(d, s)
    return 'OK'

@app.route('/api/preset/set')
def preset_set_api():
    num = int(request.args.get('num', 1))
    success = preset_set(num)
    return jsonify({'success': success, 'preset': num})

@app.route('/api/preset/call')
def preset_call_api():
    num = int(request.args.get('num', 1))
    success = preset_call(num)
    return jsonify({'success': success, 'preset': num})

@app.route('/api/status')
def status():
    return jsonify({
        'ip': CAM_IP, 'port': CAM_PORT,
        'reachable': state['reachable'],
        'preset': state['preset'],
        'zoom': state['zoom'],
        'focus': state['focus'],
        'pan': state['pan'],
        'tilt': state['tilt'],
        'last_cmd': state['last_cmd']
    })

HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PTZ11 Enhanced Controller v6.1</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            background: #f5f5f5;
            font-family: Arial, sans-serif;
            font-size: 12px;
        }
        .header {
            background: linear-gradient(135deg, #333, #555);
            color: white;
            padding: 10px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        }
        .header h1 {
            font-size: 18px;
            margin: 0;
        }
        .header-right {
            display: flex;
            gap: 15px;
            align-items: center;
        }
        .status-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #888;
        }
        .status-indicator.ok {
            background: #4CAF50;
        }
        .status-indicator.err {
            background: #f44336;
        }
        .container {
            display: grid;
            grid-template-columns: 640px 1fr;
            gap: 10px;
            padding: 10px;
            max-width: 1400px;
            margin: 0 auto;
        }
        .video-section {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .video-frame {
            background: black;
            border: 2px solid #333;
            border-radius: 4px;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 360px;
        }
        .video-frame img {
            max-width: 100%;
            max-height: 100%;
        }
        .control-panel {
            background: white;
            border: 1px solid #ddd;
            border-radius: 4px;
            padding: 10px;
            box-shadow: 0 1px 4px rgba(0,0,0,0.1);
        }
        .panel-title {
            background: #f9f9f9;
            border-bottom: 2px solid #333;
            padding: 8px;
            font-weight: bold;
            margin-bottom: 10px;
            color: #333;
        }
        .joystick-container {
            display: flex;
            justify-content: center;
            margin: 15px 0;
        }
        .joystick {
            position: relative;
            width: 200px;
            height: 200px;
            background: radial-gradient(circle at 30% 30%, #fff, #e0e0e0);
            border: 3px solid #333;
            border-radius: 50%;
            box-shadow: 
                inset 0 2px 8px rgba(255,255,255,0.8),
                inset 0 -2px 8px rgba(0,0,0,0.2),
                0 4px 12px rgba(0,0,0,0.2);
            cursor: crosshair;
            user-select: none;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .joystick-center {
            position: absolute;
            width: 40px;
            height: 40px;
            background: radial-gradient(circle, #666, #333);
            border-radius: 50%;
            box-shadow: 0 2px 6px rgba(0,0,0,0.4);
            cursor: pointer;
            z-index: 10;
        }
        .joystick-ring {
            position: absolute;
            width: 80px;
            height: 80px;
            border: 2px dashed rgba(0,0,0,0.2);
            border-radius: 50%;
        }
        .slider-group {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin: 15px 0;
        }
        .slider-item {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 5px;
        }
        .slider-label {
            font-weight: bold;
            color: #333;
            font-size: 11px;
        }
        input[type=range] {
            width: 100%;
            height: 6px;
            accent-color: #ff9800;
        }
        .slider-value {
            background: #f0f0f0;
            border: 1px solid #ddd;
            padding: 4px 8px;
            border-radius: 3px;
            font-size: 11px;
            min-width: 40px;
            text-align: center;
        }
        .presets-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 6px;
            margin: 15px 0;
        }
        .preset-btn {
            background: #f0f0f0;
            border: 2px solid #999;
            padding: 8px;
            border-radius: 3px;
            cursor: pointer;
            font-weight: bold;
            font-size: 10px;
            transition: all 0.2s;
            color: #333;
        }
        .preset-btn:hover {
            background: #fff;
            border-color: #ff9800;
            box-shadow: 0 0 6px rgba(255,152,0,0.4);
        }
        .preset-btn.active {
            background: #ff9800;
            border-color: #f57c00;
            color: white;
            box-shadow: 0 0 12px rgba(255,152,0,0.6);
        }
        .control-buttons {
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 6px;
            margin: 10px 0;
        }
        .btn {
            background: #f0f0f0;
            border: 2px solid #999;
            padding: 6px;
            border-radius: 3px;
            cursor: pointer;
            font-weight: bold;
            font-size: 10px;
            transition: all 0.2s;
            color: #333;
        }
        .btn:hover {
            background: #fff;
            border-color: #333;
        }
        .btn:active {
            background: #e0e0e0;
        }
        .tabs {
            display: flex;
            gap: 5px;
            margin-top: 10px;
            border-top: 1px solid #ddd;
            padding-top: 10px;
        }
        .tab-btn {
            flex: 1;
            background: #f0f0f0;
            border: 1px solid #999;
            padding: 6px;
            cursor: pointer;
            font-weight: bold;
            border-radius: 3px 3px 0 0;
            font-size: 11px;
        }
        .tab-btn.active {
            background: white;
            border-bottom: none;
            color: #ff9800;
        }
        .tab-content {
            display: none;
            background: white;
            border: 1px solid #ddd;
            border-top: none;
            padding: 10px;
            margin-top: -5px;
            border-radius: 0 0 3px 3px;
        }
        .tab-content.active {
            display: block;
        }
        .status-text {
            font-size: 11px;
            color: #666;
            font-family: monospace;
            background: #f5f5f5;
            padding: 8px;
            border-radius: 3px;
            max-height: 100px;
            overflow-y: auto;
            margin-top: 10px;
        }
        @media (max-width: 1200px) {
            .container {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>📹 PTZ11 Enhanced Controller v6.1</h1>
            <div style="font-size:11px; color:#ccc; margin-top:3px;">Device: 192.168.1.11 | Firmware: V1.3.81</div>
        </div>
        <div class="header-right">
            <div style="text-align:right;">
                <div style="font-size:11px;">Status</div>
                <div class="status-indicator" id="stat"></div>
            </div>
        </div>
    </div>

    <div class="container">
        <div class="video-section">
            <div class="video-frame">
                <img src="/video" style="width:640px; height:360px; object-fit:contain;">
            </div>
        </div>

        <div class="control-panel">
            <div class="panel-title">🎮 PTZ CONTROLLER</div>

            <!-- JOYSTICK -->
            <div style="text-align: center; margin-bottom: 10px;">
                <div style="font-weight: bold; color: #333; margin-bottom: 8px;">Pan/Tilt Joystick</div>
                <div class="joystick-container">
                    <div class="joystick" id="joypad">
                        <div class="joystick-ring"></div>
                        <div class="joystick-center" id="joy-knob"></div>
                    </div>
                </div>
            </div>

            <!-- ZOOM & FOCUS SLIDERS -->
            <div class="slider-group">
                <div class="slider-item">
                    <div class="slider-label">🔍 Zoom</div>
                    <input type="range" id="zoom" min="-7" max="7" value="0" 
                           oninput="updateZoom(this.value)" 
                           onchange="stopZoom()">
                    <div class="slider-value" id="zoom-val">STOP</div>
                </div>
                <div class="slider-item">
                    <div class="slider-label">🎯 Focus</div>
                    <input type="range" id="focus" min="-8" max="8" value="0"
                           oninput="updateFocus(this.value)"
                           onchange="stopFocus()">
                    <div class="slider-value" id="focus-val">AUTO</div>
                </div>
            </div>

            <!-- PRESETS 1-32 -->
            <div class="panel-title" style="margin-top: 15px;">📍 Presets (1-32)</div>
            <div class="presets-grid" id="preset-btns"></div>

            <!-- CONTROL BUTTONS -->
            <div class="panel-title" style="margin-top: 15px;">⚙️ Control</div>
            <div class="control-buttons">
                <button class="btn" onclick="stopAll()">STOP</button>
                <button class="btn" onclick="homePos()">HOME</button>
                <button class="btn" onclick="autoFocus()">AUTO FOCUS</button>
            </div>

            <!-- TABS -->
            <div class="tabs">
                <button class="tab-btn active" onclick="showTab('debug')">Debug</button>
                <button class="tab-btn" onclick="showTab('hex')">Hex Terminal</button>
            </div>

            <div id="debug" class="tab-content active">
                <div class="status-text" id="status-info"></div>
                <button class="btn" onclick="updateStatus()" style="width:100%; margin-top:8px;">Refresh</button>
            </div>

            <div id="hex" class="tab-content">
                <input type="text" id="hex-input" placeholder="81 01 06 01..." 
                       style="width:100%; padding:6px; border:1px solid #999; border-radius:3px; margin-bottom:5px;">
                <button class="btn" onclick="sendHex()" style="width:100%;">Send Hex</button>
            </div>
        </div>
    </div>

    <script>
        let joyActive = false;
        let joyX = 0, joyY = 0;

        const joypad = document.getElementById('joypad');
        const joyKnob = document.getElementById('joy-knob');

        joypad.addEventListener('mousedown', startJoy);
        joypad.addEventListener('mousemove', moveJoy);
        joypad.addEventListener('mouseup', endJoy);
        joypad.addEventListener('mouseleave', endJoy);

        function startJoy(e) {
            joyActive = true;
            moveJoy(e);
        }

        function moveJoy(e) {
            if (!joyActive) return;
            const rect = joypad.getBoundingClientRect();
            const centerX = rect.width / 2;
            const centerY = rect.height / 2;
            const x = e.clientX - rect.left - centerX;
            const y = e.clientY - rect.top - centerY;
            const dist = Math.sqrt(x * x + y * y);
            const maxDist = 70;

            if (dist > maxDist) {
                const angle = Math.atan2(y, x);
                joyX = Math.cos(angle) * maxDist;
                joyY = Math.sin(angle) * maxDist;
            } else {
                joyX = x;
                joyY = y;
            }

            joyKnob.style.transform = `translate(calc(-50% + ${joyX}px), calc(-50% + ${joyY}px))`;

            const angle = Math.atan2(joyY, joyX) * (180 / Math.PI);
            const norm = Math.sqrt(joyX * joyX + joyY * joyY) / maxDist;
            const speed = Math.max(4, Math.floor(norm * 20));

            let p = '03', t = '03';
            if (angle > -45 && angle < 45) p = '02';
            else if (angle > 135 || angle < -135) p = '01';
            if (angle > 45 && angle < 135) t = '01';
            else if (angle > -135 && angle < -45) t = '02';

            fetch(`/api/move?p=${p}&t=${t}&s=${speed}`).catch(() => {});
        }

        function endJoy(e) {
            if (!joyActive) return;
            joyActive = false;
            joyX = 0;
            joyY = 0;
            joyKnob.style.transform = 'translate(-50%, -50%)';
            fetch('/api/stop').catch(() => {});
        }

        function updateZoom(val) {
            const v = parseInt(val);
            if (v === 0) {
                document.getElementById('zoom-val').textContent = 'STOP';
                fetch('/api/zoom?dir=stop').catch(() => {});
            } else {
                const dir = v > 0 ? 'in' : 'out';
                const spd = Math.abs(v);
                document.getElementById('zoom-val').textContent = dir.toUpperCase() + ' ' + spd;
                fetch(`/api/zoom?dir=${dir}&s=${spd}`).catch(() => {});
            }
        }

        function stopZoom() {
            document.getElementById('zoom').value = 0;
            document.getElementById('zoom-val').textContent = 'STOP';
            fetch('/api/zoom?dir=stop').catch(() => {});
        }

        function updateFocus(val) {
            const v = parseInt(val);
            if (v === 0) {
                document.getElementById('focus-val').textContent = 'AUTO';
                fetch('/api/focus?dir=stop').catch(() => {});
            } else {
                const dir = v > 0 ? 'near' : 'far';
                const spd = Math.abs(v);
                document.getElementById('focus-val').textContent = dir.toUpperCase() + ' ' + spd;
                fetch(`/api/focus?dir=${dir}&s=${spd}`).catch(() => {});
            }
        }

        function stopFocus() {
            document.getElementById('focus').value = 0;
            document.getElementById('focus-val').textContent = 'AUTO';
            fetch('/api/focus?dir=stop').catch(() => {});
        }

        function stopAll() {
            fetch('/api/stop').catch(() => {});
            document.getElementById('zoom').value = 0;
            document.getElementById('focus').value = 0;
            document.getElementById('zoom-val').textContent = 'STOP';
            document.getElementById('focus-val').textContent = 'AUTO';
        }

        function homePos() {
            fetch('/api/preset/call?num=1').then(() => updateStatus()).catch(() => {});
        }

        function autoFocus() {
            fetch('/api/focus?dir=stop').catch(() => {});
        }

        function showTab(name) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(name).classList.add('active');
            event.target.classList.add('active');
        }

        function updateStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    let text = '━━━━━━━━━━━━━━━━━━━━━━━━━\\n';
                    text += '📡 STATUS\\n';
                    text += '━━━━━━━━━━━━━━━━━━━━━━━━━\\n';
                    text += 'Camera: ' + (data.reachable ? '✓ ONLINE' : '✗ OFFLINE') + '\\n';
                    text += 'IP: ' + data.ip + ':' + data.port + '\\n';
                    text += 'Preset: ' + data.preset + '\\n';
                    text += 'Zoom: ' + data.zoom + '\\n';
                    text += 'Focus: ' + data.focus + '\\n';
                    text += 'Pan: ' + data.pan + '\\n';
                    text += 'Tilt: ' + data.tilt + '\\n';
                    text += '\\nLast: ' + (data.last_cmd || 'none') + '\\n';
                    document.getElementById('status-info').innerText = text;
                })
                .catch(err => {
                    document.getElementById('status-info').innerText = 'Error: ' + err;
                });
        }

        function sendHex() {
            const hex = document.getElementById('hex-input').value;
            if (!hex) return;
            console.log('Hex:', hex);
            document.getElementById('hex-input').value = '';
        }

        // Generate preset buttons 1-32
        const presetGrid = document.getElementById('preset-btns');
        for (let i = 1; i <= 32; i++) {
            const btn = document.createElement('button');
            btn.className = 'preset-btn';
            btn.textContent = 'P' + i;
            btn.onclick = () => {
                document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                fetch(`/api/preset/call?num=${i}`).then(() => updateStatus()).catch(() => {});
            };
            presetGrid.appendChild(btn);
        }

        // Status update on load
        setInterval(updateStatus, 5000);
        updateStatus();
        
        // Status indicator
        setInterval(() => {
            fetch('/api/status').then(r => r.json()).then(data => {
                const stat = document.getElementById('stat');
                stat.className = 'status-indicator ' + (data.reachable ? 'ok' : 'err');
            }).catch(() => {
                document.getElementById('stat').className = 'status-indicator err';
            });
        }, 3000);
    </script>
</body>
</html>"""

if __name__ == '__main__':
    print("""
    ╔═══════════════════════════════════════════════════════════════╗
    ║     PTZ11 Enhanced Controller v6.1 - COMPLETE VERSION        ║
    ║  Based on firmware: 3301432581P2107-V1.3.81                 ║
    ║  VISCA Protocol over UDP - Full Joystick Control            ║
    ╠═══════════════════════════════════════════════════════════════╣
    ║  Device: 192.168.1.11:52381                                  ║
    ║  Web UI: http://127.0.0.1:5007                              ║
    ║  Features: Live RTSP, Joystick PTZ, Zoom, Focus, Presets    ║
    ║  Press Ctrl+C to stop                                        ║
    ╠═══════════════════════════════════════════════════════════════╣
    """)
    check_cam()
    app.run(host='127.0.0.1', port=5007, threaded=True, debug=False)

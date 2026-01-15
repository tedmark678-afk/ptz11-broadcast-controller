#!/usr/bin/env python3
"""
PTZ11 Broadcast Controller - Fresh Build
"""

import cv2
import threading
import socket
import time
import logging
import sys
import numpy as np
from flask import Flask, render_template_string, request, Response, jsonify
from datetime import datetime
import subprocess

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

CAM_IP = '192.168.1.11'
CAM_PORT = 52381
RTSP_URL = 'rtsp://192.168.1.11/1/h264major'

status = {'reachable': False, 'last_cmd': None}
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
            status['last_cmd'] = payload_hex
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
    return send_cmd(cmd)

def zoom(direction, speed):
    byte = (0x20 if direction == 'in' else 0x30 if direction == 'out' else 0x00) + (speed & 0x0F)
    return send_cmd(f"81 01 04 07 {byte:02X}")

def focus(direction, speed):
    byte = (0x20 if direction == 'near' else 0x30 if direction == 'far' else 0x00) + (speed & 0x0F)
    return send_cmd(f"81 01 04 08 {byte:02X}")

def check_cam():
    try:
        result = subprocess.run(['ping', '-c', '1', '-W', '1', CAM_IP], capture_output=True, timeout=2)
        status['reachable'] = result.returncode == 0
    except:
        status['reachable'] = False

def gen_frames():
    cap = None
    while True:
        try:
            if cap is None:
                cap = cv2.VideoCapture(RTSP_URL)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            
            ret, frame = cap.read()
            if not ret:
                frame = np.zeros((360, 640, 3), dtype=np.uint8)
                cv2.putText(frame, 'Stream Unavailable', (100, 180), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
            
            frame = cv2.resize(frame, (640, 360))
            ret, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
            if ret:
                yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'
        except Exception as e:
            logger.error(f"Stream: {e}")
            time.sleep(1)

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/video')
def video():
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/move')
def move():
    p = request.args.get('p', '03')
    t = request.args.get('t', '03')
    s = int(request.args.get('s', '10'))
    pan_tilt(min(24, s), min(24, s), p, t)
    return 'OK'

@app.route('/stop')
def stop():
    pan_tilt(0, 0, '03', '03')
    zoom('stop', 0)
    focus('stop', 0)
    return 'OK'

@app.route('/zoom')
def z():
    d = request.args.get('dir', 'stop')
    s = int(request.args.get('s', '1'))
    zoom(d, min(7, s))
    return 'OK'

@app.route('/focus')
def f():
    d = request.args.get('dir', 'stop')
    s = int(request.args.get('s', '1'))
    focus(d, min(8, s))
    return 'OK'

@app.route('/status')
def st():
    return jsonify({'reachable': status['reachable'], 'ip': CAM_IP, 'last': status['last_cmd']})

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PTZ11 Controller v6.1</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/nipplejs/0.10.1/nipplejs.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/nipplejs/0.10.1/nipplejs.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #0d1117;
            color: #c9d1d9;
            font-family: 'Segoe UI', monospace;
            padding: 20px;
        }
        .header {
            background: #161b22;
            border: 1px solid #30363d;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header h1 { color: #58a6ff; font-size: 24px; }
        .header p { font-size: 12px; color: #888; }
        .status { width: 12px; height: 12px; border-radius: 50%; display: inline-block; }
        .status.ok { background: #238636; }
        .status.err { background: #da3633; }
        .tabs {
            display: flex;
            background: #161b22;
            border-bottom: 1px solid #30363d;
            margin-bottom: 20px;
            border-radius: 8px 8px 0 0;
        }
        .tab {
            flex: 1;
            padding: 12px;
            background: transparent;
            color: #c9d1d9;
            border: none;
            cursor: pointer;
            font-weight: bold;
            border-bottom: 3px solid transparent;
            transition: all 0.2s;
        }
        .tab.active {
            border-bottom-color: #58a6ff;
            color: white;
            background: rgba(88, 166, 255, 0.1);
        }
        .page { display: none; }
        .page.active { display: block; }
        .video-container {
            background: black;
            border: 1px solid #30363d;
            border-radius: 8px;
            margin-bottom: 20px;
            text-align: center;
            min-height: 360px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .video-container img { max-width: 100%; max-height: 360px; }
        .controls {
            display: grid;
            grid-template-columns: 80px 1fr 80px;
            gap: 10px;
            margin-bottom: 20px;
        }
        .slider-box {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 10px;
            min-height: 200px;
        }
        input[type=range] {
            width: 8px;
            height: 150px;
            margin: 10px 0;
        }
        #joystick {
            position: relative;
            background: radial-gradient(circle, #222, transparent);
            border-radius: 50%;
            border: 2px dashed #30363d;
            min-height: 200px;
        }
        .presets {
            display: flex;
            gap: 5px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .preset {
            flex: 1;
            min-width: 60px;
            background: #161b22;
            border: 1px solid #30363d;
            color: white;
            padding: 10px;
            border-radius: 4px;
            cursor: pointer;
            font-weight: bold;
            transition: all 0.2s;
        }
        .preset:hover {
            border-color: #58a6ff;
            box-shadow: 0 0 8px rgba(88, 166, 255, 0.3);
        }
        .btn {
            background: #161b22;
            border: 1px solid #30363d;
            color: white;
            padding: 8px 12px;
            margin: 5px;
            border-radius: 4px;
            cursor: pointer;
            font-weight: bold;
            transition: all 0.2s;
        }
        .btn:hover { border-color: #58a6ff; }
        .btn-primary {
            background: #58a6ff;
            color: black;
        }
        .log {
            background: #000;
            border: 1px solid #30363d;
            border-radius: 4px;
            padding: 10px;
            font-size: 11px;
            color: #0f0;
            height: 200px;
            overflow-y: auto;
            margin-bottom: 10px;
            font-family: monospace;
        }
        .input-group {
            display: flex;
            gap: 10px;
            margin-bottom: 10px;
        }
        input[type=text] {
            flex: 1;
            background: #161b22;
            border: 1px solid #30363d;
            color: white;
            padding: 8px;
            border-radius: 4px;
            font-family: monospace;
        }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>🎥 PTZ11 Controller v6.1</h1>
            <p>Device: 192.168.1.11 | Firmware: V1.3.81</p>
        </div>
        <span class="status" id="stat" style="background: #666;"></span>
    </div>

    <div class="tabs">
        <button class="tab active" onclick="show('cam')">🎮 CONTROLLER</button>
        <button class="tab" onclick="show('hex')">💻 HEX</button>
        <button class="tab" onclick="show('diag')">🔧 DEBUG</button>
    </div>

    <div id="cam" class="page active">
        <div class="video-container">
            <img src="/video" onerror="this.style.display='none'" style="max-width:640px; max-height:360px;">
        </div>
        <div class="controls">
            <div class="slider-box"><label>FOCUS</label><input type="range" min="-8" max="8" value="0" id="foc" oninput="handleRocker('focus', this.value)" onchange="resetRocker(this)"><button class="btn" style="width:100%;padding:4px;margin-top:8px;font-size:10px;" onclick="fetch('/focus?dir=none')">AUTO</button></div>
            <div id="joystick"></div>
            <div class="slider-box"><label>ZOOM</label><input type="range" min="-7" max="7" value="0" id="zom" oninput="handleRocker('zoom', this.value)" onchange="resetRocker(this)"></div>
        </div>
        <div class="presets">
            <button class="preset" onclick="fetch('/move?p=03&t=03&s=1')">P1</button>
            <button class="preset" onclick="fetch('/move?p=03&t=03&s=1')">P2</button>
            <button class="preset" onclick="fetch('/move?p=03&t=03&s=1')">P3</button>
            <button class="preset" onclick="fetch('/move?p=03&t=03&s=1')">P4</button>
            <button class="preset" onclick="fetch('/move?p=03&t=03&s=1')">P5</button>
        </div>
    </div>

    <div id="hex" class="page">
        <h2>VISCA HEX Terminal</h2>
        <div class="log" id="log">> Ready for commands</div>
        <div class="input-group">
            <input type="text" id="hex" placeholder="81 01 06 01 0A 0A 02 02" onkeypress="if(event.key==='Enter') sendHex()">
            <button class="btn btn-primary" onclick="sendHex()">SEND</button>
        </div>
    </div>

    <div id="diag" class="page">
        <h2>System Diagnostics</h2>
        <button class="btn btn-primary" onclick="runDiag()">Run Test</button>
        <div id="diagOut" style="margin-top:15px;"></div>
    </div>

    <script>
        let joyManager = nipplejs.create({
            zone: document.getElementById('joystick'),
            mode: 'static',
            position: {left: '50%', top: '50%'},
            color: '#58a6ff',
            size: 140
        });

        let lastUrl = '';
        joyManager.on('move', (e, data) => {
            if (!data.angle) return;
            let force = Math.min(data.distance / 70, 1);
            let speed = Math.floor(force * 20) + 4;
            speed = Math.min(24, speed);
            let angle = data.angle.degree;
            let p = '03', t = '03';

            if (angle > 70 && angle < 110) t = '01';
            else if (angle > 250 && angle < 290) t = '02';
            else if (angle < 20 || angle > 340) p = '02';
            else if (angle > 160 && angle < 200) p = '01';
            else if (angle >= 20 && angle <= 70) { p = '02'; t = '01'; }
            else if (angle >= 110 && angle <= 160) { p = '01'; t = '01'; }
            else if (angle >= 200 && angle <= 250) { p = '01'; t = '02'; }
            else if (angle >= 290 && angle <= 340) { p = '02'; t = '02'; }

            let url = `/move?p=${p}&t=${t}&s=${speed}`;
            if (url !== lastUrl) {
                fetch(url).catch(e => console.error(e));
                lastUrl = url;
            }
        });

        joyManager.on('end', () => {
            fetch('/stop').catch(e => console.error(e));
            lastUrl = '';
        });

        function show(id) {
            document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
            document.getElementById(id).classList.add('active');
            event.target.classList.add('active');
        }

        function handleRocker(type, val) {
            let v = parseInt(val);
            if (v === 0) return;
            let dir = v > 0 ? 'in' : 'out';
            let spd = Math.abs(v);
            if (type === 'zoom') fetch(`/zoom?dir=${dir}&s=${spd}`).catch(e => console.error(e));
            else if (type === 'focus') {
                dir = v > 0 ? 'near' : 'far';
                fetch(`/focus?dir=${dir}&s=${spd}`).catch(e => console.error(e));
            }
        }

        function resetRocker(el) {
            el.value = 0;
            if (el.id.includes('zom')) fetch('/zoom?dir=stop').catch(() => {});
            if (el.id.includes('foc')) fetch('/focus?dir=stop').catch(() => {});
        }

        function sendHex() {
            let val = document.getElementById('hex').value;
            if (!val) return;
            let log = document.getElementById('log');
            log.innerHTML += `> ${val}\n`;
            log.scrollTop = log.scrollHeight;
            document.getElementById('hex').value = '';
        }

        function runDiag() {
            let out = document.getElementById('diagOut');
            out.innerHTML = 'Testing...';
            fetch('/status')
                .then(r => r.json())
                .then(data => {
                    out.innerHTML = `<p>Camera IP: ${data.ip}</p><p>Reachable: ${data.reachable ? '✓' : '✗'}</p><p>Last: ${data.last || 'none'}</p>`;
                })
                .catch(e => out.innerHTML = `Error: ${e}`);
        }

        function updateStatus() {
            fetch('/status')
                .then(r => r.json())
                .then(data => {
                    let stat = document.getElementById('stat');
                    stat.className = 'status ' + (data.reachable ? 'ok' : 'err');
                })
                .catch(() => {});
        }

        setInterval(updateStatus, 3000);
        updateStatus();
    </script>
</body>
</html>"""

if __name__ == '__main__':
    print("""
    ╔═══════════════════════════════════════════════════════════════╗
    ║     PTZ11 Enhanced Controller v6.1 - FRESH BUILD            ║
    ║  VISCA Protocol over UDP - Full Joystick Control            ║
    ╠═══════════════════════════════════════════════════════════════╣
    ║  Device: 192.168.1.11:52381                                  ║
    ║  Web UI: http://127.0.0.1:5007                              ║
    ║  Press Ctrl+C to stop                                        ║
    ╠═══════════════════════════════════════════════════════════════╣
    """)
    check_cam()
    app.run(host='127.0.0.1', port=5007, threaded=True, debug=False)

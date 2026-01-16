#!/usr/bin/env python3
"""
PTZ11 Broadcast Controller v6.1 - Kiloview Style
Modern Clean UI + Professional Joystick Control
Based on firmware: 3301432581P2107-V1.3.81
"""

import cv2
import threading
import socket
import time
import logging
import json
import os
from flask import Flask, render_template_string, request, Response, jsonify
import subprocess
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# Camera Config
CAM_IP = '192.168.1.11'
CAM_PORT = 52381
RTSP_URL = 'rtsp://192.168.1.11/1/h264major'
CONFIG_FILE = 'ptz_config.json'

# Global State
state = {
    'preset_active': 0,
    'zoom': 0,
    'focus': 0,
    'pan': 0,
    'tilt': 0,
    'last_cmd': None,
    'reachable': False,
    'stream_fps': 0,
    'stream_status': 'initializing'
}

presets = {}
lock = threading.Lock()
seq = 0
stream_frame_count = 0
stream_last_time = time.time()

def load_config():
    global CAM_IP, CAM_PORT, RTSP_URL
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
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
    """Build VISCA over UDP packet"""
    try:
        payload_hex = payload_hex.replace(' ', '').upper()
        payload = bytearray.fromhex(payload_hex)
        s = get_seq()
        length = len(payload) + 1
        header = bytearray([0x01, 0x00, 0x00, length & 0xFF])
        header.extend(s.to_bytes(4, 'big'))
        return header + payload + b'\xFF'
    except Exception as e:
        logger.error(f"Packet build error: {e}")
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
            state['last_cmd'] = payload_hex
            
            try:
                sock.recvfrom(1024)
            except socket.timeout:
                pass
            finally:
                sock.close()
            
            return True
        except Exception as e:
            logger.error(f"Command error: {e}")
            return False

def pan_tilt(pan_speed, tilt_speed, pan_dir, tilt_dir):
    """Pan and tilt control"""
    if pan_speed < 0 or pan_speed > 24:
        pan_speed = 0
    if tilt_speed < 0 or tilt_speed > 24:
        tilt_speed = 0
    
    cmd = f"81 01 06 01 {pan_speed:02X} {tilt_speed:02X} {pan_dir} {tilt_dir}"
    send_cmd(cmd)
    state['pan'] = pan_dir
    state['tilt'] = tilt_dir

def zoom(direction, speed):
    """Zoom control (1-7)"""
    speed = max(0, min(7, speed))
    if direction == 'in':
        byte = 0x20 + speed
    elif direction == 'out':
        byte = 0x30 + speed
    else:  # stop
        byte = 0x00
    send_cmd(f"81 01 04 07 {byte:02X}")
    state['zoom'] = direction

def focus(direction, speed):
    """Focus control (1-8)"""
    speed = max(0, min(8, speed))
    if direction == 'near':
        byte = 0x20 + speed
    elif direction == 'far':
        byte = 0x30 + speed
    else:  # stop
        byte = 0x00
    send_cmd(f"81 01 04 08 {byte:02X}")
    state['focus'] = direction

def preset_set(num):
    """Save preset at current position"""
    if 1 <= num <= 255:
        send_cmd(f"81 01 04 3F 00 {num:02X}")
        state['preset_active'] = num
        return True
    return False

def preset_call(num):
    """Call saved preset"""
    if 1 <= num <= 255:
        send_cmd(f"81 01 04 3F 02 {num:02X}")
        state['preset_active'] = num
        return True
    return False

def preset_delete(num):
    """Delete preset"""
    if 1 <= num <= 255:
        send_cmd(f"81 01 04 3F 01 {num:02X}")
        return True
    return False

def check_camera():
    """Ping camera to check connectivity"""
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', '1', CAM_IP],
            capture_output=True,
            timeout=2
        )
        state['reachable'] = result.returncode == 0
    except Exception as e:
        logger.error(f"Ping error: {e}")
        state['reachable'] = False

def gen_frames():
    """Generate MJPEG stream from RTSP"""
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
                state['stream_status'] = 'buffering'
                
                if error_count > 10:
                    frame = np.zeros((360, 640, 3), dtype=np.uint8)
                    cv2.putText(
                        frame,
                        'RTSP Stream Offline',
                        (120, 180),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        1.5,
                        (0, 0, 255),
                        2
                    )
                    state['stream_status'] = 'offline'
                else:
                    time.sleep(0.1)
                    continue
            else:
                error_count = 0
                state['stream_status'] = 'live'
                stream_frame_count += 1
                
                # Calculate FPS
                now = time.time()
                if now - stream_last_time >= 1.0:
                    state['stream_fps'] = stream_frame_count
                    stream_frame_count = 0
                    stream_last_time = now
            
            # Resize to standard size
            frame = cv2.resize(frame, (640, 360))
            
            # Encode to JPEG
            ret, buf = cv2.imencode(
                '.jpg',
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, 75]
            )
            
            if ret:
                yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'
        
        except Exception as e:
            logger.error(f"Stream error: {e}")
            time.sleep(1)

# ============= ROUTES =============

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/video')
def video():
    return Response(
        gen_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@app.route('/api/move')
def move():
    """Pan/Tilt control"""
    p = request.args.get('p', '03')
    t = request.args.get('t', '03')
    s = min(24, max(1, int(request.args.get('s', '10'))))
    pan_tilt(s, s, p, t)
    return 'OK'

@app.route('/api/stop')
def stop():
    """Stop all motion"""
    pan_tilt(0, 0, '03', '03')
    zoom('stop', 0)
    focus('stop', 0)
    return 'OK'

@app.route('/api/zoom')
def z():
    """Zoom control"""
    d = request.args.get('dir', 'stop')
    s = min(7, max(1, int(request.args.get('s', '1'))))
    zoom(d, s)
    return 'OK'

@app.route('/api/focus')
def f():
    """Focus control"""
    d = request.args.get('dir', 'stop')
    s = min(8, max(1, int(request.args.get('s', '1'))))
    focus(d, s)
    return 'OK'

@app.route('/api/preset/set')
def preset_set_api():
    """Save preset"""
    num = int(request.args.get('num', 1))
    success = preset_set(num)
    return jsonify({'success': success, 'preset': num})

@app.route('/api/preset/call')
def preset_call_api():
    """Call preset"""
    num = int(request.args.get('num', 1))
    success = preset_call(num)
    return jsonify({'success': success, 'preset': num})

@app.route('/api/preset/delete')
def preset_delete_api():
    """Delete preset"""
    num = int(request.args.get('num', 1))
    success = preset_delete(num)
    return jsonify({'success': success, 'preset': num})

@app.route('/api/config', methods=['GET', 'POST'])
def config():
    """Network configuration"""
    global CAM_IP, CAM_PORT, RTSP_URL
    
    if request.method == 'POST':
        try:
            CAM_IP = request.json.get('cam_ip', CAM_IP)
            CAM_PORT = int(request.json.get('cam_port', CAM_PORT))
            RTSP_URL = request.json.get('rtsp_url', RTSP_URL)
            save_config()
            logger.info(f"Config updated: {CAM_IP}:{CAM_PORT}")
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Config error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 400
    
    return jsonify({
        'cam_ip': CAM_IP,
        'cam_port': CAM_PORT,
        'rtsp_url': RTSP_URL
    })

@app.route('/api/status')
def status():
    """System status"""
    return jsonify({
        'cam_ip': CAM_IP,
        'cam_port': CAM_PORT,
        'reachable': state['reachable'],
        'preset_active': state['preset_active'],
        'zoom': state['zoom'],
        'focus': state['focus'],
        'pan': state['pan'],
        'tilt': state['tilt'],
        'stream_fps': state['stream_fps'],
        'stream_status': state['stream_status'],
        'last_cmd': state['last_cmd']
    })

# ============= UI HTML/CSS/JS =============

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PTZ11 Controller v6.1</title>
    <style>
        :root {
            --primary: #ff9800;
            --primary-dark: #e68900;
            --bg-dark: #1a1a1a;
            --bg-panel: #2d2d2d;
            --text-primary: #ffffff;
            --text-secondary: #aaaaaa;
            --border: #404040;
            --success: #4CAF50;
            --error: #f44336;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        html, body {
            width: 100%;
            height: 100%;
            background: var(--bg-dark);
            color: var(--text-primary);
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            font-size: 13px;
            overflow: hidden;
        }

        .container {
            display: flex;
            flex-direction: column;
            height: 100vh;
        }

        /* HEADER */
        .header {
            background: linear-gradient(135deg, #000 0%, #1a1a1a 100%);
            border-bottom: 2px solid var(--primary);
            padding: 12px 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 2px 12px rgba(0,0,0,0.5);
            z-index: 100;
        }

        .header-left h1 {
            font-size: 18px;
            font-weight: bold;
            color: var(--primary);
            margin: 0;
        }

        .header-left p {
            font-size: 11px;
            color: var(--text-secondary);
            margin-top: 2px;
        }

        .header-right {
            display: flex;
            align-items: center;
            gap: 20px;
        }

        .status-box {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
        }

        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: var(--text-secondary);
        }

        .status-dot.online {
            background: var(--success);
            box-shadow: 0 0 8px var(--success);
        }

        .status-dot.offline {
            background: var(--error);
        }

        .status-dot.buffering {
            background: var(--primary);
            animation: pulse 1s infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        /* MAIN CONTENT */
        .content {
            display: flex;
            flex: 1;
            gap: 8px;
            padding: 8px;
            overflow: hidden;
        }

        /* VIDEO SECTION */
        .video-section {
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 8px;
            min-width: 0;
        }

        .video-frame {
            background: #000;
            border: 2px solid var(--border);
            border-radius: 4px;
            overflow: hidden;
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
        }

        .video-frame img {
            width: 100%;
            height: 100%;
            object-fit: contain;
        }

        .video-overlay {
            position: absolute;
            top: 8px;
            right: 8px;
            background: rgba(0,0,0,0.7);
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 11px;
            color: var(--text-secondary);
        }

        .video-overlay.live {
            color: var(--success);
        }

        /* CONTROL PANEL */
        .control-panel {
            width: 360px;
            background: var(--bg-panel);
            border: 2px solid var(--border);
            border-radius: 4px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .panel-tabs {
            display: flex;
            background: #1a1a1a;
            border-bottom: 2px solid var(--border);
        }

        .tab-btn {
            flex: 1;
            padding: 10px;
            background: transparent;
            border: none;
            color: var(--text-secondary);
            cursor: pointer;
            font-weight: 600;
            font-size: 12px;
            transition: all 0.2s;
            border-bottom: 3px solid transparent;
        }

        .tab-btn:hover {
            background: rgba(255,152,0,0.1);
            color: var(--primary);
        }

        .tab-btn.active {
            color: var(--primary);
            border-bottom-color: var(--primary);
        }

        .panel-content {
            flex: 1;
            overflow-y: auto;
            padding: 12px;
        }

        .tab-pane {
            display: none;
        }

        .tab-pane.active {
            display: block;
        }

        /* JOYSTICK */
        .control-section {
            margin-bottom: 16px;
        }

        .section-title {
            font-size: 11px;
            font-weight: bold;
            color: var(--primary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 10px;
            padding-bottom: 6px;
            border-bottom: 1px solid var(--border);
        }

        .joystick-container {
            display: flex;
            justify-content: center;
            margin-bottom: 12px;
        }

        .joystick {
            position: relative;
            width: 180px;
            height: 180px;
            background: radial-gradient(circle at 35% 35%, #3d3d3d, #1a1a1a);
            border: 3px solid var(--border);
            border-radius: 50%;
            box-shadow:
                inset 0 2px 8px rgba(0,0,0,0.8),
                inset 0 -2px 8px rgba(255,255,255,0.1),
                0 8px 16px rgba(0,0,0,0.5);
            cursor: crosshair;
            user-select: none;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .joystick-ring {
            position: absolute;
            width: 120px;
            height: 120px;
            border: 2px dashed var(--border);
            border-radius: 50%;
            opacity: 0.5;
        }

        .joystick-center {
            position: absolute;
            width: 50px;
            height: 50px;
            background: radial-gradient(circle at 30% 30%, #666, #222);
            border-radius: 50%;
            box-shadow:
                0 4px 12px rgba(0,0,0,0.6),
                inset 0 2px 4px rgba(255,255,255,0.2),
                inset 0 -2px 4px rgba(0,0,0,0.8);
            cursor: grab;
            z-index: 10;
            transition: transform 0.05s ease-out;
            border: 2px solid var(--border);
        }

        .joystick-center.active {
            cursor: grabbing;
        }

        /* SLIDERS */
        .slider-group {
            display: flex;
            gap: 10px;
            margin-bottom: 12px;
        }

        .slider-item {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 6px;
        }

        .slider-label {
            font-size: 11px;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
        }

        input[type="range"] {
            width: 100%;
            height: 6px;
            -webkit-appearance: none;
            appearance: none;
            background: linear-gradient(to right, #ff6b00, var(--primary), #ffb700);
            outline: none;
            cursor: pointer;
            border-radius: 3px;
        }

        input[type="range"]::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 16px;
            height: 16px;
            background: white;
            border-radius: 50%;
            cursor: pointer;
            box-shadow: 0 2px 6px rgba(0,0,0,0.3);
            border: 2px solid var(--primary);
        }

        input[type="range"]::-moz-range-thumb {
            width: 16px;
            height: 16px;
            background: white;
            border-radius: 50%;
            cursor: pointer;
            box-shadow: 0 2px 6px rgba(0,0,0,0.3);
            border: 2px solid var(--primary);
        }

        .slider-value {
            background: rgba(255,152,0,0.1);
            border: 1px solid var(--border);
            padding: 4px 8px;
            border-radius: 3px;
            font-size: 10px;
            font-weight: bold;
            min-width: 50px;
            text-align: center;
            color: var(--primary);
        }

        /* BUTTONS */
        .button-group {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
            margin-bottom: 12px;
        }

        .button-group.full {
            grid-template-columns: 1fr;
        }

        .btn {
            padding: 10px 12px;
            background: linear-gradient(135deg, #444, #333);
            border: 2px solid var(--border);
            color: var(--text-primary);
            border-radius: 4px;
            cursor: pointer;
            font-weight: 600;
            font-size: 12px;
            transition: all 0.2s;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .btn:hover {
            background: linear-gradient(135deg, #555, #444);
            border-color: var(--primary);
            color: var(--primary);
            box-shadow: 0 0 12px rgba(255,152,0,0.3);
        }

        .btn:active {
            transform: scale(0.98);
        }

        .btn.primary {
            background: linear-gradient(135deg, var(--primary-dark), var(--primary));
            border-color: var(--primary);
            color: #000;
        }

        .btn.primary:hover {
            background: linear-gradient(135deg, var(--primary), var(--primary-dark));
            box-shadow: 0 0 16px rgba(255,152,0,0.4);
        }

        /* PRESETS GRID */
        .presets-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 6px;
            margin-bottom: 12px;
        }

        .preset-btn {
            aspect-ratio: 1;
            background: linear-gradient(135deg, #444, #333);
            border: 2px solid var(--border);
            color: var(--text-primary);
            border-radius: 4px;
            cursor: pointer;
            font-weight: bold;
            font-size: 10px;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .preset-btn:hover {
            background: linear-gradient(135deg, #555, #444);
            border-color: var(--primary);
            transform: scale(1.05);
        }

        .preset-btn.active {
            background: linear-gradient(135deg, var(--primary), var(--primary-dark));
            border-color: var(--primary);
            color: #000;
            box-shadow: 0 0 12px rgba(255,152,0,0.5);
        }

        /* NETWORK SETTINGS */
        .form-group {
            margin-bottom: 10px;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .form-label {
            font-size: 11px;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
        }

        input[type="text"],
        input[type="number"] {
            background: #1a1a1a;
            border: 2px solid var(--border);
            color: var(--text-primary);
            padding: 8px;
            border-radius: 4px;
            font-size: 12px;
            font-family: monospace;
            transition: all 0.2s;
        }

        input[type="text"]:focus,
        input[type="number"]:focus {
            outline: none;
            border-color: var(--primary);
            background: rgba(255,152,0,0.05);
            box-shadow: 0 0 8px rgba(255,152,0,0.2);
        }

        /* DEBUG TAB */
        .debug-display {
            background: #1a1a1a;
            border: 2px solid var(--border);
            padding: 8px;
            border-radius: 4px;
            font-family: 'Courier New', monospace;
            font-size: 11px;
            color: #0f0;
            max-height: 200px;
            overflow-y: auto;
            margin-bottom: 8px;
            white-space: pre-wrap;
            word-break: break-all;
        }

        /* SCROLLBAR */
        ::-webkit-scrollbar {
            width: 8px;
        }

        ::-webkit-scrollbar-track {
            background: #1a1a1a;
        }

        ::-webkit-scrollbar-thumb {
            background: var(--border);
            border-radius: 4px;
        }

        ::-webkit-scrollbar-thumb:hover {
            background: var(--primary);
        }

        /* RESPONSIVE */
        @media (max-width: 1200px) {
            .content {
                flex-direction: column;
            }
            .control-panel {
                width: 100%;
                height: 300px;
            }
            .video-section {
                min-height: 360px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- HEADER -->
        <div class="header">
            <div class="header-left">
                <h1>🎥 PTZ11 Controller</h1>
                <p>Device: 192.168.1.11 | Firmware: V1.3.81</p>
            </div>
            <div class="header-right">
                <div class="status-box">
                    <span>Camera</span>
                    <div class="status-dot" id="cam-status"></div>
                </div>
                <div class="status-box">
                    <span>Stream</span>
                    <div class="status-dot" id="stream-status"></div>
                </div>
            </div>
        </div>

        <!-- MAIN CONTENT -->
        <div class="content">
            <!-- VIDEO SECTION -->
            <div class="video-section">
                <div class="video-frame">
                    <img src="/video" alt="RTSP Stream" />
                    <div class="video-overlay" id="stream-info">● INITIALIZING</div>
                </div>
            </div>

            <!-- CONTROL PANEL -->
            <div class="control-panel">
                <div class="panel-tabs">
                    <button class="tab-btn active" data-tab="controller">CONTROLLER</button>
                    <button class="tab-btn" data-tab="presets">PRESETS</button>
                    <button class="tab-btn" data-tab="settings">SETTINGS</button>
                    <button class="tab-btn" data-tab="debug">DEBUG</button>
                </div>

                <div class="panel-content">
                    <!-- CONTROLLER TAB -->
                    <div class="tab-pane active" id="controller">
                        <!-- JOYSTICK -->
                        <div class="control-section">
                            <div class="section-title">Pan / Tilt</div>
                            <div class="joystick-container">
                                <div class="joystick" id="joypad">
                                    <div class="joystick-ring"></div>
                                    <div class="joystick-center" id="joy-knob"></div>
                                </div>
                            </div>
                        </div>

                        <!-- ZOOM & FOCUS -->
                        <div class="control-section">
                            <div class="section-title">Zoom & Focus</div>
                            <div class="slider-group">
                                <div class="slider-item">
                                    <div class="slider-label">Zoom</div>
                                    <input type="range" id="zoom" min="-7" max="7" value="0" 
                                           data-type="zoom" class="joystick-control">
                                    <div class="slider-value" id="zoom-val">STOP</div>
                                </div>
                                <div class="slider-item">
                                    <div class="slider-label">Focus</div>
                                    <input type="range" id="focus" min="-8" max="8" value="0"
                                           data-type="focus" class="joystick-control">
                                    <div class="slider-value" id="focus-val">AUTO</div>
                                </div>
                            </div>
                        </div>

                        <!-- ACTIONS -->
                        <div class="control-section">
                            <div class="button-group full">
                                <button class="btn primary" onclick="stopAll()">⏹ STOP ALL</button>
                            </div>
                            <div class="button-group">
                                <button class="btn" onclick="homePos()">🏠 Home</button>
                                <button class="btn" onclick="autoFocus()">🎯 Auto Focus</button>
                            </div>
                        </div>
                    </div>

                    <!-- PRESETS TAB -->
                    <div class="tab-pane" id="presets">
                        <div class="control-section">
                            <div class="section-title">Memory Presets (1-32)</div>
                            <div class="presets-grid" id="preset-grid"></div>
                        </div>
                        <div class="button-group full">
                            <button class="btn" onclick="clearAllPresets()">Clear All</button>
                        </div>
                    </div>

                    <!-- SETTINGS TAB -->
                    <div class="tab-pane" id="settings">
                        <div class="control-section">
                            <div class="section-title">Network Configuration</div>
                            <div class="form-group">
                                <label class="form-label">Camera IP</label>
                                <input type="text" id="cam-ip" placeholder="192.168.1.11">
                            </div>
                            <div class="form-group">
                                <label class="form-label">Camera Port (UDP)</label>
                                <input type="number" id="cam-port" placeholder="52381" min="1" max="65535">
                            </div>
                            <div class="form-group">
                                <label class="form-label">RTSP URL</label>
                                <input type="text" id="rtsp-url" placeholder="rtsp://...">
                            </div>
                            <div class="button-group full">
                                <button class="btn primary" onclick="saveConfig()">💾 Save Config</button>
                            </div>
                            <div class="button-group full">
                                <button class="btn" onclick="testConnection()">📡 Test Connection</button>
                            </div>
                        </div>
                    </div>

                    <!-- DEBUG TAB -->
                    <div class="tab-pane" id="debug">
                        <div class="control-section">
                            <div class="section-title">System Status</div>
                            <div class="debug-display" id="debug-info">Initializing...</div>
                            <button class="btn primary" onclick="refreshDebug()" style="width: 100%;">🔄 Refresh</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        // ============= JOYSTICK CONTROL =============
        let joyActive = false;
        let lastCmd = null;

        const joypad = document.getElementById('joypad');
        const joyKnob = document.getElementById('joy-knob');

        function updateJoyVisual(x, y) {
            const tx = x * 0.4;
            const ty = y * 0.4;
            joyKnob.style.transform = `translate(calc(-50% + ${tx}px), calc(-50% + ${ty}px))`;
        }

        function getJoyDirection(x, y) {
            if (x === 0 && y === 0) return { p: '03', t: '03', s: 1 };
            
            const angle = Math.atan2(y, x) * (180 / Math.PI);
            const dist = Math.sqrt(x*x + y*y);
            const maxDist = 70;
            const speed = Math.max(4, Math.min(24, Math.floor((dist/maxDist) * 20)));
            
            let p = '03', t = '03';
            
            // 8-direction mapping
            if (angle > -22.5 && angle <= 22.5) {
                p = '02'; // Right
            } else if (angle > 22.5 && angle <= 67.5) {
                p = '02'; t = '01'; // Right-Up
            } else if (angle > 67.5 && angle <= 112.5) {
                t = '01'; // Up
            } else if (angle > 112.5 && angle <= 157.5) {
                p = '01'; t = '01'; // Left-Up
            } else if (angle > 157.5 || angle <= -157.5) {
                p = '01'; // Left
            } else if (angle > -157.5 && angle <= -112.5) {
                p = '01'; t = '02'; // Left-Down
            } else if (angle > -112.5 && angle <= -67.5) {
                t = '02'; // Down
            } else if (angle > -67.5 && angle <= -22.5) {
                p = '02'; t = '02'; // Right-Down
            }
            
            return { p, t, s: speed };
        }

        joypad.addEventListener('mousedown', (e) => {
            joyActive = true;
            joyKnob.classList.add('active');
            handleJoyMove(e);
        });

        document.addEventListener('mousemove', (e) => {
            if (!joyActive) return;
            handleJoyMove(e);
        });

        document.addEventListener('mouseup', () => {
            if (!joyActive) return;
            joyActive = false;
            joyKnob.classList.remove('active');
            updateJoyVisual(0, 0);
            fetch('/api/stop').catch(() => {});
        });

        function handleJoyMove(e) {
            if (!joyActive) return;
            
            const rect = joypad.getBoundingClientRect();
            const centerX = rect.width / 2;
            const centerY = rect.height / 2;
            const x = e.clientX - rect.left - centerX;
            const y = e.clientY - rect.top - centerY;
            
            const dist = Math.sqrt(x*x + y*y);
            const maxDist = 70;
            
            let finalX = x, finalY = y;
            if (dist > maxDist) {
                const angle = Math.atan2(y, x);
                finalX = Math.cos(angle) * maxDist;
                finalY = Math.sin(angle) * maxDist;
            }
            
            updateJoyVisual(finalX, finalY);
            
            const dir = getJoyDirection(finalX, finalY);
            const url = `/api/move?p=${dir.p}&t=${dir.t}&s=${dir.s}`;
            
            if (url !== lastCmd) {
                fetch(url).catch(() => {});
                lastCmd = url;
            }
        }

        // ============= ZOOM & FOCUS SLIDERS =============
        document.querySelectorAll('.joystick-control').forEach(el => {
            el.addEventListener('input', (e) => {
                const type = e.target.dataset.type;
                const val = parseInt(e.target.value);
                
                if (type === 'zoom') {
                    if (val === 0) {
                        fetch('/api/zoom?dir=stop').catch(() => {});
                        document.getElementById('zoom-val').textContent = 'STOP';
                    } else {
                        const dir = val > 0 ? 'in' : 'out';
                        const spd = Math.abs(val);
                        fetch(`/api/zoom?dir=${dir}&s=${spd}`).catch(() => {});
                        document.getElementById('zoom-val').textContent = dir.toUpperCase() + ' ' + spd;
                    }
                } else if (type === 'focus') {
                    if (val === 0) {
                        fetch('/api/focus?dir=stop').catch(() => {});
                        document.getElementById('focus-val').textContent = 'AUTO';
                    } else {
                        const dir = val > 0 ? 'near' : 'far';
                        const spd = Math.abs(val);
                        fetch(`/api/focus?dir=${dir}&s=${spd}`).catch(() => {});
                        document.getElementById('focus-val').textContent = dir.toUpperCase() + ' ' + spd;
                    }
                }
            });
            
            el.addEventListener('change', (e) => {
                e.target.value = 0;
                const type = e.target.dataset.type;
                if (type === 'zoom') {
                    fetch('/api/zoom?dir=stop').catch(() => {});
                    document.getElementById('zoom-val').textContent = 'STOP';
                } else {
                    fetch('/api/focus?dir=stop').catch(() => {});
                    document.getElementById('focus-val').textContent = 'AUTO';
                }
            });
        });

        // ============= BUTTONS =============
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
            document.getElementById('focus').value = 0;
            document.getElementById('focus-val').textContent = 'AUTO';
        }

        // ============= PRESETS =============
        function generatePresets() {
            const grid = document.getElementById('preset-grid');
            grid.innerHTML = '';
            for (let i = 1; i <= 32; i++) {
                const btn = document.createElement('button');
                btn.className = 'preset-btn';
                btn.textContent = 'P' + i;
                btn.id = 'preset-' + i;
                btn.addEventListener('click', () => presetCall(i));
                btn.addEventListener('dblclick', () => presetSet(i));
                btn.title = 'Click: Call | Double-click: Save';
                grid.appendChild(btn);
            }
        }

        function presetCall(num) {
            fetch(`/api/preset/call?num=${num}`)
                .then(() => {
                    document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
                    document.getElementById('preset-' + num).classList.add('active');
                    updateStatus();
                })
                .catch(() => {});
        }

        function presetSet(num) {
            if (confirm(`Save current position to Preset ${num}?`)) {
                fetch(`/api/preset/set?num=${num}`)
                    .then(() => alert(`Preset ${num} saved!`))
                    .catch(() => alert('Error saving preset'));
            }
        }

        function clearAllPresets() {
            if (confirm('Delete ALL presets? This cannot be undone.')) {
                for (let i = 1; i <= 32; i++) {
                    fetch(`/api/preset/delete?num=${i}`).catch(() => {});
                }
                alert('All presets deleted');
            }
        }

        // ============= SETTINGS =============
        function loadConfig() {
            fetch('/api/config')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('cam-ip').value = data.cam_ip;
                    document.getElementById('cam-port').value = data.cam_port;
                    document.getElementById('rtsp-url').value = data.rtsp_url;
                })
                .catch(() => {});
        }

        function saveConfig() {
            const config = {
                cam_ip: document.getElementById('cam-ip').value,
                cam_port: parseInt(document.getElementById('cam-port').value),
                rtsp_url: document.getElementById('rtsp-url').value
            };
            
            fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    alert('Configuration saved!');
                } else {
                    alert('Error: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(err => alert('Error: ' + err));
        }

        function testConnection() {
            const btn = event.target;
            btn.disabled = true;
            btn.textContent = 'Testing...';
            
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    if (data.reachable) {
                        alert('✓ Camera is ONLINE');
                    } else {
                        alert('✗ Camera is OFFLINE');
                    }
                })
                .catch(() => alert('✗ Connection failed'))
                .finally(() => {
                    btn.disabled = false;
                    btn.textContent = '📡 Test Connection';
                });
        }

        // ============= DEBUG & STATUS =============
        function updateStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    // Update indicators
                    const camStat = document.getElementById('cam-status');
                    const streamStat = document.getElementById('stream-status');
                    const streamInfo = document.getElementById('stream-info');
                    
                    camStat.className = 'status-dot ' + (data.reachable ? 'online' : 'offline');
                    
                    if (data.stream_status === 'live') {
                        streamStat.className = 'status-dot online';
                        streamInfo.textContent = '● LIVE ' + data.stream_fps + ' FPS';
                        streamInfo.className = 'video-overlay live';
                    } else if (data.stream_status === 'buffering') {
                        streamStat.className = 'status-dot buffering';
                        streamInfo.textContent = '⟳ BUFFERING';
                        streamInfo.className = 'video-overlay';
                    } else {
                        streamStat.className = 'status-dot offline';
                        streamInfo.textContent = '● OFFLINE';
                        streamInfo.className = 'video-overlay';
                    }
                })
                .catch(() => {});
        }

        function refreshDebug() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    let text = 'PTZ11 SYSTEM STATUS\\n';
                    text += '═══════════════════════════════════\\n';
                    text += 'Camera IP: ' + data.cam_ip + ':' + data.cam_port + '\\n';
                    text += 'Reachable: ' + (data.reachable ? '✓ YES' : '✗ NO') + '\\n';
                    text += '\\n';
                    text += 'Stream Status: ' + data.stream_status.toUpperCase() + '\\n';
                    text += 'Stream FPS: ' + data.stream_fps + '\\n';
                    text += '\\n';
                    text += 'PTZ State:\\n';
                    text += '  Pan: ' + data.pan + '\\n';
                    text += '  Tilt: ' + data.tilt + '\\n';
                    text += '  Zoom: ' + data.zoom + '\\n';
                    text += '  Focus: ' + data.focus + '\\n';
                    text += '  Preset: ' + data.preset_active + '\\n';
                    text += '\\n';
                    text += 'Last Command:\\n';
                    text += '  ' + (data.last_cmd || 'none') + '\\n';
                    
                    document.getElementById('debug-info').textContent = text;
                })
                .catch(err => {
                    document.getElementById('debug-info').textContent = 'Error: ' + err;
                });
        }

        // ============= TAB NAVIGATION =============
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const tabName = btn.dataset.tab;
                
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
                
                btn.classList.add('active');
                document.getElementById(tabName).classList.add('active');
            });
        });

        // ============= INIT =============
        window.addEventListener('load', () => {
            generatePresets();
            loadConfig();
            updateStatus();
            refreshDebug();
            
            setInterval(updateStatus, 2000);
        });
    </script>
</body>
</html>"""

if __name__ == '__main__':
    load_config()
    
    print("""
    ╔════════════════════════════════════════════════════════════════╗
    ║        🎥 PTZ11 Controller v6.1 - Kiloview Edition             ║
    ║     Modern Clean UI + Professional Joystick Control            ║
    ║     Based on firmware: 3301432581P2107-V1.3.81                ║
    ╠════════════════════════════════════════════════════════════════╣
    ║                                                                ║
    ║  ✓ Device: 192.168.1.11:52381                                 ║
    ║  ✓ Web UI: http://127.0.0.1:5007                              ║
    ║  ✓ RTSP Stream: rtsp://192.168.1.11/1/h264major               ║
    ║                                                                ║
    ║  Features:                                                     ║
    ║    • Live RTSP H.264 stream (640x360)                         ║
    ║    • Fixed joystick with 8-direction control                  ║
    ║    • Smooth zoom/focus sliders                                ║
    ║    • Memory presets (P1-P32)                                  ║
    ║    • Network configuration                                    ║
    ║    • System diagnostics                                       ║
    ║                                                                ║
    ║  Press Ctrl+C to stop                                         ║
    ╠════════════════════════════════════════════════════════════════╣
    """)
    
    check_camera()
    
    # Background status checker
    def bg_check():
        while True:
            time.sleep(5)
            check_camera()
    
    t = threading.Thread(target=bg_check, daemon=True)
    t.start()
    
    app.run(host='127.0.0.1', port=5007, threaded=True, debug=False)

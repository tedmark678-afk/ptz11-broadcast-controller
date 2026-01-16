#!/usr/bin/env python3
"""
PTZ11 Broadcast Controller v6.3 - Kiloview Style
FIXED: Pointer capture for reliable joystick tracking
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

def pan_tilt(pan_speed, tilt_speed, pan_byte, tilt_byte):
    """
    Pan and tilt control with proper VISCA parameters
    
    pan_byte:  01=left, 02=right, 03=stop
    tilt_byte: 01=up, 02=down, 03=stop
    speeds: 1-24 (0x01-0x18)
    """
    if pan_speed < 0 or pan_speed > 24:
        pan_speed = 0
    if tilt_speed < 0 or tilt_speed > 24:
        tilt_speed = 0
    
    cmd = f"81 01 06 01 {pan_speed:02X} {tilt_speed:02X} {pan_byte} {tilt_byte}"
    send_cmd(cmd)
    state['pan'] = pan_byte
    state['tilt'] = tilt_byte

def zoom(direction, speed):
    """Zoom control (1-7)"""
    speed = max(0, min(7, speed))
    if direction == 'in':
        byte = 0x20 + speed
    elif direction == 'out':
        byte = 0x30 + speed
    else:
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
    else:
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
                
                now = time.time()
                if now - stream_last_time >= 1.0:
                    state['stream_fps'] = stream_frame_count
                    stream_frame_count = 0
                    stream_last_time = now
            
            frame = cv2.resize(frame, (640, 360))
            
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
    p_byte = request.args.get('p', '03')
    t_byte = request.args.get('t', '03')
    s = min(24, max(1, int(request.args.get('s', '10'))))
    pan_tilt(s, s, p_byte, t_byte)
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

HTML = """<!DOCTYPE html>
<html lang="en": 
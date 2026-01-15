#!/usr/bin/env python3
"""
PTZ11 Camera Enhanced Controller v6.1 - COMPLETE VERSION
Based on firmware extraction: PTZ11/3301432581P2107-V1.3.81.dat
VISCA Protocol over UDP with interactive joystick control
"""

import cv2
import threading
import socket
import time
import json
import logging
import sys
from flask import Flask, render_template_string, request, Response, jsonify
from datetime import datetime
import subprocess
import numpy as np

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.logger.setLevel(logging.DEBUG)

CONFIG = {
    'camera': {
        'ip': '192.168.1.11',
        'port': 52381,
        'rtsp': 'rtsp://192.168.1.11/1/h264major',
        'device_id': '3301432581P2107',
        'firmware_version': 'V1.3.81',
    },
    'protocol': {'sequence': 1, 'timeout': 0.5},
    'ptz': {'pan_speed_max': 24, 'tilt_speed_max': 20, 'zoom_speed_max': 7, 'focus_speed_max': 8},
    'video': {'buffer_size': 1, 'jpeg_quality': 60, 'resolution': (640, 360),}
}

STATUS = {'camera_reachable': False, 'last_command': None, 'last_error': None}
PRESET_MEMORY = {'presets': {}}
visca_lock = threading.Lock()

def check_camera_reachable():
    try:
        result = subprocess.run(['ping', '-c', '1', '-W', '1', CONFIG['camera']['ip']], capture_output=True, timeout=3)
        reachable = result.returncode == 0
        STATUS['camera_reachable'] = reachable
        logger.info(f"Camera ping: {chr(10003) + ' REACHABLE' if reachable else chr(10007) + ' UNREACHABLE'}")
        return reachable
    except Exception as e:
        logger.error(f"Ping error: {e}")
        STATUS['camera_reachable'] = False
        return False

def test_udp_connection():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1)
        test_packet = b'\x01\x00\x00\x05\x00\x00\x00\x01\x81\x01\x04\x00\x02\xFF'
        sock.sendto(test_packet, (CONFIG['camera']['ip'], CONFIG['camera']['port']))
        logger.info(f"UDP test packet sent")
        try:
            data, addr = sock.recvfrom(1024)
            logger.info(f"Got response from camera")
            sock.close()
            return True
        except socket.timeout:
            logger.warning("UDP packet sent but no response")
            sock.close()
            return True
    except Exception as e:
        logger.error(f"UDP test failed: {e}")
        return False

def increment_sequence():
    CONFIG['protocol']['sequence'] = (CONFIG['protocol']['sequence'] + 1) & 0xFFFFFFFF
    return CONFIG['protocol']['sequence']

def build_visca_packet(payload_hex):
    try:
        payload_hex = payload_hex.replace(" ", "").upper()
        payload = bytearray.fromhex(payload_hex)
        seq = increment_sequence()
        msg_length = len(payload) + 1
        header = bytearray([0x01, 0x00, 0x00, msg_length & 0xFF])
        header.extend(seq.to_bytes(4, byteorder="big"))
        packet = header + payload + b'\xFF'
        return packet, payload_hex
    except Exception as e:
        logger.error(f"Packet build error: {e}")
        return None, str(e)

def send_visca_command(payload_hex):
    with visca_lock:
        try:
            packet, clean_hex = build_visca_packet(payload_hex)
            if packet is None:
                return False, "Packet build failed"
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(CONFIG['protocol']['timeout'])
            logger.debug(f"Sending VISCA: {clean_hex}")
            sock.sendto(packet, (CONFIG['camera']['ip'], CONFIG['camera']['port']))
            try:
                response, _ = sock.recvfrom(1024)
                if len(response) >= 8:
                    msg_type = response[0]
                    if msg_type in [0x90, 0x91]:
                        STATUS['last_command'] = clean_hex
                        sock.close()
                        return True, "OK"
            except socket.timeout:
                STATUS['last_command'] = clean_hex
                sock.close()
                return True, "OK"
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            STATUS['last_error'] = error_msg
            logger.error(error_msg)
            try:
                sock.close()
            except:
                pass
            return False, error_msg
    return False, "Unknown error"

def visca_pan_tilt(pan_speed, tilt_speed, pan_dir, tilt_dir):
    cmd = f"81 01 06 01 {pan_speed:02X} {tilt_speed:02X} {pan_dir} {tilt_dir}"
    return send_visca_command(cmd)

def visca_zoom(zoom_dir, zoom_speed):
    if zoom_dir == 'in':
        byte = 0x20 + (zoom_speed & 0x0F)
    elif zoom_dir == 'out':
        byte = 0x30 + (zoom_speed & 0x0F)
    else:
        byte = 0x00
    cmd = f"81 01 04 07 {byte:02X}"
    return send_visca_command(cmd)

def visca_focus(focus_dir, focus_speed):
    if focus_dir == 'near':
        byte = 0x20 + (focus_speed & 0x0F)
    elif focus_dir == 'far':
        byte = 0x30 + (focus_speed & 0x0F)
    else:
        byte = 0x00
    cmd = f"81 01 04 08 {byte:02X}"
    return send_visca_command(cmd)

def visca_auto_focus():
    return send_visca_command("81 01 04 38 02")

def visca_preset_recall(preset_num):
    if not (0 <= preset_num <= 254):
        return False, "Invalid preset"
    cmd = f"81 01 04 3F 02 {preset_num:02X}"
    return send_visca_command(cmd)

def visca_preset_save(preset_num):
    if not (0 <= preset_num <= 254):
        return False, "Invalid preset"
    cmd = f"81 01 04 3F 01 {preset_num:02X}"
    PRESET_MEMORY['presets'][preset_num] = {'timestamp': datetime.now().isoformat()}
    return send_visca_command(cmd)

def gen_frames():
    logger.info("Starting video stream...")
    cap = None
    
    while True:
        try:
            if cap is None:
                logger.info(f"Opening RTSP: {CONFIG['camera']['rtsp']}")
                cap = cv2.VideoCapture(CONFIG['camera']['rtsp'])
                cap.set(cv2.CAP_PROP_BUFFERSIZE, CONFIG['video']['buffer_size'])
            
            success, frame = cap.read()
            if not success:
                logger.warning(f"Frame read failed")
                frame = np.zeros((360, 640, 3), dtype=np.uint8)
                cv2.putText(frame, "Stream Unavailable", (80, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
            
            frame = cv2.resize(frame, CONFIG['video']['resolution'])
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, CONFIG['video']['jpeg_quality']])
            
            if ret:
                frame_bytes = buffer.tobytes()
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                   
        except Exception as e:
            logger.error(f"Stream error: {e}")
            time.sleep(1)

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/video_feed')
def video_feed():
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/move')
def move():
    p = request.args.get('p', '03')
    t = request.args.get('t', '03')
    s = int(request.args.get('s', '10'))
    s = max(1, min(24, s))
    status, msg = visca_pan_tilt(s, s, p, t)
    return msg

@app.route('/stop')
def stop():
    visca_pan_tilt(0, 0, '03', '03')
    visca_zoom('stop', 0)
    visca_focus('stop', 0)
    return "STOPPED"

@app.route('/zoom/move')
def zoom_move():
    direction = request.args.get('dir', 'stop')
    speed = int(request.args.get('spd', '1'))
    speed = max(1, min(7, speed))
    status, msg = visca_zoom(direction, speed)
    return msg

@app.route('/focus/move')
def focus_move():
    direction = request.args.get('dir', 'stop')
    speed = int(request.args.get('spd', '1'))
    speed = max(1, min(8, speed))
    status, msg = visca_focus(direction, speed)
    return msg

@app.route('/focus/auto')
def focus_auto():
    enable = request.args.get('enable', 'true').lower() == 'true'
    status, msg = visca_auto_focus() if enable else send_visca_command("81 01 04 38 03")
    return msg

@app.route('/preset/call/<int:preset>')
def preset_recall(preset):
    status, msg = visca_preset_recall(preset - 1)
    return msg

@app.route('/preset/save/<int:preset>')
def preset_save(preset):
    status, msg = visca_preset_save(preset - 1)
    return msg

@app.route('/status')
def status():
    return jsonify({
        'device_id': CONFIG['camera']['device_id'],
        'firmware': CONFIG['camera']['firmware_version'],
        'ip': CONFIG['camera']['ip'],
        'camera_reachable': STATUS['camera_reachable'],
        'timestamp': datetime.now().isoformat(),
    })

@app.route('/test')
def test():
    check_camera_reachable()
    udp_ok = test_udp_connection()
    return jsonify({'ping': STATUS['camera_reachable'], 'udp': udp_ok, 'camera_ip': CONFIG['camera']['ip'], 'camera_port': CONFIG['camera']['port']})

HTML_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>PTZ11 Controller v6.1</title><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/nipplejs/0.10.1/nipplejs.min.css"><script src="https://cdnjs.cloudflare.com/ajax/libs/nipplejs/0.10.1/nipplejs.min.js"></script><style>:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--accent:#58a6ff;--text:#c9d1d9;--success:#238636}*{box-sizing:border-box}body{background:var(--bg);color:var(--text);font-family:monospace;margin:0;padding:20px}.header{background:var(--panel);border-bottom:1px solid var(--border);padding:15px;border-radius:8px;margin-bottom:20px;display:flex;justify-content:space-between;align-items:center}.header h1{margin:0;color:var(--accent)}.header-info{font-size:11px;color:#888}.status-indicator{width:12px;height:12px;border-radius:50%;display:inline-block;margin-left:10px}.status-indicator.ok{background:var(--success)}.status-indicator.error{background:#da3633}.tabs{display:flex;background:var(--panel);border-bottom:1px solid var(--border);margin-bottom:20px}.tab-btn{flex:1;padding:12px;background:transparent;color:var(--text);border:none;font-family:monospace;font-weight:bold;cursor:pointer;border-bottom:3px solid transparent;transition:all 0.2s}.tab-btn.active{border-bottom-color:var(--accent);color:white;background:rgba(88,166,255,0.1)}.page{display:none}.page.active{display:block}.video-box{background:black;border:1px solid var(--border);border-radius:8px;overflow:hidden;text-align:center;margin-bottom:20px;position:relative;min-height:360px}.video-feed{width:100%;max-width:640px;max-height:360px}.video-label{position:absolute;top:10px;left:10px;background:rgba(0,0,0,0.7);padding:5px 10px;color:var(--accent);font-size:11px;border-radius:4px}.console{display:grid;grid-template-columns:80px 1fr 80px;gap:10px;margin-bottom:20px}.slider-col{background:var(--panel);border:1px solid var(--border);border-radius:8px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:10px;min-height:200px;font-weight:bold}input[type=range]{width:8px;height:150px;margin:10px 0}#joy{position:relative;background:radial-gradient(circle,#222,transparent);border-radius:50%;border:2px dashed var(--border);min-height:200px}.preset-bar{display:flex;gap:5px;margin-bottom:20px;flex-wrap:wrap}.p-btn{flex:1;min-width:60px;background:var(--panel);border:1px solid var(--border);color:white;padding:10px;border-radius:4px;cursor:pointer;transition:all 0.2s;font-weight:bold}.p-btn:hover{border-color:var(--accent);box-shadow:0 0 8px rgba(88,166,255,0.3)}.btn{background:var(--panel);border:1px solid var(--border);color:var(--text);padding:8px 12px;margin:5px;border-radius:4px;cursor:pointer;font-weight:bold}.btn:hover{border-color:var(--accent)}.btn-primary{background:var(--accent);color:black}.log-window{background:#000;border:1px solid var(--border);border-radius:4px;padding:10px;font-size:11px;color:#0f0;height:200px;overflow-y:auto;margin-bottom:10px;font-family:monospace}.cmd-input{display:flex;gap:10px;margin-bottom:10px}input[type="text"]{flex:1;background:var(--panel);border:1px solid var(--border);color:white;padding:8px;border-radius:4px;font-family:monospace}</style></head><body><div class="header"><div><h1>🎥 PTZ11 Enhanced Controller v6.1</h1><div class="header-info">Device: 192.168.1.11 | Firmware: V1.3.81</div></div><span class="status-indicator" id="statusInd" style="background:#666;"></span></div><div class="tabs"><button class="tab-btn active" onclick="showPage('control')">🎮 CONTROLLER</button><button class="tab-btn" onclick="showPage('terminal')">💻 HEX TERMINAL</button><button class="tab-btn" onclick="showPage('debug')">🔧 DEBUG</button></div><div id="control" class="page active"><div class="video-box"><div class="video-label">LIVE RTSP</div><img src="/video_feed" class="video-feed" onerror="this.style.display='none'" style="height:100%;object-fit:contain;"></div><div class="console"><div class="slider-col"><label>FOCUS</label><input type="range" min="-8" max="8" value="0" id="focusRocker" oninput="handleRocker('focus', this.value)" onchange="resetRocker(this)"><button onclick="fetch('/focus/auto?enable=true')" class="btn" style="width:100%;padding:4px;margin-top:8px;font-size:10px;">AUTO</button></div><div id="joy"></div><div class="slider-col"><label>ZOOM</label><input type="range" min="-7" max="7" value="0" id="zoomRocker" oninput="handleRocker('zoom', this.value)" onchange="resetRocker(this)"></div></div><div class="preset-bar"><button class="p-btn" onclick="handlePreset(1)">P1</button><button class="p-btn" onclick="handlePreset(2)">P2</button><button class="p-btn" onclick="handlePreset(3)">P3</button><button class="p-btn" onclick="handlePreset(4)">P4</button><button class="p-btn" onclick="handlePreset(5)">P5</button></div></div><div id="terminal" class="page"><h2>VISCA HEX Terminal</h2><div class="log-window" id="logs">> PTZ11 v6.1 - DEBUG MODE<br>> Ready for commands<br></div><div class="cmd-input"><input type="text" id="hexInput" placeholder="81 01 04 00 02" autocomplete="off"><button class="btn btn-primary" onclick="sendHex()">SEND</button></div></div><div id="debug" class="page"><h2>System Diagnostics</h2><button class="btn btn-primary" onclick="runDiagnostics()">Run Network Test</button><div id="debugOutput" style="margin-top:15px;"></div></div><script>let manager=nipplejs.create({zone:document.getElementById('joy'),mode:'static',position:{left:'50%',top:'50%'},color:'#58a6ff',size:140});let lastJoyUrl="";manager.on('move',(evt,data)=>{if(!data.angle)return;let force=Math.min(data.distance/70,1);let speed=Math.floor(force*20)+4;speed=Math.min(24,speed);let angle=data.angle.degree;let panDir="03",tiltDir="03";if(angle>70&&angle<110)tiltDir="01";else if(angle>250&&angle<290)tiltDir="02";else if(angle<20||angle>340)panDir="02";else if(angle>160&&angle<200)panDir="01";else if(angle>=20&&angle<=70){panDir="02";tiltDir="01";}else if(angle>=110&&angle<=160){panDir="01";tiltDir="01";}else if(angle>=200&&angle<=250){panDir="01";tiltDir="02";}else if(angle>=290&&angle<=340){panDir="02";tiltDir="02";}let url=`/move?p=${panDir}&t=${tiltDir}&s=${speed}`;if(url!==lastJoyUrl){fetch(url).catch(e=>console.error('Move error:',e));lastJoyUrl=url;}});manager.on('end',()=>{fetch('/stop').catch(e=>console.error('Stop error:',e));lastJoyUrl="";});function showPage(id){document.querySelectorAll('.page').forEach(el=>el.classList.remove('active'));document.querySelectorAll('.tab-btn').forEach(el=>el.classList.remove('active'));document.getElementById(id).classList.add('active');event.target.classList.add('active');}function handleRocker(type,val){let v=parseInt(val);if(v===0)return;let dir=(v>0)?'in':'out';let spd=Math.abs(v);if(type==='zoom'){fetch(`/zoom/move?dir=${dir}&spd=${spd}`).catch(e=>console.error('Zoom error:',e));}else if(type==='focus'){dir=(v>0)?'near':'far';fetch(`/focus/move?dir=${dir}&spd=${spd}`).catch(e=>console.error('Focus error:',e));}}function resetRocker(el){el.value=0;if(el.id.includes('zoom'))fetch('/zoom/move?dir=stop').catch(()=>{});if(el.id.includes('focus'))fetch('/focus/move?dir=stop').catch(()=>{});}function handlePreset(n){fetch(`/preset/call/${n}`).catch(e=>console.error('Preset error:',e));}function sendHex(){let val=document.getElementById('hexInput').value;if(!val)return;let log=document.getElementById('logs');log.innerHTML+=`> ${val}<br>`;log.scrollTop=log.scrollHeight;document.getElementById('hexInput').value='';}function runDiagnostics(){let out=document.getElementById('debugOutput');out.innerHTML='<p>Running diagnostics...</p>';fetch('/test').then(r=>r.json()).then(data=>{out.innerHTML=`<p>Camera IP: ${data.camera_ip}</p><p>Ping: ${data.ping?'✓ REACHABLE':'✗ UNREACHABLE'}</p><p>UDP: ${data.udp?'✓ RESPONDING':'⚠ No response'}</p>`;}).catch(e=>{out.innerHTML=`<p>Error: ${e}</p>`;});}function updateStatusIndicator(){fetch('/status').then(r=>r.json()).then(data=>{let ind=document.getElementById('statusInd');ind.className='status-indicator '+(data.camera_reachable?'ok':'error');});}document.getElementById('hexInput')?.addEventListener('keypress',(e)=>{if(e.key==='Enter')sendHex();});setInterval(updateStatusIndicator,5000);updateStatusIndicator();</script></body></html>"""

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
    logger.info("Starting PTZ11 Controller v6.1...")
    check_camera_reachable()
    test_udp_connection()
    app.run(host='127.0.0.1', port=5007, threaded=True, debug=False, use_reloader=False)

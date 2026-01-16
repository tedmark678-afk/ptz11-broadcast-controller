#!/usr/bin/env python3
"""
PTZ11 Broadcast Controller v6.5
FIXED: Joystick + Button events working
"""

import cv2, threading, socket, time, logging, json, os
from flask import Flask, render_template_string, request, Response, jsonify
import subprocess, numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

CAM_IP, CAM_PORT, RTSP_URL = '192.168.1.11', 52381, 'rtsp://192.168.1.11/1/h264major'
CONFIG_FILE = 'ptz_config.json'

state = {'preset_active': 0, 'zoom': 0, 'focus': 0, 'pan': 0, 'tilt': 0, 'last_cmd': None, 'reachable': False, 'stream_fps': 0, 'stream_status': 'initializing'}
lock, seq, stream_frame_count, stream_last_time = threading.Lock(), 0, 0, time.time()

def load_config():
    global CAM_IP, CAM_PORT, RTSP_URL
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
                CAM_IP, CAM_PORT, RTSP_URL = cfg.get('cam_ip', CAM_IP), cfg.get('cam_port', CAM_PORT), cfg.get('rtsp_url', RTSP_URL)
        except: pass

def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f: json.dump({'cam_ip': CAM_IP, 'cam_port': CAM_PORT, 'rtsp_url': RTSP_URL}, f, indent=2)
    except: pass

def get_seq():
    global seq
    seq = (seq + 1) & 0xFFFFFFFF
    return seq

def visca_packet(payload_hex):
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
    with lock:
        try:
            pkt = visca_packet(payload_hex)
            if not pkt: return False
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            sock.sendto(pkt, (CAM_IP, CAM_PORT))
            state['last_cmd'] = payload_hex
            try: sock.recvfrom(1024)
            except: pass
            finally: sock.close()
            return True
        except: return False

def pan_tilt(pan_speed, tilt_speed, pan_byte, tilt_byte):
    pan_speed = max(0, min(24, pan_speed))
    tilt_speed = max(0, min(24, tilt_speed))
    cmd = f"81 01 06 01 {pan_speed:02X} {tilt_speed:02X} {pan_byte} {tilt_byte}"
    send_cmd(cmd)
    state['pan'], state['tilt'] = pan_byte, tilt_byte

def zoom(direction, speed):
    speed = max(0, min(7, speed))
    byte = (0x20 + speed) if direction == 'in' else (0x30 + speed) if direction == 'out' else 0x00
    send_cmd(f"81 01 04 07 {byte:02X}")
    state['zoom'] = direction

def focus(direction, speed):
    speed = max(0, min(8, speed))
    byte = (0x20 + speed) if direction == 'near' else (0x30 + speed) if direction == 'far' else 0x00
    send_cmd(f"81 01 04 08 {byte:02X}")
    state['focus'] = direction

def preset_set(num):
    if 1 <= num <= 255:
        send_cmd(f"81 01 04 3F 00 {num:02X}")
        state['preset_active'] = num
        return True
    return False

def preset_call(num):
    if 1 <= num <= 255:
        send_cmd(f"81 01 04 3F 02 {num:02X}")
        state['preset_active'] = num
        return True
    return False

def preset_delete(num):
    if 1 <= num <= 255:
        send_cmd(f"81 01 04 3F 01 {num:02X}")
        return True
    return False

def check_camera():
    try:
        result = subprocess.run(['ping', '-c', '1', '-W', '1', CAM_IP], capture_output=True, timeout=2)
        state['reachable'] = result.returncode == 0
    except:
        state['reachable'] = False

def gen_frames():
    global stream_frame_count, stream_last_time
    cap, error_count = None, 0
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
                    cv2.putText(frame, 'RTSP Stream Offline', (120, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 255), 2)
                    state['stream_status'] = 'offline'
                else:
                    time.sleep(0.1)
                    continue
            else:
                error_count, state['stream_status'], stream_frame_count = 0, 'live', stream_frame_count + 1
                now = time.time()
                if now - stream_last_time >= 1.0:
                    state['stream_fps'], stream_frame_count, stream_last_time = stream_frame_count, 0, now
            frame = cv2.resize(frame, (640, 360))
            ret, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
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
    p, t, s = request.args.get('p', '03'), request.args.get('t', '03'), min(24, max(1, int(request.args.get('s', '10'))))
    logger.info(f"MOVE: p={p} t={t} s={s}")
    pan_tilt(s, s, p, t)
    return 'OK'

@app.route('/api/stop')
def stop():
    logger.info("STOP")
    pan_tilt(0, 0, '03', '03')
    zoom('stop', 0)
    focus('stop', 0)
    return 'OK'

@app.route('/api/zoom')
def z():
    d, s = request.args.get('dir', 'stop'), min(7, max(1, int(request.args.get('s', '1'))))
    logger.info(f"ZOOM: dir={d} s={s}")
    zoom(d, s)
    return 'OK'

@app.route('/api/focus')
def f():
    d, s = request.args.get('dir', 'stop'), min(8, max(1, int(request.args.get('s', '1'))))
    logger.info(f"FOCUS: dir={d} s={s}")
    focus(d, s)
    return 'OK'

@app.route('/api/preset/set')
def preset_set_api():
    num = int(request.args.get('num', 1))
    logger.info(f"PRESET SET: {num}")
    return jsonify({'success': preset_set(num), 'preset': num})

@app.route('/api/preset/call')
def preset_call_api():
    num = int(request.args.get('num', 1))
    logger.info(f"PRESET CALL: {num}")
    return jsonify({'success': preset_call(num), 'preset': num})

@app.route('/api/preset/delete')
def preset_delete_api():
    num = int(request.args.get('num', 1))
    logger.info(f"PRESET DELETE: {num}")
    return jsonify({'success': preset_delete(num), 'preset': num})

@app.route('/api/config', methods=['GET', 'POST'])
def config():
    global CAM_IP, CAM_PORT, RTSP_URL
    if request.method == 'POST':
        try:
            CAM_IP, CAM_PORT, RTSP_URL = request.json.get('cam_ip', CAM_IP), int(request.json.get('cam_port', CAM_PORT)), request.json.get('rtsp_url', RTSP_URL)
            save_config()
            logger.info(f"CONFIG SAVED: {CAM_IP}:{CAM_PORT}")
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Config error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'cam_ip': CAM_IP, 'cam_port': CAM_PORT, 'rtsp_url': RTSP_URL})

@app.route('/api/status')
def status():
    return jsonify({'cam_ip': CAM_IP, 'cam_port': CAM_PORT, 'reachable': state['reachable'], 'preset_active': state['preset_active'], 'zoom': state['zoom'], 'focus': state['focus'], 'pan': state['pan'], 'tilt': state['tilt'], 'stream_fps': state['stream_fps'], 'stream_status': state['stream_status'], 'last_cmd': state['last_cmd']})

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PTZ11 Controller v6.5</title>
<style>
:root{--primary:#ff9800;--bg:#1a1a1a;--text:#fff;--border:#404040}
*{margin:0;padding:0;box-sizing:border-box}
body{width:100%;height:100vh;background:var(--bg);color:var(--text);font-family:Segoe UI;font-size:13px;overflow:hidden}
.container{display:flex;flex-direction:column;height:100vh}
.header{background:linear-gradient(135deg,#000,#1a1a1a);border-bottom:2px solid var(--primary);padding:12px 16px;display:flex;justify-content:space-between;align-items:center}
.header h1{font-size:18px;color:var(--primary)}
.content{display:flex;flex:1;gap:8px;padding:8px;overflow:hidden}
.video{flex:1;background:#000;border:2px solid var(--border);border-radius:4px;display:flex;align-items:center;justify-content:center;position:relative}
.video img{width:100%;height:100%;object-fit:contain}
.video-overlay{position:absolute;top:8px;right:8px;background:rgba(0,0,0,0.7);padding:6px 12px;border-radius:4px;font-size:11px;color:#aaa}
.panel{width:360px;background:#2d2d2d;border:2px solid var(--border);border-radius:4px;display:flex;flex-direction:column;overflow:hidden}
.tabs{display:flex;background:#1a1a1a;border-bottom:2px solid var(--border)}
.tab-btn{flex:1;padding:10px;background:0;border:0;color:#aaa;cursor:pointer;font-weight:600;transition:all 0.2s;border-bottom:3px solid transparent}
.tab-btn.active{color:var(--primary);border-bottom-color:var(--primary)}
.tab-content{flex:1;overflow-y:auto;padding:12px}
.tab-pane{display:none}
.tab-pane.active{display:block}
.section{margin-bottom:16px}
.section-title{font-size:11px;font-weight:bold;color:var(--primary);text-transform:uppercase;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.joystick-wrap{display:flex;justify-content:center;margin-bottom:12px}
.joystick{position:relative;width:180px;height:180px;background:radial-gradient(circle at 35% 35%,#3d3d3d,#1a1a1a);border:3px solid var(--border);border-radius:50%;box-shadow:inset 0 2px 8px rgba(0,0,0,0.8),inset 0 -2px 8px rgba(255,255,255,0.1),0 8px 16px rgba(0,0,0,0.5);cursor:crosshair;user-select:none;display:flex;align-items:center;justify-content:center;touch-action:none}
.joystick-ring{position:absolute;width:120px;height:120px;border:2px dashed var(--border);border-radius:50%;opacity:0.5}
.joystick-knob{position:absolute;width:50px;height:50px;background:radial-gradient(circle at 30% 30%,#666,#222);border-radius:50%;box-shadow:0 4px 12px rgba(0,0,0,0.6),inset 0 2px 4px rgba(255,255,255,0.2);cursor:grab;z-index:10;transition:transform 0.02s ease-out;border:2px solid var(--border)}
.joystick-knob.active{cursor:grabbing}
.slider-group{display:flex;gap:10px;margin-bottom:12px}
.slider-item{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px}
input[type="range"]{width:100%;height:6px;background:linear-gradient(90deg,#ff6b00,var(--primary),#ffb700);border-radius:3px;cursor:pointer;-webkit-appearance:none;appearance:none}
input[type="range"]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;background:white;border-radius:50%;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,0.3);border:2px solid var(--primary)}
input[type="range"]::-moz-range-thumb{width:16px;height:16px;background:white;border-radius:50%;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,0.3);border:2px solid var(--primary)}
.slider-value{background:rgba(255,152,0,0.1);border:1px solid var(--border);padding:4px 8px;border-radius:3px;font-size:10px;font-weight:bold;color:var(--primary);text-align:center}
.btn{padding:10px 12px;background:linear-gradient(135deg,#444,#333);border:2px solid var(--border);color:var(--text);border-radius:4px;cursor:pointer;font-weight:600;font-size:12px;transition:all 0.2s;text-transform:uppercase}
.btn:hover{background:linear-gradient(135deg,#555,#444);border-color:var(--primary);color:var(--primary)}
.btn:active{transform:scale(0.98)}
.btn.primary{background:linear-gradient(135deg,var(--primary),#ff9800);border-color:var(--primary);color:#000}
.btn-group{display:grid;grid-template-columns:1fr;gap:8px;margin-bottom:12px}
.btn-group.two{grid-template-columns:repeat(2,1fr)}
.presets{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:12px}
.preset-btn{aspect-ratio:1;background:linear-gradient(135deg,#444,#333);border:2px solid var(--border);color:var(--text);border-radius:4px;cursor:pointer;font-weight:bold;font-size:10px;display:flex;align-items:center;justify-content:center;transition:all 0.2s}
.preset-btn:hover{border-color:var(--primary)}
.preset-btn:active{transform:scale(0.95)}
.preset-btn.active{background:linear-gradient(135deg,var(--primary),#ff9800);color:#000}
@media(max-width:1200px){.content{flex-direction:column}.panel{width:100%;height:300px}}
</style>
</head>
<body>
<div class="container">
<div class="header">
<div>
<h1>[CAMERA] PTZ11 Controller</h1>
<p style="font-size:11px;color:#aaa;margin-top:2px">Device: 192.168.1.11 | v6.5</p>
</div>
<div style="display:flex;gap:20px">
<div style="display:flex;gap:8px;font-size:12px"><span>Camera</span><div style="width:10px;height:10px;border-radius:50%;background:#aaa" id="cam-dot"></div></div>
<div style="display:flex;gap:8px;font-size:12px"><span>Stream</span><div style="width:10px;height:10px;border-radius:50%;background:#aaa" id="stream-dot"></div></div>
</div>
</div>
<div class="content">
<div class="video">
<img src="/video" alt="RTSP">
<div class="video-overlay" id="stream-info">* INITIALIZING</div>
</div>
<div class="panel">
<div class="tabs">
<button class="tab-btn active" data-tab="controller">CONTROLLER</button>
<button class="tab-btn" data-tab="presets">PRESETS</button>
<button class="tab-btn" data-tab="settings">SETTINGS</button>
<button class="tab-btn" data-tab="debug">DEBUG</button>
</div>
<div class="tab-content">
<div class="tab-pane active" id="controller">
<div class="section">
<div class="section-title">Pan / Tilt</div>
<div class="slider-item" style="margin-bottom:12px;font-size:10px;color:#aaa">
<div style="font-size:11px;font-weight:600;text-transform:uppercase">Speed</div>
<input type="range" id="joy-speed" min="1" max="10" value="5" style="margin:4px 0">
<div class="slider-value" id="joy-spd">Medium (5/10)</div>
</div>
<div class="joystick-wrap">
<div class="joystick" id="joypad">
<div class="joystick-ring"></div>
<div class="joystick-knob" id="joy-knob"></div>
</div>
</div>
</div>
<div class="section">
<div class="section-title">Zoom & Focus</div>
<div class="slider-group">
<div class="slider-item">
<div style="font-size:11px;font-weight:600;text-transform:uppercase">Zoom</div>
<input type="range" id="zoom" min="-7" max="7" value="0" data-type="zoom" class="ctrl-slider">
<div class="slider-value" id="zoom-val">STOP</div>
</div>
<div class="slider-item">
<div style="font-size:11px;font-weight:600;text-transform:uppercase">Focus</div>
<input type="range" id="focus" min="-8" max="8" value="0" data-type="focus" class="ctrl-slider">
<div class="slider-value" id="focus-val">AUTO</div>
</div>
</div>
</div>
<div class="section">
<div class="btn-group"><button class="btn primary" id="stop-btn">[STOP] STOP ALL</button></div>
<div class="btn-group two">
<button class="btn" id="home-btn">[HOME] Home</button>
<button class="btn" id="focus-btn">[FOCUS] Auto Focus</button>
</div>
</div>
</div>
<div class="tab-pane" id="presets">
<div class="section">
<div class="section-title">Memory Presets</div>
<div class="presets" id="preset-grid"></div>
</div>
<div class="btn-group"><button class="btn" id="clear-btn">Clear All</button></div>
</div>
<div class="tab-pane" id="settings">
<div class="section">
<div class="section-title">Network Config</div>
<div style="margin-bottom:10px">
<label style="font-size:11px;font-weight:600;text-transform:uppercase;display:block;margin-bottom:4px">Camera IP</label>
<input type="text" id="cam-ip" style="width:100%;padding:8px;background:#1a1a1a;border:2px solid var(--border);color:var(--text);border-radius:4px;font-family:monospace;font-size:12px">
</div>
<div style="margin-bottom:10px">
<label style="font-size:11px;font-weight:600;text-transform:uppercase;display:block;margin-bottom:4px">Camera Port</label>
<input type="number" id="cam-port" style="width:100%;padding:8px;background:#1a1a1a;border:2px solid var(--border);color:var(--text);border-radius:4px;font-family:monospace;font-size:12px">
</div>
<div style="margin-bottom:10px">
<label style="font-size:11px;font-weight:600;text-transform:uppercase;display:block;margin-bottom:4px">RTSP URL</label>
<input type="text" id="rtsp-url" style="width:100%;padding:8px;background:#1a1a1a;border:2px solid var(--border);color:var(--text);border-radius:4px;font-family:monospace;font-size:12px">
</div>
<div class="btn-group"><button class="btn primary" id="save-btn" style="width:100%">[SAVE] Save</button></div>
<div class="btn-group"><button class="btn" id="test-btn" style="width:100%">[TEST] Test</button></div>
</div>
</div>
<div class="tab-pane" id="debug">
<div class="section">
<div class="section-title">System Status</div>
<div style="background:#1a1a1a;border:2px solid var(--border);padding:8px;border-radius:4px;font-family:monospace;font-size:11px;color:#0f0;max-height:180px;overflow-y:auto;margin-bottom:8px;white-space:pre-wrap;word-break:break-all" id="debug-info">Initializing...</div>
<button class="btn primary" id="refresh-btn" style="width:100%">[REFRESH] Refresh</button>
</div>
</div>
</div>
</div>
</div>
</div>
</body>
<script>
let joyActive=false, lastCmd=null, joySpeedMult=0.5;
const joypad=document.getElementById('joypad'),
      joyKnob=document.getElementById('joy-knob'),
      joySpeed=document.getElementById('joy-speed');

joySpeed.addEventListener('input', e => {
    const val=parseInt(e.target.value);
    joySpeedMult=val/10;
    const labels=['Very Slow','Slow','Slow','Normal','Normal','Medium','Fast','Fast','Very Fast','Max'];
    document.getElementById('joy-spd').textContent=labels[val-1]+' ('+val+'/10)';
});

function updateJoyVisual(x,y) {
    const tx=x*0.4, ty=y*0.4;
    joyKnob.style.transform=`translate(calc(-50% + ${tx}px), calc(-50% + ${ty}px))`;
}

function getJoyDir(x,y) {
    if(x===0&&y===0) return {p:'03',t:'03',s:1};
    const angle=Math.atan2(y,x)*(180/Math.PI), dist=Math.sqrt(x*x+y*y), maxDist=70;
    const speed=Math.max(1,Math.min(24,Math.floor((dist/maxDist)*24*joySpeedMult)));
    let p='03', t='03';
    if(angle>-45&&angle<=45){p='02';t=(angle>0)?'02':(angle<0)?'01':'03';}
    else if(angle>45&&angle<=135){t='02';p=(angle<90)?'02':'01';}
    else if(angle>135||angle<=-135){p='01';t=(angle>0)?'02':'01';}
    else if(angle>-135&&angle<=-45){t='01';p=(angle>-90)?'02':'01';}
    return {p,t,s:speed};
}

joypad.addEventListener('pointerdown', e => {
    e.preventDefault();
    joyActive=true;
    joyKnob.classList.add('active');
    joypad.setPointerCapture(e.pointerId);
    console.log('Joystick captured');
});

document.addEventListener('pointermove', e => {
    if(!joyActive) return;
    const rect=joypad.getBoundingClientRect();
    const centerX=rect.width/2, centerY=rect.height/2;
    let x=e.clientX-rect.left-centerX, y=e.clientY-rect.top-centerY;
    const dist=Math.sqrt(x*x+y*y), maxDist=70;
    if(dist>maxDist){const angle=Math.atan2(y,x);x=Math.cos(angle)*maxDist;y=Math.sin(angle)*maxDist;}
    updateJoyVisual(x,y);
    const dir=getJoyDir(x,y), url=`/api/move?p=${dir.p}&t=${dir.t}&s=${dir.s}`;
    if(url!==lastCmd){
        fetch(url).then(r=>r.text()).then(t=>console.log('Move:',t)).catch(e=>console.error('Move error:',e));
        lastCmd=url;
    }
});

document.addEventListener('pointerup', () => {
    if(!joyActive) return;
    joyActive=false;
    joyKnob.classList.remove('active');
    updateJoyVisual(0,0);
    fetch('/api/stop').then(r=>r.text()).then(t=>console.log('Stop:',t)).catch(e=>console.error('Stop error:',e));
    lastCmd=null;
    console.log('Joystick released');
});

document.querySelectorAll('.ctrl-slider').forEach(el=>{
    el.addEventListener('input', e => {
        const type=e.target.dataset.type, val=parseInt(e.target.value);
        if(type==='zoom'){
            if(val===0){
                fetch('/api/zoom?dir=stop').then(r=>r.text()).catch(e=>console.error('Zoom stop error:',e));
                document.getElementById('zoom-val').textContent='STOP';
            } else {
                const dir=(val>0)?'in':'out', spd=Math.abs(val);
                fetch(`/api/zoom?dir=${dir}&s=${spd}`).then(r=>r.text()).catch(e=>console.error('Zoom error:',e));
                document.getElementById('zoom-val').textContent=dir.toUpperCase()+' '+spd;
            }
        } else {
            if(val===0){
                fetch('/api/focus?dir=stop').then(r=>r.text()).catch(e=>console.error('Focus stop error:',e));
                document.getElementById('focus-val').textContent='AUTO';
            } else {
                const dir=(val>0)?'near':'far', spd=Math.abs(val);
                fetch(`/api/focus?dir=${dir}&s=${spd}`).then(r=>r.text()).catch(e=>console.error('Focus error:',e));
                document.getElementById('focus-val').textContent=dir.toUpperCase()+' '+spd;
            }
        }
    });
});

document.getElementById('stop-btn').addEventListener('click', () => {
    console.log('Stop all clicked');
    if(joyActive){joyActive=false;joyKnob.classList.remove('active');updateJoyVisual(0,0);}
    fetch('/api/stop').then(r=>r.text()).catch(e=>console.error('Stop error:',e));
    document.getElementById('zoom').value=0;
    document.getElementById('focus').value=0;
    document.getElementById('zoom-val').textContent='STOP';
    document.getElementById('focus-val').textContent='AUTO';
});

document.getElementById('home-btn').addEventListener('click', () => {
    console.log('Home clicked');
    fetch('/api/preset/call?num=1').then(()=>updateStatus()).catch(e=>console.error('Home error:',e));
});

document.getElementById('focus-btn').addEventListener('click', () => {
    console.log('Auto focus clicked');
    fetch('/api/focus?dir=stop').then(r=>r.text()).catch(e=>console.error('Focus error:',e));
    document.getElementById('focus').value=0;
    document.getElementById('focus-val').textContent='AUTO';
});

document.getElementById('clear-btn').addEventListener('click', () => {
    if(confirm('Delete ALL presets?')){
        for(let i=1;i<=32;i++)fetch(`/api/preset/delete?num=${i}`).catch(e=>console.error('Delete error:',e));
        alert('Cleared');
    }
});

function genPresets(){
    const grid=document.getElementById('preset-grid');
    grid.innerHTML='';
    for(let i=1;i<=32;i++){
        const btn=document.createElement('button');
        btn.className='preset-btn';
        btn.textContent='P'+i;
        btn.id='preset-'+i;
        btn.addEventListener('click',()=>{
            console.log('Preset call:',i);
            fetch(`/api/preset/call?num=${i}`).then(r=>r.json()).then(d=>{
                document.querySelectorAll('.preset-btn').forEach(b=>b.classList.remove('active'));
                document.getElementById('preset-'+i).classList.add('active');
                updateStatus();
            }).catch(e=>console.error('Preset call error:',e));
        });
        btn.addEventListener('dblclick',()=>{
            if(confirm(`Save Preset ${i}?`)){
                console.log('Preset set:',i);
                fetch(`/api/preset/set?num=${i}`).then(r=>r.json()).then(d=>{
                    alert(d.success?'Saved!':'Error');
                }).catch(e=>{console.error('Preset set error:',e);alert('Error');});
            }
        });
        grid.appendChild(btn);
    }
}

function loadConfig(){
    fetch('/api/config').then(r=>r.json()).then(d=>{
        document.getElementById('cam-ip').value=d.cam_ip;
        document.getElementById('cam-port').value=d.cam_port;
        document.getElementById('rtsp-url').value=d.rtsp_url;
    }).catch(e=>console.error('Config load error:',e));
}

document.getElementById('save-btn').addEventListener('click', () => {
    console.log('Config save clicked');
    const config={
        cam_ip:document.getElementById('cam-ip').value,
        cam_port:parseInt(document.getElementById('cam-port').value),
        rtsp_url:document.getElementById('rtsp-url').value
    };
    fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(config)})
        .then(r=>r.json())
        .then(d=>{alert(d.success?'Saved!':'Error');})
        .catch(err=>{console.error('Config save error:',err);alert('Error: '+err);});
});

document.getElementById('test-btn').addEventListener('click', () => {
    console.log('Test clicked');
    const btn=document.getElementById('test-btn');
    btn.disabled=true;
    btn.textContent='Testing...';
    fetch('/api/status')
        .then(r=>r.json())
        .then(d=>{alert(d.reachable?'OK ONLINE':'NOT OFFLINE');})
        .catch(e=>{console.error('Test error:',e);alert('FAILED');})
        .finally(()=>{btn.disabled=false;btn.textContent='[TEST] Test';});
});

function updateStatus(){
    fetch('/api/status')
        .then(r=>r.json())
        .then(d=>{
            const camDot=document.getElementById('cam-dot'),
                  streamDot=document.getElementById('stream-dot'),
                  streamInfo=document.getElementById('stream-info');
            camDot.style.background=d.reachable?'#4CAF50':'#f44336';
            if(d.stream_status==='live'){
                streamDot.style.background='#4CAF50';
                streamInfo.textContent='* LIVE '+d.stream_fps+' FPS';
                streamInfo.style.color='#4CAF50';
            } else if(d.stream_status==='buffering'){
                streamDot.style.background='var(--primary)';
                streamInfo.textContent='* BUFFERING';
            } else {
                streamDot.style.background='#f44336';
                streamInfo.textContent='* OFFLINE';
            }
        })
        .catch(e=>console.error('Status error:',e));
}

document.getElementById('refresh-btn').addEventListener('click', () => {
    console.log('Refresh clicked');
    refreshDebug();
});

function refreshDebug(){
    fetch('/api/status')
        .then(r=>r.json())
        .then(d=>{
            let text='PTZ11 STATUS\\n';
            text+='================\\n';
            text+='Camera: '+d.cam_ip+':'+d.cam_port+'\\n';
            text+='Reachable: '+(d.reachable?'YES':'NO')+'\\n';
            text+='Stream: '+d.stream_status.toUpperCase()+'\\n';
            text+='FPS: '+d.stream_fps+'\\n';
            text+='Pan: '+d.pan+' Tilt: '+d.tilt+'\\n';
            text+='Zoom: '+d.zoom+' Focus: '+d.focus+'\\n';
            text+='Preset: '+d.preset_active+'\\n';
            document.getElementById('debug-info').textContent=text;
        })
        .catch(err=>{document.getElementById('debug-info').textContent='Error: '+err;});
}

document.querySelectorAll('.tab-btn').forEach(btn=>{
    btn.addEventListener('click',()=>{
        const tab=btn.dataset.tab;
        document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
        document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(tab).classList.add('active');
    });
});

window.addEventListener('load',()=>{
    console.log('Page loaded');
    genPresets();
    loadConfig();
    updateStatus();
    refreshDebug();
    setInterval(updateStatus,2000);
});
</script>
</html>
"""

if __name__ == '__main__':
    load_config()
    check_camera()
    def bg_check():
        while True:
            time.sleep(5)
            check_camera()
    t = threading.Thread(target=bg_check, daemon=True)
    t.start()
    app.run(host='127.0.0.1', port=5007, threaded=True, debug=False)

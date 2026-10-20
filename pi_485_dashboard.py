#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""XK19 RS-485 实时监控 + 自助对照实验台 (Pi)"""
import json, time, threading, re, mimetypes
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import serial

PORT_TTY = "/dev/ttyUSB0"
BAUD = 1200
HTTP_PORT = 8080
MAX_FRAMES = 400
SNAP_DIR = "/home/pi"
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"

# ---------- 全局状态 ----------
frames_lock = threading.Lock()
frames = []          # [{ts, hex, ascii, tag, len, raw}]
last_ts = [time.time()]

ser = None
ser_lock = threading.Lock()
capturing = threading.Event()
running = True

results_lock = threading.Lock()
results_list = []    # [{op, ts, snap, diff}]
last_snap = [None]   # 上一个快照原始字节（用于 diff）

# 后台锁存：持续监听并锁定最近一次“有效温度回显帧”。
# 注意：XK19 空闲时发 `72 xx 20 20 02 77 3C` 心跳，其 byte1 是滚动计数器(0x82~0x8e)，
# 会被误读成 17~21℃；真实温度回显帧只在按键后短暂爆发，byte1 落在 0x9a+/0xc6+ 区间。
# 故排除噪声带 0x80~0x92，仅对有效温度帧做锁存。
confirmed_lock = threading.Lock()
g_confirmed = {"temp": None, "family": None, "b1": None, "ts": 0, "raw": "—"}

# 设定温度（面板主显示，制冷时=设定温度）来自 72xx 长帧 byte2。
# 实测锚点：面板制冷30℃ -> 72xx 长帧 `72 82 20 20 ...` 的 byte2/byte3 = 0x20。
# 编码：温度 = 字节值 - 2（0x20=32 -> 30℃），范围 16~30℃ 对应字节 0x12~0x20。
# 设定温度来自内机状态帧：地址 `ba aa ce 3e` 后跟 `f2 74 XX`，XX 编码设定温度。
# 实测三点锚定（受控）：25℃->0x9a, 26℃->0x96, 29℃->0x8a，公式 temp = (0xFE - XX)//4。
# 总线里同一地址帧有两种 XX（稳态只有一种；过渡期会出现控制器滞后回显旧值），
# 故取最近若干帧解码值的众数作为稳定设定温度，滤掉陈旧帧。
g_status = {"setpoint": None, "b1": 0xF2, "ts": 0, "raw": "—", "sbytes": None}
g_temp_hist = []          # 最近解码的温度样本（用于众数滤波）
HIST_MAX = 12
# F6 FA 连续帧（每秒数十次）byte2/byte3 携带室温/其它字段（随温度非单调，暂作原始参考）。
g_f6fa = {"b2": None, "ts": 0, "raw": "—"}
# 风速方式(自动/手动) 与 风速档位 均来自内机状态帧 ba aa ce 3e 的 byte+8 (sbytes[8])：
#   受控实测 cap_manual(byte+8=0xFA)=1档, cap_auto(byte+8=0xF6)=2档, 另有 0xF2=3档, 0xEC/0xF4 亦见；
#   即 byte+8 是“风速档位”。cap_manual/cap_auto 仅此字节不同，所谓 “auto” 其实是 2 档(手动固定档)，
#   cap 文件名“auto”是误导。总线层自动档与手动同档不可分（见前端 fanLevelOf 注释），
#   故默认：byte+8 命中已知手动档值 => “手动”；仅当命中 AUTO_FAN_BYTE 才报“自动”。
#   AUTO_FAN_BYTE 未标定时一律“手动”，杜绝旧版在任意帧搜 fa/e2 子串导致的随机误报“自动”。
AUTO_FAN_BYTE = None          # 切到“自动”风速方式时状态帧 byte+8 的取值（抓到后填入）
g_fanmode = {"mode": "手动", "ts": 0}
# blow(干燥) 解码：受控单变量抓包（blow 开↔关，模式/温度/风速全部不变）已锁定字节位置：
# blow 字节在内机 E9 帧  e9 a6 ce 3e f2 [B5] [B6] fe  的 byte5(B5)：
#   实测 blow OFF(手动,制冷,24℃) -> B5=0xFC, B6=0xAC；
#   实测 blow ON (手动,制冷,24℃) -> B5=0x04, B6=0x56；
#   另 exp2 手动/自动样本 B5 均为 0x04（若当时 blow 开则一致）。=> B5=0x04 即 blow 开。
# B6 随风速方式变化（5a=手动候选, 56=自动候选），但 blow 关时为 ac，疑似与 blow 联动的组合字段，
# 单独作风速方式判定仍待复核，故仅记录不显示，避免重新引入“自动”误报。
_ADDR_E9 = bytes([0xe9, 0xa6, 0xce, 0x3e])
g_e9 = {"b5": None, "b6": None, "ts": 0, "raw": "—"}
BLOW_ON_BYTE  = 0x04   # E9 byte5：blow 开
BLOW_OFF_BYTE = 0xFC   # E9 byte5：blow 关
BLOW_STANDBY_BYTE = 0xF4  # E9 byte5：关机/待机态第三值（既非开也非关，内机未运行）
g_blow = {"on": None, "byte": None, "val_on": BLOW_ON_BYTE, "val_off": BLOW_OFF_BYTE, "ts": 0}

# 全帧持续日志（环形缓冲），用于对照实验：在用户按键前后抓取总线，diff 出温度/状态字节。
buslog_lock = threading.Lock()
g_buslog = []          # [(ts, hex), ...]
BUSLOG_MAX = 40000

# 噪声带：空闲滚动计数器所在区间（实测 0x82/0x86/0x8a/0x8e），一律排除
NOISE_LO, NOISE_HI = 0x80, 0x92

_ADDR_BAAACE = bytes([0xba, 0xaa, 0xce, 0x3e])
def _decode_baaace_temp(b):
    """内机状态帧（地址 ba aa ce 3e）中，紧跟在 `f2 74` 之后的字节 XX 编码设定温度。
    实测受控三点：25℃->0x9a, 26℃->0x96, 29℃->0x8a，公式 temp = (0xFE - XX)//4。
    XX 位于地址起始偏移 +6 处（ba aa ce 3e f2 74 XX ...）。"""
    i = b.find(_ADDR_BAAACE)
    if i >= 0 and i + 6 < len(b):
        xx = b[i + 6]
        t = (0xFE - xx) // 4
        if 16 <= t <= 30:
            return t
    return None


def _decode_baaace_temp_mode(b):
    """Decode all STATUS frames in a capture and return the modal setpoint."""
    vals = []
    start = 0
    while True:
        i = b.find(_ADDR_BAAACE, start)
        if i < 0 or i + 6 >= len(b):
            break
        t = (0xFE - b[i + 6]) // 4
        if 16 <= t <= 30:
            vals.append(t)
        start = i + 1
    if not vals:
        return None
    return Counter(vals).most_common(1)[0][0]

# ---------- 串口 ----------
def open_serial():
    try:
        s = serial.Serial(PORT_TTY, BAUD, bytesize=serial.EIGHTBITS,
                          parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                          timeout=0.05)
        s.reset_input_buffer()
        return s
    except Exception as e:
        print("[!] serial open fail:", e)
        return None

def classify(fr):
    if not fr:
        return "其他"
    h = fr[0]
    if h == 0xE2:
        return "状态/心跳帧"
    if h == 0x72 and len(fr) >= 2 and fr[1] == 0xAA:
        return "帧型B(72AA)"
    if h == 0x5E:
        return "帧型C(5E)"
    if h == 0x5A:
        return "帧型D(5A)"
    return "其他"

def to_ascii(data):
    return ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)

# ---------- 实时帧缓冲 ----------
_buf = bytearray()

def add_frame(b):
    with frames_lock:
        fr = {"ts": time.time(), "hex": b.hex(' ').upper(),
              "ascii": to_ascii(b), "tag": classify(b),
              "len": len(b), "raw": b.hex()}
        frames.append(fr)
        if len(frames) > MAX_FRAMES:
            del frames[0:len(frames) - MAX_FRAMES]
    # 所有 72xx 帧持久化日志（含时间戳），用于离线构建多状态语料、钉死温度/模式/风速字节
    if b and b[0] == 0x72:
        try:
            with open("/home/pi/all72.log", "a") as lf:
                lf.write("%.3f %d %s\n" % (time.time(), len(b), b.hex()))
        except Exception:
            pass
    # 设定温度：内机状态帧（地址 ba aa ce 3e）中 f2 74 之后的字节 XX，temp=(0xFE-XX)//4。
    # 取最近 HIST_MAX 个解码值的众数作为稳定设定温度（滤掉过渡期控制器滞后回显的旧值）。
    sp = _decode_baaace_temp(b)
    if sp is not None:
        with confirmed_lock:
            g_temp_hist.append(sp)
            if len(g_temp_hist) > HIST_MAX:
                g_temp_hist.pop(0)
            best = Counter(g_temp_hist).most_common(1)[0][0]
            g_status["setpoint"] = best
            # 完整状态帧 12 字节，供离线/实时反推 模式/风速/blow 字节（专家模式）
            i = b.find(_ADDR_BAAACE)
            g_status["sbytes"] = list(b[i:i+12])
            g_status["ts"] = time.time()
            g_status["raw"] = b.hex()
    # F6 FA 连续帧：byte2 疑似室温传感器原始值（E2/DE 间跳），仅记录原始，不解码为温度。
    if b and len(b) >= 3 and b[0] == 0xF6 and b[1] == 0xFA:
        try:
            with confirmed_lock:
                g_f6fa["b2"] = b[2]
                g_f6fa["ts"] = time.time()
                g_f6fa["raw"] = b.hex()
            with open("/home/pi/f6fa.log", "a") as lf:
                lf.write("%.3f %s\n" % (time.time(), b.hex()))
        except Exception:
            pass
    # 风速方式(自动/手动)：状态帧 ba aa ce 3e 的 byte+8 (sbytes[8]) 是风速档位；
    # 已知手动档值 => “手动”，仅命中 AUTO_FAN_BYTE 才报“自动”。AUTO_FAN_BYTE 未标定前一律“手动”，
    # 避免旧版在任意帧搜 fa/e2 子串造成的随机误报。
    if g_status["sbytes"] is not None and len(g_status["sbytes"]) >= 9:
        fb = g_status["sbytes"][8]
        with confirmed_lock:
            if AUTO_FAN_BYTE is not None and fb == AUTO_FAN_BYTE:
                g_fanmode["mode"] = "自动"
            else:
                g_fanmode["mode"] = "手动"
            g_fanmode["ts"] = time.time()
    # blow(干燥) 解码：字节在内机 E9 帧（e9 a6 ce 3e f2 [B5][B6] fe）的 byte5。
    # 受控单变量抓包确认 B5=0x04 => blow 开，B5=0xFC => blow 关；状态帧/72帧/F6FA 帧在该实验中均不变。
    i9 = b.find(_ADDR_E9)
    if i9 >= 0 and i9 + 6 < len(b) and b[i9 + 4] == 0xF2:
        eb = b[i9:i9 + 8]
        b5, b6 = eb[5], (eb[6] if len(eb) > 6 else None)
        with confirmed_lock:
            g_e9["b5"] = b5
            g_e9["b6"] = b6
            g_e9["ts"] = time.time()
            g_e9["raw"] = eb.hex()
            if b5 == BLOW_ON_BYTE:
                g_blow["on"] = True
                g_blow["state"] = "on"
            elif b5 == BLOW_OFF_BYTE:
                g_blow["on"] = False
                g_blow["state"] = "off"
            else:  # 关机/待机态第三值（实测 0xf4），内机未运行，blow 无意义
                g_blow["on"] = None
                g_blow["state"] = "standby"
            g_blow["byte"] = b5
            g_blow["val_on"] = BLOW_ON_BYTE
            g_blow["val_off"] = BLOW_OFF_BYTE
            g_blow["ts"] = time.time()
    # 全帧环形日志（对照实验用）：记录每一帧的时间戳+hex
    with buslog_lock:
        g_buslog.append((time.time(), b.hex()))
        if len(g_buslog) > BUSLOG_MAX:
            del g_buslog[:len(g_buslog) - BUSLOG_MAX]

# 0xFE 空闲填充通常成串出现；状态帧/72帧内部也可能含单个 0xFE（如状态帧 +7=0xFE）。
# 只在「连续 >=2 个 0xFE」处切帧，单个 0xFE 视为帧内数据，避免把状态帧从中间劈开。
_IDLE_SPLIT = re.compile(rb'\xfe{2,}')
def feed(data):
    global _buf
    _buf += data
    parts = _IDLE_SPLIT.split(bytes(_buf))
    _buf = bytearray(parts.pop())     # 最后一段可能不完整，留作下次
    for p in parts:
        # 保留 >=2 字节：状态回显帧 `72 f6` 仅 2 字节，承载温度 B1，绝不能丢弃
        if len(p) >= 2:
            add_frame(bytes(p))

def reader():
    global ser
    while running:
        if capturing.is_set():
            time.sleep(0.1)
            continue
        with ser_lock:
            if ser is None or not ser.is_open:
                time.sleep(0.5)
                continue
            try:
                chunk = ser.read(512)
            except Exception:
                chunk = b""
        if chunk:
            try:
                feed(chunk)
            except Exception as e:
                print("[reader] feed error:", e)
        else:
            time.sleep(0.01)

# ---------- 抓快照 + 自动 diff ----------
def capture_snapshot(dur=10):
    global ser
    capturing.set()
    time.sleep(0.3)
    with ser_lock:
        old = ser
        if old:
            try:
                old.close()
            except Exception:
                pass
        s2 = open_serial()
        chunks = []
        t = time.time()
        while time.time() - t < dur:
            c = s2.read(4096)
            if c:
                chunks.append(c)
                feed(c)   # 同时走正常管线：写入带时间戳的 g_buslog / all72.log，便于离线对齐按键时刻与字节跳变
        try:
            s2.close()
        except Exception:
            pass
        ser = open_serial()    # 恢复给 reader
    capturing.clear()
    return b"".join(chunks)

def split_frames(data):
    out, cur = [], bytearray()
    for b in data:
        if b == 0xFE:
            if len(cur) >= 3:
                out.append(bytes(cur))
            cur = bytearray()
        else:
            cur.append(b)
    if len(cur) >= 3:
        out.append(bytes(cur))
    return out

def diff_snapshots(base, changed):
    fb = split_frames(base)
    fc = split_frames(changed)
    res = {"base_frames": len(fb), "changed_frames": len(fc),
           "frame_count_changes": [], "byte_diffs": []}
    bm = Counter(tuple(f) for f in fb)
    cm = Counter(tuple(f) for f in fc)
    for k in (set(bm) | set(cm)):
        if bm[k] != cm[k]:
            res["frame_count_changes"].append(
                {"frame": bytes(k).hex(' '), "base": bm[k], "changed": cm[k]})
    gb, gc = {}, {}
    for f in fb:
        gb.setdefault((len(f), f[0]), []).append(f)
    for f in fc:
        gc.setdefault((len(f), f[0]), []).append(f)
    for key in (set(gb) & set(gc)):
        bmost = Counter(gb[key]).most_common(1)[0][0]
        cmost = Counter(gc[key]).most_common(1)[0][0]
        if bmost != cmost:
            diffs = [i for i, (x, y) in enumerate(zip(bmost, cmost)) if x != y]
            res["byte_diffs"].append({
                "type": "0x%02X" % key[1], "len": key[0], "bytes": diffs,
                "base": bmost.hex(' '), "changed": cmost.hex(' ')})
    return res

def decode_temp(data):
    """72xx 帧 byte1 是 模式(+风速+温度)复合体。排除空闲滚动计数器噪声带(0x80~0x92)后，
       制冷族 base=0xf2(27℃)、送风族 base=0xaa(27℃)，均 +1℃ -> +4。
       返回 (temp, family, b1)；无有效帧则 (None,None,None)。
       注：风速档位会在 byte1 叠加偏移(送风 1档=+0x0c≈+3℃)，单帧无法剥离，故制冷优先且取各族最小 byte1 以逼近自动风温度。"""
    cool = {}; fan = {}   # b1 -> temp，取最小 byte1 以剥离风速偏移
    for f in split_frames(data):
        if len(f) >= 2 and f[0] == 0x72:
            b1 = f[1]
            if NOISE_LO <= b1 <= NOISE_HI:
                continue
            if 0xC6 <= b1 <= 0xFE and (b1 - 0xF2) % 4 == 0:
                t = 27 + (b1 - 0xF2) / 4
                if 16 <= t <= 30:
                    cool[b1] = t
            elif 0x9A <= b1 <= 0xB6 and (b1 - 0xAA) % 4 == 0:
                t = 27 + (b1 - 0xAA) / 4
                if 16 <= t <= 30:
                    fan[b1] = t
    if cool:
        b1 = min(cool); return (cool[b1], '制冷族', b1)
    if fan:
        b1 = min(fan); return (fan[b1], '送风族', b1)
    return (None, None, None)

# ---------- HTTP ----------

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _serve_file(self, path, ctype=None):
        """Serve a dashboard asset without allowing path traversal."""
        try:
            data = path.read_bytes()
        except OSError:
            self._send(404, json.dumps({"err": "not found"}))
            return
        if ctype is None:
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
            "application/javascript", "application/json", "image/svg+xml"
        ):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            index = DASHBOARD_DIR / "index.html"
            if index.is_file():
                self._serve_file(index)
            else:
                self._send(503, json.dumps({"err": "dashboard/index.html missing"}))
        elif p == "/demo.json":
            self._serve_file(DASHBOARD_DIR / "demo.json", "application/json")
        elif p.startswith("/assets/"):
            rel = p[len("/assets/"):]
            target = (DASHBOARD_DIR / "assets" / rel).resolve()
            try:
                target.relative_to(DASHBOARD_DIR.resolve())
            except ValueError:
                self._send(404, json.dumps({"err": "not found"}))
                return
            if target.is_file():
                self._serve_file(target)
            else:
                self._send(404, json.dumps({"err": "not found"}))
        elif p == "/data":
            self._send(200, json.dumps(data_payload()))
        elif p == "/health":
            self._send(200, json.dumps({"ok": True, "now": time.time()}))
        elif p == "/results":
            self._send(200, json.dumps(results_payload()))
        elif p == "/buslog":
            n = parse_qs(urlparse(self.path).query).get("n", ["0"])[0]
            try:
                n = int(n)
            except Exception:
                n = 0
            with buslog_lock:
                bl = list(g_buslog)
            if n and n < len(bl):
                bl = bl[-n:]
            self._send(200, json.dumps({"now": time.time(), "count": len(bl),
                                        "frames": [[t, h] for t, h in bl]}))
        else:
            self._send(404, json.dumps({"err": "not found"}))

    def do_POST(self):
        if self.path.startswith("/record"):
            op = parse_qs(urlparse(self.path).query).get("op", ["unknown"])[0]
            try:
                qs = parse_qs(urlparse(self.path).query)
                try:
                    dur = int(qs.get("dur", ["15"])[0])
                except Exception:
                    dur = 15
                if dur < 3:
                    dur = 3
                if dur > 120:
                    dur = 120
                data = capture_snapshot(dur)
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "err": str(e)}))
                return
            snap = "%s/snap_%s_%d.bin" % (SNAP_DIR, op, int(time.time()))
            open(snap, "wb").write(data)
            with results_lock:
                if last_snap[0] is not None:
                    dr = diff_snapshots(last_snap[0], data)
                else:
                    dr = {"note": "首个快照，已作为基准"}
                last_snap[0] = data
                # Prefer the signature-based STATUS decoder. decode_temp() is
                # kept only as a legacy fallback and is known to be noisy on
                # the 72xx heartbeat frames.
                fresh_t = _decode_baaace_temp_mode(data)
                fresh_f, fresh_b1 = None, 0xF2
                if fresh_t is None:
                    fresh_t, fresh_f, fresh_b1 = decode_temp(data)
                if fresh_t is None:
                    with confirmed_lock:
                        fresh_t = g_status.get("setpoint")
                if fresh_t is not None:
                    with confirmed_lock:
                        g_confirmed["temp"] = fresh_t
                        g_confirmed["family"] = fresh_f
                        g_confirmed["b1"] = fresh_b1
                        g_confirmed["ts"] = time.time()
                with confirmed_lock:
                    setpoint = g_confirmed["temp"]
                    family = g_confirmed["family"]
                    b1 = g_confirmed["b1"]
                entry = {"op": op, "ts": time.time(), "snap": snap, "diff": dr, "setpoint": setpoint, "family": family, "b1": b1}
                results_list.append(entry)
                if len(results_list) > 30:
                    del results_list[0]
            with confirmed_lock:
                setpoint = g_confirmed["temp"]; family = g_confirmed["family"]; b1 = g_confirmed["b1"]
            self._send(200, json.dumps({"ok": True, "op": op, "snap": snap, "diff": dr, "setpoint": setpoint, "family": family, "b1": b1}))
        elif self.path.startswith("/reset"):
            with results_lock:
                last_snap[0] = None
                results_list.clear()
            self._send(200, json.dumps({"ok": True, "msg": "基准与历史已重置"}))
        else:
            self._send(404, json.dumps({"err": "not found"}))

    def log_message(self, *a):
        pass

def data_payload():
    with frames_lock:
        recent = frames[-200:]
        last = recent[-1]["ts"] if recent else 0
    with confirmed_lock:
        status = dict(g_status)
        f6fa = dict(g_f6fa)
        fanmode = dict(g_fanmode)
        blow = dict(g_blow)
        e9 = dict(g_e9)
    return {"port": PORT_TTY, "baud": BAUD,
            "total_bytes": sum(f["len"] for f in recent),
            "frame_count": len(frames),
            "last_update": last,
            "now": time.time(),
            "status": status,
            "fanmode": fanmode,
            "blow": blow,
            "e9": e9,
            "f6fa": f6fa,
            "frames": [{"ts": f["ts"], "hex": f["hex"], "ascii": f["ascii"],
                        "tag": f["tag"], "len": f["len"]} for f in recent]}

def results_payload():
    with results_lock:
        return {"results": [
            {"op": r["op"], "ts": r["ts"], "snap": r["snap"], "diff": r["diff"], "setpoint": r.get("setpoint"), "family": r.get("family"), "b1": r.get("b1")}
            for r in results_list]}

def main():
    global ser
    ser = open_serial()
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), H)
    print("[*] dashboard on http://0.0.0.0:%d" % HTTP_PORT)
    srv.serve_forever()

if __name__ == "__main__":
    main()

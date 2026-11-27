"""Clipear (videos ya subidos): link -> transcripción -> IA propone clips -> render con
formato, seguimiento de cara, subtítulos, logo y texto. Corre solo en tu PC (127.0.0.1)."""
import base64, hashlib, json, math, os, re, shutil, subprocess, sys, threading, time, urllib.parse, urllib.request, uuid
from pathlib import Path

import numpy as np
import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

import yt_dlp
from yt_dlp.utils import download_range_func

def _add_cuda_dlls():
    """Windows: registra las DLL de CUDA instaladas con pip (nvidia-cublas-cu12 y nvidia-cudnn-cu12)."""
    if os.name != "nt":
        return
    import site, sysconfig
    bases = {Path(sysconfig.get_paths()["purelib"])} | {Path(x) for x in site.getsitepackages()}
    for b in bases:
        for sub in ("cublas", "cudnn", "cuda_runtime"):
            dll = b / "nvidia" / sub / "bin"
            if dll.is_dir():
                os.add_dll_directory(str(dll))
                os.environ["PATH"] = str(dll) + os.pathsep + os.environ.get("PATH", "")


_add_cuda_dlls()

try:
    import cv2  # seguimiento de cara: pip install "opencv-python<5"
except Exception:  # noqa: BLE001
    cv2 = None

ROOT = Path(__file__).parent
DATA = ROOT / "data"
JOBS, CLIPS, LOGOS, MODELS = DATA / "jobs", DATA / "clips", DATA / "logos", DATA / "models"
BADGES_DIR = LOGOS / "badges"
BADGES_DIR.mkdir(parents=True, exist_ok=True)

for _d in (JOBS, CLIPS, LOGOS, MODELS):
    _d.mkdir(parents=True, exist_ok=True)
for _old in DATA.glob("logo.*"):  # el logo de versiones anteriores pasa a la biblioteca
    shutil.move(str(_old), str(LOGOS / ("anterior" + _old.suffix.lower())))
PRESETS = {"9:16": (1080, 1920), "3:4": (1080, 1440), "4:5": (1080, 1350), "1:1": (1080, 1080),
           "4:3": (1440, 1080), "16:9": (1920, 1080)}


def canvas_size(o):
    """Tamaño del video final según la relación elegida (9:16, 3:4, 4:5, 1:1, 4:3, 16:9 o personalizada 'a:b')."""
    r = str(o.get("ratio") or "9:16")
    if r == "custom":
        r = str(o.get("cratio") or "9:16")
    if r in PRESETS:
        return PRESETS[r]
    try:
        a, b = (float(x) for x in r.replace("/", ":").split(":")[:2])
        if a <= 0 or b <= 0:
            raise ValueError
    except Exception:  # noqa: BLE001
        return PRESETS["9:16"]
    k = min(3.0, max(1 / 3, b / a))  # entre 1:3 y 3:1
    if a > b:
        return 1920, max(2, int(round(1920 * k / 2)) * 2)
    return 1080, max(2, int(round(1080 * k / 2)) * 2)
MAX_LOGOS = 5
COOKIES = {"browser": "", "failed": ""}


def cookie_opts():
    """Cookies para que YouTube no pida 'confirma que no eres un robot': data/cookies.txt o las de un navegador."""
    f = DATA / "cookies.txt"
    if f.exists():
        return {"cookiefile": str(f)}
    return {"cookiesfrombrowser": (COOKIES["browser"],)} if COOKIES["browser"] else {}


def find_js_runtime():
    """YouTube exige un motor de JavaScript (Deno) para dar los formatos. Lo busca aunque no esté en el PATH (por ejemplo, recién instalado con winget)."""
    cands = []
    for name in ("deno", "node", "bun"):
        w = shutil.which(name)
        if w:
            cands.append((name, w))
    if os.name == "nt":
        la, home = os.environ.get("LOCALAPPDATA", ""), Path.home()
        for f in [Path(la) / "Microsoft" / "WinGet" / "Links" / "deno.exe", home / ".deno" / "bin" / "deno.exe", *sorted((Path(la) / "Microsoft" / "WinGet" / "Packages").glob("DenoLand.Deno*/deno.exe"))]:
            if la and f.is_file():
                cands.append(("deno", str(f)))
    return cands[0] if cands else (None, None)


def js_opts():
    name, path = find_js_runtime()
    o = {"remote_components": ["ejs:github", "ejs:npm"]}  # si falta el componente de yt-dlp, lo baja solo
    if name:
        o["js_runtimes"] = {name: {"path": path}}
    return o


def ytdlp_diag():
    name, path = find_js_runtime()
    try:
        import yt_dlp_ejs  # noqa: F401
        ejs = "sí"
    except Exception:  # noqa: BLE001
        ejs = "no (se intentará bajar solo)"
    return f"yt-dlp {yt_dlp.version.__version__} · motor JavaScript: {(name + ' en ' + path) if name else 'NO ENCONTRADO'} · componente ejs instalado: {ejs}"


_YDL_BASE = yt_dlp.YoutubeDL


class _YoutubeDL(_YDL_BASE):
    """Igual que yt_dlp.YoutubeDL pero siempre con el motor de JavaScript que YouTube necesita."""
    def __init__(self, params=None, auto_init=True):
        super().__init__({**js_opts(), **(params or {})}, auto_init)


yt_dlp.YoutubeDL = _YoutubeDL


LOW_FMT = "bv*[height<=360]+ba/b[height<=360]/b"   # análisis y vista previa: liviano y rápido
HI_FMT = "bv*[height<=1080]+ba/b[height<=1080]/b"  # clips finales: solo el tramo de cada clip


def hi_fmt_for(q):
    try:
        h = int(q or 1080)
    except (TypeError, ValueError):
        h = 1080
    h = max(720, min(2160, h))
    return f"bv*[height<={h}]+ba/b[height<={h}]/b"
LOGO_EXT = {".png", ".jpg", ".jpeg", ".webp"}
app = Flask(__name__)
jobs, models = {}, {}
ENGINE = {"state": "cargando", "dev": None, "note": "", "t0": time.time()}


class Cancelled(Exception):
    pass


def setp(job, step=None, pct=None, msg=None, overall=None):
    """Actualiza el avance del trabajo: paso, % del paso, mensaje y tiempo estimado."""
    now = time.time()
    if step is not None and step != job.get("step"):
        names = job.get("names") or []
        prev = job.get("step")
        if isinstance(prev, int) and prev < len(names) and job.get("state", "working") == "working":
            job.setdefault("times", {})[names[prev]] = round(now - job.get("t_step", now), 1)
        job["step"], job["t_step"], job["prog_t"] = step, now, now
        print(f">> [{job['id']}] {names[step] if step < len(names) else 'fin'}", flush=True)
    if pct != job.get("pct"):
        job["prog_t"] = now
    job["pct"] = pct
    if msg is not None:
        job["msg"] = msg
    job["overall"] = overall
    el = now - job.get("t_start", now)
    job["eta"] = round(el * (1 - overall) / overall) if overall and overall >= 0.05 else None
    if job.get("cancel"):
        raise Cancelled()


def t2s(v):
    """Tiempo a segundos. Acepta 1:00:00, 01:05, 90, 1,5 y también 1h05m30s."""
    if v in (None, ""):
        return None
    txt = str(v).strip().lower().replace(",", ".")
    if not txt:
        return None
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s?)?", txt)
    if m and any(m.groups()) and any(ch in txt for ch in "hms"):
        h, mi, se = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + se
    s = 0.0
    for x in txt.split(":"):
        s = s * 60 + float(x or 0)
    return s


def ts(x):
    x = max(0, x)
    return f"{int(x // 3600)}:{int(x % 3600 // 60):02d}:{x % 60:05.2f}"


def hexcol(h, a=0):  # '#RRGGBB' -> &HAABBGGRR
    h = h.lstrip("#")
    return f"&H{a:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


# ---------- seguimiento de cara ----------
YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
YUNET = MODELS / "face_detection_yunet_2023mar.onnx"
_faces = {"loaded": False, "yunet": None, "haar": None}
_faces_lock = threading.Lock()


def _load_face_models():
    if _faces["loaded"] or cv2 is None:
        return
    _faces["loaded"] = True
    try:  # detector bueno (YuNet, ~230 KB); se baja una sola vez
        if not YUNET.exists() or YUNET.stat().st_size < 50000:
            with urllib.request.urlopen(YUNET_URL, timeout=40) as r:
                YUNET.write_bytes(r.read())
        if YUNET.stat().st_size > 50000:
            _faces["yunet"] = cv2.FaceDetectorYN.create(str(YUNET), "", (320, 320), 0.6, 0.3, 5000)
    except Exception as e:  # noqa: BLE001
        print(">> YuNet no disponible, uso el detector básico:", e, flush=True)
        if YUNET.exists() and YUNET.stat().st_size < 50000:
            YUNET.unlink()
    try:  # detector básico que ya viene con OpenCV
        _faces["haar"] = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    except Exception:  # noqa: BLE001
        _faces["haar"] = None


def detect_faces(frame):
    """Caras como (centro_x, centro_y, ancho, alto), en fracciones 0-1 del cuadro."""
    h, w = frame.shape[:2]
    if w > 640:
        frame = cv2.resize(frame, (640, int(h * 640 / w)))
        h, w = frame.shape[:2]
    out = []
    with _faces_lock:
        _load_face_models()
        det = _faces["yunet"]
        if det is not None:
            try:
                det.setInputSize((w, h))
                found = det.detect(frame)[1]
                for f in (found if found is not None else []):
                    out.append((float(f[0] + f[2] / 2) / w, float(f[1] + f[3] / 2) / h, float(f[2]) / w, float(f[3]) / h))
                return out
            except Exception as e:  # noqa: BLE001
                print(">> YuNet falló, uso el detector básico:", e, flush=True)
                _faces["yunet"] = None
        haar = _faces["haar"]
        if haar is not None and not haar.empty():
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            for x, y, fw, fh in haar.detectMultiScale(gray, 1.1, 5, minSize=(max(30, h // 14),) * 2):
                out.append((float(x + fw / 2) / w, float(y + fh / 2) / h, float(fw) / w, float(fh) / h))
    return out


def video_info(path):
    """(ancho, alto, fps, duración en segundos) o None."""
    if cv2 is None:
        return None
    cap = cv2.VideoCapture(str(path))
    try:
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        return (w, h, fps, n / fps) if w and h else None
    finally:
        cap.release()


def pick_face(faces, prev):
    """Elige la cara a seguir: la más grande; si ya seguíamos a alguien, la más cercana a esa posición."""
    if not faces:
        return None
    big = max(f[2] for f in faces)
    cand = [f for f in faces if f[2] >= 0.5 * big]
    if prev is None:
        return max(cand, key=lambda f: f[2])[0]
    return min(cand, key=lambda f: abs(f[0] - prev))[0]


def sample_faces(path, s, e, step=0.25, cb=None):
    """Mira el video cada 'step' segundos entre s y e. Devuelve [(segundo_relativo, centro_x o None)]."""
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, s) * 1000)
    every, out, prev, i = max(1, int(round(fps * step))), [], None, 0
    try:
        while s + i / fps <= e and cap.grab():
            if i % every == 0:
                ok, frame = cap.retrieve()
                if ok:
                    cx = pick_face(detect_faces(frame), prev)
                    prev = cx if cx is not None else prev
                    out.append((i / fps, cx))
                    if cb:
                        cb(min(1.0, (i / fps) / max(0.1, e - s)))
            i += 1
    finally:
        cap.release()
    return out


MAX_EXPR_MOVES = 40  # límite de movimientos por clip; la expresión se arma en árbol balanceado para no desbordar el parser de ffmpeg


def plan_camera(samples, scaled_w, W, quick=False):
    """Cámara virtual: se queda quieta y solo se mueve cuando la cara se sale de la zona central.
    Devuelve (x_inicial, [(inicio_s, duración_s, desplazamiento_px)]) o None si no hay caras."""
    if not any(cx is not None for _, cx in samples):
        return None
    last = next(cx for _, cx in samples if cx is not None) * scaled_w
    xs = []
    for _, cx in samples:  # los huecos sin detección mantienen la última posición
        last = cx * scaled_w if cx is not None else last
        xs.append(last)
    lo, hi = 0.0, max(0.0, scaled_w - W)
    sm = []
    for i in range(len(xs)):  # mediana de 5 muestras: ignora detecciones sueltas
        win = sorted(xs[max(0, i - 2): i + 3])
        sm.append(min(hi, max(lo, win[len(win) // 2] - W / 2)))
    head = sorted(sm[:6])
    x0 = head[len(head) // 2]
    for thresh in (0.12, 0.18, 0.25, 0.35, 0.50):
        cur, busy, moves = x0, 0.0, []
        for (t, _), tg in zip(samples, sm):
            if t >= busy and abs(tg - cur) > thresh * W:
                dur = 0.2 if quick else max(0.5, abs(tg - cur) / (0.9 * W))
                moves.append((round(t, 2), round(dur, 2), round(tg - cur, 1)))
                cur, busy = tg, t + dur
        if len(moves) <= MAX_EXPR_MOVES:
            break
    if len(moves) > MAX_EXPR_MOVES:
        moves = moves[:MAX_EXPR_MOVES]
    return float(round(x0, 1)), [(float(a), float(b), float(c)) for a, b, c in moves]


def _sum_tree(terms):
    """Suma de términos en árbol balanceado: la profundidad crece como log2(n), no como n."""
    if len(terms) == 1:
        return terms[0]
    m = len(terms) // 2
    return f"({_sum_tree(terms[:m])}+{_sum_tree(terms[m:])})"


def x_expr(x0, moves):
    """Posición X del recorte en función del tiempo t (rampas lineales, una por movimiento)."""
    if not moves:
        return f"{x0}"
    return f"{x0}+" + _sum_tree([f"({d}*clip((t-{t0})/{dur},0,1))" for t0, dur, d in moves])


# ---------- cámara inteligente (varias personas, gente que camina) ----------
def sample_faces_multi(path, s, e, step=0.25, cb=None):
    """Como sample_faces, pero devuelve TODAS las caras de cada muestra: [(segundo_relativo, [(centro_x, ancho)])]."""
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, s) * 1000)
    every, out, i = max(1, int(round(fps * step))), [], 0
    try:
        while s + i / fps <= e and cap.grab():
            if i % every == 0:
                ok, frame = cap.retrieve()
                if ok:
                    out.append((i / fps, [(f[0], f[2]) for f in detect_faces(frame)]))
                    if cb:
                        cb(min(1.0, (i / fps) / max(0.1, e - s)))
            i += 1
    finally:
        cap.release()
    return out


def _rdp(pts, eps):
    """Simplifica la curva (t, x): deja solo los puntos que se desvían más de eps. Menos puntos = menos movimientos."""
    if len(pts) < 3:
        return pts
    (t0, x0), (t1, x1) = pts[0], pts[-1]
    best, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        t, x = pts[i]
        dv = abs(x - (x0 + (x1 - x0) * (t - t0) / ((t1 - t0) or 1e-9)))
        if dv > best:
            best, idx = dv, i
    if best <= eps:
        return [pts[0], pts[-1]]
    return _rdp(pts[: idx + 1], eps)[:-1] + _rdp(pts[idx:], eps)


def plan_smart(samples, scaled_w, W, voice=None):
    """Cámara 'de cine' para varias personas o gente que camina.
    - Si todas las personas caben en el recorte, las deja a todas en cuadro (sin elegir a una).
    - Si no caben, sigue a una (la de la voz que habla si hay datos; si no, la que ya seguía) con corte rápido al cambiar.
    - Se mueve poco: pocos movimientos, largos y suaves; ignora detecciones sueltas.
    Devuelve (x_inicial, [(inicio, duración, desplazamiento)], info) o None si no hay caras."""
    if not any(faces for _, faces in samples):
        return None
    lo, hi = 0.0, max(0.0, scaled_w - W)
    vt = [t for t, _ in (voice or [])]

    def voice_x(t):
        if not vt:
            return None
        v = voice[min(range(len(vt)), key=lambda k: abs(vt[k] - t))][1]
        return None if v is None else v * scaled_w

    grp, foc, fit, prev_f = [], [], [], None
    for t, faces in samples:
        if not faces:
            grp.append(None), foc.append(None), fit.append(None)
            continue
        big = max(w for _, w in faces)
        fs = [(c * scaled_w, w * scaled_w) for c, w in faces if w >= 0.45 * big]  # ignora caras de fondo
        left, right = min(c - w / 2 for c, w in fs), max(c + w / 2 for c, w in fs)
        grp.append((left + right) / 2)
        fit.append((right - left) <= 0.66 * W)
        ref = voice_x(t)
        ref = prev_f if ref is None else ref
        f = max(fs, key=lambda q: q[1])[0] if ref is None else min(fs, key=lambda q: abs(q[0] - ref))[0]
        foc.append(f)
        prev_f = f

    def fill(a):  # los huecos sin cara mantienen lo último que se vio
        first = next(v for v in a if v is not None)
        out, last = [], first
        for v in a:
            last = last if v is None else v
            out.append(last)
        return out

    grp, foc, fit = fill(grp), fill(foc), fill(fit)
    n = len(samples)
    fit_s = [sum(fit[max(0, i - 4): i + 5]) * 2 >= len(fit[max(0, i - 4): i + 5]) for i in range(n)]  # sin parpadeo
    raw = [min(hi, max(lo, (grp[i] if fit_s[i] else foc[i]) - W / 2)) for i in range(n)]
    sm = [sorted(raw[max(0, i - 2): i + 3])[len(raw[max(0, i - 2): i + 3]) // 2] for i in range(n)]
    ts_list = [t for t, _ in samples]
    head = sorted(sm[:6])
    x0 = head[len(head) // 2]
    for tol in (0.08, 0.12, 0.18, 0.26, 0.36, 0.50):
        pts = _rdp(list(zip(ts_list, sm)), tol * W)
        cur, busy, moves, cuts = x0, 0.0, [], 0
        for (ta, _), (tb, xb) in zip(pts, pts[1:]):
            d = xb - cur
            if abs(d) < tol * W:
                continue  # no vale la pena mover la cámara
            if abs(d) > 0.55 * W and tb - ta <= 0.6:  # salto grande y repentino (otra persona): corte rápido, no paneo
                t0, dur, cuts = max(ta, busy), 0.2, cuts + 1
            else:
                t0, dur = max(ta, busy), max(tb - ta, abs(d) / (0.8 * W), 0.5)
            moves.append((round(t0, 2), round(dur, 2), round(d, 1)))
            cur, busy = xb, t0 + dur
        if len(moves) <= MAX_EXPR_MOVES:
            break
    if len(moves) > MAX_EXPR_MOVES:
        moves = moves[:MAX_EXPR_MOVES]
    info = {"moves": len(moves), "cuts": cuts, "group": round(100 * sum(fit_s) / n)}
    return float(round(x0, 1)), [(float(a), float(b), float(c)) for a, b, c in moves], info


def x_expr_ease(x0, moves, W):
    """Como x_expr, pero los movimientos cortos arrancan y frenan suave (smoothstep). Si hay más de 24 movimientos,
    usa rampa lineal para mantener la expresión corta."""
    if not moves:
        return f"{x0}"
    if len(moves) > 24:
        return x_expr(x0, moves)
    terms = []
    for t0, dur, d in moves:
        u = f"clip((t-{t0})/{dur},0,1)"
        slow = dur >= 2 and abs(d) / dur < 0.25 * W
        terms.append(f"({d}*" + (u if slow else f"(3*pow({u},2)-2*pow({u},3))") + ")")
    return f"{x0}+" + _sum_tree(terms)


# ---------- voces: ¿quién está hablando? (no depende de ver la cara) ----------
SR, FL, HOP = 16000, 400, 160  # 16 kHz; ventanas de 25 ms cada 10 ms


def _mel_fb(n_fft=512, n_mels=26, fmin=60.0, fmax=7600.0):
    hz2mel = lambda f: 2595 * np.log10(1 + f / 700)  # noqa: E731
    mel2hz = lambda m: 700 * (10 ** (m / 2595) - 1)  # noqa: E731
    pts = mel2hz(np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2))
    bins = np.fft.rfftfreq(n_fft, 1 / SR)
    fb = np.zeros((n_mels, len(bins)), np.float32)
    for i in range(n_mels):
        l, c, r = pts[i], pts[i + 1], pts[i + 2]
        fb[i] = np.clip(np.minimum((bins - l) / (c - l), (r - bins) / (r - c)), 0, None)
    return fb


def _read_audio(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                       capture_output=True)
    if r.returncode or not r.stdout:
        raise RuntimeError("No pude leer el audio del video.")
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _voice_frames(x):
    """Por cada 10 ms: timbre (MFCC 1-19), tono (f0), si es voz con tono y su energía."""
    n = 1 + (len(x) - FL) // HOP
    win = np.hamming(FL).astype(np.float32)
    fb = _mel_fb()
    dct = np.cos(np.pi / 26 * (np.arange(26) + 0.5)[None, :] * np.arange(20)[:, None]).astype(np.float32)
    mf = np.zeros((n, 19), np.float32)
    f0 = np.zeros(n, np.float32)
    pk = np.zeros(n, np.float32)
    en = np.zeros(n, np.float32)
    lo, hi = SR // 400, SR // 70  # tonos entre 70 y 400 Hz
    for a in range(0, n, 6000):
        b = min(n, a + 6000)
        fr = x[np.arange(a, b)[:, None] * HOP + np.arange(FL)[None, :]]
        fr = fr - fr.mean(1, keepdims=True)
        en[a:b] = np.log(1e-8 + (fr ** 2).mean(1))
        pre = np.concatenate([fr[:, :1], fr[:, 1:] - 0.97 * fr[:, :-1]], 1)
        sp = np.abs(np.fft.rfft(pre * win, 512, axis=1)) ** 2
        mf[a:b] = (np.log(sp @ fb.T + 1e-8) @ dct.T)[:, 1:20]
        ac = np.fft.irfft(np.abs(np.fft.rfft(fr * win, 1024, axis=1)) ** 2, axis=1)[:, :FL]
        ac = ac / (ac[:, :1] + 1e-9)
        seg = ac[:, lo:hi]
        k = seg.argmax(1)
        pk[a:b] = seg[np.arange(len(seg)), k]
        f0[a:b] = SR / (k + lo)
    voiced = (pk > 0.35) & (en > np.percentile(en, 35))
    return mf, f0, voiced


def _voice_windows(words, mf, f0, voiced, dur, win=1.0, hop=0.5):
    """Ventanas de 1 s (cada 0,5 s) donde alguien habla, con la 'huella' de la voz de cada una."""
    speech = np.zeros(len(mf), bool)
    for w in words:
        speech[max(0, int(w[0] * 100)): int(w[1] * 100) + 1] = True
    feats, cen, t = [], [], 0.0
    while t + win <= dur + 1e-6:
        a, b = int(t * 100), min(len(mf), int((t + win) * 100))
        m = speech[a:b] & voiced[a:b]
        if m.sum() >= 15:
            feats.append(np.concatenate([mf[a:b][m].mean(0), [np.log(np.median(f0[a:b][m]))]]))
            cen.append(t + win / 2)
        t += hop
    return np.array(feats, np.float32), np.array(cen)


def _kmeans(X, k, tries=8):
    rng, best = np.random.default_rng(0), None
    sq = lambda C: ((X[:, None, :] - C[None]) ** 2).sum(2)  # noqa: E731
    for _ in range(tries):
        C = X[[rng.integers(len(X))]]
        for _ in range(k - 1):
            d = sq(C).min(1) + 1e-9
            C = np.vstack([C, X[rng.choice(len(X), p=d / d.sum())]])
        for _ in range(60):
            lab = sq(C).argmin(1)
            new = np.array([X[lab == j].mean(0) if (lab == j).any() else C[j] for j in range(k)])
            if np.allclose(new, C):
                break
            C = new
        D = sq(C)
        lab = D.argmin(1)
        inertia = D[np.arange(len(X)), lab].sum()
        if best is None or inertia < best[0]:
            best = (inertia, lab, C)
    return best[1], best[2]


def _silhouette(X, lab):
    if len(X) > 1200:
        idx = np.random.default_rng(1).choice(len(X), 1200, replace=False)
        X, lab = X[idx], lab[idx]
    sq = (X ** 2).sum(1)
    D = np.sqrt(np.maximum(0, sq[:, None] + sq[None] - 2 * X @ X.T))
    ks = sorted(set(lab.tolist()))
    if len(ks) < 2:
        return -1.0
    a = np.zeros(len(X))
    b = np.full(len(X), np.inf)
    for k in ks:
        m = lab == k
        cnt = max(1, m.sum() - 1)
        a[m] = D[np.ix_(m, m)].sum(1) / cnt
        for k2 in ks:
            if k2 != k:
                b[m] = np.minimum(b[m], D[np.ix_(m, lab == k2)].mean(1))
    return float(np.mean((b - a) / np.maximum(a, b)))


def _diarize_basic(job, d, k_req):
    """Método básico (sin instalar nada extra). Solo se usa si falta sherpa-onnx."""
    x = _read_audio(d / "source.mp4")
    mf, f0, voiced = _voice_frames(x)
    F, cen = _voice_windows(job["words"], mf, f0, voiced, len(x) / SR)
    if len(F) < 12:
        raise RuntimeError("Hay muy poca voz en este tramo para separar a las personas.")
    F[:, :19] -= F[:, :19].mean(0)  # quita el 'color' del micrófono/sala
    X = (F - F.mean(0)) / (F.std(0) + 1e-6)
    X[:, 19] *= 3.0  # el tono de voz pesa más: separa muy bien voces distintas
    sil = None
    if k_req == 1:
        k, lab = 1, np.zeros(len(X), int)
    elif k_req in (2, 3):
        k = k_req
        lab, _ = _kmeans(X, k)
    else:  # auto: prueba 2 y 3 voces y se queda con la que más se separa; si ninguna, es 1 sola
        k, lab, sil = 1, np.zeros(len(X), int), -1.0
        for kk in (2, 3):
            l2, _ = _kmeans(X, kk)
            if np.bincount(l2, minlength=kk).min() < 0.10 * len(X):
                continue  # un grupito diminuto no es una persona
            sc = _silhouette(X, l2)
            if sc > sil:
                sil, k, lab = sc, kk, l2
        if sil < 0.28:
            k, lab = 1, np.zeros(len(X), int)
    order = list(dict.fromkeys(lab.tolist()))  # Persona 1 = la primera que habla
    lab = np.array([order.index(v) for v in lab.tolist()])
    if k > 1 and len(lab) > 2:  # un cambio aislado de 0,5 s casi siempre es error
        sm = lab.copy()
        for i in range(1, len(lab) - 1):
            if lab[i - 1] == lab[i + 1] != lab[i]:
                sm[i] = lab[i - 1]
        lab = sm
    job["spk"] = {"k": k, "t": cen.tolist(), "lab": lab.tolist()}
    people = []
    for j in range(k):
        m = lab == j
        c = X[m].mean(0)
        dist = ((X - c) ** 2).sum(1)
        reps = []
        for i in np.argsort(dist):  # momentos más típicos de esa voz, separados entre sí
            if lab[i] == j and all(abs(cen[i] - r) > 4 for r in reps):
                reps.append(round(float(cen[i]), 1))
            if len(reps) == 5:
                break
        people.append({"share": round(100 * float(m.mean())), "reps": reps})
    return {"k": k, "auto": k_req == 0, "sil": None if sil is None else round(sil, 2), "people": people}


def voice_samples(job, s, e, xs, step=0.25, lead=0.4):
    """[(segundo_relativo, centro_x)] según quién habla; xs = posición (0-1) de cada persona.
    'lead' adelanta la cámara unos instantes para que ya esté en la persona cuando empieza a hablar."""
    cands = [x for x in job.get("spk_sets", []) if x["s"] <= s + 0.5 and x["e"] >= e - 0.5]
    sp = min(cands, key=lambda x: x["e"] - x["s"]) if cands else job.get("spk")  # la detección más específica para este clip
    if not sp or len(xs) < sp["k"] or any(v is None for v in xs[: sp["k"]]):
        return None
    t_arr, lab, out, last = np.array(sp["t"]), sp["lab"], [], None
    for i in range(int((e - s) / step) + 1):
        ta = s + i * step + lead
        j = int(np.abs(t_arr - ta).argmin())
        if abs(t_arr[j] - ta) <= 0.5:
            last = lab[j]
        out.append((i * step, float(xs[last]) if last is not None else None))
    return out


# ---------- ¿quién habla? por los labios (sin marcar voces) ----------
def _mouth_patch(gray, f):
    """Recorte pequeño y normalizado de la boca; sirve para medir si los labios se mueven."""
    ih, iw = gray.shape[:2]
    rx, ry, lx, ly = float(f[10]), float(f[11]), float(f[12]), float(f[13])
    mx, my = (rx + lx) / 2, (ry + ly) / 2
    mw = max(abs(lx - rx), 0.35 * float(f[2]))
    pw, ph = mw * 1.5, mw * 1.1
    x0, x1 = int(max(0, mx - pw / 2)), int(min(iw, mx + pw / 2))
    y0, y1 = int(max(0, my - ph / 2)), int(min(ih, my + ph / 2))
    if x1 - x0 < 6 or y1 - y0 < 4:
        return None
    p = cv2.resize(gray[y0:y1, x0:x1], (24, 16), interpolation=cv2.INTER_AREA).astype(np.float32)
    p -= p.mean()
    return p / (p.std() + 1e-3)


def detect_faces_lm(frame, max_w=1280):
    """Caras con su boca: [(cx, cy, ancho, parche_boca)], todo en fracciones del ANCHO del cuadro. Solo con YuNet."""
    h, w = frame.shape[:2]
    if w > max_w:
        frame = cv2.resize(frame, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
        h, w = frame.shape[:2]
    with _faces_lock:
        _load_face_models()
        det = _faces["yunet"]
        if det is None:
            return []
        try:
            det.setInputSize((w, h))
            found = det.detect(frame)[1]
        except Exception as e:  # noqa: BLE001
            print(">> YuNet falló en el análisis de labios:", e, flush=True)
            return []
    if found is None:
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    out = []
    for f in found:
        if float(f[2]) < 24:  # cara muy chica: los labios no se distinguen
            continue
        out.append((float(f[0] + f[2] / 2) / w, float(f[1] + f[3] / 2) / w, float(f[2]) / w, _mouth_patch(gray, f)))
    return out


def sample_speakers(path, s, e, step=0.125, cb=None):
    """Como sample_faces_multi pero a más cuadros por segundo y con la boca de cada cara: [(t, [(cx, cy, w, parche)])]."""
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, s) * 1000)
    every, out, i = max(1, int(round(fps * step))), [], 0
    try:
        while s + i / fps <= e and cap.grab():
            if i % every == 0:
                ok, frame = cap.retrieve()
                if ok:
                    out.append((i / fps, detect_faces_lm(frame)))
                    if cb:
                        cb(min(1.0, (i / fps) / max(0.1, e - s)))
            i += 1
    finally:
        cap.release()
    return out


def speaker_series(samples, speech, hold=1.4):
    """Decide quién habla en cada instante mirando qué boca se mueve más (solo cuando hay palabras en el audio).
    samples = [(t, [(cx, cy, w, parche)])]; speech = [(inicio, fin)] del clip. Devuelve ([(t, cx del que habla)], cambios)."""
    prev, nid, rows = [], 0, []
    for t, faces in samples:
        cur, used = [], set()
        for cx, cy, w, patch in faces:
            best, bd = None, 9.0
            for k, p in enumerate(prev):
                if k in used:
                    continue
                d = math.hypot(cx - p[1], cy - p[2])
                if d < bd:
                    best, bd = k, d
            mot = None
            if best is not None and bd <= max(0.6 * w, 0.02):
                used.add(best)
                fid = prev[best][0]
                if patch is not None and prev[best][4] is not None:
                    mot = float(np.abs(patch - prev[best][4]).mean())
            else:
                fid = nid
                nid += 1
            cur.append((fid, cx, cy, w, patch, mot))
        prev = [(c[0], c[1], c[2], c[3], c[4]) for c in cur]
        rows.append((t, cur))
    allm = [c[5] for _, cur in rows for c in cur if c[5] is not None]
    if not allm:
        return None
    floor = float(np.percentile(allm, 20))  # el "ruido" de una boca quieta
    act = {}
    for i, (_, cur) in enumerate(rows):
        for fid, _cx, _cy, _w, _p, mot in cur:
            if mot is not None:
                act.setdefault(fid, {})[i] = max(0.0, mot - floor)

    def smooth(fid, i):  # promedio de ~1 s
        d = act.get(fid, {})
        v = [d.get(j, 0.0) for j in range(i - 4, i + 5)]
        return sum(v) / len(v)

    def talking(t):
        return not speech or any(a - 0.25 <= t <= b + 0.25 for a, b in speech)

    out, cur_id, hold_until, changes, last_x = [], None, -1.0, 0, None
    for i, (t, cur) in enumerate(rows):
        if not cur:
            out.append((t, last_x))
            continue
        sc = {c[0]: smooth(c[0], i) for c in cur}
        best = max(sc, key=sc.get)
        if cur_id not in sc:
            if cur_id is None:
                best = max(cur, key=lambda c: c[3])[0]  # al inicio: la cara más grande
            cur_id, hold_until = best, t + hold
            changes += 0 if len(out) == 0 else 1
        elif best != cur_id and talking(t) and t >= hold_until and sc[best] > 1.35 * sc[cur_id] and sc[best] - sc[cur_id] > 0.03:
            cur_id, hold_until = best, t + hold
            changes += 1
        last_x = next(c[1] for c in cur if c[0] == cur_id)
        out.append((t, last_x))
    return out, changes


# ---------- dos personas en pantalla dividida ----------
def split_people(samples):
    """Separa las caras en 2 grupos (izquierda / derecha). samples = [(t, [(cx, ancho)])].
    Devuelve (serie_izq, serie_der, centros) con series [(t, cx o None)], o None si no hay dos personas."""
    pts = []
    for _, faces in samples:
        if faces:
            big = max(w for _, w in faces)
            pts += [c for c, w in faces if w >= 0.45 * big]
    if len(pts) < 8:
        return None
    xs = np.array(pts)
    c = np.array([np.percentile(xs, 15), np.percentile(xs, 85)])
    lab = np.zeros(len(xs), int)
    for _ in range(30):
        lab = np.abs(xs[:, None] - c[None]).argmin(1)
        new = np.array([xs[lab == j].mean() if (lab == j).any() else c[j] for j in range(2)])
        if np.allclose(new, c):
            break
        c = new
    if (lab == 0).sum() < 0.2 * len(xs) or (lab == 1).sum() < 0.2 * len(xs) or c[1] - c[0] < 0.12:
        return None
    A, B = [], []
    for t, faces in samples:
        big = max((w for _, w in faces), default=0)
        fs = [(cx, w) for cx, w in faces if w >= 0.45 * big]
        fa = [q for q in fs if abs(q[0] - c[0]) <= abs(q[0] - c[1])]
        fb = [q for q in fs if abs(q[0] - c[0]) > abs(q[0] - c[1])]
        A.append((t, max(fa, key=lambda q: q[1])[0] if fa else None))
        B.append((t, max(fb, key=lambda q: q[1])[0] if fb else None))
    return A, B, c


# ---------- voces con huellas de voz (mucho más preciso que el método básico) ----------
SPK_MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx"
SPK_MODEL = MODELS / "nemo_en_titanet_small.onnx"
_spk = {"ext": None}


def _spk_extractor(say):
    if _spk["ext"] is not None:
        return _spk["ext"]
    import sherpa_onnx  # pip install sherpa-onnx  (si falta, diarize() usa el método básico)
    if not SPK_MODEL.exists() or SPK_MODEL.stat().st_size < 10_000_000:
        say("Descargando el modelo de voces (40 MB, solo la primera vez)...")
        tmp = SPK_MODEL.with_suffix(".part")
        with urllib.request.urlopen(SPK_MODEL_URL, timeout=90) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        if tmp.stat().st_size < 10_000_000:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("No pude bajar el modelo de voces (revisa tu internet).")
        tmp.replace(SPK_MODEL)
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(SPK_MODEL), num_threads=min(4, os.cpu_count() or 2))
    if not cfg.validate():
        SPK_MODEL.unlink(missing_ok=True)
        raise RuntimeError("El modelo de voces no es válido; lo borré. Vuelve a pulsar «Detectar voces».")
    _spk["ext"] = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    return _spk["ext"]


def _embed_windows(x, words, ext, say, win=1.4, hop=0.7):
    """Huella de voz de cada ventana de 1,4 s donde alguien habla (según las palabras de la transcripción)."""
    dur = len(x) / SR
    speech = np.zeros(int(dur * 100) + 3, bool)
    for w in words:
        speech[max(0, int(w[0] * 100)): int((w[1] + 0.25) * 100) + 1] = True  # une huecos cortos entre palabras
    starts, t = [], 0.0
    while t + win <= dur + 1e-6:
        if speech[int(t * 100): int((t + win) * 100)].mean() >= 0.55:
            starts.append(t)
        t += hop
    if len(starts) < 12:
        raise RuntimeError("Hay muy poca voz en este tramo para separar a las personas.")
    E, cen = [], []
    for n, t in enumerate(starts):
        st = ext.create_stream()
        st.accept_waveform(sample_rate=SR, waveform=x[int(t * SR): int((t + win) * SR)])
        st.input_finished()
        E.append(np.array(ext.compute(st), np.float32))
        cen.append(t + win / 2)
        if n % 25 == 0:
            say(f"Analizando las voces... {int(100 * n / len(starts))}%")
    E = np.array(E, np.float32)
    E /= np.linalg.norm(E, axis=1, keepdims=True) + 1e-9
    return E, np.array(cen)


def diarize(job, d, k_req, say=None, rng=None):
    """Separa las voces del tramo. k_req = 0 (auto), 1, 2, 3 o 4. Guarda job['spk'] y devuelve un resumen."""
    say = say or (lambda m: None)
    try:
        ext = _spk_extractor(say)
    except ImportError:
        say("Usando el método básico de voces (para más precisión: pip install sherpa-onnx)...")
        r = _diarize_basic(job, d, k_req)
        r["engine"] = "básico"
        if job.get("spk"):
            job["spk"].update(s=0.0, e=1e9)
            job["spk_sets"] = [x for x in job.get("spk_sets", []) if not (x["s"] == 0.0 and x["e"] == 1e9)] + [job["spk"]]
        return r
    say("Leyendo el audio...")
    live_rng = bool(job.get("live") and rng)
    x = live_audio_slice(d, max(0.0, float(rng[0]) - 0.3), float(rng[1]) + 0.3) if live_rng else _read_audio(d / "source.mp4")
    rs, re_, off, words = 0.0, 1e9, 0.0, job["words"]
    if rng:  # solo el tramo de ESTE clip: así no aparecen personas que no salen en él
        rs, re_ = float(rng[0]), float(rng[1])
        a = int(max(0.0, rs - 0.3) * SR)
        x, off = (x, a / SR) if live_rng else (x[a: int((re_ + 0.3) * SR)], a / SR)
        words = [(w[0] - off, w[1] - off, w[2]) for w in job["words"] if w[1] > rs - 0.3 and w[0] < re_ + 0.3]
    E, cen = _embed_windows(x, words, ext, say)
    cen = cen + off
    say("Agrupando las voces...")
    sil = None
    if k_req == 1:
        k, lab = 1, np.zeros(len(E), int)
    elif k_req in (2, 3, 4):
        k = k_req
        lab, _ = _kmeans(E, k)
    else:  # auto: la cantidad de voces que mejor se separa (prefiere más voces si casi empatan)
        scores = {}
        for kk in (2, 3, 4):
            l2, _ = _kmeans(E, kk)
            if np.bincount(l2, minlength=kk).min() >= 0.08 * len(E):
                scores[kk] = (_silhouette(E, l2), l2)
        k, lab = 1, np.zeros(len(E), int)
        if scores:
            best = max(v[0] for v in scores.values())
            if best >= 0.22:
                sil = best
                for kk in sorted(scores):
                    if scores[kk][0] >= 0.65 * best:
                        k, lab = kk, scores[kk][1]
    order = list(dict.fromkeys(lab.tolist()))  # Persona 1 = la primera que habla
    lab = np.array([order.index(v) for v in lab.tolist()])
    if k > 1 and len(lab) > 2:  # un cambio aislado de 0,7 s casi siempre es error
        sm = lab.copy()
        for i in range(1, len(lab) - 1):
            if lab[i - 1] == lab[i + 1] != lab[i]:
                sm[i] = lab[i - 1]
        lab = sm
    job["spk"] = {"k": k, "t": cen.tolist(), "lab": lab.tolist(), "s": rs, "e": re_}
    job["spk_sets"] = [x for x in job.get("spk_sets", []) if not (x["s"] == rs and x["e"] == re_)] + [job["spk"]]
    people = []
    for j in range(k):
        m = lab == j
        c = E[m].mean(0)
        c /= np.linalg.norm(c) + 1e-9
        sim = E @ c
        reps = []
        for i in np.argsort(-sim):  # los momentos más típicos de esa voz, separados entre sí
            if lab[i] == j and all(abs(cen[i] - r) > 4 for r in reps):
                reps.append(round(float(cen[i]), 1))
            if len(reps) == 5:
                break
        people.append({"share": round(100 * float(m.mean())), "reps": reps})
    return {"k": k, "auto": k_req == 0, "sil": None if sil is None else round(float(sil), 2), "people": people, "engine": "huellas"}


def run_diarize(job, k, rng=None):
    """Corre la detección de voces en segundo plano; la página consulta el avance."""
    st = job["spk_job"] = {"state": "working", "msg": "Preparando...", "res": None}

    def work():
        try:
            st["res"] = diarize(job, JOBS / job["id"], k, lambda m: st.update(msg=m), rng)
            st["state"] = "done"
        except Exception as ex:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            st.update(state="error", msg=str(ex)[-300:])

    threading.Thread(target=work, daemon=True).start()


# ---------- subtítulos: conservar el tiempo de cada palabra al editar el texto ----------
def _norm_tok(t):
    return re.sub(r"[\W_]+", "", str(t).lower())


def retime_words(orig, new_toks):
    """orig = [(inicio, fin, palabra)] originales; new_toks = textos nuevos. Lo que no cambió conserva su tiempo exacto;
    lo que cambió se reparte (según el largo de cada palabra) dentro del tiempo que ocupaban las palabras reemplazadas."""
    import difflib
    on = [_norm_tok(w[2]) for w in orig]
    nn = [_norm_tok(t) for t in new_toks]
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=on, b=nn, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out.append((orig[i1 + k][0], orig[i1 + k][1], new_toks[j1 + k]))
        elif tag in ("replace", "insert"):
            toks = new_toks[j1:j2]
            if i2 > i1:
                a, b = orig[i1][0], orig[i2 - 1][1]
            else:  # palabra(s) nueva(s) entre dos que ya existían
                a = orig[i1 - 1][1] if i1 > 0 else orig[0][0]
                b = orig[i1][0] if i1 < len(orig) else a + 0.3 * len(toks)
                need = 0.15 * len(toks)
                if b - a < need:  # no hay hueco: se lo quito al final de la palabra anterior
                    a = b - need
                    if out:
                        a = max(a, out[-1][0] + 0.05)
                        out[-1] = (out[-1][0], min(out[-1][1], a), out[-1][2])
                        b = max(b, a + 0.05)
            wts = [max(1, len(t)) for t in toks]
            tot, t0 = sum(wts), a
            for t, wt in zip(toks, wts):
                dd = (b - a) * wt / tot
                out.append((round(t0, 3), round(t0 + dd, 3), t))
                t0 += dd
    return out


def speech_rel(words, s, e):
    """Intervalos de habla del clip, en segundos desde el inicio del clip."""
    return [(a - s, b - s) for a, b, _ in words if b >= s and a <= e]


# ---------- alinear el clip en alta calidad con la transcripción ----------
def _env100(x):
    n = len(x) // 160
    return np.abs(x[: n * 160]).reshape(n, 160).mean(1).astype(np.float32)


def measure_shift(low, hi, rs, span=8.0):
    """Segundos que el clip bajado en alta calidad empieza más tarde (+) o más temprano (-) de lo pedido (rs, en el tiempo
    del video liviano). Compara el sonido de los dos. Devuelve 0.0 si no está seguro."""
    try:
        a = _read_audio(hi)
        L = len(a) / SR
        w0 = max(0.0, rs - span)
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{w0:.2f}", "-t", f"{L + 2 * span:.2f}", "-i", str(low),
                            "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], capture_output=True)
        if not r.stdout:
            return 0.0
        b = np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0
        A, B = _env100(b), _env100(a)
        if len(B) < 300 or len(A) <= len(B) or B.std() < 1e-4:
            return 0.0
        A = (A - A.mean()) / (A.std() + 1e-6)
        B = (B - B.mean()) / (B.std() + 1e-6)
        n = 1 << int(np.ceil(np.log2(len(A) + len(B))))
        cc = np.fft.irfft(np.fft.rfft(A, n) * np.conj(np.fft.rfft(B, n)), n)[: len(A) - len(B) + 1] / len(B)
        p = int(cc.argmax())
        z = (cc[p] - cc.mean()) / (cc.std() + 1e-9)
        if z < 6 or cc[p] < 0.25:
            print(f">> No pude medir el desfase del clip (confianza baja, z={z:.1f})", flush=True)
            return 0.0
        return round(w0 + p / 100.0 - rs, 2)
    except Exception as ex:  # noqa: BLE001
        print(">> No pude medir el desfase del clip:", ex, flush=True)
        return 0.0


# ---------- análisis ----------
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def clean_name(s, maxlen=80):
    """Nombre seguro para carpeta/archivo en Windows (sin caracteres prohibidos ni nombres reservados)."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")[:maxlen].rstrip(" .")
    if not s:
        return "Proyecto"
    return "_" + s if s.split(".")[0].upper() in WIN_RESERVED else s


def fetch_title(url):
    """Título del video en YouTube (si no se puede obtener, devuelve vacío)."""
    try:
        o = {"quiet": True, "noplaylist": True, "socket_timeout": 30}
        o.update(cookie_opts())
        with yt_dlp.YoutubeDL(o) as ydl:
            return str((ydl.extract_info(url, download=False, process=False) or {}).get("title") or "")
    except Exception:  # noqa: BLE001
        return ""


def project_dir(job, base):
    """Carpeta <ruta elegida>\\<título del video>; se crea si no existe."""
    b = Path(str(base)).expanduser()
    if not b.is_absolute():
        raise ValueError("Escribe la ruta completa de la carpeta (por ejemplo C:\\Users\\tu_usuario\\iCloudDrive\\Clipear).")
    title = job.get("title") or ""
    if not title:
        m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{6,})", job.get("url", ""))
        title = f"Video {m.group(1)}" if m else "Proyecto " + time.strftime("%Y-%m-%d")
    d = b / clean_name(title)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_clip(job, o, src, i, c):
    """Copia el clip terminado a la carpeta del proyecto y anota su caption en captions.txt."""
    base = (o.get("outdir") or "").strip()
    if not base:
        return
    try:
        d = job.get("folder_path")
        if not d:
            d = job["folder_path"] = project_dir(job, base)
        dest = d / f"{i + 1:02d} - {clean_name(c['title'], 60)}.mp4"
        if dest.exists():
            dest = d / f"{dest.stem} ({job['id']}).mp4"
        shutil.copy2(src, dest)
        cap = (c.get("caption") or "").strip()
        with open(d / "captions.txt", "a", encoding="utf-8") as f:
            if not job.get("cap_head"):
                f.write(f"===== {time.strftime('%Y-%m-%d %H:%M')} =====\n\n")
                job["cap_head"] = True
            f.write(f"{dest.stem}\n{cap or '(este clip no tenía caption)'}\n\n")
        if not cap:
            job["cap_empty"] = job.get("cap_empty", 0) + 1
        job["saved"] = job.get("saved", 0) + 1
    except Exception as ex:  # noqa: BLE001
        job["save_err"] = str(ex)[-200:]
        print(">> No pude guardar en la carpeta elegida:", ex, flush=True)


class VideoError(Exception):
    """Problema del video que se explica al usuario en la página."""


def check_video(url, start_sec, end_sec):
    """Antes de bajar: avisa si el video está EN DIRECTO (no se puede bajar un tramo, solo grabaría en vivo)
    y ajusta el 'Hasta' si se pasa del final. Si no puede revisar, sigue igual. Devuelve ('Hasta' final, título)."""
    try:
        o = {"quiet": True, "noplaylist": True, "socket_timeout": 30}
        o.update(cookie_opts())
        with yt_dlp.YoutubeDL(o) as ydl:
            info = ydl.extract_info(url, download=False, process=False) or {}
    except Exception as ex:  # noqa: BLE001
        print(">> No pude revisar el estado del video (sigo igual):", str(ex)[:150], flush=True)
        return end_sec, ""
    status = info.get("live_status") or ("is_live" if info.get("is_live") else "")
    if status == "is_live":
        raise VideoError("Este video está EN DIRECTO ahora mismo. YouTube no deja bajar un tramo de un directo en curso: "
                         "solo se grabaría en vivo, minuto a minuto. Espera a que termine la transmisión y YouTube la "
                         "procese, o prueba con un video que ya terminó.")
    dur = info.get("duration")
    if dur:
        if start_sec and start_sec >= dur:
            raise VideoError(f"El video dura {int(dur // 60)} min y el 'Desde' queda después del final. Corrige los tiempos.")
        if end_sec is not None and end_sec > dur:
            print(f">> El 'Hasta' se pasa del final; lo ajusto a {int(dur)} s", flush=True)
            end_sec = float(dur)
    return end_sec, str(info.get("title") or "")


def download(url, start_sec, end_sec, out_base, job=None, fmt=None):
    """out_base: ruta SIN extensión. Deja siempre el archivo como source.mp4"""
    if fmt is None:  # solo en el análisis (en el render ya se sabe que el video sirve)
        end_sec, title = check_video(url, start_sec, end_sec)
        if job is not None and title:
            job["title"] = title
    opts = {
        "format": fmt or LOW_FMT,
        "merge_output_format": "mp4",
        "outtmpl": out_base + ".%(ext)s",
        "noplaylist": True,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "retries": 5,
    }
    opts.update(cookie_opts())
    # Si hay desde/hasta, baja solo ese tramo (sin recodificar = rápido)
    if start_sec is not None or end_sec is not None:
        opts["download_ranges"] = download_range_func(
            None, [(start_sec or 0, end_sec if end_sec is not None else float("inf"))])
    def hook(d):
        if job is None:
            return
        if job.get("cancel"):
            raise Cancelled()
        tot = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d.get("status") == "downloading" and tot:
            pc = min(99, int(d.get("downloaded_bytes", 0) / tot * 100))
            setp(job, 0, pc, f"Descargando el video... {pc}%", 0.12 * pc / 100)

    opts["progress_hooks"] = [hook]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Cancelled:
        raise
    except Exception as ex:  # noqa: BLE001
        msg = str(ex).lower()
        if ("cookiesfrombrowser" in opts or "cookiefile" in opts) and "cookie" in msg and "sign in" not in msg:
            COOKIES["failed"] = COOKIES["browser"] or "cookies.txt"
            print(">> No se pudieron leer las cookies; sigo sin ellas:", ex, flush=True)
            opts.pop("cookiesfrombrowser", None)
            opts.pop("cookiefile", None)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        else:
            raise

    final = Path(out_base + ".mp4")
    if not final.exists():
        cands = sorted(final.parent.glob(final.stem + "*.mp4"), key=lambda f: f.stat().st_mtime)
        if not cands:
            raise FileNotFoundError("yt-dlp no generó el archivo de video")
        cands[-1].rename(final)
    return str(final)


# ---------- video propio (archivo de tu computadora) ----------
def clean_path(p):
    """Quita comillas y espacios (Windows pone comillas al 'Copiar como ruta')."""
    return (p or "").strip().strip('"').strip("'").strip()


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True)
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        return 0.0


def ffmpeg_local(job, src, dst, ss, t, vf, codecs, total, prog, label):
    """Corta/convierte un tramo del video local a dst (mp4). Prueba GPU y luego CPU. Respeta Cancelar."""
    d = Path(dst).parent
    for vc in codecs:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
        if ss and ss > 0:
            cmd += ["-ss", f"{ss:.3f}"]
        cmd += ["-i", str(src)]
        if t:
            cmd += ["-t", f"{t:.3f}"]
        cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, *vc, "-g", "30", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(dst)]
        with open(d / "ff_err.txt", "w", encoding="utf-8") as err:
            pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
            job["proc"] = pr
            try:
                for line in pr.stdout:
                    if line.startswith(("out_time_us=", "out_time_ms=")) and total > 0:
                        try:
                            f = min(1.0, max(0.0, int(line.split("=")[1]) / 1e6 / total))
                        except ValueError:
                            continue
                        prog(f, f"{label}... {int(f * 100)}%")
            except Cancelled:
                pr.kill()
                raise
            pr.wait()
        if job.get("cancel"):
            raise Cancelled()
        if pr.returncode == 0 and Path(dst).exists():
            return
    raise RuntimeError("No pude leer ese video con ffmpeg: " + (d / "ff_err.txt").read_text(errors="ignore")[-300:])


def local_prepare(job, path, start_sec, end_sec, d):
    """Análisis: crea d/source.mp4 liviano (360p) a partir del video local, con el tramo Desde/Hasta si lo hay."""
    dur = probe_duration(path)
    if not dur:
        raise VideoError("No pude leer ese archivo como video. Prueba con otro (mp4, mkv, mov, webm...).")
    ss = start_sec or 0.0
    if ss >= dur:
        raise VideoError(f"El video dura {int(dur // 60)} min y el 'Desde' queda después del final. Corrige los tiempos.")
    if end_sec is not None and end_sec > dur:
        end_sec = dur
    span = (end_sec if end_sec is not None else dur) - ss
    codecs = [["-c:v", "h264_nvenc", "-preset", "p3", "-cq", "27", "-b:v", "0"], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "25"]]
    ffmpeg_local(job, path, d / "source.mp4", ss, span if (start_sec is not None or end_sec is not None) else None,
                 "scale=-2:'min(360,ih)'", codecs, span,
                 lambda f, m: setp(job, 0, int(f * 100), m, 0.20 * f), "Preparando el video")


def local_cut(job, path, start_sec, end_sec, dst, prog, max_h=1080):
    """Render: tramo del video local en alta calidad (1080p, 2K o 4K) como dst."""
    max_h = max(720, min(2160, int(max_h or 1080)))
    codecs = [["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "17", "-b:v", "0"], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16"]]
    ffmpeg_local(job, path, dst, max(0.0, start_sec), max(1.0, end_sec - start_sec),
                 f"scale=-2:'min({max_h},ih)'", codecs, max(1.0, end_sec - start_sec), prog, "Cortando el tramo")


_PICK_SCRIPT = r"""
import tkinter as tk
from tkinter import filedialog
r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)
p = filedialog.askopenfilename(title='Elige el video', filetypes=[('Videos', '*.mp4 *.mkv *.mov *.avi *.webm *.flv *.ts *.m4v *.wmv'), ('Todos los archivos', '*.*')])
print(p)
"""


def probe_starts(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time", "-of", "json", str(path)],
                       capture_output=True, text=True)
    v = a = 0.0
    for s in json.loads(r.stdout or "{}").get("streams", []):
        try:
            st = float(s.get("start_time") or 0)
        except ValueError:
            st = 0.0
        if s.get("codec_type") == "video":
            v = st
        elif s.get("codec_type") == "audio":
            a = st
    return v, a


def normalize(d, job, name="source", prog=None, light=False):
    """Al cortar sin recodificar, el video suele empezar unos segundos antes que el audio. Aquí se recorta
    para que audio, video, transcripción y clips compartan el mismo tiempo 0."""
    prog = prog or (lambda f, m: None)
    src, raw = d / f"{name}.mp4", d / f"{name}_raw.mp4"
    cut = max(probe_starts(src))
    if cut < 0.05:
        prog(1.0, "El video ya está alineado.")
        return
    total = (video_info(src) or (0, 0, 0, 0))[3] - cut
    src.replace(raw)
    prog(0.0, "Alineando audio y video...")
    ok = False
    codecs = ([["-c:v", "h264_nvenc", "-preset", "p3", "-cq", "27", "-b:v", "0"], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "25"]] if light
              else [["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "19", "-b:v", "0"], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]])
    for vc in codecs:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-i", str(raw),
               "-ss", f"{cut:.3f}", "-map", "0:v:0", "-map", "0:a:0?", *vc, "-g", "30", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(src)]
        with open(d / "ff_err.txt", "w", encoding="utf-8") as err:
            pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
            job["proc"] = pr
            try:
                for line in pr.stdout:
                    if line.startswith(("out_time_us=", "out_time_ms=")) and total > 0:
                        try:
                            f = min(1.0, max(0.0, int(line.split("=")[1]) / 1e6 / total))
                        except ValueError:
                            continue
                        prog(f, f"Alineando audio y video... {int(f * 100)}%")
            except Cancelled:
                pr.kill()
                raise
            pr.wait()
        if job.get("cancel"):
            raise Cancelled()
        if pr.returncode == 0:
            ok = True
            break
    if ok:
        raw.unlink(missing_ok=True)
    else:
        src.unlink(missing_ok=True)
        raw.replace(src)
        print(">> No se pudo alinear el video:", (d / "ff_err.txt").read_text(errors="ignore")[-300:], flush=True)


def snap(words, s, e, dur):
    """Ajusta inicio y fin a límites de palabra cercanos para no cortar a media palabra."""
    s = max(0.0, s)
    if dur:
        e = min(e, dur)
    if words:
        ws = min(words, key=lambda w: abs(w[0] - s))
        we = min(words, key=lambda w: abs(w[1] - e))
        if abs(ws[0] - s) < 1.5:
            s = max(0.0, ws[0] - 0.15)
        if abs(we[1] - e) < 1.5:
            e = we[1] + 0.25
        if dur:
            e = min(e, dur)
    return s, e


def get_model(size, dev):
    from faster_whisper import WhisperModel
    if (size, dev) not in models:
        if dev == "cuda":
            models[(size, dev)] = WhisperModel(size, device="cuda", compute_type="int8_float16")
        else:
            models[(size, dev)] = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=min(8, os.cpu_count() or 4))
    return models[(size, dev)]


def warmup():
    """Al arrancar: prueba la GPU con 1 segundo de silencio, para saber YA si funciona."""
    import numpy as np
    silence = np.zeros(16000, dtype="float32")
    for dev in ("cuda", "cpu"):
        try:
            print(f">> Probando el motor de transcripción ({dev})...", flush=True)
            list(get_model("small", dev).transcribe(silence, language="es")[0])
            ENGINE.update(state="listo", dev=dev)
            print(f">> Motor de transcripción listo: {'GPU' if dev == 'cuda' else 'CPU'}", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            models.pop(("small", dev), None)
            ENGINE["note"] = (f"La GPU no funcionó: {str(e)[:150]}" if dev == "cuda" else str(e)[:150])
            print(f">> {dev} falló:", e, flush=True)
    ENGINE.update(state="error", dev=None)


def transcribe(path, size, job):
    def run(m, dev):
        words, lines = [], []
        segs, info = m.transcribe(str(path), language="es", word_timestamps=True, vad_filter=True,
                                  beam_size=1, condition_on_previous_text=False)
        total = info.duration or 0
        for sg in segs:
            lines.append((sg.start, sg.text.strip()))
            words += [(w.start, w.end, w.word.strip()) for w in (sg.words or [])]
            pc = min(99, int(sg.end / total * 100)) if total else 0
            setp(job, 2, pc, f"Transcribiendo con {dev}... {pc}%", 0.20 + 0.50 * pc / 100)
        return words, lines

    t0 = time.time()
    while ENGINE["state"] == "cargando":
        setp(job, 2, None, f"Preparando el motor de transcripción... {int(time.time() - t0)} s")
        time.sleep(1)
    if ENGINE["dev"] != "cpu":
        try:
            setp(job, 2, None, "Cargando el modelo de transcripción (GPU)...")
            return run(get_model(size, "cuda"), "GPU")
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            print(">> GPU falló, uso CPU. Motivo:", e, flush=True)
    setp(job, 2, None, "Cargando el modelo de transcripción (CPU)...")
    return run(get_model(size, "cpu"), "CPU")


def _ask_ai_text(prompt, cfg, job=None):
    if cfg["ai"] == "gemini":
        last = "sin respuesta"
        for attempt in range(1, 4):
            try:
                if job is not None:
                    setp(job, 3, None, f"La IA está buscando los mejores momentos... (intento {attempt} de 3)")
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['model']}:generateContent",
                    headers={"x-goog-api-key": cfg["key"]}, timeout=60,
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json"}})
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f"Gemini respondió {r.status_code}: {r.text[:200]}"
                    time.sleep(5)
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"Gemini respondió {r.status_code}: {r.text[:300]}")
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last = f"Sin respuesta de Gemini en 60 s ({type(e).__name__})"
        else:
            raise RuntimeError(last + ". Prueba otro modelo (gemini-3.5-flash-lite) o inténtalo en unos minutos.")
    else:
        r = requests.post("http://localhost:11434/api/chat", timeout=1200,
                          json={"model": cfg["model"], "format": "json", "stream": False, "options": {"num_ctx": 16384},
                                "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        txt = r.json()["message"]["content"]
    return txt


def _parse_ai_json(txt):
    """Lee la lista JSON que devuelve la IA, aunque venga con ```json, texto antes o después, o comas de más."""
    t = (txt or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    cands = [t]
    i, j = t.find("["), t.rfind("]")
    if i != -1 and j > i:
        cands.append(t[i:j + 1])
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        cands.append(t[i:j + 1])
    last = None
    for c in cands:
        for fix in (c, re.sub(r",\s*([\]}])", r"\1", c)):  # la segunda quita comas sobrantes antes de ] o }
            try:
                data = json.loads(fix)
            except ValueError as ex:
                last = ex
                continue
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                lst = next((v for v in data.values() if isinstance(v, list)), None)
                if lst is not None:
                    return lst
                if "start" in data and "end" in data:
                    return [data]
    raise ValueError(f"La IA no devolvió una lista JSON válida ({last})")


def ask_ai(prompt, cfg, job=None):
    """Pide la lista de clips a la IA. Si la respuesta no es un JSON válido (pasa de vez en cuando), vuelve a pedirla hasta 3 veces."""
    err = None
    for k in range(3):
        extra = "" if k == 0 else "\n\nIMPORTANTE: responde únicamente un JSON válido (una lista), con los tiempos como números en segundos (por ejemplo 125.5, nunca 2:05)."
        try:
            return _parse_ai_json(_ask_ai_text(prompt + extra, cfg, job))
        except ValueError as ex:
            err = ex
            print(">> La IA devolvió un JSON inválido, reintento:", ex, flush=True)
    raise RuntimeError(str(err) + ". Se intentó 3 veces.")


CACHE_DIR = DATA / "cache"


def _tree_size(p, seen):
    """Bytes de una carpeta sin contar dos veces los archivos que son enlaces duros del mismo archivo."""
    tot = 0
    for f in Path(p).rglob("*"):
        try:
            if f.is_file():
                st = f.stat()
                key = (st.st_dev, st.st_ino)
                if st.st_ino and key in seen:
                    continue
                seen.add(key)
                tot += st.st_size
        except OSError:
            pass
    return tot


def storage_info():
    seen = set()
    clips = _tree_size(CLIPS, seen) if CLIPS.exists() else 0
    cache = _tree_size(CACHE_DIR, seen) if CACHE_DIR.exists() else 0
    jobs_b = _tree_size(JOBS, seen) if JOBS.exists() else 0
    return {"jobs": jobs_b, "cache": cache, "clips": clips,
            "clips_n": len(list(CLIPS.glob("*.mp4"))) if CLIPS.exists() else 0,
            "cache_n": len([x for x in CACHE_DIR.iterdir() if x.is_dir()]) if CACHE_DIR.exists() else 0}


def _active_dirs():
    """Carpetas que no se deben borrar porque un trabajo en curso las usa."""
    jd, cd = set(), set()
    for j in list(jobs.values()):
        if j.get("state") in ("working", "rendering"):
            jd.add(j["id"])
            if j.get("cache_tr"):
                cd.add(Path(j["cache_tr"]).parent.name)
    return jd, cd


def trim_cache(max_gb, keep=None):
    """Si el caché pasa de max_gb, borra los análisis guardados más viejos."""
    if not CACHE_DIR.exists():
        return
    items = sorted((x for x in CACHE_DIR.iterdir() if x.is_dir() and x != keep), key=lambda x: x.stat().st_mtime)
    seen = set()
    sizes = {x: _tree_size(x, seen) for x in items}
    total = sum(sizes.values()) + (_tree_size(keep, seen) if keep and keep.exists() else 0)
    for x in items:
        if total <= max_gb * 1e9:
            break
        total -= sizes[x]
        shutil.rmtree(x, ignore_errors=True)


def clean_storage(what, days):
    jd, cd = _active_dirs()
    before = storage_info()
    cutoff = time.time() - max(0.0, float(days)) * 86400
    if what == "jobs":
        for x in JOBS.iterdir() if JOBS.exists() else []:
            if x.is_dir() and x.name not in jd:
                shutil.rmtree(x, ignore_errors=True)
    elif what == "cache":
        for x in CACHE_DIR.iterdir() if CACHE_DIR.exists() else []:
            if x.is_dir() and x.name not in cd and x.stat().st_mtime <= cutoff:
                shutil.rmtree(x, ignore_errors=True)
    elif what == "clips":
        for x in CLIPS.iterdir() if CLIPS.exists() else []:
            if x.is_file() and x.stat().st_mtime <= cutoff:
                x.unlink(missing_ok=True)
    after = storage_info()
    return {"freed": max(0, sum(before[k] for k in ("jobs", "cache", "clips")) - sum(after[k] for k in ("jobs", "cache", "clips"))), **after}


@app.get("/api/storage")
def api_storage():
    return jsonify(storage_info())


@app.post("/api/storage/clean")
def api_storage_clean():
    p = request.get_json(force=True)
    if p.get("what") not in ("jobs", "cache", "clips"):
        return jsonify(error="Qué borrar: jobs, cache o clips"), 400
    return jsonify(clean_storage(p["what"], float(p.get("days", 0))))


def link_or_copy(src, dst):
    """Enlace duro (no ocupa espacio extra); si el disco no lo permite, copia normal."""
    dst = Path(dst)
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def analyze(job, p):
    d = JOBS / job["id"]
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    local = clean_path(p.get("local"))
    job.update(names=[("Preparar el video de tu computadora" if local else "Descargar audio y vista previa"), "Alinear audio y video", "Transcribir el audio", "La IA elige los clips"], step=0, pct=None,
               url=p.get("url") or "", local=local, offset=t2s(p.get("start")) or 0.0,
               t_start=now, t_step=now, prog_t=now, cancel=False)
    try:
        if local:
            st = Path(local).stat()
            ident = f"local:{local}:{st.st_size}:{int(st.st_mtime)}"
        else:
            ident = p["url"]
        key = hashlib.md5(f"v3|{ident}|{p.get('start')}|{p.get('end')}|{p['whisper']}".encode()).hexdigest()[:10]
        cache = DATA / "cache" / key
        cache.mkdir(parents=True, exist_ok=True)
        job["cache_tr"] = str(cache / "tr.json")
        if (cache / "source.mp4").exists() and (cache / "tr.json").exists():
            setp(job, 3, None, "Usando el video y la transcripción ya guardados (sin descargar)...")
            link_or_copy(cache / "source.mp4", d / "source.mp4")
            os.utime(cache, None)  # lo usado hace poco se borra al último
            saved = json.loads((cache / "tr.json").read_text(encoding="utf-8"))
            words = [tuple(w) for w in saved["words"]]
            lines = [tuple(x) for x in saved["lines"]]
        else:
            if local:
                setp(job, 0, None, "Preparando el video de tu computadora...", 0.0)
                local_prepare(job, local, t2s(p.get("start")), t2s(p.get("end")), d)
                job["title"] = Path(local).stem
            else:
                setp(job, 0, None, "Descargando el video...", 0.0)
                download(p["url"], t2s(p.get("start")), t2s(p.get("end")), str(d / "source"), job)
                normalize(d, job, "source", lambda f, m: setp(job, 1, None if f is None else int(f * 100), m, 0.12 + 0.08 * (f or 0)), light=True)
            setp(job, 2, None, "Preparando la transcripción...", 0.20)
            words, lines = transcribe(d / "source.mp4", p["whisper"], job)
            link_or_copy(d / "source.mp4", cache / "source.mp4")
            trim_cache(6.0, keep=cache)
            (cache / "tr.json").write_text(json.dumps({"words": words, "lines": lines}), encoding="utf-8")
        job["words"] = words
        job["lines"] = lines
        meta = cache / "meta.json"
        if not job.get("title") and meta.exists():
            try:
                job["title"] = json.loads(meta.read_text(encoding="utf-8")).get("title", "")
            except Exception:  # noqa: BLE001
                job["title"] = ""
        if not job.get("title") and not local:
            job["title"] = fetch_title(p["url"])
        if job.get("title"):
            meta.write_text(json.dumps({"title": job["title"]}, ensure_ascii=False), encoding="utf-8")
        info = video_info(d / "source.mp4")
        job["dur"] = round(info[3], 1) if info else 0
        setp(job, 3, None, "La IA está buscando los mejores momentos...")
        text = "\n".join(f"[{int(t)}] {x}" for t, x in lines)
        flex = bool(p.get("flex", True))
        if flex:
            dur_txt = (f"La duración ideal está entre {p['min']} y {p['max']} segundos, pero es solo una guía: cada clip dura "
                       "lo que pida su contexto. Si la idea se cierra antes, hazlo más corto (mínimo unos 15 segundos); si el "
                       f"tema lo necesita, puede ser más largo (hasta {int(p['max'] * 2)} segundos). No cortes una idea a la "
                       "mitad ni alargues con relleno. Empieza al inicio de una idea y termina cuando se cierra. ")
        else:
            dur_txt = (f"Cada clip dura entre {p['min']} y {p['max']} segundos, empieza al inicio de una idea "
                       "y termina cuando se cierra. ")
        tags_txt = "Termina el caption con 3 a 5 hashtags relevantes. " if p.get("tags", True) else "No pongas hashtags. "
        cap_txt = ('Para cada clip escribe también "caption": un texto para publicar de 1 a 3 frases cortas '
                   '(máximo 220 caracteres sin contar los hashtags) que explique con claridad de qué trata ese clip: '
                   'quién dice o hace qué y por qué importa, usando datos concretos que aparezcan en ese tramo. '
                   'Escríbelo en español neutral y natural, como lo escribiría una persona que edita la página, '
                   'NO como un anuncio. No uses modismos, jerga ni expresiones dominicanas, no imites cómo hablan en '
                   'el video, no uses emojis ni signos de exclamación, y evita frases hechas como "no te lo pierdas", '
                   '"increíble", "tienes que ver" o "lo que pasó después". No exageres ni inventes nada que no esté en '
                   'el texto; si no queda claro algún dato, no lo menciones. ' + tags_txt)
        prompt = (
            "Eres editor de clips virales para redes sociales. El video es en español dominicano (RD); "
            "entiende sus modismos y humor local para elegir bien los momentos, pero no los uses al escribir. Del siguiente texto con tiempos en segundos, elige hasta "
            f"{p['n']} momentos con potencial: anécdotas, humor, polémica, frases fuertes, revelaciones o "
            "consejos. " + ctx_block(p) + dur_txt + "No se solapan. " + cap_txt + "Responde SOLO un JSON: lista de objetos con "
            '"start" y "end" (segundos, número), "title" (corto y llamativo), "reason" (una frase), "caption" y '
            '"score" (1 a 100).\n\nTRANSCRIPCIÓN:\n' + text)
        out = []
        for c in ask_ai(prompt, p, job):
            s, e = float(c["start"]), float(c["end"])
            e = min(e, s + p["max"] * (2.0 if flex else 1.3))
            s, e = snap(words, s, e, job.get("dur") or 0)
            if e - s < (8 if flex else 5):
                continue
            out.append({"start": round(s, 1), "end": round(e, 1),
                        "title": str(c.get("title", "Clip")), "reason": str(c.get("reason", "")),
                        "score": c.get("score", ""), "caption": str(c.get("caption") or "").strip()})
        nota = (f" (No se pudieron leer las cookies de {COOKIES['failed']}; se siguió sin ellas. "
                "Prueba con Firefox.)") if COOKIES.get("failed") else ""
        names = job.get("names") or []
        if isinstance(job.get("step"), int) and job["step"] < len(names):
            job.setdefault("times", {})[names[job["step"]]] = round(time.time() - job.get("t_step", time.time()), 1)
        job["times"]["Total"] = round(time.time() - job.get("t_start", time.time()), 1)
        print(">> Tiempos:", job["times"], flush=True)
        job["clips"], job["state"], job["msg"] = out[: p["n"]], "ready", f"{len(out)} clips sugeridos{nota}"
        job.update(step=4, pct=100, eta=None)
    except Cancelled:
        job["state"], job["msg"] = "cancelled", "Cancelado."
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        msg = str(e)
        if "Sign in to confirm" in msg:
            msg = ("YouTube pidió verificar que no eres un robot. Elige tu navegador en 'Cookies de YouTube' "
                   "(Firefox suele funcionar mejor), con la sesión de YouTube iniciada, y vuelve a analizar.")
        job["state"], job["msg"] = "error", msg[-400:]


# ---------- modo directo: grabar mientras transmite, analizar por bloques y al final editar como un video normal ----------
def edit_src(d):
    """Video para editar y previsualizar. En un directo es la copia liviana (proxy.mp4, 360p); si no existe, la grabación."""
    pr = Path(d) / "proxy.mp4"
    return pr if pr.exists() and pr.stat().st_size > 50_000 else Path(d) / "source.mp4"


_PROXY_VC = []


def proxy_codec():
    """Códec para la copia liviana del directo: la GPU (casi no se nota) o, si no se puede, la CPU en modo muy ligero."""
    if not _PROXY_VC:
        try:
            r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.3:r=30", "-c:v", "h264_nvenc",
                                "-f", "null", "-"], capture_output=True, timeout=30)
            ok = r.returncode == 0
        except Exception:  # noqa: BLE001
            ok = False
        _PROXY_VC.extend(["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "30", "-b:v", "0"] if ok
                         else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30"])
    return list(_PROXY_VC)


class LiveSkip(Exception):
    """Marca: en un directo no hay descarga en alta calidad; los clips salen de la grabación."""


def _clean_err(ex):
    return re.sub(r"\x1b\[[0-9;]*m|\[\d+(?:;\d+)*m", "", str(ex)).strip()


class LiveNoFormat(Exception):
    """YouTube no entregó ningún formato de video: reintentar no sirve, hay que actualizar yt-dlp."""


def live_stream_url(url, quality):
    """Direcciones de la transmisión para grabarla con ffmpeg. Devuelve (urls, headers, título, en_vivo, alto).
    Primero busca un formato con audio y video juntos; si el directo solo trae video y audio separados, usa los dos."""
    q = int(quality)
    base = {"quiet": True, "noplaylist": True, "socket_timeout": 30}
    sels = [f"best[height<={q}][acodec!=none][vcodec!=none]/best[acodec!=none][vcodec!=none]/best",
            f"bv*[height<={q}]+ba/bv*+ba/b"]

    def extract(sel):
        tries = [{**base, "format": sel, **cookie_opts()}]
        if cookie_opts():  # con las cookies de la sesión a veces YouTube da menos formatos: se prueba también sin ellas
            tries.append({**base, "format": sel})
        err = None
        for k, o in enumerate(tries):
            try:
                with yt_dlp.YoutubeDL(o) as ydl:
                    return ydl.extract_info(url, download=False)
            except Exception as ex:  # noqa: BLE001
                err = ex
                low = str(ex).lower()
                if k < len(tries) - 1 and ("cookie" in low or "no video formats" in low or "format is not available" in low):
                    if "cookie" in low:
                        COOKIES["failed"] = COOKIES.get("browser") or "el navegador"
                    continue
                raise
        raise err

    info = None
    for i, sel in enumerate(sels):
        try:
            info = extract(sel)
            break
        except Exception as ex:  # noqa: BLE001
            low = str(ex).lower()
            if "no video formats" in low:
                i = len(sels) - 1  # no hay formatos de ningún tipo: no sirve probar otro selector
            if "format is not available" in low or "no video formats" in low:
                if i == len(sels) - 1:
                    raise LiveNoFormat("YouTube no entregó ningún formato de video para ese link. [" + ytdlp_diag() + "] Si dice NO ENCONTRADO, "
                                       "instala Deno y cierra y abre Clipear. Si el link ya no es un directo en vivo, o es solo para miembros, tampoco se puede.") from ex
                continue
            raise
    fm = info.get("requested_formats") or [info]
    urls = [f["url"] for f in fm if f.get("url")]
    if not urls:
        raise RuntimeError("No pude obtener la dirección de la transmisión.")
    live = bool(info.get("is_live")) or info.get("live_status") == "is_live"
    hdr = info.get("http_headers") or fm[0].get("http_headers") or {}
    height = max([f.get("height") or 0 for f in fm] + [0]) or info.get("height")
    return urls[:2], hdr, info.get("title") or "", live, height


AUDIO_BPS = SR * 2  # audio.raw: 16 kHz, mono, 16 bits = 32 000 bytes por segundo


def live_audio_slice(d, t0, t1):
    """Audio (16 kHz mono) de la grabación entre t0 y t1, leído del archivo audio.raw que se va escribiendo."""
    a, n = int(max(0.0, t0) * SR) * 2, int(max(0.0, t1 - t0) * SR) * 2
    try:
        with open(Path(d) / "audio.raw", "rb") as f:
            f.seek(a)
            b = f.read(n)
    except OSError:
        return np.zeros(0, np.float32)
    return np.frombuffer(b[: len(b) // 2 * 2], np.int16).astype(np.float32) / 32768.0


def _live_audio_seconds(d):
    try:
        return (Path(d) / "audio.raw").stat().st_size / AUDIO_BPS
    except OSError:
        return 0.0


def _live_recorder(job, p, lv, d):
    """Graba la transmisión sin recomprimir: video+audio en source.mp4 (fragmentado, se puede leer mientras crece) y el audio aparte
    (audio.raw) para analizarlo. Termina al pulsar «Detener», al cumplirse el tiempo, si la transmisión acaba o si el disco se llena."""
    fails = 0
    try:
        while True:
            try:
                urls, hdr, title, _, height = live_stream_url(p["url"], p.get("quality", 1080))
                lv["height"] = height
                if title and not job.get("title"):
                    job["title"] = title
                if COOKIES.get("failed") and not lv["note"]:
                    lv["note"] = f"No se pudieron leer las cookies de {COOKIES['failed']}; se grabó sin ellas (para un directo público suele bastar)."
                break
            except LiveNoFormat as ex:
                lv["note"] = _clean_err(ex)
                return
            except Exception as ex:  # noqa: BLE001
                fails += 1
                lv["note"] = f"No pude conectar con la transmisión ({fails}): {_clean_err(ex)[-300:]}"
                if fails >= 4 or lv["stop"] or job.get("cancel"):
                    return
                time.sleep(10)
        limit = lv["limit"]
        tl = ["-t", f"{limit:.1f}"] if limit > 0 else []
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
        hdrs = "".join(f"{k}: {v}\r\n" for k, v in hdr.items())
        for u in urls:
            if u.startswith("http"):
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30"]
                if hdrs:
                    cmd += ["-headers", hdrs]
            cmd += ["-i", u]
        aud = "1:a:0" if len(urls) == 2 else "0:a:0"  # video y audio separados: el audio viene de la segunda entrada
        cmd += ["-map", "0:v:0", "-map", aud + "?", "-c", "copy", "-bsf:a", "aac_adtstoasc", *tl, "-flush_packets", "1", "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                str(d / "source.mp4"), "-map", aud, "-vn", "-ac", "1", "-ar", str(SR), *tl, "-f", "s16le", str(d / "audio.raw"),
                "-map", "0:v:0", "-map", aud + "?", "-vf", "scale=-2:360", *proxy_codec(), "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
                "-ar", "48000", *tl, "-flush_packets", "1", "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", str(d / "proxy.mp4")]
        last_disk = 0.0
        with open(d / "ff_live_err.txt", "a", encoding="utf-8") as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True)
            lv["proc"] = proc
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    try:
                        lv["rec"] = max(lv["rec"], int(line.split("=")[1]) / 1e6)
                    except ValueError:
                        pass
                if time.time() - last_disk > 30:
                    last_disk = time.time()
                    if shutil.disk_usage(d).free < 2.5e9:
                        lv["note"] = "Casi no queda espacio en el disco: se detuvo la grabación para no llenarlo."
                        lv["stop"] = True
                if lv["stop"] or job.get("cancel"):
                    try:
                        proc.stdin.write("q\n")
                        proc.stdin.flush()
                    except Exception:  # noqa: BLE001
                        pass
                    break
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        lv["proc"] = None
        if lv["rec"] < 1 and not lv["stop"] and not job.get("cancel"):
            try:
                tail = (d / "ff_live_err.txt").read_text(encoding="utf-8", errors="ignore").strip()[-350:]
            except OSError:
                tail = ""
            lv["note"] = "ffmpeg no pudo grabar la transmisión" + (": " + tail if tail else ".")
        elif not lv["stop"] and not job.get("cancel") and (limit <= 0 or lv["rec"] < limit - 2):
            lv["note"] = lv["note"] or "La transmisión terminó o se cortó."
    finally:
        lv["rec_done"] = True


def transcribe_audio(audio, size):
    """Transcribe un trozo de audio (array 16 kHz). Devuelve (palabras, líneas) con tiempos desde el inicio del trozo."""
    t0 = time.time()
    while ENGINE["state"] == "cargando":
        time.sleep(1)
        if time.time() - t0 > 600:
            break

    def run(m):
        segs, _ = m.transcribe(audio, language="es", word_timestamps=True, vad_filter=True, beam_size=1, condition_on_previous_text=False)
        words, lines = [], []
        for sg in segs:
            lines.append((sg.start, sg.text.strip()))
            words += [(w.start, w.end, w.word.strip()) for w in (sg.words or [])]
        return words, lines

    if ENGINE["dev"] != "cpu":
        try:
            return run(get_model(size, "cuda"))
        except Exception as e:  # noqa: BLE001
            print(">> GPU falló en el directo, uso CPU. Motivo:", e, flush=True)
    return run(get_model(size, "cpu"))


def ctx_block(p):
    """Contexto que el editor escribió sobre el programa y sus participantes, para que la IA reconozca a la gente y escriba bien los nombres."""
    t = (p.get("context") or "").strip()
    if not t:
        return ""
    return ("CONTEXTO DEL PROGRAMA (lo escribió el editor; úsalo para saber quiénes son las personas): " + t[:4000] + "\n"
            "Cuando se pueda saber quién habla o quién hace algo (porque lo llaman por su nombre, se presenta o el contexto lo deja claro), "
            "pon su nombre o apodo en el título y en el caption (por ejemplo «Fulana confronta a Mengana»). Si no estás seguro de quién es, NO adivines: "
            "describe la situación sin nombrar. Escribe los nombres como aparecen en el contexto, aunque la transcripción los escriba mal. ")


def propose_clips(lines, words, p, job, dur, n):
    """Pide a la IA hasta n clips con el texto dado (igual que el análisis normal). Devuelve [{start, end, title, ...}]."""
    flex = bool(p.get("flex", True))
    if flex:
        dur_txt = (f"La duración ideal está entre {p['min']} y {p['max']} segundos, pero es solo una guía: cada clip dura lo que pida su "
                   "contexto. No cortes una idea a la mitad ni alargues con relleno. Empieza al inicio de una idea y termina cuando se cierra. ")
    else:
        dur_txt = f"Cada clip dura entre {p['min']} y {p['max']} segundos, empieza al inicio de una idea y termina cuando se cierra. "
    tags_txt = "Termina el caption con 3 a 5 hashtags relevantes. " if p.get("tags", True) else "No pongas hashtags. "
    cap_txt = ('Para cada clip escribe también "caption": un texto para publicar de 1 a 3 frases cortas (máximo 220 caracteres sin contar los '
               'hashtags) que explique con claridad de qué trata ese clip, con datos concretos que aparezcan en ese tramo. Español neutral y '
               'natural, sin emojis, sin signos de exclamación, sin jerga ni modismos y sin frases hechas. No inventes nada. ' + tags_txt)
    text = "\n".join(f"[{int(t)}] {x}" for t, x in lines)
    prompt = ("Eres editor de clips virales para redes sociales. Es un fragmento de un reality o transmisión en directo en español dominicano "
              "(RD); entiende sus modismos y humor local para elegir bien los momentos, pero no los uses al escribir. Del siguiente texto con "
              f"tiempos en segundos, elige hasta {n} momentos con potencial : anécdotas, humor, "
              "peleas o polémica, frases fuertes, revelaciones. " + ctx_block(p) + dur_txt + "No se solapan. " + cap_txt +
              'Responde SOLO un JSON: lista de objetos con "start" y "end" (segundos, número como 125.5, nunca mm:ss), "title" (corto y llamativo), "reason" (una frase), '
              '"caption" y "score" (número de 1 a 100).\n\nTRANSCRIPCIÓN:\n' + text)
    out = []
    for c in ask_ai(prompt, p, None):
        s, e = float(c["start"]), float(c["end"])
        e = min(e, s + p["max"] * (2.0 if flex else 1.3))
        s, e = snap(words, s, e, dur)
        if e - s < (8 if flex else 5):
            continue
        out.append({"start": round(s, 1), "end": round(e, 1), "title": str(c.get("title", "Clip")), "reason": str(c.get("reason", "")),
                    "score": c.get("score", ""), "caption": str(c.get("caption") or "").strip()})
    return out


def _overlaps(a, b):
    ov = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return ov > 0.3 * min(a["end"] - a["start"], b["end"] - b["start"])


def live_run(job, p):
    d = JOBS / job["id"]
    d.mkdir(parents=True, exist_ok=True)
    lv = job["lv"] = {"rec": 0.0, "limit": float(p["limit"]), "stop": False, "rec_done": False, "proc": None, "block": float(p["block"]),
                      "blocks": 0, "words": [], "lines": [], "clips": [], "note": "", "phase": "grabando", "msg": "Conectando con el directo...",
                      "next_uid": 1, "height": None, "min_score": float(p.get("min_score", 0))}
    job.update(url=p["url"], offset=0.0, live=True, dur=0.0, words=lv["words"], lines=lv["lines"])  # el editor usa estas mismas listas mientras crecen
    rec_thread = threading.Thread(target=_live_recorder, args=(job, p, lv, d), daemon=True)
    rec_thread.start()
    try:
        t0, ctx = 0.0, 60.0
        while True:
            avail, done = min(lv["rec"], _live_audio_seconds(d)) if not lv["rec_done"] else _live_audio_seconds(d), lv["rec_done"]
            job["dur"] = round(max(job["dur"], lv["rec"]), 1)
            be = t0 + lv["block"]
            if not (avail >= be + 2 or (done and avail - t0 >= 8)):
                if done:
                    break
                lv["msg"] = f"{'Parando…' if lv['stop'] else 'Grabando'} · bloques analizados: {lv['blocks']} · clips encontrados: {len(lv['clips'])}"
                time.sleep(2)
                continue
            b1 = min(be, avail) if done else be
            win0 = max(0.0, t0 - 8.0)
            audio = live_audio_slice(d, win0, b1)
            lv["msg"] = f"Analizando el bloque {lv['blocks'] + 1}..."
            if len(audio) > SR * 3:
                w, ln = transcribe_audio(audio, p["whisper"])
                lv["words"] += [(a + win0, b + win0, x) for a, b, x in w if a + win0 >= t0 - 0.01]
                lv["lines"] += [(s + win0, x) for s, x in ln if s + win0 >= t0 - 0.01]
            lv["blocks"] += 1
            ctx_lines = [(t, x) for t, x in lv["lines"] if t >= t0 - ctx]
            if ctx_lines:
                try:
                    props = propose_clips(ctx_lines, lv["words"], p, job, b1, int(p["per_block"]))
                    print(f">> [Directo bloque {lv['blocks']}] líneas={len(ctx_lines)} · IA propuso {len(props)} clips", flush=True)
                    for c in props:
                        try:
                            if float(c["score"]) < lv["min_score"]:
                                continue
                        except (TypeError, ValueError):
                            pass
                        if c["end"] <= t0 - 0.5 or any(_overlaps(c, o) for o in lv["clips"]):
                            continue
                        c["uid"] = lv["next_uid"]
                        lv["next_uid"] += 1
                        lv["clips"].append(c)
                except Exception as ex:  # noqa: BLE001
                    print(f">> [Directo bloque {lv['blocks']}] error IA:", ex, flush=True)
                    lv["note"] = f"La IA falló en un bloque: {_clean_err(ex)[-100:]}"
            t0 = b1
        job["dur"] = round(max(job["dur"], lv["rec"]), 1)
        lv["phase"], lv["msg"] = "listo", f"Grabación terminada: {len(lv['clips'])} clips sugeridos."
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        lv["stop"] = True
        lv["phase"], lv["msg"] = "error", str(e)[-300:]


def live_public(j):
    lv = j.get("lv")
    if not lv:
        return None
    try:
        gb = (JOBS / j["id"] / "source.mp4").stat().st_size / 1e9
    except OSError:
        gb = 0.0
    return {"height": lv.get("height"), "rec": round(lv["rec"], 1), "limit": lv["limit"], "blocks": lv["blocks"], "phase": lv["phase"],
            "stopping": lv["stop"], "note": lv["note"], "msg": lv["msg"], "gb": round(gb, 2), "n": len(lv["clips"])}


@app.post("/api/live/start")
def api_live_start():
    p = request.get_json(force=True)
    if not (p.get("url") or "").strip():
        return jsonify(error="Falta el link del directo"), 400
    if any((x.get("lv") or {}).get("phase") == "grabando" for x in jobs.values()):
        return jsonify(error="Ya hay una grabación en curso. Detenla antes de empezar otra."), 400
    mins = float(p.get("minutes", 0))
    if mins != 0 and not 2 <= mins <= 720:
        return jsonify(error="La duración debe ser 0 (hasta que la detengas) o entre 2 minutos y 12 horas."), 400
    p.update(limit=mins * 60, block=min(1200.0, max(120.0, float(p.get("block_min", 6)) * 60)), per_block=max(1, min(6, int(p.get("per_block", 2)))),
             min=int(p.get("min", 20)), max=int(p.get("max", 60)), flex=bool(p.get("flex", True)), tags=bool(p.get("tags", True)),
             quality=int(p.get("quality", 1080)), min_score=float(p.get("min_score", 0)))
    COOKIES["browser"] = (p.get("browser") or "").strip().lower()
    COOKIES["failed"] = ""
    j = {"id": uuid.uuid4().hex[:8], "state": "ready", "msg": "Directo en curso", "clips": [], "out": []}
    jobs[j["id"]] = j
    threading.Thread(target=live_run, args=(j, p), daemon=True).start()
    return jsonify(public(j))


@app.get("/api/live/status")
def api_live_status():
    j = jobs.get(request.args.get("job"))
    if not j or not j.get("lv"):
        return jsonify(error="No hay una grabación en curso."), 404
    since = int(request.args.get("since", 0))
    return jsonify(live=live_public(j), clips=[c for c in j["lv"]["clips"] if c["uid"] > since])


@app.post("/api/live/stop")
def api_live_stop():
    j = jobs.get((request.get_json(force=True) or {}).get("job"))
    if not j or not j.get("lv"):
        return jsonify(error="No hay una grabación en curso."), 400
    j["lv"]["stop"] = True
    return jsonify(ok=True)


def group_sub_phrases(words, s, e, max_words=5):
    """Agrupa las palabras del clip (entre s y e) en frases cortas para que el usuario pueda editarlas."""
    ws = [(a, b, w) for a, b, w in words if a >= s - 0.05 and b <= e + 0.3]
    groups, cur = [], []
    for w in ws:
        if cur and (len(cur) >= max_words or w[0] - cur[-1][1] > 0.8):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return [{"start": round(g[0][0], 2), "end": round(g[-1][1], 2),
             "text": " ".join(x[2] for x in g)} for g in groups]


def build_ass(words, s, e, W, H, sub, txt):
    pos = {"bottom": 2, "middle": 5, "top": 8}[sub.get("pos", "bottom")]
    wpct = min(100.0, max(30.0, float(sub.get("width") or 84)))
    mlr = int(W * (100 - wpct) / 200)
    mv = int(H * 0.12)
    ptag = ""
    if sub.get("pos_y") is not None:  # posición libre: centro del bloque de subtítulos en % del lienzo
        pos, mv = 5, 0
        wpx = W - 2 * mlr
        px = int(min(max(W * float(sub.get("pos_x", 50)) / 100, wpx / 2), W - wpx / 2))
        py = int(H * min(100.0, max(0.0, float(sub["pos_y"]))) / 100)
        ptag = f"{{\\an5\\pos({px},{py})}}"
    max_chars = max(8, int((W - 2 * mlr) / (max(10, float(sub["size"])) * (0.70 if sub.get("upper") else 0.58))))
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
            "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            f"Style: Sub,{sub['font']},{sub['size']},{hexcol(sub['color'])},&H000000FF,{hexcol(sub['outline_color'])},&H00000000,"
            f"-1,0,0,0,100,100,0,0,1,{sub['outline']},0,{pos},{mlr},{mlr},{mv},1\n"
            f"Style: Mark,Arial,{txt['size']},{hexcol(txt['color'])},&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,{int(W * 0.04)},{int(W * 0.04)},0,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    ev = []
    sev = []
    if sub.get("enabled"):
        ws = [(a - s, b - s, w.upper() if sub.get("upper") else w) for a, b, w in words if a >= s - 0.05 and b <= e + 0.3]
        groups, cur = [], []
        for w in ws:
            if cur and (len(cur) >= sub.get("max_words", 4) or w[0] - cur[-1][1] > 0.8
                        or sum(len(x[2]) for x in cur) + len(cur) + len(w[2]) > max_chars):
                groups.append(cur)
                cur = []
            cur.append(w)
        if cur:
            groups.append(cur)
        for g in groups:
            longest = max(len(x[2]) for x in g)
            fit = min(100, int(100 * max_chars / longest)) if longest > max_chars else 100
            pre = ptag + (f"{{\\fscx{fit}\\fscy{fit}}}" if fit < 100 else "")
            if sub.get("highlight"):
                for i, w in enumerate(g):
                    end_t = max(w[0] + 0.05, g[i + 1][0] if i + 1 < len(g) else g[-1][1])
                    body = " ".join(f"{{\\c{hexcol(sub['highlight'])}}}{x[2]}{{\\c{hexcol(sub['color'])}}}" if j == i else x[2]
                                    for j, x in enumerate(g))
                    sev.append([w[0], end_t, f"{pre}{body}"])
            else:
                sev.append([g[0][0], g[-1][1], f"{pre}{' '.join(x[2] for x in g)}"])
        # Nunca dos líneas de subtítulo a la vez: cada una termina cuando empieza la siguiente
        sev.sort(key=lambda z: z[0])
        for k in range(len(sev) - 1):
            if sev[k][1] > sev[k + 1][0]:
                sev[k][1] = sev[k + 1][0]
        for st, en, body in sev:
            if en - st >= 0.02:
                ev.append(f"Dialogue: 0,{ts(st)},{ts(en)},Sub,,0,0,0,,{body}")
    if txt.get("value"):
        m = int(W * 0.04)
        if txt.get("pos") == "custom":  # arrastrado con el ratón: centro del texto en % del lienzo
            an, x, y = 5, int(W * min(100.0, max(0.0, float(txt.get("x", 50)))) / 100), int(H * min(100.0, max(0.0, float(txt.get("y", 70)))) / 100)
        else:
            an, x, y = {"tl": (7, m, m), "tr": (9, W - m, m), "bl": (1, m, H - m), "br": (3, W - m, H - m),
                        "c": (5, W // 2, H // 2), "bc": (5, W // 2, int(H * 0.70))}[txt["pos"]]
        a = int((1 - float(txt["opacity"])) * 255)
        ev.append(f"Dialogue: 1,0:00:00.00,{ts(e - s)},Mark,,0,0,0,,{{\\an{an}\\pos({x},{y})\\alpha&H{a:02X}&}}{txt['value']}")
    return head + "\n".join(ev) + "\n"


def find_logo(lg):
    """Ruta del logo elegido o generado (creadores / plataformas) si está activado."""
    if not lg.get("enabled"):
        return None
    file_val = (lg.get("file") or "").strip()
    if file_val.startswith("badge:"):
        return generate_badge_image(file_val)
    name = secure_filename(file_val)
    p = LOGOS / name
    return p if name and p.is_file() else None


TITLE_DIR = DATA / "titles"


def _title_png(tt):
    """El título lo dibuja el navegador (con emojis) y llega como PNG en base64; se guarda y se superpone."""
    m = re.match(r"data:image/png;base64,(.+)", str(tt.get("png") or ""), re.S)
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group(1))
    except Exception:  # noqa: BLE001
        return None
    if not raw.startswith(b"\x89PNG") or len(raw) > 6_000_000:
        return None
    TITLE_DIR.mkdir(parents=True, exist_ok=True)
    p = TITLE_DIR / (hashlib.sha1(raw).hexdigest()[:16] + ".png")
    if not p.exists():
        p.write_bytes(raw)
    return p


def find_overlays(o):
    """Lista de (ruta, ajustes) con el logo y la insignia de creador; pueden ir los dos a la vez."""
    out = []
    for key in ("logo", "badge"):
        cfg = o.get(key)
        if not cfg:
            continue
        p = find_logo(cfg)
        if p:
            out.append((p, cfg))
    tt = o.get("title")
    if tt:
        p = _title_png(tt)
        if p:
            out.append((p, {"kind": "title", "y": tt.get("y", 62), "x": tt.get("x", 50)}))
    return out


def generate_badge_image(badge_str):
    """Genera una insignia limpia sin fondo negro y con el texto más pegado al logo."""
    if cv2 is None:
        return None
    try:
        parts = badge_str.split(":", 2)
        plat = (parts[1] if len(parts) > 1 else "kick").lower()
        creator = (parts[2] if len(parts) > 2 else "@creador").upper()
        if not creator.startswith("@") and not "." in creator:
            creator = f"@{creator}"

        badge_png = BADGES_DIR / f"{plat}.png"

        domain_text = {
            "kick": f"KICK.COM/{creator.lstrip('@')}",
            "twitch": f"TWITCH.TV/{creator.lstrip('@')}",
            "youtube": f"YOUTUBE.COM/{creator.lstrip('@')}",
            "tiktok": f"TIKTOK.COM/{creator}",
            "instagram": f"INSTAGRAM.COM/{creator}"
        }.get(plat, f"{plat.upper()}.COM/{creator}")

        h = 56
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.62
        thickness = 2

        (tw, th), _ = cv2.getTextSize(domain_text, font, scale, thickness)
        icon_sz = 40
        pad_x = 8  # Más pegado entre el logo y el texto
        w = icon_sz + tw + (pad_x * 3)

        # Lienzo RGBA totalmente transparente
        img = np.zeros((h, w, 4), dtype=np.uint8)

        # Cargar icono PNG transparente desde badges/
        icon_loaded = False
        if badge_png.is_file():
            try:
                icon_img = cv2.imread(str(badge_png), cv2.IMREAD_UNCHANGED)
                if icon_img is not None:
                    ih, iw = icon_img.shape[:2]
                    scale_ic = icon_sz / max(ih, iw)
                    res_ic = cv2.resize(icon_img, (0, 0), fx=scale_ic, fy=scale_ic)
                    i_h, i_w = res_ic.shape[:2]
                    
                    ix = pad_x
                    iy = (h - i_h) // 2

                    if res_ic.shape[2] == 4:
                        alpha = res_ic[:, :, 3:4] / 255.0
                        rgb = res_ic[:, :, :3]
                        roi = img[iy:iy+i_h, ix:ix+i_w, :3]
                        img[iy:iy+i_h, ix:ix+i_w, :3] = (alpha * rgb + (1 - alpha) * roi).astype(np.uint8)
                        img[iy:iy+i_h, ix:ix+i_w, 3] = np.maximum(img[iy:iy+i_h, ix:ix+i_w, 3], res_ic[:, :, 3])
                    else:
                        img[iy:iy+i_h, ix:ix+i_w, :3] = res_ic[:, :, :3]
                        img[iy:iy+i_h, ix:ix+i_w, 3] = 255
                    icon_loaded = True
            except Exception as e:
                print(">> Error cargando icono PNG:", e, flush=True)

        if not icon_loaded:
            # Respaldo si no hay PNG: un círculo de color
            cv2.circle(img, (pad_x + icon_sz//2, h//2), icon_sz//2, (50, 50, 50, 255), -1)

        # Escribir solo el texto en blanco nítido (SIN NINGÚN FONDO DETRÁS)
        tx = pad_x + icon_sz + pad_x
        ty = (h + th) // 2 - 2
        cv2.putText(img, domain_text, (tx, ty), font, scale, (255, 255, 255, 255), thickness, cv2.LINE_AA)

        out_path = DATA / "badge_tmp.png"
        cv2.imwrite(str(out_path), img)
        return out_path
    except Exception as e:
        print(">> Error generando insignia:", e, flush=True)
        return None


def even(v):
    return max(2, int(round(v / 2)) * 2)


def parse_ratio(txt, default=16 / 9):
    try:
        a, b = (float(x) for x in str(txt).replace("/", ":").split(":")[:2])
        return a / b if a > 0 and b > 0 else default
    except Exception:  # noqa: BLE001
        return default


def layout_dims(o, W, H, info):
    """Tamaño (ancho, alto) del video dentro del lienzo en el modo 'marco'; y si va completo (sin recortar)."""
    lay = o.get("layout") or {}
    FW = even(W * min(100.0, max(30.0, float(lay.get("scale", 100) or 100))) / 100)
    fg = str(lay.get("fg") or "orig")
    if fg == "custom":
        fg = str(lay.get("fgc") or "16:9")
    nocrop = fg == "orig" or ":" not in fg
    ar = (info[0] / info[1]) if (nocrop and info and info[1]) else parse_ratio(fg if not nocrop else "16:9")
    FH = even(FW / ar)
    if FH > H:
        FH = H
        FW = min(W, even(H * ar))
    return FW, FH, nocrop


def frame_compose(chain, o, W, H, tw, th):
    """Pone el video (tw x th) sobre un fondo (borroso, negro o de color) del tamaño del lienzo."""
    lay = o.get("layout") or {}
    blur = int(min(60, max(4, float(lay.get("blur", 25) or 25))))
    bw, bh = even(W / 4), even(H / 4)
    bg = (f"scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur={max(2, blur // 4)}:2,"
          f"scale={W}:{H}:flags=bicubic")
    kind = lay.get("bg", "blur")
    if kind in ("black", "color"):
        col = re.sub(r"[^0-9a-fA-F]", "", str(lay.get("bg_color") or "000000"))[:6].rjust(6, "0") if kind == "color" else "000000"
        bg += f",drawbox=x=0:y=0:w=iw:h=ih:color=0x{col}:t=fill"
    pct = min(100.0, max(0.0, float(lay.get("y", 50) if lay.get("y") is not None else 50)))
    x, y = even((W - tw) / 2) if W > tw else 0, int(round((H - th) * pct / 100 / 2) * 2) if H > th else 0
    return f"[0:v]split[s1][s2];[s2]{bg}[bg];[s1]{chain}[fg];[bg][fg]overlay={x}:{y},setsar=1[b]"


def fg_chain(o, tw, th, nocrop, src, s, e, info, cb=None, spk=None):
    """Filtros (sin etiquetas) que dejan el video en tw x th, con la cámara elegida. Devuelve (cadena, nota)."""
    if nocrop:
        return f"scale={tw}:{th}:flags=lanczos,setsar=1", "Video completo dentro del marco"
    note = ""
    chain = f"scale={tw}:{th}:force_original_aspect_ratio=increase:flags=lanczos,crop={tw}:{th},setsar=1"
    tr = o.get("track") or {}
    tm = tr.get("mode")
    scaled_w = int(round(info[0] * th / info[1] / 2) * 2) if info else 0
    rec = o.get("_plan_out")  # si viene, aquí se deja el plan calculado (lo usa /api/cam_plan)
    if rec is not None:
        rec.update(tw=tw, scaled_w=scaled_w, x0=None, moves=[], samples=[])

    def rec_plan(x0, moves, samples=None):
        if rec is not None:
            rec["x0"], rec["moves"] = x0, list(moves)
            if samples is not None:
                rec["samples"] = [(float(t), bool(h)) for t, h in samples]

    def pan(xe):
        return f"scale=-2:{th}:flags=lanczos,crop={tw}:{th}:x='{xe}':y=0,setsar=1"

    def smart(sampler_out, voice):
        plan = plan_smart(sampler_out, scaled_w, tw, voice) if sampler_out is not None else None
        return plan

    if scaled_w - tw > 4 and tm == "manual":
        span = scaled_w - tw
        # Convertir centro deseado (0..1 del ancho original) en desplazamiento X del recorte
        def center_to_px(v):
            cx = min(1.0, max(0.0, float(v))) * scaled_w
            return int(round(min(float(span), max(0.0, cx - tw / 2.0))))

        raw_keys = sorted((tr.get("keys") or [])[:MAX_EXPR_MOVES], key=lambda k: float(k.get("t", 0)))
        keys = [(float(k.get("t", 0)), center_to_px(k.get("x", 0.5)), str(k.get("tr") or "cut"),
                 min(3.0, max(0.02, float(k.get("d") or 0))) if k.get("d") else 0.0) for k in raw_keys]
        x0 = center_to_px(tr.get("x", 0.5))
        if keys and keys[0][0] <= 0.05:
            x0, keys = keys[0][1], keys[1:]
        if keys:
            moves, prev = [], x0
            for kt, kx, ktr, kd in keys:
                dur_m = kd if kd else (0.45 if ktr == "pan" else 0.02)
                moves.append((round(max(0.0, kt), 2), dur_m, kx - prev))
                prev = kx
            chain = pan(x_expr_ease(x0, moves, tw))
            note = f"Cámara manual con {len(keys)} cambio(s) de toma"
        else:
            chain = f"scale=-2:{th}:flags=lanczos,crop={tw}:{th}:{x0}:0,setsar=1"
            note = "Cámara manual fija"
    elif scaled_w - tw > 4 and tm == "ab":
        span = scaled_w - tw
        def ab_px(v):
            cx = min(1.0, max(0.0, float(v))) * scaled_w
            return float(round(min(float(span), max(0.0, cx - tw / 2.0)), 1))

        px_a = ab_px(tr.get("ax", 0.28))
        px_b = ab_px(tr.get("bx", 0.72))
        cut_mode = str(tr.get("ab_tr") or "cut")
        dur_m = 0.35 if cut_mode == "pan" else 0.12

        # Detectar cuál de las dos bocas (cerca de ax vs cerca de bx) se mueve más cuando alguien habla
        ax_norm = min(1.0, max(0.0, float(tr.get("ax", 0.28))))
        bx_norm = min(1.0, max(0.0, float(tr.get("bx", 0.72))))
        sp = sample_speakers(src, s, e, 0.125, cb) if cv2 is not None else []
        speech = o.get("_speech") or []

        def talking(t):
            return not speech or any(a - 0.25 <= t <= b + 0.25 for a, b in speech)

        # Asociar detecciones a Persona A o Persona B según proximidad horizontal
        prev_pts, rows = [], []
        for t, faces in sp:
            cur_a, cur_b = None, None
            for cx, cy, w, patch in faces:
                da, db = abs(cx - ax_norm), abs(cx - bx_norm)
                if min(da, db) > 0.28:
                    continue
                who = "A" if da <= db else "B"
                mot = None
                for p_who, p_cx, p_cy, p_patch, _pm in prev_pts:
                    if p_who == who and math.hypot(cx - p_cx, cy - p_cy) <= 0.18:
                        if patch is not None and p_patch is not None:
                            mot = float(np.abs(patch - p_patch).mean())
                        break
                if who == "A" and (cur_a is None or (mot or 0) > (cur_a[4] or 0)):
                    cur_a = ("A", cx, cy, patch, mot)
                elif who == "B" and (cur_b is None or (mot or 0) > (cur_b[4] or 0)):
                    cur_b = ("B", cx, cy, patch, mot)
            prev_pts = [q for q in (cur_a, cur_b) if q is not None]
            rows.append((t, cur_a[4] if cur_a else None, cur_b[4] if cur_b else None))

        allm = [m for _, ma, mb in rows for m in (ma, mb) if m is not None]
        if allm:
            floor = float(np.percentile(allm, 20))
            act_a = {i: max(0.0, ma - floor) for i, (_, ma, _) in enumerate(rows) if ma is not None}
            act_b = {i: max(0.0, mb - floor) for i, (_, _, mb) in enumerate(rows) if mb is not None}

            def sm(d, i):
                v = [d.get(j, 0.0) for j in range(i - 4, i + 5)]
                return sum(v) / len(v)

            state = "A" if sm(act_a, 0) >= sm(act_b, 0) else "B"
            x0 = px_a if state == "A" else px_b
            cur_px, hold_until, moves, switches = x0, -1.0, [], 0
            for i, (t, _, _) in enumerate(rows):
                sa, sb = sm(act_a, i), sm(act_b, i)
                want = "A" if sa > sb else "B"
                hi_s, lo_s = (sa, sb) if want == "A" else (sb, sa)
                if want != state and talking(t) and t >= hold_until and hi_s > 1.25 * lo_s and (hi_s - lo_s) > 0.025:
                    target_px = px_a if want == "A" else px_b
                    if abs(target_px - cur_px) > 4:
                        moves.append((round(t, 2), dur_m, round(target_px - cur_px, 1)))
                        cur_px = target_px
                        switches += 1
                    state = want
                    hold_until = t + 1.2
            if len(moves) > MAX_EXPR_MOVES:
                moves = moves[:MAX_EXPR_MOVES]
            rec_plan(x0, moves, [(t, a is not None or b is not None) for t, a, b in rows])
            chain = pan(x_expr_ease(x0, moves, tw)) if moves else f"scale=-2:{th}:flags=lanczos,crop={tw}:{th}:{int(x0)}:0,setsar=1"
            note = f"2 Posiciones Fijas (A/B): {switches} cambios automáticos al hablar"
        else:
            chain = f"scale=-2:{th}:flags=lanczos,crop={tw}:{th}:{int(px_a)}:0,setsar=1"
            note = "2 Posiciones Fijas (A/B): enfocando Persona A (sin movimiento de labios detectable)"
    elif scaled_w - tw > 4 and tm == "voice":
        plan = plan_camera(spk, scaled_w, tw, quick=True) if spk else None
        if plan:
            rec_plan(plan[0], plan[1])
            chain = pan(x_expr(*plan))
            note = f"Cámara por voz: {len(plan[1])} cambios de persona en este tramo"
        else:
            note = "Faltan detectar las voces o marcar dónde está cada persona; se usa el centro"
    elif scaled_w - tw > 4 and tm == "smart":
        smp = sample_faces_multi(src, s, e, 0.25, cb) if cv2 is not None else None
        plan = smart(smp, spk) if smp is not None else None
        if plan:
            x0, moves, inf = plan
            rec_plan(x0, moves, [(t, f) for t, f in smp])
            chain = pan(x_expr_ease(x0, moves, tw))
            note = (f"Cámara inteligente: {inf['moves']} movimientos suaves y {inf['cuts']} cortes; "
                    f"encuadra al grupo el {inf['group']} % del tiempo")
        else:
            vp = plan_camera(spk, scaled_w, tw, quick=True) if spk else None
            if vp:
                chain = pan(x_expr(*vp))
                note = f"No se vieron caras; se usó la cámara por voz ({len(vp[1])} cambios)"
            else:
                note = "No se detectó ninguna cara; se usa el centro"
    elif scaled_w - tw > 4 and tm == "speaker":
        sp = sample_speakers(src, s, e, 0.125, cb) if cv2 is not None else []
        res = speaker_series(sp, o.get("_speech") or []) if sp and any(f for _, f in sp) else None
        if res:
            ser, changes = res
            samples = [(t, [(f[0], f[2]) for f in faces]) for t, faces in sp][::2]
            plan = smart(samples, ser[::2])
            if plan:
                x0, moves, inf = plan
                rec_plan(x0, moves, [(t, f) for t, f in samples])
                chain = pan(x_expr_ease(x0, moves, tw))
                note = (f"Hablante automático (labios): {changes} cambios de hablante, {inf['moves']} movimientos "
                        f"y {inf['cuts']} cortes")
        if note == "":  # sin YuNet o sin labios medibles: cámara inteligente normal
            smp = sample_faces_multi(src, s, e, 0.25, cb) if cv2 is not None else None
            plan = smart(smp, spk) if smp is not None else None
            if plan:
                x0, moves, inf = plan
                rec_plan(x0, moves, [(t, f) for t, f in smp])
                chain = pan(x_expr_ease(x0, moves, tw))
                note = "No pude medir los labios en este clip; se usó la cámara inteligente"
            else:
                note = "No se detectó ninguna cara; se usa el centro"
    elif scaled_w - tw > 4 and tm == "auto":
        smp = sample_faces(src, s, e, 0.25, cb) if cv2 is not None else None
        plan = plan_camera(smp, scaled_w, tw) if smp is not None else None
        if plan:
            rec_plan(plan[0], plan[1], [(t, cx is not None) for t, cx in smp])
            chain = pan(x_expr(*plan))
            note = f"Cámara automática: sigue la cara ({len(plan[1])} movimientos)"
        else:
            note = "No se detectó ninguna cara; se usa el centro"
    return chain, note


def split_graph(o, W, H, src, s, e, info, cb=None):
    """Pantalla dividida: una persona arriba y otra abajo (automática o elegida manualmente en el fotograma)."""
    ph = even(H / 2)
    pb = H - ph
    if not info:
        return None, "Pantalla dividida: sin info de video; se usa el centro"
    sw = int(round(info[0] * ph / info[1] / 2) * 2)
    sw_b = int(round(info[0] * pb / info[1] / 2) * 2)
    if sw - W <= 4:
        return None, "Pantalla dividida: el video es muy angosto para dividirlo; se usa el centro"
    sp_cfg = o.get("split") or {}
    if sp_cfg.get("mode") == "manual":
        tx = min(1.0, max(0.0, float(sp_cfg.get("top_x", 0.28))))
        bx = min(1.0, max(0.0, float(sp_cfg.get("bot_x", 0.72))))
        x_top = int(round(min(float(sw - W), max(0.0, tx * sw - W / 2.0))))
        x_bot = int(round(min(float(sw_b - W), max(0.0, bx * sw_b - W / 2.0))))
        fc = (f"[0:v]split=2[a][c];[a]scale=-2:{ph}:flags=lanczos,crop={W}:{ph}:{x_top}:0[top];"
              f"[c]scale=-2:{pb}:flags=lanczos,crop={W}:{pb}:{x_bot}:0[bot];"
              f"[top][bot]vstack,drawbox=x=0:y={ph - 2}:w={W}:h=4:color=black:t=fill,setsar=1[b]")
        return fc, "Pantalla dividida manual (arriba y abajo elegidos)"
    if cv2 is None:
        return None, "Pantalla dividida: falta OpenCV; se usa el centro"
    res = split_people(sample_faces_multi(src, s, e, 0.25, cb))
    if not res:
        return None, "No encontré dos personas en este clip; usa el modo Manual en Pantalla dividida"
    A, B, c = res

    def xe(series, swv):
        plan = plan_camera(series, swv, W)
        return x_expr(*plan) if plan else f"{max(0, (swv - W) // 2)}"

    top, bot = (A, B) if sp_cfg.get("order", "lr") == "lr" else (B, A)
    fc = (f"[0:v]split=2[a][c];[a]scale=-2:{ph}:flags=lanczos,crop={W}:{ph}:x='{xe(top, sw)}':y=0[top];"
          f"[c]scale=-2:{pb}:flags=lanczos,crop={W}:{pb}:x='{xe(bot, sw_b)}':y=0[bot];"
          f"[top][bot]vstack,drawbox=x=0:y={ph - 2}:w={W}:h=4:color=black:t=fill,setsar=1[b]")
    return fc, "Pantalla dividida: una persona arriba y otra abajo"


def stream_graph(o, W, H):
    """Modo streamer: dos tomas del mismo video (la cámara del streamer y el gameplay / lo que mira) en una sola pantalla."""
    st = o.get("stream") or {}

    def rect(v, d):
        try:
            x, y, w, h = (float(q) for q in v)
        except Exception:  # noqa: BLE001
            x, y, w, h = d
        x, y = min(max(x, 0.0), 0.95), min(max(y, 0.0), 0.95)
        return x, y, min(max(w, 0.04), 1 - x), min(max(h, 0.04), 1 - y)

    cam, game = rect(st.get("cam"), (0.74, 0.62, 0.25, 0.36)), rect(st.get("game"), (0.0, 0.0, 1.0, 1.0))
    crop = lambda r: (f"crop=trunc(iw*{r[2]:.4f}/2)*2:trunc(ih*{r[3]:.4f}/2)*2:trunc(iw*{r[0]:.4f}/2)*2:trunc(ih*{r[1]:.4f}/2)*2")  # noqa: E731
    fit = st.get("fit") == "fit"

    def game_chain(src, w, h, out):  # el gameplay llenando (w x h), o completo sobre su propio fondo borroso
        if not fit:
            return f"[{src}]{crop(game)},scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}[{out}]"
        return (f"[{src}]{crop(game)},split[gq][gr];[gq]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:4[gbg];"
                f"[gr]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[gfg];[gbg][gfg]overlay=(W-w)/2:(H-h)/2[{out}]")

    if st.get("layout", "stack") == "pip":
        pw = even(W * min(70.0, max(15.0, float(st.get("pip", 38)))) / 100)
        m = int(W * 0.04)
        xy = {"tl": f"{m}:{m}", "tr": f"W-w-{m}:{m}", "bl": f"{m}:H-h-{m}", "br": f"W-w-{m}:H-h-{m}",
              "tc": f"(W-w)/2:{m}", "bc": f"(W-w)/2:H-h-{m}"}.get(st.get("pos", "tr"), f"W-w-{m}:{m}")
        return (f"[0:v]split=2[sa][sb];{game_chain('sb', W, H, 'gm')};[sa]{crop(cam)},scale={pw}:-2:flags=lanczos[cm];"
                f"[gm][cm]overlay={xy},setsar=1[b]")
    ch = even(H * min(70.0, max(20.0, float(st.get("camh", 38)))) / 100)
    gh = H - ch
    top, bot = ("cm", "gm") if st.get("camPos", "top") == "top" else ("gm", "cm")
    return (f"[0:v]split=2[sa][sb];[sa]{crop(cam)},scale={W}:{ch}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{ch}[cm];"
            f"{game_chain('sb', W, gh, 'gm')};[{top}][{bot}]vstack,setsar=1[b]")


def build_graph(o, W, H, src, s, e, ass_name, logo, info, cb=None, spk=None, out_scale=None):
    """Arma el filtro de ffmpeg. Devuelve (filtro, entradas_extra, nota)."""
    note = ""
    mode = o["mode"]
    if mode == "blur":  # completo con fondo borroso (el de siempre)
        fc = (f"[0:v]split[a][c];[a]scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},boxblur=25:5[bg];"
              f"[c]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[b]")
    elif mode == "streamer":
        fc, note = stream_graph(o, W, H), "Modo streamer"
    elif mode == "split":
        fc, note = split_graph(o, W, H, src, s, e, info, cb)
        if fc is None:
            fc = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[b]"
    else:
        tw, th, nocrop = layout_dims(o, W, H, info) if mode == "frame" else (W, H, False)
        chain, note = fg_chain(o, tw, th, nocrop, src, s, e, info, cb, spk)
        fc = frame_compose(chain, o, W, H, tw, th) if mode == "frame" else f"[0:v]{chain}[b]"
    cur, extra = "b", []
    m = int(W * 0.04)
    for k, (path, lg) in enumerate(logo or []):
        if lg.get("kind") == "title":  # título editable: PNG hecho a 1080 px de ancho, se escala al lienzo
            yy = min(100.0, max(0.0, float(lg.get("y", 62))))
            xx = min(100.0, max(0.0, float(lg.get("x", 50))))
            fc += (f";[{k + 1}:v]scale=iw*{W / 1080:.4f}:-1,format=rgba[lg{k}];"
                   f"[{cur}][lg{k}]overlay=max(0\\,min(W-w\\,W*{xx:.1f}/100-w/2)):max(0\\,min(H-h\\,H*{yy:.1f}/100-h/2))[o{k}]")
            cur = f"o{k}"
            extra += ["-i", str(path.resolve())]
            continue
        if lg.get("pos") == "custom":  # arrastrado con el ratón: centro del logo en % del lienzo
            gx = min(100.0, max(0.0, float(lg.get("x", 85))))
            gy = min(100.0, max(0.0, float(lg.get("y", 10))))
            xy = f"max(0\\,min(W-w\\,W*{gx:.1f}/100-w/2)):max(0\\,min(H-h\\,H*{gy:.1f}/100-h/2))"
        else:
            xy = {"tl": f"{m}:{m}", "tr": f"W-w-{m}:{m}", "bl": f"{m}:H-h-{m}", "br": f"W-w-{m}:H-h-{m}",
                  "c": "(W-w)/2:(H-h)/2", "bc": "(W-w)/2:H*0.70"}[lg["pos"]]
        nxt = f"o{k}"
        fc += (f";[{k + 1}:v]scale={int(W * float(lg['scale']) / 100)}:-1,format=rgba,colorchannelmixer=aa={float(lg['opacity'])}[lg{k}];"
               f"[{cur}][lg{k}]overlay={xy}[{nxt}]")
        cur = nxt
        extra += ["-i", str(path.resolve())]
    if out_scale:  # vista previa: reduce primero y deja que los subtítulos se dibujen ya a ese tamaño (más nítidos)
        fc += f";[{cur}]scale={out_scale[0]}:{out_scale[1]}:flags=lanczos[sc];[sc]ass={ass_name}[v]"
    else:
        fc += f";[{cur}]ass={ass_name}[v]"
    return fc, extra, note


def render(job, items, o):
    try:
        _render(job, items, o)
    except Cancelled:
        job["state"], job["msg"] = "cancelled", "Cancelado."
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        job["state"], job["msg"] = "error", str(e)[-400:]


def _render(job, items, o):
    W, H = canvas_size(o)
    d = JOBS / job["id"]
    logo = find_overlays(o)
    n = len(items)
    for k, v in (("caps", {}), ("notes", {}), ("meta", {}), ("seq", 0), ("out", [])):
        job.setdefault(k, v)
    low = []
    now = time.time()
    job.update(names=[f"Clip {i + 1} de {n}" for i in range(n)], step=-1, pct=None, t_start=now, t_step=now,
               prog_t=now, cancel=False, enc="")
    for i, c in enumerate(items):
        s, e = float(c["start"]), float(c["end"])
        dur = e - s
        base = c.get("opt") or o  # ajustes propios del clip, si los tiene
        oi, cam = base, (c.get("cam") or "")
        if cam in ("smart", "auto", "voice", "speaker", "center", "blur", "split"):
            oi = {**base, "track": dict(base.get("track") or {})}
            if cam == "blur":
                oi["mode"] = "blur"
            elif cam == "split":
                oi["mode"] = "split"
            else:
                oi["mode"] = "frame" if base.get("mode") == "frame" else "crop"
                oi["track"]["mode"] = cam
        W, H = canvas_size(oi)
        logo = find_overlays(oi)
        tri = oi.get("track") or {}
        q_cfg = oi.get("quality") or {}
        src_q = int(q_cfg.get("src", 1080) or 1080)
        sharp_on = bool(q_cfg.get("sharp", False))
        ultra_enc = bool(q_cfg.get("ultra", False)) or src_q > 1080
        auto = oi["mode"] == "split" or (oi["mode"] in ("crop", "frame") and tri.get("mode") in ("auto", "smart", "speaker", "ab"))
        lo = 45 if auto else 35
        setp(job, i, None, f"Clip {i + 1} de {n}: descargando el tramo en calidad {src_q}p...", i / n)
        rs, off, hi = max(0.0, s - 1.0), job.get("offset", 0.0), f"hi{i}"
        name, ls = "source", s
        try:
            if job.get("live"):
                raise LiveSkip()  # directo: los clips salen de la grabación
            if job.get("local"):
                local_cut(job, job["local"], off + rs, off + e + 1.0, d / f"{hi}.mp4",
                          lambda f, m, i=i: setp(job, i, 25 + int(10 * f), f"Clip {i + 1} de {n}: {m}", (i + 0.25 + 0.10 * f) / n),
                          max_h=src_q)
            else:
                download(job["url"], off + rs, off + e + 1.0, str(d / hi), None, hi_fmt_for(src_q))
            if job.get("cancel"):
                raise Cancelled()
            normalize(d, job, hi, lambda f, m, i=i: setp(job, i, 25 + int(10 * (f or 0)), f"Clip {i + 1} de {n}: {m}",
                                                         (i + 0.25 + 0.10 * (f or 0)) / n))
            shift = measure_shift(d / "source.mp4", d / f"{hi}.mp4", rs)
            if abs(shift) >= 0.04:
                print(f">> Clip {i + 1}: corrijo {shift:+.2f} s de desfase entre el video en alta calidad y la transcripción", flush=True)
            name, ls = hi, max(0.0, s - rs - shift)
            if abs(shift) >= 0.15:
                job.setdefault("shifts", {})[i] = shift
        except Cancelled:
            raise
        except LiveSkip:
            pass
        except Exception as ex:  # noqa: BLE001
            print(">> No se pudo bajar en alta calidad; uso la versión liviana:", ex, flush=True)
            low.append(i + 1)
        srcf = d / f"{name}.mp4"
        info = video_info(srcf)
        (d / f"c{i}.ass").write_text(build_ass(job["words"], s, e, W, H, oi["sub"], oi["text"]), encoding="utf-8")

        def face_cb(f, i=i):
            setp(job, i, 35 + int(10 * f), f"Clip {i + 1} de {n}: buscando la cara del hablante... {int(f * 100)}%",
                 (i + 0.35 + 0.10 * f) / n)

        oi = {**oi, "_speech": speech_rel(job["words"], s, e)}
        spk = voice_samples(job, s, e, tri.get("speakers") or []) if oi["mode"] in ("crop", "frame") and tri.get("mode") in ("voice", "smart") else None
        fc, extra, note = build_graph(oi, W, H, srcf, ls, ls + dur, f"c{i}.ass", logo, info, face_cb if auto else None, spk)
        if sharp_on:
            fc = fc.replace(f"ass=c{i}.ass[v]", f"unsharp=5:5:0.75:3:3:0.35,ass=c{i}.ass[v]")
        cmd_in = ["-ss", f"{ls:.2f}", "-t", f"{dur:.2f}", "-i", srcf.name, *extra]
        slug = re.sub(r"[^\w-]+", "_", c["title"])[:40]
        job["seq"] += 1
        seq = job["seq"]
        outname = f"{slug}_{job['id']}_{seq}.mp4"
        out = CLIPS / outname
        ok = False
        cq_v = "15" if ultra_enc else "19"
        crf_v = "15" if ultra_enc else "18"
        for enc, vc in (("GPU (nvenc)", ["-c:v", "h264_nvenc", "-preset", "p7" if ultra_enc else "p6", "-rc", "vbr", "-cq", cq_v, "-b:v", "0", "-profile:v", "high"]),
                        ("CPU (x264)", ["-c:v", "libx264", "-preset", "medium" if ultra_enc else "fast", "-crf", crf_v])):
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", *cmd_in,
                   "-filter_complex", fc, "-map", "[v]", "-map", "0:a?", "-t", f"{dur:.2f}", *vc, "-c:a", "aac",
                   "-b:a", "192k", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
            with open(d / "ff_err.txt", "w", encoding="utf-8") as err:
                pr = subprocess.Popen(cmd, cwd=d, stdout=subprocess.PIPE, stderr=err, text=True)
                job["proc"] = pr
                try:
                    for line in pr.stdout:
                        if line.startswith(("out_time_us=", "out_time_ms=")):
                            try:
                                f = min(1.0, max(0.0, int(line.split("=")[1]) / 1e6 / dur))
                            except ValueError:
                                continue
                            part = (lo + (100 - lo) * f) / 100
                            setp(job, i, int(part * 100), f"Clip {i + 1} de {n}: codificando con {enc}... {int(f * 100)}%", (i + part) / n)
                except Cancelled:
                    pr.kill()
                    raise
                pr.wait()
            if job.get("cancel"):
                raise Cancelled()
            if pr.returncode == 0:
                job["enc"], ok = enc, True
                break
        if not ok:
            job["state"], job["msg"] = "error", (d / "ff_err.txt").read_text(encoding="utf-8", errors="ignore")[-500:]
            return
        (d / f"{hi}.mp4").unlink(missing_ok=True)
        job["out"].append(outname)
        cap = (c.get("caption") or "").strip()
        job["caps"][outname] = cap
        
        mode_val = oi.get("mode")
        track_mode = (oi.get("track") or {}).get("mode")
        if mode_val == "blur":
            note_val = "Completo con fondo borroso"
        elif mode_val == "frame" and not note:
            note_val = "Marco con fondo"
        elif track_mode == "center":
            note_val = "Centrado fijo"
        else:
            note_val = note or ""
        if abs(job.get("shifts", {}).get(i, 0.0)) >= 0.15:
            note_val = (note_val + " · " if note_val else "") + f"audio realineado {job['shifts'][i]:+.1f} s"
        job["notes"][outname] = note_val

        job["meta"][outname] = {"title": c["title"], "caption": cap, "n": seq}
    where = ""
    warn = (f" ⚠ Los clips {low} salieron en calidad baja: YouTube no dejó bajar el tramo en alta calidad "
            "(prueba elegir tu navegador en 'Cookies de YouTube').") if low else ""
    job.update(state="done", msg=f"Listo: {n} clips ({job.get('enc', '')}).{warn}{where}", step=n, pct=100, eta=None)


# ---------- rutas ----------
def public(j):
    now = time.time()
    d = {k: j.get(k) for k in ("id", "state", "msg", "clips", "out", "dur", "names", "step", "pct", "overall", "eta", "enc", "caps", "notes", "times")}
    d["live"] = live_public(j)
    d.update(el=round(now - j.get("t_start", now)), stall=round(now - j.get("prog_t", now)), stepel=round(now - j.get("t_step", now)))
    return d


@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.post("/api/analyze")
def api_analyze():
    p = request.get_json(force=True)
    p["local"] = clean_path(p.get("local"))
    if p["local"]:
        if not Path(p["local"]).is_file():
            return jsonify(error="No encuentro ese archivo. Pulsa «Elegir video…» o revisa la ruta."), 400
    elif not (p.get("url") or "").strip():
        return jsonify(error="Falta el link"), 400
    p.update(n=int(p.get("n", 5)), min=int(p.get("min", 20)), max=int(p.get("max", 60)),
             flex=bool(p.get("flex", True)), tags=bool(p.get("tags", True)))
    COOKIES["browser"] = (p.get("browser") or "").strip().lower()
    COOKIES["failed"] = ""
    j = {"id": uuid.uuid4().hex[:8], "state": "working", "msg": "Iniciando...", "clips": [], "out": []}
    jobs[j["id"]] = j
    threading.Thread(target=analyze, args=(j, p), daemon=True).start()
    return jsonify(public(j))


@app.post("/api/pick_video")
def api_pick_video():
    """Abre la ventana de Windows para elegir un video de tu computadora (el programa corre en tu PC)."""
    try:
        r = subprocess.run([sys.executable, "-c", _PICK_SCRIPT], capture_output=True, encoding="utf-8", errors="ignore",
                           timeout=900, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except Exception as ex:  # noqa: BLE001
        return jsonify(error="No pude abrir la ventana para elegir el archivo: " + str(ex)[-150:] + ". Pega la ruta a mano."), 500
    path = (r.stdout or "").strip().splitlines()[-1].strip() if (r.stdout or "").strip() else ""
    if not path:
        return jsonify(path="", cancelled=True)
    return jsonify(path=os.path.normpath(path))


@app.post("/api/render")
def api_render():
    p = request.get_json(force=True)
    j = jobs[p["job"]]
    j.update(state="rendering", cancel=False, enc="")
    threading.Thread(target=render, args=(j, p["clips"], p["opt"]), daemon=True).start()
    return jsonify(public(j))


@app.get("/api/job/<jid>")
def api_job(jid):
    if jid not in jobs:
        return jsonify(state="error", msg="El programa se reinició y perdió este trabajo. Pulsa Analizar otra vez (usa lo ya guardado y será rápido).")
    return jsonify(public(jobs[jid]))


@app.post("/api/cancel/<jid>")
def api_cancel(jid):
    j = jobs.get(jid)
    if j:
        j["cancel"] = True
        pr = j.get("proc")
        if pr and pr.poll() is None:
            pr.terminate()
    return jsonify(ok=True)


@app.get("/api/health")
def api_health():
    return jsonify(state=ENGINE["state"], dev=ENGINE["dev"], note=ENGINE["note"], el=round(time.time() - ENGINE["t0"]))


def list_logos():
    return sorted((f for f in LOGOS.iterdir() if f.suffix.lower() in LOGO_EXT), key=lambda f: f.stat().st_mtime)


@app.get("/api/logos")
def api_logos():
    return jsonify([f.name for f in list_logos()])


@app.post("/api/logo")
def api_logo():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="No llegó ningún archivo"), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in LOGO_EXT:
        return jsonify(error="Usa un logo PNG, JPG o WEBP"), 400
    if len(list_logos()) >= MAX_LOGOS:
        return jsonify(error=f"Ya tienes {MAX_LOGOS} logos guardados. Borra uno para subir otro."), 400
    name = f"{uuid.uuid4().hex[:6]}_{secure_filename(Path(f.filename).stem)[:30] or 'logo'}{ext}"
    f.save(LOGOS / name)
    return jsonify(ok=True, name=name)


@app.delete("/api/logo/<name>")
def api_logo_delete(name):
    (LOGOS / secure_filename(name)).unlink(missing_ok=True)
    return jsonify(ok=True)


@app.get("/logos/<path:name>")
def get_logo(name):
    return send_from_directory(LOGOS, name)


@app.post("/api/sub_phrases")
def api_sub_phrases():
    """Devuelve las frases de subtítulos del clip actual para poder editarlas."""
    p = request.get_json(force=True)
    j = jobs.get(p.get("job"))
    if not j or "words" not in j:
        return jsonify(error="Primero analiza un video."), 400
    s, e = float(p.get("start", 0)), float(p.get("end", 0))
    return jsonify(phrases=group_sub_phrases(j["words"], s, e))


@app.post("/api/sub_save")
def api_sub_save():
    """Guarda las frases editadas del clip actual reasignando los tiempos por palabra en job['words'] y en el caché."""
    p = request.get_json(force=True)
    j = jobs.get(p.get("job"))
    if not j or "words" not in j:
        return jsonify(error="Primero analiza un video."), 400
    s, e = float(p.get("start", 0)), float(p.get("end", 0))
    phrases = p.get("phrases") or []
    new_clip_words = []
    orig = [w for w in j["words"] if w[0] >= s - 0.05 and w[1] <= e + 0.3]
    if orig:  # conserva el tiempo real de cada palabra que no cambió
        toks_all = [w for ph in phrases for w in str(ph.get("text") or "").strip().split() if w]
        new_clip_words = retime_words(orig, toks_all)
    else:
        for ph in phrases:
            ps, pe = float(ph.get("start", s)), float(ph.get("end", e))
            toks = [w for w in str(ph.get("text") or "").strip().split() if w]
            if not toks:
                continue
            step = max(0.05, pe - ps) / len(toks)
            for idx, tok in enumerate(toks):
                new_clip_words.append((round(ps + idx * step, 3), round(ps + (idx + 1) * step, 3), tok))
    before = [w for w in j["words"] if not (w[0] >= s - 0.05 and w[1] <= e + 0.3)]
    merged = sorted(before + new_clip_words, key=lambda x: x[0])
    j["words"] = merged
    tr_path = j.get("cache_tr")
    if tr_path and Path(tr_path).exists():
        try:
            Path(tr_path).write_text(json.dumps({"words": j["words"], "lines": j.get("lines", [])}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return jsonify(ok=True, phrases=group_sub_phrases(j["words"], s, e))


@app.post("/api/preview")
def api_preview():
    """Un solo fotograma con exactamente el mismo filtro del render (encuadre, subtítulos reales, logo, texto)."""
    p = request.get_json(force=True)
    j = jobs.get(p.get("job"))
    if not j or "words" not in j:
        return jsonify(error="Primero analiza un video para ver la vista previa."), 400
    try:
        o = p["opt"]
        W, H = canvas_size(o)
        d = JOBS / j["id"]
        t = max(0.0, float(p.get("t", 0)))
        nxt = next((w for w in j["words"] if w[0] >= t), None)
        if nxt and nxt[0] - t < 10:  # muestra los subtítulos reales de la primera frase desde ese momento
            s = max(0.0, nxt[0] - 1.0)
            rel = nxt[0] + 0.15 - s
        else:
            s, rel = t, 0.1
        e = s + 6
        tag = uuid.uuid4().hex[:6]
        ass, out = f"pv_{tag}.ass", d / f"pv_{tag}.jpg"
        (d / ass).write_text(build_ass(j["words"], s, e, W, H, o["sub"], o["text"]), encoding="utf-8")
        tr = o.get("track") or {}
        o = {**o, "_speech": speech_rel(j["words"], s, e)}
        spk = voice_samples(j, s, e, tr.get("speakers") or []) if o["mode"] in ("crop", "frame") and tr.get("mode") in ("voice", "smart") else None
        esrc = edit_src(d)
        fc, extra, note = build_graph(o, W, H, esrc, s, e, ass, find_overlays(o), video_info(esrc), None, spk)
        r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{s:.2f}", "-t", "6",
                            "-i", esrc.name, *extra, "-filter_complex", fc, "-map", "[v]",
                            "-ss", f"{rel:.2f}", "-frames:v", "1", "-q:v", "3", out.name],
                           cwd=d, capture_output=True, text=True)
        (d / ass).unlink(missing_ok=True)
        if r.returncode or not out.exists():
            return jsonify(error="ffmpeg: " + r.stderr[-300:]), 500
        data = out.read_bytes()
        out.unlink(missing_ok=True)
        return Response(data, mimetype="image/jpeg", headers={"X-Note": urllib.parse.quote(note)})
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)[-300:]), 500


@app.post("/api/preview_video")
def api_preview_video():
    """Previsualización rápida en video del clip completo con cámara, subtítulos, logo y texto."""
    p = request.get_json(force=True)
    j = jobs.get(p.get("job"))
    if not j or "words" not in j:
        return jsonify(error="Primero analiza un video para ver la vista previa."), 400
    try:
        o = p["opt"]
        W, H = canvas_size(o)
        d = JOBS / j["id"]
        s = max(0.0, float(p.get("start", 0)))
        e = max(s + 1.0, float(p.get("end", s + 10)))
        dur = e - s

        tag = uuid.uuid4().hex[:6]
        ass, out = f"pvv_{tag}.ass", d / f"pvv_{tag}.mp4"
        (d / ass).write_text(build_ass(j["words"], s, e, W, H, o["sub"], o["text"]), encoding="utf-8")
        tr = o.get("track") or {}
        o = {**o, "_speech": speech_rel(j["words"], s, e)}
        spk = voice_samples(j, s, e, tr.get("speakers") or []) if o["mode"] in ("crop", "frame") and tr.get("mode") in ("voice", "smart") else None
        esrc = edit_src(d)
        fc, extra, note = build_graph(o, W, H, esrc, s, e, ass, find_overlays(o), video_info(esrc), None, spk,
                                      out_scale=(even(W / 2), even(H / 2)))

        cmd_in = ["-ss", f"{s:.2f}", "-t", f"{dur:.2f}", "-i", esrc.name, *extra]
        ok, last_err = False, ""
        for vc in (["-c:v", "h264_nvenc", "-preset", "p3", "-cq", "24", "-b:v", "0"],
                   ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]):
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *cmd_in,
                   "-filter_complex", fc, "-map", "[v]", "-map", "0:a?", "-t", f"{dur:.2f}",
                   *vc, "-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out.name]
            r = subprocess.run(cmd, cwd=d, capture_output=True, text=True)
            if r.returncode == 0 and out.exists():
                ok = True
                break
            last_err = r.stderr
        (d / ass).unlink(missing_ok=True)
        if not ok:
            return jsonify(error="ffmpeg: " + last_err[-300:]), 500
        data = out.read_bytes()
        out.unlink(missing_ok=True)
        return Response(data, mimetype="video/mp4", headers={"X-Note": urllib.parse.quote(note)})
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)[-300:]), 500


def keys_from_plan(x0, moves, tw, scaled_w):
    """Convierte un plan automático (px de recorte) en tomas editables: x en 0..1 (centro del encuadre)."""
    def norm(px):
        return round(min(1.0, max(0.0, (px + tw / 2.0) / float(scaled_w))), 3)
    keys, cur = [], float(x0)
    for t0, dur, d in sorted(moves, key=lambda m: m[0]):
        cur += d
        keys.append({"t": round(float(t0), 2), "x": norm(cur), "tr": "pan" if dur >= 0.3 else "cut", "d": round(float(dur), 2)})
    return norm(x0), keys


def doubtful_ranges(samples, min_gap=0.75):
    """Tramos (inicio, fin) donde no se vio ninguna cara durante más de min_gap segundos."""
    out, a, last = [], None, None
    for t, has in samples:
        if not has and a is None:
            a = t
        if has and a is not None:
            if t - a >= min_gap:
                out.append([round(a, 2), round(t, 2)])
            a = None
        last = t
    if a is not None and last is not None and last - a >= min_gap:
        out.append([round(a, 2), round(last, 2)])
    return out


@app.post("/api/cam_plan")
def api_cam_plan():
    """Calcula la cámara automática de un clip y la devuelve como tomas editables (para corregirla a mano)."""
    p = request.get_json(force=True)
    j = jobs.get(p.get("job"))
    if not j or "words" not in j:
        return jsonify(error="Primero analiza un video."), 400
    if cv2 is None:
        return jsonify(error="OpenCV no está disponible: no puedo detectar caras."), 400
    try:
        o = p["opt"]
        if o.get("mode") not in ("crop", "frame"):
            return jsonify(error="Este formato no usa seguimiento de cámara."), 400
        W, H = canvas_size(o)
        d = JOBS / j["id"]
        s = max(0.0, float(p.get("start", 0)))
        e = max(s + 1.0, float(p.get("end", s + 10)))
        esrc = edit_src(d)
        info = video_info(esrc)
        if not info:
            return jsonify(error="No pude leer el video."), 500
        tw, th, nocrop = layout_dims(o, W, H, info) if o["mode"] == "frame" else (W, H, False)
        if nocrop:
            return jsonify(error="Este encuadre muestra el video completo: no hay cámara que mover."), 400
        tr = {**(o.get("track") or {}), "mode": "speaker"}
        rec = {}
        o = {**o, "track": tr, "_speech": speech_rel(j["words"], s, e), "_plan_out": rec}
        _, note = fg_chain(o, tw, th, nocrop, esrc, s, e, info, None, None)
        if rec.get("x0") is None:
            return jsonify(error="No se detectó ninguna cara en este clip; no hay cámara automática que traer."), 200
        x0, keys = keys_from_plan(rec["x0"], rec["moves"], rec["tw"], rec["scaled_w"])
        return jsonify(x0=x0, keys=keys, doubt=doubtful_ranges(rec.get("samples") or []), note=note)
    except Exception as ex:  # noqa: BLE001
        return jsonify(error=str(ex)[-300:]), 500


@app.post("/api/speakers")
def api_speakers():
    """Detecta cuántas voces hay y en qué momentos habla cada una."""
    p = request.get_json(force=True)
    j = jobs.get(p.get("job"))
    if not j or "words" not in j:
        return jsonify(error="Primero analiza un video."), 400
    st = j.get("spk_job")
    if st and st["state"] == "working":
        return jsonify(state="working", msg=st["msg"])
    rng = (float(p["start"]), float(p["end"])) if p.get("start") is not None and p.get("end") is not None else None
    run_diarize(j, int(p.get("k", 0)), rng)
    return jsonify(state="working", msg="Preparando...")


@app.get("/api/speakers_status")
def api_speakers_status():
    j = jobs.get(request.args.get("job"))
    st = (j or {}).get("spk_job")
    if not st:
        return jsonify(state="error", msg="No hay una detección de voces en curso.")
    return jsonify(state=st["state"], msg=st["msg"], result=st["res"] if st["state"] == "done" else None)


@app.post("/api/save")
def api_save():
    """Copia a la carpeta del proyecto los clips que el usuario eligió."""
    p = request.get_json(force=True)
    j = jobs.get(p.get("job"))
    if not j:
        return jsonify(error="No encuentro este trabajo (¿se reinició el programa?)."), 400
    base = (p.get("outdir") or "").strip()
    if not base:
        return jsonify(error="Escribe primero la carpeta donde guardar."), 400
    j["save_err"] = None
    j["cap_empty"] = 0
    j.pop("folder_path", None)
    j.pop("cap_head", None)
    saved, missing, done = 0, 0, j.setdefault("saved_to", {})
    for name in p.get("files") or []:
        m = (j.get("meta") or {}).get(name)
        # el nombre ya está validado porque tiene que existir en meta (así no se pierden los clips con tildes o ñ)
        src = CLIPS / name if m and Path(name).name == name else None
        if not m or src is None or done.get((name, base)):
            continue
        if not src.is_file():
            missing += 1
            continue
        before = j.get("saved", 0)
        save_clip(j, {"outdir": base}, src, m["n"] - 1, {"title": m["title"], "caption": m["caption"]})
        if j.get("saved", 0) > before:
            saved += 1
            done[(name, base)] = True
    if j.get("save_err"):
        return jsonify(error=f"No pude guardar en esa carpeta: {j['save_err']}"), 500
    if missing and not saved:
        return jsonify(error=f"No encuentro {missing} clip(s) en la carpeta data\\clips (¿se borraron?). Genera el clip otra vez."), 400
    return jsonify(saved=saved, nocap=j.get("cap_empty", 0), path=str(j.get("folder_path") or ""))


@app.post("/api/outdir")
def api_outdir():
    """Comprueba que se puede escribir en la carpeta elegida."""
    base = (request.get_json(force=True).get("base") or "").strip()
    if not base:
        return jsonify(error="Escribe la ruta de la carpeta donde quieres guardar los clips."), 400
    try:
        b = Path(base).expanduser()
        if not b.is_absolute():
            return jsonify(error="Escribe la ruta completa (por ejemplo C:\\Users\\tu_usuario\\iCloudDrive\\Clipear)."), 400
        b.mkdir(parents=True, exist_ok=True)
        t = b / f"_prueba_clipear_{uuid.uuid4().hex[:6]}.tmp"
        t.write_text("ok", encoding="utf-8")
        t.unlink()
        return jsonify(ok=True, path=str(b))
    except Exception as e:  # noqa: BLE001
        return jsonify(error="No pude escribir en esa carpeta: " + str(e)[-200:]), 400


@app.get("/api/audio_clip")
def api_audio_clip():
    """Extrae un pequeño WAV de 3.5 s alrededor del segundo t para escuchar la voz sin depender del reproductor de video."""
    j = jobs.get(request.args.get("job"))
    if not j:
        return jsonify(error="Trabajo no encontrado"), 404
    t = max(0.0, float(request.args.get("t", 0)))
    d = JOBS / j["id"]
    if j.get("live") and (d / "audio.raw").exists():
        import io, wave
        arr = live_audio_slice(d, max(0.0, t - 0.5), t + 3.0)
        pcm = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(pcm)
        return Response(buf.getvalue(), mimetype="audio/wav")
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t - 0.5):.2f}", "-t", "3.5",
                        "-i", edit_src(d).name, "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
                       cwd=d, capture_output=True)
    if r.returncode or not r.stdout:
        return jsonify(error="No pude extraer el audio"), 500
    return Response(r.stdout, mimetype="audio/wav")


@app.get("/api/frame")
def api_frame():
    """Un fotograma completo del video (sin recortar) para marcar las voces."""
    j = jobs.get(request.args.get("job"))
    if not j:
        return jsonify(error="Trabajo no encontrado"), 404
    t = max(0.0, float(request.args.get("t", 0)))
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", edit_src(JOBS / j["id"]).name, "-frames:v", "1",
                        "-vf", "scale=960:-2", "-q:v", "3", "-f", "mjpeg", "pipe:1"],
                       cwd=JOBS / j["id"], capture_output=True)
    if r.returncode or not r.stdout:
        return jsonify(error="No pude sacar el fotograma"), 500
    return Response(r.stdout, mimetype="image/jpeg")


@app.get("/source/<jid>")
def source(jid):
    return send_from_directory(JOBS / jid, edit_src(JOBS / jid).name)


# ---------- publicar en Facebook e Instagram (API oficial de Meta, con tu propia app) ----------
SOCIAL_FILE = DATA / "social.json"
GRAPH_VER = "v26.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VER}"
_pages_tmp, _pubs, _conn = {}, {}, {}


def social_load():
    """Cuentas conectadas: {"accounts": [{page_id, page_name, page_token, ig_id, ig_username, missing}]}. Migra el formato viejo (una sola Página)."""
    try:
        s = json.loads(SOCIAL_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"accounts": []}
    if "accounts" not in s:
        s = {"accounts": [s] if s.get("page_token") else []}
    return s


def social_save(s):
    SOCIAL_FILE.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


def _gx(r):
    """Respuesta de Meta como dict; si trae error, lanza un mensaje entendible."""
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        j = {}
    if r.status_code >= 400 or (isinstance(j, dict) and "error" in j):
        e = (j.get("error") or {}) if isinstance(j, dict) else {}
        code = f" (código {e.get('code')}" + (f"/{e.get('error_subcode')}" if e.get("error_subcode") else "") + ")" if e.get("code") else ""
        raise RuntimeError(f"Meta dice: {e.get('message') or r.text[:250]}{code}")
    return j


def _rupload(url, token, path):
    size = path.stat().st_size
    with open(path, "rb") as f:
        r = requests.post(url, headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
                                        "Content-Type": "application/octet-stream"}, data=f, timeout=1800)
    return _gx(r)


def _pub_facebook(path, caption, soc, say):
    tok, pid = soc["page_token"], soc["page_id"]
    info = video_info(path)
    if info and not (3 <= info[3] <= 90):
        raise RuntimeError(f"Los Reels de Facebook deben durar entre 3 y 90 s (este dura {info[3]:.0f} s).")
    say("Facebook: preparando la subida...")
    j = _gx(requests.post(f"{GRAPH}/{pid}/video_reels", data={"upload_phase": "start", "access_token": tok}, timeout=60))
    vid, up = j["video_id"], j["upload_url"]
    say("Facebook: subiendo el video...")
    _rupload(up, tok, path)
    say("Facebook: publicando...")
    _gx(requests.post(f"{GRAPH}/{pid}/video_reels", data={"access_token": tok, "video_id": vid, "upload_phase": "finish",
                                                         "video_state": "PUBLISHED", "description": caption}, timeout=60))
    for _ in range(36):  # hasta ~3 min esperando que Meta lo procese
        st = _gx(requests.get(f"{GRAPH}/{vid}", params={"fields": "status", "access_token": tok}, timeout=30)).get("status", {})
        ph, pr = st.get("publishing_phase", {}) or {}, st.get("processing_phase", {}) or {}
        if st.get("video_status") == "error" or ph.get("status") == "error" or pr.get("status") == "error":
            raise RuntimeError("Facebook no aceptó el video: " + json.dumps(st, ensure_ascii=False)[:250])
        if ph.get("status") in ("complete", "completed") or ph.get("publish_status") == "published":
            break
        say("Facebook: Meta está procesando el video...")
        time.sleep(5)
    return f"https://www.facebook.com/reel/{vid}"


def _ig_compat(path, say, light=False):
    """Copia del clip para Instagram: H.264 cerrado, 30 fps, AAC 48 kHz estéreo y con tope de bitrate (si no, puede salir muy pesada)."""
    out = path.with_name(path.stem + ("_ig2" if light else "_ig") + "_tmp.mp4")
    say("Instagram: preparando una versión más liviana del video..." if light else "Instagram: preparando una copia compatible del video...")
    rate = ["-crf", "26", "-maxrate", "2500k", "-bufsize", "5000k", "-vf", "scale='min(720,iw)':-2"] if light else ["-crf", "23", "-maxrate", "5000k", "-bufsize", "10000k"]
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast", *rate,
                        "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", "30", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
                        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not out.is_file():
        raise RuntimeError("No pude preparar la copia para Instagram: " + r.stderr[-200:])
    return out


def _ig_attempt(path, caption, soc, say, light):
    tok, ig = soc["page_token"], soc["ig_id"]
    j = _gx(requests.post(f"{GRAPH}/{ig}/media", data={"media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
                                                        "access_token": tok}, timeout=60))
    cid = j["id"]
    tmp = _ig_compat(path, say, light)
    mb = tmp.stat().st_size / 1e6
    upload_err = None
    try:
        say("Instagram: subiendo el video...")
        try:
            _rupload(j.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VER}/{cid}", tok, tmp)
        except RuntimeError as ex:  # a veces Meta responde error pero igual recibió el video: se confirma abajo
            upload_err = str(ex)
    finally:
        tmp.unlink(missing_ok=True)
    deadline = time.time() + (100 if upload_err else 600)
    pre = f"[{'liviana' if light else 'normal'}, {mb:.0f} MB] " + (f"La subida respondió: {upload_err} | " if upload_err else "")
    while True:
        say("Instagram: confirmando que Meta recibió el video..." if upload_err else "Instagram: Meta está procesando el video...")
        st = _gx(requests.get(f"{GRAPH}/{cid}", params={"fields": "status_code,status", "access_token": tok}, timeout=30))
        sc = st.get("status_code")
        if sc == "FINISHED":
            return cid
        if sc in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"{pre}Instagram no aceptó el video ({sc}): {st.get('status', '')}")
        if time.time() > deadline:
            raise RuntimeError(pre + "Instagram no terminó de procesar el video.")
        time.sleep(8)


def _pub_instagram(path, caption, soc, say):
    tok, ig = soc["page_token"], soc.get("ig_id")
    if not ig:
        raise RuntimeError("Esta Página no tiene una cuenta de Instagram conectada.")
    errs, cid = [], None
    for light in (False, True):
        try:
            cid = _ig_attempt(path, caption, soc, say, light)
            break
        except RuntimeError as ex:
            errs.append(str(ex))
            if not any(k in str(ex) for k in ("ProcessingFailed", "no terminó", "no aceptó")):
                raise  # error de permisos u otro: reintentar no sirve
            if not light:
                say("Instagram: no lo aceptó; reintentando con una versión más liviana...")
    if cid is None:
        info = video_info(path)
        det = f" (clip original: {path.stat().st_size / 1e6:.0f} MB" + (f", {info[0]}x{info[1]}, {info[3]:.0f} s" if info else "") + ")"
        raise RuntimeError("Instagram rechazó el video dos veces" + det + " | " + " || ".join(errs))
    say("Instagram: publicando...")
    pub = _gx(requests.post(f"{GRAPH}/{ig}/media_publish", data={"creation_id": cid, "access_token": tok}, timeout=60))
    try:
        return _gx(requests.get(f"{GRAPH}/{pub['id']}", params={"fields": "permalink", "access_token": tok}, timeout=30)).get("permalink") or ""
    except Exception:  # noqa: BLE001
        return ""


@app.get("/api/social/status")
def api_social_status():
    accs = social_load()["accounts"]
    return jsonify(connected=bool(accs), accounts=[{"id": x["page_id"], "page": x.get("page_name"), "ig": x.get("ig_username"),
                                                    "has_ig": bool(x.get("ig_id")), "missing": x.get("missing", [])} for x in accs])


@app.post("/api/social/connect")
def api_social_connect():
    """Cambia el token corto del Explorador de Meta por uno largo y lista tus Páginas (con su Instagram)."""
    p = request.get_json(force=True)
    app_id, secret, tok = (str(p.get(k) or "").strip() for k in ("app_id", "app_secret", "token"))
    if not (app_id and secret and tok):
        return jsonify(error="Faltan el ID de la app, la clave secreta o el token."), 400
    try:
        j = _gx(requests.get(f"{GRAPH}/oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": app_id,
                                                                    "client_secret": secret, "fb_exchange_token": tok}, timeout=30))
        r = _gx(requests.get(f"{GRAPH}/me/accounts", params={"fields": "id,name,access_token,instagram_business_account{id,username},connected_instagram_account{id,username}",
                                                              "limit": 50, "access_token": j["access_token"]}, timeout=30))
        granted = []
        try:
            pr = _gx(requests.get(f"{GRAPH}/me/permissions", params={"access_token": j["access_token"]}, timeout=30))
            granted = [x["permission"] for x in pr.get("data", []) if x.get("status") == "granted"]
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)[-400:]), 400
    _pages_tmp.clear()
    out = []
    for pg in r.get("data", []):
        iga = pg.get("instagram_business_account") or pg.get("connected_instagram_account") or {}
        _pages_tmp[pg["id"]] = {"page_id": pg["id"], "page_name": pg.get("name"), "page_token": pg["access_token"],
                                "ig_id": iga.get("id"), "ig_username": iga.get("username")}
        out.append({"id": pg["id"], "name": pg.get("name"), "ig": iga.get("username")})
    if not out:
        return jsonify(error="El token no ve ninguna Página. Al generarlo marca los permisos pages_show_list y pages_read_engagement (y business_management si tu Página está en un Portafolio comercial)."), 400
    need = ["pages_show_list", "pages_read_engagement", "pages_manage_posts", "instagram_basic", "instagram_content_publish"]
    missing = [x for x in need if granted and x not in granted]
    _conn["missing"] = missing
    return jsonify(pages=out, missing=missing)


@app.post("/api/social/select")
def api_social_select():
    """Agrega (o actualiza) las Páginas elegidas a la lista de cuentas; las que ya estaban se conservan."""
    p = request.get_json(force=True) or {}
    ids = [str(x) for x in (p.get("page_ids") or ([p["page_id"]] if p.get("page_id") else []))]
    chosen = [_pages_tmp[i] for i in ids if i in _pages_tmp]
    if not chosen:
        return jsonify(error="Vuelve a conectar: la lista de Páginas expiró o no marcaste ninguna."), 400
    s = social_load()
    for pg in chosen:
        acc = {**pg, "missing": _conn.get("missing", []), "saved": int(time.time())}
        s["accounts"] = [x for x in s["accounts"] if x["page_id"] != acc["page_id"]] + [acc]
    social_save(s)
    _pages_tmp.clear()
    return jsonify(ok=True, n=len(chosen))


@app.post("/api/social/disconnect")
def api_social_disconnect():
    """Quita una cuenta (con «id») o todas (sin «id»)."""
    pid = str((request.get_json(force=True) or {}).get("id") or "")
    if not pid:
        SOCIAL_FILE.unlink(missing_ok=True)
        return jsonify(ok=True)
    s = social_load()
    s["accounts"] = [x for x in s["accounts"] if x["page_id"] != pid]
    if s["accounts"]:
        social_save(s)
    else:
        SOCIAL_FILE.unlink(missing_ok=True)
    return jsonify(ok=True)


@app.post("/api/social/publish")
def api_social_publish():
    p = request.get_json(force=True)
    j, name = jobs.get(p.get("job")), str(p.get("file") or "")
    accs = {x["page_id"]: x for x in social_load()["accounts"]}
    if not accs:
        return jsonify(error="Primero conecta tus redes."), 400
    if not j or not (j.get("meta") or {}).get(name) or Path(name).name != name or not (CLIPS / name).is_file():
        return jsonify(error="No encuentro ese clip (¿se reinició el programa o se borró?)."), 400
    dests = [(str(d.get("acc")), d.get("t")) for d in (p.get("dests") or []) if d.get("t") in ("facebook", "instagram")]
    if not dests:
        return jsonify(error="Elige dónde publicar: marca al menos una casilla."), 400
    for acc, t in dests:
        x = accs.get(acc)
        if not x:
            return jsonify(error="Una de las cuentas elegidas ya no está conectada. Recarga la página."), 400
        if t == "instagram" and not x.get("ig_id"):
            return jsonify(error=f"«{x.get('page_name')}» no tiene Instagram vinculado."), 400
        if x.get("missing"):
            return jsonify(error=f"El token de «{x.get('page_name')}» no tiene estos permisos: " + ", ".join(x["missing"]) +
                                 ". Quita esa cuenta, genera un token nuevo con todos los permisos y vuelve a conectarla."), 400
    caption, pid = str(p.get("caption") or ""), uuid.uuid4().hex[:10]
    pub = _pubs[pid] = {"state": "working", "msg": "Empezando...", "res": {}}

    def work():
        for acc, t in dests:
            x = accs[acc]
            label = f"{x.get('page_name')} · {'Facebook' if t == 'facebook' else 'Instagram'}"
            try:
                fn = _pub_facebook if t == "facebook" else _pub_instagram
                url = fn(CLIPS / name, caption, x, lambda m, label=label: pub.update(msg=f"{label}: {m}"))
                pub["res"][f"{acc}:{t}"] = {"ok": True, "url": url, "label": label}
            except Exception as ex:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                pub["res"][f"{acc}:{t}"] = {"ok": False, "msg": str(ex)[-400:], "label": label}
        pub["state"] = "done"

    threading.Thread(target=work, daemon=True).start()
    return jsonify(id=pid)


@app.get("/api/social/publish_status")
def api_social_publish_status():
    pub = _pubs.get(request.args.get("id"))
    return jsonify(pub) if pub else (jsonify(error="No encontrado"), 404)


PROFILES_FILE = DATA / "profiles.json"


def _profiles():
    try:
        return json.loads(PROFILES_FILE.read_text(encoding="utf-8")).get("profiles", [])
    except Exception:  # noqa: BLE001
        return []


@app.get("/api/profiles")
def api_profiles():
    return jsonify(profiles=_profiles())


@app.post("/api/profiles")
def api_profiles_save():
    """Guarda (o actualiza) un perfil de contexto: nombre del programa + participantes."""
    p = request.get_json(force=True) or {}
    name, text = str(p.get("name") or "").strip()[:60], str(p.get("text") or "").strip()[:6000]
    if not name or not text:
        return jsonify(error="Escribe un nombre para el perfil y el contexto."), 400
    lst = [x for x in _profiles() if x["name"] != name] + [{"name": name, "text": text}]
    PROFILES_FILE.write_text(json.dumps({"profiles": lst}, ensure_ascii=False), encoding="utf-8")
    return jsonify(profiles=lst)


@app.post("/api/profiles/delete")
def api_profiles_delete():
    name = str((request.get_json(force=True) or {}).get("name") or "")
    lst = [x for x in _profiles() if x["name"] != name]
    PROFILES_FILE.write_text(json.dumps({"profiles": lst}, ensure_ascii=False), encoding="utf-8")
    return jsonify(profiles=lst)


@app.get("/clips/<path:name>")
def get_clip(name):
    return send_from_directory(CLIPS, name)


if __name__ == "__main__":
    # los trabajos solo viven en la memoria del programa: al arrancar, lo que quedó en data\jobs ya no sirve para nada
    try:
        for _x in (JOBS.iterdir() if JOBS.exists() else []):
            if _x.is_dir():
                shutil.rmtree(_x, ignore_errors=True)
        trim_cache(6.0)
    except Exception as _ex:  # noqa: BLE001
        print(">> No pude limpiar el espacio al arrancar:", _ex, flush=True)
    print(">> " + ytdlp_diag(), flush=True)
    threading.Thread(target=warmup, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, threaded=True)
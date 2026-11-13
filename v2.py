#!/usr/bin/env python3
"""

Run:
    termux-wake-lock          # keeps Termux alive with the screen off
    python musicapp.py        # then open http://localhost:8080 in Chrome

or for different port: python musicapp.py 9000  
"""
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp is missing. Run:  pip install yt-dlp")

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
DATA_DIR = os.path.join(os.path.expanduser("~"), "musicapp")
MUSIC_DIR = os.path.join(DATA_DIR, "downloads")
DB_PATH = os.path.join(DATA_DIR, "library.db")
HAS_FFMPEG = shutil.which("ffmpeg") is not None
os.makedirs(MUSIC_DIR, exist_ok=True)

LOCK = threading.Lock()
JOBS = {}  # job_id -> {title, status, progress, error}


# ----------------------------------------------------------------- database
def db(sql, args=(), commit=False):
    with LOCK:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(sql, args).fetchall()
            if commit:
                conn.commit()
            return rows
        finally:
            conn.close()


def init_db():
    db("CREATE TABLE IF NOT EXISTS tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, artist TEXT, filepath TEXT UNIQUE, duration INTEGER DEFAULT 180)", commit=True)
    db("CREATE TABLE IF NOT EXISTS playlists (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)", commit=True)
    db("CREATE TABLE IF NOT EXISTS playlist_tracks (playlist_id INTEGER, track_id INTEGER, UNIQUE(playlist_id, track_id))", commit=True)


def track_dicts(rows):
    return [
        {"id": r[0], "title": r[1], "artist": r[2] or "Offline", "duration": r[4]}
        for r in rows if r[3] and os.path.exists(r[3])
    ]


def get_library():
    return track_dicts(db("SELECT id, title, artist, filepath, duration FROM tracks ORDER BY id DESC"))


def get_playlists():
    return [{"id": r[0], "name": r[1]} for r in db("SELECT id, name FROM playlists ORDER BY name")]


def get_playlist_tracks(pid):
    q = ("SELECT t.id, t.title, t.artist, t.filepath, t.duration FROM tracks t "
         "JOIN playlist_tracks pt ON t.id = pt.track_id WHERE pt.playlist_id = ?")
    return track_dicts(db(q, (pid,)))


def create_playlist(name):
    if name.strip():
        db("INSERT OR IGNORE INTO playlists (name) VALUES (?)", (name.strip(),), commit=True)


def delete_playlist(pid):
    db("DELETE FROM playlist_tracks WHERE playlist_id = ?", (pid,), commit=True)
    db("DELETE FROM playlists WHERE id = ?", (pid,), commit=True)


def add_to_playlist(pid, tid):
    db("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id) VALUES (?, ?)", (pid, tid), commit=True)


def remove_from_playlist(pid, tid):
    db("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (pid, tid), commit=True)


def delete_track(tid):
    rows = db("SELECT filepath FROM tracks WHERE id = ?", (tid,))
    db("DELETE FROM playlist_tracks WHERE track_id = ?", (tid,), commit=True)
    db("DELETE FROM tracks WHERE id = ?", (tid,), commit=True)
    if rows and rows[0][0] and os.path.exists(rows[0][0]):
        os.remove(rows[0][0])


def get_filepath(tid):
    rows = db("SELECT filepath FROM tracks WHERE id = ?", (tid,))
    return rows[0][0] if rows else None


# ------------------------------------------------------- search / download
def search(query):
    opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch10:{query}", download=False)
    out = []
    for e in info.get("entries", []):
        vid = e.get("id")
        out.append({
            "title": e.get("title") or "Unknown",
            "artist": e.get("uploader") or e.get("channel") or "Unknown",
            "duration": int(e.get("duration") or 180),
            "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
        })
    return out


class Quiet:
    def debug(self, m): pass
    def warning(self, m): print(f"[yt-dlp warning] {m}")
    def error(self, m): print(f"[yt-dlp error] {m}")


def start_download(url, title, artist, duration):
    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"title": title, "status": "downloading", "progress": 3, "error": ""}

    def worker():
        def hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                JOBS[job_id]["progress"] = max(3, min(95, int(d.get("downloaded_bytes", 0) / total * 100)))

        opts = {
            "format": "bestaudio/best" if HAS_FFMPEG else "bestaudio[ext=m4a]/bestaudio",
            "outtmpl": os.path.join(MUSIC_DIR, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "logger": Quiet(),
            "progress_hooks": [hook],
        }
        if HAS_FFMPEG:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if HAS_FFMPEG:
                    filepath = os.path.join(MUSIC_DIR, f"{info['id']}.mp3")
                else:
                    filepath = (info.get("requested_downloads") or [{}])[0].get("filepath") or ydl.prepare_filename(info)
                dur = int(info.get("duration") or duration)
            db("INSERT OR IGNORE INTO tracks (title, artist, filepath, duration) VALUES (?, ?, ?, ?)",
               (title, artist, filepath, dur), commit=True)
            JOBS[job_id].update(status="done", progress=100)
        except Exception as e:
            JOBS[job_id].update(status="error", error=str(e)[:200])

    threading.Thread(target=worker, daemon=True).start()
    return job_id


# --------------------------------------------------------------- web server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif p == "/api/library":
                self.send_json(get_library())
            elif p == "/api/playlists":
                self.send_json(get_playlists())
            elif m := re.fullmatch(r"/api/playlist/(\d+)", p):
                self.send_json(get_playlist_tracks(int(m.group(1))))
            elif p == "/api/search":
                q = (parse_qs(u.query).get("q") or [""])[0].strip()
                self.send_json(search(q) if q else [])
            elif p == "/api/jobs":
                self.send_json(JOBS)
            elif m := re.fullmatch(r"/audio/(\d+)", p):
                self.serve_audio(int(m.group(1)))
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            data = self.read_json()
            if p == "/api/playlist":
                create_playlist(data.get("name", ""))
                self.send_json({"ok": True})
            elif m := re.fullmatch(r"/api/playlist/(\d+)/add", p):
                add_to_playlist(int(m.group(1)), int(data["track_id"]))
                self.send_json({"ok": True})
            elif m := re.fullmatch(r"/api/playlist/(\d+)/remove", p):
                remove_from_playlist(int(m.group(1)), int(data["track_id"]))
                self.send_json({"ok": True})
            elif p == "/api/download":
                jid = start_download(data["url"], data.get("title", "Unknown"), data.get("artist", "Unknown"), int(data.get("duration") or 180))
                self.send_json({"job": jid})
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_DELETE(self):
        p = urlparse(self.path).path
        try:
            if m := re.fullmatch(r"/api/playlist/(\d+)", p):
                delete_playlist(int(m.group(1)))
            elif m := re.fullmatch(r"/api/track/(\d+)", p):
                delete_track(int(m.group(1)))
            else:
                return self.send_json({"error": "not found"}, 404)
            self.send_json({"ok": True})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def serve_audio(self, tid):
        path = get_filepath(tid)
        if not path or not os.path.exists(path):
            return self.send_json({"error": "missing file"}, 404)
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "audio/mpeg"
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            elif m.group(2):
                start = max(size - int(m.group(2)), 0)
            code = 206
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(64 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


# ----------------------------------------------------------------- web page
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#121212">
<title>Offline Music</title>
<style>
:root{--bg:#121212;--card:#1e1e1e;--teal:#26a69a;--txt:#eee;--dim:#9a9a9a}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;margin:0;background:var(--bg);color:var(--txt);font-family:system-ui,sans-serif}
body{display:flex;flex-direction:column;max-width:900px;margin:0 auto}
nav{display:flex;background:var(--card)}
nav button{flex:1;padding:14px;background:none;border:0;border-bottom:3px solid transparent;color:var(--dim);font-size:15px}
nav button.on{color:var(--teal);border-color:var(--teal)}
main{flex:1;overflow-y:auto;padding:8px 12px}
.row{display:flex;align-items:center;gap:8px;padding:10px 4px;border-bottom:1px solid #2a2a2a}
.row .t{flex:1;min-width:0;cursor:pointer}
.row .t b,.row .t small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .t small{color:var(--dim)}
button.ic{background:none;border:0;color:var(--txt);font-size:22px;padding:8px 10px;border-radius:50%}
button.ic.tog{opacity:.35}
button.ic.tog.active{opacity:1;background:var(--teal);box-shadow:0 0 0 2px var(--teal)}
button.pill{background:var(--teal);color:#000;border:0;border-radius:6px;padding:10px 14px;font-weight:600}
input[type=text]{flex:1;min-width:0;padding:12px;border-radius:6px;border:1px solid #444;background:#1a1a1a;color:var(--txt);font-size:16px}
.bar{display:flex;gap:8px;align-items:center;margin:6px 0}
.dim{color:var(--dim);font-size:13px}
progress{width:100%;height:4px;accent-color:var(--teal)}
#player{background:#1f1f1f;padding:8px 14px calc(8px + env(safe-area-inset-bottom))}
#np{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;height:24px}
.sl{display:flex;align-items:center;gap:8px}
.sl input{flex:1;accent-color:var(--teal)}
.ctl{display:flex;justify-content:center;gap:18px}
#modal{position:fixed;inset:0;background:#000a;display:none;align-items:center;justify-content:center}
#modal .box{background:var(--card);padding:16px;border-radius:10px;min-width:260px;max-width:90vw;display:flex;flex-direction:column;gap:8px}
#modal button{padding:12px;border:0;border-radius:6px;background:#333;color:var(--txt);font-size:15px}
#modal button.bad{color:#f66}
</style></head><body>
<nav>
  <button id="n0" class="on">Library</button>
  <button id="n1">Playlists</button>
  <button id="n2">Search</button>
</nav>
<main id="main"></main>
<div id="player">
  <div id="np">No track selected</div>
  <div class="sl"><span id="el" class="dim">0:00</span><input id="seek" type="range" min="0" max="100" value="0" step="1"><span id="tot" class="dim">0:00</span></div>
  <div class="ctl">
    <button class="ic tog" id="rep">🔁</button>
    <button class="ic" id="prev">⏮</button>
    <button class="ic" id="pp">▶️</button>
    <button class="ic" id="next">⏭</button>
    <button class="ic tog" id="shuf">🔀</button>
  </div>
</div>
<div id="modal"><div class="box" id="mbox"></div></div>
<audio id="au" preload="auto"></audio>

<script>
const $ = s => document.querySelector(s);
const au = $('#au');
function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith('on')) e[k] = v; else if (k === 'class') e.className = v; else e.setAttribute(k, v);
  }
  for (const c of kids) e.append(c);
  return e;
}
async function api(path, method = 'GET', body) {
  const r = await fetch(path, {method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return r.json();
}
const fmt = s => { s = Math.max(0, Math.floor(s || 0)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };

let tab = 0, plOpen = null, queue = [], base = [], idx = -1, shuffle = false, repeat = false, seeking = false;
let searchResults = [], searchMsg = 'Ready.', jobsTimer = null;
const turns = new Map(); // track id -> songs since it last played

// ---------- modal
function modal(title, buttons) {
  const box = $('#mbox'); box.innerHTML = '';
  box.append(h('b', {}, title));
  for (const [label, fn, bad] of buttons) box.append(h('button', {class: bad ? 'bad' : '', onclick: () => { closeModal(); fn && fn(); }}, label));
  $('#modal').style.display = 'flex';
}
function closeModal() { $('#modal').style.display = 'none'; }
$('#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };

// ---------- tabs
[0, 1, 2].forEach(i => $('#n' + i).onclick = () => { tab = i; render(); });
async function render() {
  [0, 1, 2].forEach(i => $('#n' + i).classList.toggle('on', i === tab));
  const m = $('#main'); m.innerHTML = '';
  if (tab === 0) await renderLibrary(m); else if (tab === 1) await renderPlaylists(m); else renderSearch(m);
}

function trackRow(list, i, opts = {}) {
  const t = list[i];
  return h('div', {class: 'row'},
    h('div', {class: 't', onclick: () => startQueue(list, i)}, h('b', {}, t.title), h('small', {}, t.artist)),
    h('button', {class: 'ic', onclick: () => trackMenu(t, opts)}, '⋮'));
}

function trackMenu(t, opts) {
  const b = [['Add to Playlist', () => addDialog(t.id)]];
  if (opts.plId) b.push(['Remove from this playlist', async () => { await api(`/api/playlist/${opts.plId}/remove`, 'POST', {track_id: t.id}); render(); }]);
  b.push(['Delete Song', async () => { await api('/api/track/' + t.id, 'DELETE'); render(); }, true]);
  b.push(['Cancel']);
  modal(t.title, b);
}

async function addDialog(tid) {
  const pls = await api('/api/playlists');
  if (!pls.length) { searchMsg = 'Create a playlist first!'; return modal('Create a playlist first!', [['OK']]); }
  modal('Add to Playlist', [...pls.map(p => [p.name, () => api(`/api/playlist/${p.id}/add`, 'POST', {track_id: t_id(tid)})]), ['Cancel']]);
}
const t_id = x => x;

async function renderLibrary(m) {
  const lib = await api('/api/library');
  if (!lib.length) m.append(h('p', {class: 'dim'}, 'No songs yet. Use the Search tab to download some.'));
  lib.forEach((_, i) => m.append(trackRow(lib, i)));
}

async function renderPlaylists(m) {
  if (plOpen) {
    const top = h('div', {class: 'bar'}, h('button', {class: 'ic', onclick: () => { plOpen = null; render(); }}, '←'), h('b', {style: 'flex:1'}, plOpen.name));
    m.append(top);
    const tr = await api('/api/playlist/' + plOpen.id);
    if (!tr.length) m.append(h('p', {class: 'dim'}, 'Empty playlist. Add songs from the Library tab.'));
    tr.forEach((_, i) => m.append(trackRow(tr, i, {plId: plOpen.id})));
    return;
  }
  m.append(h('div', {class: 'bar'}, h('b', {style: 'flex:1'}, 'Your Playlists'), h('button', {class: 'pill', onclick: newPlaylist}, '+ New')));
  const pls = await api('/api/playlists');
  pls.forEach(p => m.append(h('div', {class: 'row'},
    h('div', {class: 't', onclick: () => { plOpen = p; render(); }}, h('b', {}, '🎵 ' + p.name)),
    h('button', {class: 'ic', onclick: () => modal('Delete "' + p.name + '"?', [['Delete', async () => { await api('/api/playlist/' + p.id, 'DELETE'); render(); }, true], ['Cancel']])}, '🗑'))));
}

function newPlaylist() {
  const name = prompt('Playlist name');
  if (name) api('/api/playlist', 'POST', {name}).then(render);
}

// ---------- search / download
function renderSearch(m) {
  const inp = h('input', {type: 'text', id: 'q', placeholder: 'Song or artist...'});
  const go = () => doSearch(inp.value);
  inp.onkeydown = e => { if (e.key === 'Enter') go(); };
  m.append(h('div', {class: 'bar'}, inp, h('button', {class: 'pill', onclick: go}, 'Search')));
  m.append(h('div', {class: 'dim', id: 'status'}, searchMsg), h('progress', {id: 'prog', value: '0', max: '100'}));
  searchResults.forEach(r => m.append(h('div', {class: 'row'},
    h('div', {class: 't'}, h('b', {}, r.title), h('small', {}, r.artist)),
    h('button', {class: 'ic', onclick: () => download(r)}, '⬇'))));
}
function setStatus(s) { searchMsg = s; const e = $('#status'); if (e) e.textContent = s; }
async function doSearch(q) {
  q = q.trim(); if (!q) return;
  setStatus(`Searching '${q}'...`); searchResults = []; render2();
  const r = await api('/api/search?q=' + encodeURIComponent(q));
  if (r.error) return setStatus('Error: ' + r.error);
  searchResults = r; setStatus(`Found ${r.length} tracks.`); render2(q);
}
function render2(q) { if (tab === 2) { render().then(() => { if (q) $('#q').value = q; }); } }
async function download(r) {
  setStatus('Downloading: ' + r.title + '...');
  const res = await api('/api/download', 'POST', r);
  if (res.error) return setStatus('Failed: ' + res.error);
  pollJobs();
}
function pollJobs() {
  if (jobsTimer) return;
  jobsTimer = setInterval(async () => {
    const jobs = await api('/api/jobs');
    const active = Object.values(jobs).filter(j => j.status === 'downloading');
    const p = $('#prog');
    if (active.length) {
      if (p) p.value = active[0].progress;
      setStatus('Downloading: ' + active[0].title + '... ' + active[0].progress + '%');
      return;
    }
    clearInterval(jobsTimer); jobsTimer = null;
    const err = Object.values(jobs).find(j => j.status === 'error');
    if (p) p.value = 100;
    setStatus(err ? 'Failed: ' + err.error : 'Download finished. Check the Library tab.');
    for (const k of Object.keys(jobs)) { /* keep list tidy server-side on restart */ }
  }, 1000);
}

// ---------- queue / shuffle
function weight(t) {
  const n = turns.has(t.id) ? turns.get(t.id) : 10;
  const r = (a, b) => a + Math.random() * (b - a);
  return n === 0 ? 0 : n === 1 ? r(50, 150) : n === 2 ? r(300, 500) : r(800, 1000);
}
function buildQueue(list) {
  if (!shuffle) return [...list];
  const pool = list.map(t => ({t, w: weight(t)})), out = [];
  while (pool.length) {
    const sum = pool.reduce((a, b) => a + b.w, 0);
    let i = 0;
    if (sum <= 0) i = Math.floor(Math.random() * pool.length);
    else { let r = Math.random() * sum; while (i < pool.length - 1 && r >= pool[i].w) { r -= pool[i].w; i++; } }
    out.push(pool[i].t); pool.splice(i, 1);
  }
  return out;
}
function startQueue(list, i) {
  base = [...list];
  const chosen = base[i];
  queue = buildQueue(base);
  idx = Math.max(0, queue.findIndex(t => t.id === chosen.id));
  playCurrent();
}
function playCurrent() {
  if (idx < 0 || idx >= queue.length) return;
  const t = queue[idx];
  for (const k of turns.keys()) turns.set(k, turns.get(k) + 1);
  turns.set(t.id, 0);
  au.src = '/audio/' + t.id;
  au.play().catch(() => {});
  $('#np').textContent = t.title + ' - ' + t.artist;
  if ('mediaSession' in navigator) navigator.mediaSession.metadata = new MediaMetadata({title: t.title, artist: t.artist, album: 'Offline Music'});
}
function next() {
  if (!queue.length) return;
  if (repeat) { au.currentTime = 0; au.play(); return; }
  idx++;
  if (idx >= queue.length) { idx = 0; if (shuffle) queue = buildQueue(base); } // new weighted order each lap
  playCurrent();
}
function prev() {
  if (!queue.length) return;
  if (repeat || au.currentTime > 3) { au.currentTime = 0; au.play(); return; }
  idx = (idx - 1 + queue.length) % queue.length; playCurrent();
}
function toggle() {
  if (idx < 0) return;
  au.paused ? au.play() : au.pause();
}

$('#next').onclick = next; $('#prev').onclick = prev; $('#pp').onclick = toggle;
$('#rep').onclick = () => { repeat = !repeat; $('#rep').classList.toggle('active', repeat); };
$('#shuf').onclick = () => {
  shuffle = !shuffle; $('#shuf').classList.toggle('active', shuffle);
  if (!base.length) return;
  const cur = queue[idx];
  queue = buildQueue(base);
  if (cur) {
    const j = queue.findIndex(t => t.id === cur.id);
    if (shuffle) { if (j > 0) { queue.splice(j, 1); queue.unshift(cur); } idx = 0; } // current song first, rest weighted-shuffled
    else idx = Math.max(0, j); // back to normal order, keep your place
  }
};
au.onended = next;
au.onplay = () => { $('#pp').textContent = '⏸'; if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing'; };
au.onpause = () => { $('#pp').textContent = '▶️'; if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused'; };
au.onloadedmetadata = () => { $('#seek').max = Math.floor(au.duration || 100); $('#tot').textContent = fmt(au.duration); };
au.ontimeupdate = () => {
  if (!seeking) { $('#seek').value = Math.floor(au.currentTime); $('#el').textContent = fmt(au.currentTime); }
  if ('mediaSession' in navigator && isFinite(au.duration)) {
    try { navigator.mediaSession.setPositionState({duration: au.duration, position: au.currentTime, playbackRate: 1}); } catch (e) {}
  }
};
const sk = $('#seek');
sk.oninput = () => { seeking = true; $('#el').textContent = fmt(sk.value); };
sk.onchange = () => { au.currentTime = +sk.value; seeking = false; };

if ('mediaSession' in navigator) {
  const ms = navigator.mediaSession;
  ms.setActionHandler('play', () => au.play());
  ms.setActionHandler('pause', () => au.pause());
  ms.setActionHandler('nexttrack', next);
  ms.setActionHandler('previoustrack', prev);
  ms.setActionHandler('seekto', d => { au.currentTime = d.seekTime; });
}

render();
</script>
</body></html>
"""


def main():
    init_db()
    print(f"Offline Music running.  Open  http://localhost:{PORT}  in Chrome.")
    print(f"Songs are stored in {MUSIC_DIR}")
    if not HAS_FFMPEG:
        print("Note: ffmpeg not found (pkg install ffmpeg). Downloads will be saved as m4a instead of mp3.")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
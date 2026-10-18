"""Parse xTool Omni (DT002) log bundles into structured data.

A log bundle exported from xTool Studio is a zip (named *.gz) containing:
  - YYYY-MM-DD/main.log etc.   xTool Studio desktop logs (computer's local time)
  - xTool_device_log.gz        gzip'd tar of the printer's /var/log and /mnt/config
  - diagnostic.gz              zip of calibration images (ignored)

Printer timestamps are naive local time on the printer (UTC+8 by default);
desktop timestamps are naive local time on the computer. Both offsets are
auto-detected by matching task IDs against epoch timestamps in the logs.
"""

import gzip
import os
import io
import json
import re
import tarfile
import zipfile
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

TEXT_EXT = ('.log', '.json', '.txt')

# Fallback names (Omni O1 Dual-UV) when the logs don't include the printer's own names.
# Head 1 (UV1): C M Y K, varnish, white.  Head 2 (UV2): fluorescent yellow/red (Studio
# channels V2/V1), flexible white (SW), rigid white (HW).
INK_NAMES = {
    'UC': 'UV Cyan', 'UM': 'UV Magenta', 'UY': 'UV Yellow', 'UK': 'UV Black',
    'UV': 'UV Varnish', 'UW': 'UV White', 'UH': 'Fluorescent Yellow', 'UR': 'Fluorescent Red',
    'US': 'Flexible White', 'UD': 'Rigid White',
}

# --------------------------------------------------------------------------
# Archive loading
# --------------------------------------------------------------------------

def _is_text_name(name):
    base = name.rsplit('/', 1)[-1]
    if '__MACOSX/' in name or base.startswith('._'):   # macOS resource-fork clutter in zips
        return False
    return base.endswith(TEXT_EXT) or re.search(r'\.log\.\d+$', base) is not None


def _walk(data, name, out):
    """Recursively expand zip / gzip / tar blobs into {path: text}."""
    if data[:4] == b'PK\x03\x04':
        prefix = name + '/' if name else ''
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if not info.is_dir():
                    _walk(z.read(info), prefix + info.filename, out)
        return
    if data[:2] == b'\x1f\x8b':
        try:
            raw = gzip.decompress(data)
        except OSError:
            return
        _walk(raw, re.sub(r'\.gz$', '', name), out)
        return
    if data[257:262] == b'ustar':
        prefix = name + '/' if name else ''
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            for m in t.getmembers():
                if m.isfile():
                    _walk(t.extractfile(m).read(), prefix + m.name.lstrip('./'), out)
        return
    if _is_text_name(name):
        out[name] = data.decode('utf-8', errors='replace')


BUNDLE_EXT = ('.gz', '.zip')


def _looks_like_logs_folder(path):
    return any(re.fullmatch(r'\d{4}-\d\d-\d\d', d) for d in os.listdir(path))


def find_inputs(paths):
    """Expand folders into the .gz / .zip bundles inside them (recursively).
    A folder that is itself an xTool Studio logs folder (date sub-folders)
    is used as one source."""
    out = []
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isdir(p) and not _looks_like_logs_folder(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                out += [os.path.join(root, n) for n in names if n.lower().endswith(BUNDLE_EXT) and not n.startswith('.')]
        elif os.path.exists(p):
            out.append(p)
    return sorted(set(out))


def load_source(path):
    """One export / zip / logs folder -> {inner path: text}."""
    part = {}
    if os.path.isdir(path):
        for root, _, names in os.walk(path):
            for fn in names:
                full = os.path.join(root, fn)
                with open(full, 'rb') as f:
                    _walk(f.read(), os.path.relpath(full, path).replace(os.sep, '/'), part)
    else:
        with open(path, 'rb') as f:
            data = f.read()
        if data[:4] == b'PK\x03\x04':
            _walk(data, '', part)
        else:
            _walk(data, os.path.basename(path), part)
    return part


def load_sources(paths):
    sources = []
    for p in find_inputs(paths):
        try:
            files = load_source(p)
        except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
            sources.append({'path': p, 'files': {}, 'error': str(exc)})
            continue
        sources.append({'path': p, 'files': files})
    return sources


# ---- merging overlapping exports -------------------------------------------

REC_START = re.compile(r'^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\]')


def merge_device_logs(texts):
    """Union of timestamped records from several copies / rotations of the same
    device log, with exact duplicates removed and everything in time order.

    A record is a timestamped line plus any continuation lines under it.
    Records written before the printer synced its clock (1970-...) are kept
    next to the first real timestamp that follows them in their own file."""
    seen = set()
    items = []
    stats = {'records': 0, 'duplicates': 0}
    for fi, text in enumerate(texts):
        recs = []
        for line in text.splitlines():
            if REC_START.match(line) or not recs:
                recs.append([line])
            else:
                recs[-1].append(line)
        anchor = '9999'
        keyed = []
        for rec in reversed(recs):
            m = REC_START.match(rec[0])
            if m and not m.group(1).startswith('1970'):
                anchor = m.group(1)
            keyed.append(anchor)
        keyed.reverse()
        occurrence = {}
        for i, rec in enumerate(recs):
            body = '\n'.join(rec)
            stats['records'] += 1
            # the n-th copy of a record within a file only matches the n-th copy in
            # another file, so lines a file legitimately repeats are kept
            n = occurrence[body] = occurrence.get(body, 0) + 1
            if (body, n) in seen:
                stats['duplicates'] += 1
                continue
            seen.add((body, n))
            items.append((keyed[i], fi, i, body))
    items.sort()
    return '\n'.join(it[3] for it in items), stats


def dedupe_desktop_logs(entries):
    """entries: [(label, text)] for one date's main.log from various exports.
    A copy that is a prefix of (or equal to) another copy is an older export of
    the same computer's log and is dropped; different computers are all kept."""
    entries = sorted(entries, key=lambda e: len(e[1]), reverse=True)
    kept = []
    for label, text in entries:
        if any(k[1].startswith(text) for k in kept):
            continue
        kept.append((label, text))
    return kept


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

DEV_LINE = re.compile(r'^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\] \[-(\w)-\] (?:\[([^\]]*)\] )?: ?(.*)$')
DESK_LINE = re.compile(r'^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\] \[(\w+)\]\s+(.*)$')


def naive_ms(ts):
    """Naive timestamp string -> ms as if it were UTC (offset applied later)."""
    dt = datetime.strptime(ts[:23], '%Y-%m-%d %H:%M:%S.%f')
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def valid_clock(ts):
    return not ts.startswith('1970')


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def kv(text):
    """'a=1 b=two' -> {'a': '1', 'b': 'two'}"""
    return dict(re.findall(r'(\w+)=([^\s,]+)', text))


def files_matching(files, pattern):
    rx = re.compile(pattern)
    return sorted((k for k in files if rx.search(k)), key=_rotation_key)


def _rotation_key(name):
    # app-controller.2.log is older than .1.log which is older than .log
    m = re.search(r'\.(\d+)\.log$', name)
    return (re.sub(r'\.\d+\.log$', '.log', name), -(int(m.group(1)) if m else 0))

# --------------------------------------------------------------------------
# Device: record.log (the printer's own high-level event journal)
# --------------------------------------------------------------------------

def classify_record(msg):
    """Return (category, kind, detail dict)."""
    head, _, rest = msg.partition(', ')
    d = kv(rest)
    if head.startswith('print '):
        return 'print', head, d
    if head.startswith('clean '):
        d['inPrint'] = '(in print)' in msg
        m = re.search(r'action=(.+?) head=(\S+)', msg)
        if m:
            d['action'], d['head'] = m.group(1), m.group(2)
        return 'cleaning', head, d
    if head == 'low power maintain' and d.get('action') == 'standardClean':
        # a scheduled standard clean the printer runs while in low power (seen on the UV/DT)
        return 'cleaning', 'clean start', {'action': 'scheduled standard', 'head': None, 'inPrint': False, 'scheduled': True}
    if head.startswith('white ') or head == 'low power maintain':
        return 'white-ink', head, d
    if head in ('boot', 'time synced', 'upgrade completed'):
        return 'power', head, d
    if 'sleep' in head or 'low power' in head:
        return 'power', head, d
    if head.startswith(('calibration', 'measure', 'scan')):
        return 'calibration', head, d
    if head.startswith('alarm'):
        m = re.match(r'alarm (\S+) (\S+)', msg)
        if m:
            d['code'], d['name'] = m.group(1), m.group(2)
        d['text'] = rest
        return 'alarm', 'alarm', d
    if head in ('ink tank removed', 'ink tank inserted', 'moisture tank removed', 'moisture tank inserted'):
        return 'cartridge', head, d
    if head == 'upgrade start':
        return 'power', head, d
    if head == 'config set':
        return 'config', head, {'text': rest}
    return 'other', head, {'text': rest}


def parse_record_log(text):
    events = []
    for line in text.splitlines():
        m = DEV_LINE.match(line)
        if not m:
            continue
        ts, lvl, _, msg = m.groups()
        cat, kind, d = classify_record(msg)
        events.append({'ts': ts, 'valid': valid_clock(ts), 'level': lvl,
                       'cat': cat, 'kind': kind, 'msg': msg, 'd': d})
    return events

# --------------------------------------------------------------------------
# Device: app-controller logs (ink levels, counters, boots)
# --------------------------------------------------------------------------

INK_RX = re.compile(r'ink_tank_status":(\{[^}]*\})')
MOIST_RX = re.compile(r'moisture_tank_status":(\{[^}]*\})')
BATCH_RX = re.compile(r'batch config: (\{.*\})')


def parse_app_controller(texts):
    ink, moist, counters, boots, wastes = [], [], [], [], []
    for text in texts:
        for line in text.splitlines():
            if 'ink_tank_status' in line or 'moisture_tank_status' in line or \
               'batch config' in line or 'STARTED pid' in line:
                m = DEV_LINE.match(line)
                if not m:
                    continue
                ts, _, _, msg = m.groups()
                if 'STARTED pid' in msg:
                    boots.append(ts)
                    continue
                if not valid_clock(ts):
                    continue
                if (x := INK_RX.search(msg)):
                    j = json.loads(x.group(1))
                    if j.get('exist'):
                        ink.append((ts, j['route'], j['tank_id'], j['remain_ml'], j['full_ml']))
                elif (x := MOIST_RX.search(msg)):
                    j = json.loads(x.group(1))
                    if j.get('exist'):
                        moist.append((ts, j['index'], j['remain_ml'], j['full_ml']))
                elif (x := BATCH_RX.search(msg)):
                    try:
                        counters.append((ts, json.loads(x.group(1))))
                    except json.JSONDecodeError:
                        pass
    return ink, moist, counters, boots

# ---- per-head time counters ------------------------------------------------

HEAD_STAT_RX = re.compile(r'AppHeadTimeStat(?:::add| on print start:) head=(\d) counter=(\w+) (?:addSec=\d+ total=(\d+)|value=(\d+))')
THRESH_RX = re.compile(r'cache in-print clean thresholds: (.*)$')
SEVEN_DAYS = 7 * 24 * 3600
LINE_FILL_RX = re.compile(r'^\[(\d{4}-[^\]]+)\].*?\b(ink|white) pipeline fill consumed: route=\d+, ch=\d+, slot=(\d+), ml=([\d.]+)', re.M)
RATE_WINDOW_MS = 48 * 3600 * 1000


def parse_head_timers(ac_text, dev_t, config):
    """The printer keeps per-head time counters (seconds): Idle (time not working
    since the head's last standard clean; keeps counting while asleep), NoRun,
    WhiteNoRun, ColorNoRun, FluNoRun, MoistNoRun, Moisrizing, ContinousWork.
    They are only logged when the printer changes state, so values are
    snapshots. Also returns the clean thresholds the firmware reports, and an
    estimate of when each head's Idle reaches 7 days, assuming the printer
    stays idle the same share of the time as in the last 48 h of the logs.
    (The 7-day moisturizing trigger is not shown in any log; it is an assumption.)"""
    heads = {}
    thresholds = {}
    for line in ac_text.splitlines():
        if 'AppHeadTimeStat' in line:
            m = DEV_LINE.match(line)
            if not m or not valid_clock(m.group(1)):
                continue
            s = HEAD_STAT_RX.search(m.group(4))
            if not s:
                continue
            h = heads.setdefault(s.group(1), {'counters': {}, 'idle': []})
            val = int(s.group(3) or s.group(4))
            t = dev_t(m.group(1))
            h['counters'][s.group(2)] = {'value': val, 't': t}
            if s.group(2) == 'Idle':
                h['idle'].append([t, val])
        elif 'cache in-print clean thresholds' in line:
            x = THRESH_RX.search(line)
            if x:
                for k, v in re.findall(r'(\w+(?:\([^)]*\))?)=([\d/]+)', x.group(1)):
                    thresholds[k] = v
    if config.get('uvPrintNorunTimeSwitchMoisturizing') is not None:
        thresholds['uvPrintNorunTimeSwitchMoisturizing'] = str(config['uvPrintNorunTimeSwitchMoisturizing'])

    for h in heads.values():
        pts = h['idle']
        # last reset: a reading lower than the one before it
        h['lastReset'] = None
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if v1 < v0:
                h['lastReset'] = t1 - v1 * 1000 if v1 else t1
        if not pts:
            h['estimate'] = None
            continue
        end = pts[-1][0]
        start = end - RATE_WINDOW_MS
        idle_sec = 0.0
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t1 <= start or t1 == t0:
                continue
            d = v1 - v0 if v1 >= v0 else v1
            if t0 < start:
                d *= (t1 - start) / (t1 - t0)
            idle_sec += d
        span = (end - max(start, pts[0][0])) / 1000
        frac = min(1.0, idle_sec / span) if span > 0 else None
        latest = pts[-1][1]
        remaining = SEVEN_DAYS - latest
        h['estimate'] = {
            'windowStart': max(start, pts[0][0]), 'windowEnd': end,
            'idleFraction': frac, 'latest': latest, 'latestT': end,
            'remainingSec': remaining,
            'reachT': (end + remaining / frac * 1000) if frac and remaining > 0 else (end if remaining <= 0 else None),
        }
    return {'heads': heads, 'thresholds': thresholds, 'target': SEVEN_DAYS}


# --------------------------------------------------------------------------
# Desktop (xTool Studio) main.log: job configuration
# --------------------------------------------------------------------------

def _js_obj_fields(s):
    """Grab simple key: value pairs from a JS-style object dump."""
    out = {}
    for k, v in re.findall(r"(\w+): ('[^']*'|-?[\d.]+|true|false)", s):
        out.setdefault(k, v.strip("'"))
    return out


def _collect_json_block(lines, i):
    """lines[i] starts a multi-line JSON log entry '[ts] [info] ... [' -> parsed list."""
    first = lines[i]
    start = first.index('[', first.index(']=>') if ']=>' in first else 30)
    buf = [first[start:]]
    j = i + 1
    while j < len(lines) and not DESK_LINE.match(lines[j]):
        buf.append(lines[j])
        j += 1
    try:
        return json.loads('\n'.join(buf)), j
    except json.JSONDecodeError:
        return None, j


ELEMENT_RX = re.compile(
    r"id: '([0-9a-f-]{36})', isCompensate: \w+, printThickness: ([\d.]+), layerThicknessMm: ([\d.]+), "
    r"inkMode: '(\w*)', processingType: '(\w*)', varnishType: '(\w*)', whiteInkType: '(\w*)'"
    r"(?:, inverted: \w+, printPos: \{ x: ([-\d.]+), y: ([-\d.]+), width: ([\d.]+), height: ([\d.]+) \})?")


def parse_desktop_main(text, file_label):
    """Yield one dict per task created from this computer, with the most recent
    preparation details that preceded it."""
    lines = text.splitlines()
    jobs = []
    tasks_seen = {}
    ctx = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        m = DESK_LINE.match(line)
        if not m:
            i += 1
            continue
        ts = m.group(1)
        if '"write slice params json success"' in lines[i + 1 if i + 1 < len(lines) else i] and line.rstrip().endswith('['):
            block, nxt = _collect_json_block(lines, i)
            if block and len(block) > 1:
                ctx['slice'] = block[1][1] if isinstance(block[1], list) and len(block[1]) > 1 else None
            i = nxt
            continue
        if "'createdPrintSourceImage'" in line:
            ctx['bbox'] = _js_obj_fields(line.split("'createdPrintSourceImage'", 1)[1])
        elif "'write prn param json success'" in line:
            ctx['prn'] = line
        elif "'computeInkVolume workDes'" in line:
            ctx['inkplan'] = _js_obj_fields(line.split('workDes', 1)[1])
        elif "'step:createTask.params'" in line:
            ctx['params'] = _js_obj_fields(line.split('createTask.params', 1)[1])
        elif "step:createTask.apiOk" in line:
            tid = re.search(r"taskId: '(\w+)'", line)
            if tid:
                jobs.append(_build_desktop_job(tid.group(1), ts, ctx))
                ctx = {}
        # Task history summaries reported by the device (name, passes, epoch times)
        if 'deviceHisTasks' in line or ("createdAt:" in line and "totalPrintPass" in line):
            for obj in re.findall(r"\{ clientId: '[^']*'[^{}]*\}", line):
                f = _js_obj_fields(obj)
                if 'id' in f:
                    prev = tasks_seen.get(f['id'], {})
                    prev.update({k: v for k, v in f.items() if v not in ('', None)})
                    tasks_seen[f['id']] = prev
        i += 1
    for j in jobs:
        j['desktopFile'] = file_label
    return jobs, tasks_seen


# Paths use / on Mac and \\ on Windows; UV jobs have one folder per element, DTF jobs a dtf/<n> folder.
GENPD_RX = re.compile(r'\[GenPd\] eleIndex=(\d+), imgIndex=(\d+), color: (\w+),.*?atomm[\\/]+DT002[\\/]+task[\\/]+(\w+)[\\/]+(?:uv[\\/]+\d+_([0-9a-f]{8})|dtf[\\/]+(\d+))[\\/]')


def parse_genpd(texts):
    """xTool Studio's slicer (algo-server) logs which ink each layer image is
    printed with: HW/SW/W = white, V = varnish, C = colour. This is the only
    place varnish layers can be told apart from white ones, since the slicer
    parameters call both 'white'. Returns {taskId: {elementKey: [ink, ...]}}."""
    out = {}
    for text in texts:
        for m in GENPD_RX.finditer(text):
            ele, img, ink, tid = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
            key = m.group(5) or 'dtf' + m.group(6)
            out.setdefault(tid, {}).setdefault(key, {})[img] = (ele, ink)
    return {tid: {k: [v[i][1] for i in sorted(v)] for k, v in els.items()} for tid, els in out.items()}


def _build_desktop_job(task_id, ts, ctx):
    job = {'taskId': task_id, 'createdLocal': ts}
    p = ctx.get('params', {})
    job['name'] = p.get('name')
    job['totalPrintPass'] = num(p.get('totalPrintPass'))
    job['estimateTime'] = num(p.get('estimateTime'))
    job['processingMode'] = p.get('processingMode')
    bb = ctx.get('bbox') or {}
    if bb:
        job['size'] = {'w': num(bb.get('width')), 'h': num(bb.get('height')),
                       'x': num(bb.get('x')), 'y': num(bb.get('y'))}
    plan = ctx.get('inkplan') or {}
    job['inkPlan'] = {k[4:]: num(v) for k, v in plan.items() if k.startswith('Ink_') and num(v)}
    prn = ctx.get('prn') or ''
    elems = {}
    for e in ELEMENT_RX.finditer(prn):
        eid = e.group(1)
        elems[eid[:8]] = {
            'id': eid, 'printThickness': num(e.group(2)), 'layerThicknessMm': num(e.group(3)),
            'inkMode': e.group(4), 'processingType': e.group(5), 'varnishType': e.group(6),
            'whiteInkType': e.group(7),
            'pos': ({'x': num(e.group(8)), 'y': num(e.group(9)), 'w': num(e.group(10)), 'h': num(e.group(11))}
                    if e.group(10) else None),
        }
    for k in ('precision', 'speed', 'uvLightPower', 'inkSavingMode', 'halfToningType', 'renderingIntent', 'highSpeedPrint'):
        mm = re.search(k + r": ('[^']*'|\"[^\"]*\"|[\w.]+)", prn)
        if mm:
            job.setdefault('settings', {})[k] = mm.group(1).strip("'\"")
    mm = re.search(r"precision: '\{ x: (\d+), y: (\d+) \}'", prn)
    if mm:
        job.setdefault('settings', {})['dpi'] = f'{mm.group(1)}x{mm.group(2)}'
    # Layer stack per element from the slicer parameters
    elements = []
    for el in (ctx.get('slice') or []):
        key = el.get('input_dir', '').split('_')[-1][:8]
        layers = [{'mode': lp.get('color_mode'), 'count': lp.get('slice_count', 1),
                   'slice': lp.get('slice_mode')} for lp in el.get('layer_parameters', [])]
        info = dict(elems.get(key, {}))
        info['key'] = key
        info['layers'] = layers
        elements.append(info)
    # Elements known from prn param but not in slice (rare)
    for key, info in elems.items():
        if not any(e['key'] == key for e in elements):
            elements.append({**info, 'key': key, 'layers': []})
    job['elements'] = elements
    return job

# --------------------------------------------------------------------------
# Clock offsets
# --------------------------------------------------------------------------

def _round_offset(diff_ms):
    q = 15 * 60 * 1000
    return round(diff_ms / q) * q


def detect_device_offset(record_events, history):
    """Device naive time - UTC, using the device's 'print finish' vs history finishTime."""
    finishes = {}
    current = None
    for e in record_events:
        if e['kind'] == 'print start':
            current = e['d'].get('taskId')
        elif e['kind'] == 'print finish' and current:
            finishes[current] = naive_ms(e['ts'])
            current = None
    diffs = []
    for tid, h in history.items():
        ft = num(h.get('finishTime'))
        if tid in finishes and ft:
            diffs.append(finishes[tid] - ft)
    if diffs:
        diffs.sort()
        return _round_offset(diffs[len(diffs) // 2]), 'auto'
    return 8 * 3600 * 1000, 'default (UTC+8)'


def detect_desktop_offset(desktop_jobs, history):
    diffs = []
    for j in desktop_jobs:
        h = history.get(j['taskId'], {})
        ca = num(h.get('createdAt'))
        if ca:
            diffs.append(naive_ms(j['createdLocal']) - ca)
    if diffs:
        diffs.sort()
        return _round_offset(diffs[len(diffs) // 2]), 'auto'
    return None, 'unknown'

# --------------------------------------------------------------------------
# Main analysis
# --------------------------------------------------------------------------

def analyze(sources):
    stats = {'sources': []}
    rec_texts, ac_texts, desktop, algo_texts = [], [], {}, []
    newest_inkcons = (None, '')
    files = []
    for si, src in enumerate(sources):
        f = src['files']
        name = os.path.basename(src['path'].rstrip('/'))
        files += [f'{name}: {k}' for k in sorted(f)]
        recs = [f[k] for k in f if k.endswith('record/record.log')]
        rec_texts += recs
        ac_texts += [f[k] for k in files_matching(f, r'app-controller/app-controller(\.\d+)?\.log$')]
        algo = [k for k in f if k.endswith('algo-server/server.log')]
        algo_texts += [f[k] for k in algo]
        days = []
        for k in f:
            m = re.search(r'(?:^|/)(\d{4}-\d\d-\d\d)/main\.log$', k)
            if m:
                desktop.setdefault(m.group(1), []).append((f'{name}: {k}', f[k]))
                days.append(m.group(1))
        # the newest cartridge state comes from the export with the latest record.log entry
        last = max((re.findall(r'^\[(\d{4}-[^\]]+)\]', t[-4000:], re.M) or [''])[-1] for t in recs) if recs else ''
        for k in f:
            if k.endswith('inkConsumption.json') and last >= newest_inkcons[1]:
                newest_inkcons = (f[k], last)
        stats['sources'].append({'file': name, 'path': src['path'], 'error': src.get('error'),
                                 'device': bool(recs), 'deviceLast': last or None,
                                 'desktopDays': sorted(set(days)), 'slicerLogs': len(algo)})

    record_text, stats['record'] = merge_device_logs(rec_texts)
    record = parse_record_log(record_text)
    ac_text, stats['appController'] = merge_device_logs(ac_texts)
    ink, moist, counters, boots = parse_app_controller([ac_text])
    # the printer names each cartridge in its ink-tank replies, e.g. "Fluorescent Red"
    device_names = {}
    for m in re.finditer(r'"name":"([^"]+)","remain_ml":[-\d.]+,"serial":"A0000T(\w\w)', ac_text):
        device_names[m.group(2)] = m.group(1)

    desktop_jobs, history = [], {}
    stats['desktopDropped'] = 0
    for day in sorted(desktop):
        kept = dedupe_desktop_logs(desktop[day])
        stats['desktopDropped'] += len(desktop[day]) - len(kept)
        for label, text in kept:
            jobs, hist = parse_desktop_main(text, label)
            desktop_jobs += jobs
            for tid, h in hist.items():
                history.setdefault(tid, {}).update(h)
    # the same job can appear in two computers' logs only if both were open; keep the richest
    by_id = {}
    for j in desktop_jobs:
        prev = by_id.get(j['taskId'])
        if prev is None or len(j.get('elements') or []) > len(prev.get('elements') or []):
            by_id[j['taskId']] = j
    desktop_jobs = list(by_id.values())

    dev_off, dev_off_src = detect_device_offset(record, history)
    desk_off, desk_off_src = detect_desktop_offset(desktop_jobs, history)

    def dev_t(ts):
        return naive_ms(ts) - dev_off

    # ---------- tank identities ----------
    tank_info = {}
    if newest_inkcons[0]:
        try:
            ic = json.loads(newest_inkcons[0])
            for tank in ic.get('inkTanks', []):
                slot = tank['slot']
                carts = tank.get('cartridges', [])
                serial = carts[0]['serial'] if carts else ''
                code = serial[6:8] if len(serial) >= 8 else f'S{slot}'
                tank_info[f'{slot // 6}-{slot % 6}'] = {
                    'code': code, 'name': device_names.get(code) or INK_NAMES.get(code, code), 'serial': serial,
                    'cartridges': carts}
            moist_tanks = ic.get('moistTanks', [])
        except (json.JSONDecodeError, KeyError):
            moist_tanks = []
    else:
        moist_tanks = []

    # ---------- ink level series ----------
    series = {}
    for ts, route, tank, remain, full in ink:
        key = f'{route}-{tank}'
        series.setdefault(key, []).append((dev_t(ts), remain, full))
        tank_info.setdefault(key, {'code': key, 'name': f'Head {route + 1} tank {tank + 1}'})
    for key in series:
        series[key].sort()
        tank_info[key]['full'] = series[key][-1][2]
        tank_info[key]['head'] = int(key.split('-')[0]) + 1
    model = detect_model(tank_info)

    # ---------- events ----------
    events = []
    for e in record:
        if not e['valid']:
            t = None
        else:
            t = dev_t(e['ts'])
        events.append({'t': t, 'rawTs': e['ts'], 'cat': e['cat'], 'kind': e['kind'],
                       'level': e['level'], 'msg': e['msg'], 'd': e['d']})

    # Pair start/finish into durations
    open_ = {}
    for e in events:
        k = e['kind']
        base = re.sub(r' (start|finish)$', '', k)
        if k.endswith(' start') and base not in ('print', 'scan'):
            open_[base] = e
        elif k.endswith(' finish') and base in open_:
            s = open_.pop(base)
            if s['t'] and e['t']:
                s['dur'] = (e['t'] - s['t']) / 1000
            e['paired'] = True
        elif k.startswith('clean finish') and 'clean' in open_:
            s = open_.pop('clean')
            if s['t'] and e['t']:
                s['dur'] = (e['t'] - s['t']) / 1000
            e['paired'] = True

    # ---------- print jobs from device ----------
    jobs = []
    cur = None
    for e in events:
        if e['kind'] == 'print start':
            cur = {'taskId': e['d'].get('taskId'), 'start': e['t'], 'pauses': 0,
                   'cleans': [], 'alarms': [], 'end': None}
            jobs.append(cur)
        elif cur and cur['end'] is None:
            if e['kind'] == 'print finish':
                cur['end'] = e['t']
                cur = None
            elif e['kind'] == 'print pause':
                cur['pauses'] += 1
            elif e['cat'] == 'cleaning' and e['kind'] == 'clean start':
                cur['cleans'].append(e['d'].get('action'))
            elif e['cat'] == 'alarm':
                cur['alarms'].append(e['d'].get('code'))

    genpd = parse_genpd(algo_texts)
    desk_by_id = {j['taskId']: j for j in desktop_jobs}
    for j in jobs:
        tid = j['taskId']
        j['isTest'] = tid in (None, 'taskNoQueue')
        j['dur'] = (j['end'] - j['start']) / 1000 if j['end'] and j['start'] else None
        h = history.get(tid, {})
        j['name'] = h.get('name')
        j['passes'] = num(h.get('totalPrintPass'))
        j['estimate'] = num(h.get('estimateTime'))
        j['status'] = h.get('status')
        j['mode'] = h.get('processingMode')
        if tid in desk_by_id:
            dj = desk_by_id[tid]
            for k in ('size', 'inkPlan', 'elements', 'settings'):
                if dj.get(k):
                    j[k] = dj[k]
            j['name'] = j['name'] or dj.get('name')
            j['passes'] = j['passes'] or dj.get('totalPrintPass')
            j['estimate'] = j['estimate'] or dj.get('estimateTime')
        _apply_layer_inks(j, genpd.get(tid))
        j['inkUsed'] = {}
        j['inkClean'] = {}
        j['inkPlanMl'] = {k: v / 1e9 for k, v in (j.get('inkPlan') or {}).items()}

    # ---------- ink usage attribution ----------
    usage_events = []   # every drop between consecutive snapshots, tagged
    refills = []
    for key, pts in series.items():
        for (t0, r0, _), (t1, r1, _) in zip(pts, pts[1:]):
            d = r0 - r1
            if d < -1:
                refills.append({'t': t1, 'tank': key, 'from': r0, 'to': r1})
            elif d > 1e-4:
                src_, ji = _attribute(t0, t1, jobs, events)
                usage_events.append({'t': t1, 'tank': key, 'ml': d, 'src': src_, 'job': ji})
                if ji is not None:
                    u = jobs[ji]['inkClean' if src_.startswith('clean') else 'inkUsed']
                    u[key] = u.get(key, 0) + d

    counters_latest = counters[-1][1] if counters else {}
    config_all = {}
    for _, c in counters:
        config_all.update(c)
    head_timers = parse_head_timers(ac_text, dev_t, config_all)
    # line-fill amounts the printer deducts when it first fills each ink line
    line_fills = [{'t': dev_t(m.group(1)), 'kind': m.group(2), 'tank': f'{int(m.group(3)) // 6}-{int(m.group(3)) % 6}',
                   'ml': float(m.group(4))}
                  for m in LINE_FILL_RX.finditer(ac_text) if valid_clock(m.group(1))]
    counters_series = [(dev_t(ts), c) for ts, c in counters]

    moist_series = {}
    for ts, idx, remain, full in moist:
        moist_series.setdefault(str(idx), []).append((dev_t(ts), remain, full))

    boot_ts = sorted(set(boots))
    return {
        'generated': datetime.now(timezone.utc).isoformat(),
        'deviceOffsetMs': dev_off, 'deviceOffsetSource': dev_off_src,
        'desktopOffsetMs': desk_off, 'desktopOffsetSource': desk_off_src,
        'firmware': next((e['d'].get('version') for e in events if e['kind'] == 'upgrade completed'), None),
        'tanks': tank_info,
        'model': model,
        'inkSeries': series,
        'moistSeries': moist_series,
        'moistTanks': moist_tanks,
        'usage': usage_events,
        'refills': refills,
        'jobs': jobs,
        'events': [e for e in events],
        'counters': counters_latest,
        'headTimers': head_timers,
        'lineFills': line_fills,
        'countersSeries': counters_series[::max(1, len(counters_series) // 200)],
        'serviceStarts': len(boot_ts),
        'files': files,
        'stats': stats,
    }


def detect_model(tank_info):
    """Which Omni O1 variant this is, from the cartridges on the second head:
    DTG inks = UV/DT, fluorescent / flexible / rigid white = Dual-UV."""
    head2 = {v.get('code') for k, v in tank_info.items() if k.startswith('1-')}
    if head2 & {'DC', 'DM', 'DY', 'DK', 'DS'}:
        return {'name': 'Omni O1 UV/DT', 'basis': 'DTG cartridges on head 2'}
    if head2 & {'UH', 'UR', 'US', 'UD'}:
        return {'name': 'Omni O1 Dual-UV', 'basis': 'fluorescent and white UV cartridges on head 2'}
    return {'name': None, 'basis': None}


def _apply_layer_inks(job, genpd_els):
    """Give each element its per-layer inks: from the slicer log when present,
    otherwise inferred from the element's ink mode (V = varnish)."""
    els = job.get('elements') or []
    if genpd_els:
        known = {e['key'] for e in els}
        for key in genpd_els:
            if key not in known:
                els.append({'key': key, 'layers': []})
        job['elements'] = els
    for e in els:
        if genpd_els and e['key'] in genpd_els:
            e['inks'] = genpd_els[e['key']]
            e['inkSource'] = 'slicer'
        elif e.get('inkMode') == 'V':
            e['inks'] = ['V'] * sum(l.get('count', 1) for l in e.get('layers', []))
            e['inkSource'] = 'ink mode'


def _attribute(t0, t1, jobs, events):
    """Decide what consumed the ink that disappeared between snapshots t0 and t1.

    Returns (source label, index of the job it happened in or None).
    A clean other than the periodic in-print "flash" spit wins, since the
    printer snapshots right after each clean; flash cleans are interleaved with
    printing every few minutes, so that ink stays with the print."""
    clean = None
    maint = None
    for e in events:
        if e['t'] and t0 < e['t'] <= t1:
            if e['cat'] == 'cleaning' and e['kind'] == 'clean start' and e['d'].get('action') != 'flash':
                clean = e
            elif (e['cat'] == 'cleaning' and e['kind'] == 'clean start') or e['cat'] == 'white-ink':
                maint = e
    job = None
    for i, j in enumerate(jobs):
        if j['start'] and j['end'] and j['start'] - 1 <= t1 <= j['end'] + 60000:
            job = i
    if clean:
        in_print = clean['d'].get('inPrint')
        return 'clean: ' + (clean['d'].get('action') or '?'), (job if in_print else None)
    if job is not None:
        return ('test print' if jobs[job]['isTest'] else 'print'), job
    if maint:
        if maint['cat'] == 'cleaning':
            return 'clean: ' + (maint['d'].get('action') or '?'), None
        return 'white maintenance', None
    return 'other', None

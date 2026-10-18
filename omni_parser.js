/* Parse xTool Omni (DT002) log bundles into structured data, in the browser.
 *
 * A JavaScript port of omni_parser.py with the same behaviour and output; keep
 * the two in step. Nothing here touches the network: files are read from the
 * user's disk by the page and decoded in memory.
 *
 * Needs fflate (window.fflate) for zip and gzip; tar is read here.
 */
(function (root) {
'use strict';

const TEXT_EXT = ['.log', '.json', '.txt'];

// Fallback names (Omni O1 Dual-UV) when the logs don't include the printer's own names.
const INK_NAMES = {
  UC: 'UV Cyan', UM: 'UV Magenta', UY: 'UV Yellow', UK: 'UV Black',
  UV: 'UV Varnish', UW: 'UV White', UH: 'Fluorescent Yellow', UR: 'Fluorescent Red',
  US: 'Flexible White', UD: 'Rigid White',
};

// --------------------------------------------------------------------------
// Python-compatible helpers
// --------------------------------------------------------------------------

const has = (o, k) => o != null && Object.prototype.hasOwnProperty.call(o, k);
const get = (o, k, def = null) => (has(o, k) ? o[k] : def);
const setdefault = (m, k, def) => { if (!m.has(k)) m.set(k, def); return m.get(k); };
const truthy = v => !(v == null || v === false || v === 0 || v === '' ||
  (Array.isArray(v) && !v.length) || (typeof v === 'object' && !Array.isArray(v) && !(v instanceof Map) && !Object.keys(v).length) ||
  (v instanceof Map && !v.size));
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const cmpTuple = (a, b) => { for (let i = 0; i < Math.min(a.length, b.length); i++) { const c = cmp(a[i], b[i]); if (c) return c; } return a.length - b.length; };
const stripChars = (s, chars) => { let a = 0, b = s.length; while (a < b && chars.includes(s[a])) a++; while (b > a && chars.includes(s[b - 1])) b--; return s.slice(a, b); };
const basename = p => p.replace(/\/+$/, '').split('/').pop();

// str.splitlines()
function splitlines(s) {
  if (!s) return [];
  const parts = s.split(/\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]/);
  if (parts[parts.length - 1] === '') parts.pop();
  return parts;
}

// Python round(): halves go to the even neighbour
function pyRound(x) {
  const f = Math.floor(x), d = x - f;
  if (d > 0.5) return f + 1;
  if (d < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

// float(s) or null
const FLOAT_RX = /^[+-]?(?:(?:\d(?:_?\d)*)?\.?\d(?:_?\d)*|\d(?:_?\d)*\.)(?:[eE][+-]?\d(?:_?\d)*)?$/;
function num(s) {
  if (s == null) return null;
  if (typeof s === 'number') return s;
  if (typeof s === 'boolean') return s ? 1 : 0;
  if (typeof s !== 'string') return null;
  const t = s.trim();
  if (FLOAT_RX.test(t)) return parseFloat(t.replace(/_/g, ''));
  const l = t.toLowerCase().replace(/^[+-]/, '');
  if (l === 'inf' || l === 'infinity') return t[0] === '-' ? -Infinity : Infinity;
  if (l === 'nan') return NaN;
  return null;
}

// --------------------------------------------------------------------------
// Archive loading
// --------------------------------------------------------------------------

function isTextName(name) {
  const base = name.split('/').pop();
  if (name.includes('__MACOSX/') || base.startsWith('._')) return false;   // macOS resource-fork clutter in zips
  return TEXT_EXT.some(e => base.endsWith(e)) || /\.log\.\d+$/.test(base);
}

// files that can never be archives or logs; skipped unread to save time
const SKIP_EXT = /\.(png|jpe?g|bmp|gif|webp|tiff?|mp4|mov)$/i;

const decoder = new TextDecoder('utf-8');
const isZip = d => d.length >= 4 && d[0] === 0x50 && d[1] === 0x4b && d[2] === 0x03 && d[3] === 0x04;
const isGzip = d => d.length >= 2 && d[0] === 0x1f && d[1] === 0x8b;
const isTar = d => d.length >= 262 && String.fromCharCode(...d.subarray(257, 262)) === 'ustar';

function tarString(b, off, len) {
  let end = off;
  while (end < off + len && b[end]) end++;
  return decoder.decode(b.subarray(off, end));
}
function tarNumber(b, off, len) {
  if (b[off] & 0x80) {   // base-256
    let v = 0;
    for (let i = off + 1; i < off + len; i++) v = v * 256 + b[i];
    return v;
  }
  return parseInt(tarString(b, off, len).trim() || '0', 8) || 0;
}

// Minimal tar reader: regular files with ustar prefix, GNU long names and pax paths.
function readTar(data) {
  const out = [];
  let off = 0, longName = null, paxPath = null;
  while (off + 512 <= data.length) {
    const h = data.subarray(off, off + 512);
    if (h.every(x => x === 0)) break;
    let name = tarString(h, 0, 100);
    const size = tarNumber(h, 124, 12);
    const type = String.fromCharCode(h[156] || 0x30);
    if (tarString(h, 257, 5) === 'ustar') {
      const prefix = tarString(h, 345, 155);
      if (prefix) name = prefix + '/' + name;
    }
    const body = data.subarray(off + 512, off + 512 + size);
    off += 512 + Math.ceil(size / 512) * 512;
    if (type === 'L') { longName = tarString(body, 0, body.length); continue; }
    if (type === 'x') {
      const m = /(?:^|\n)\d+ path=([^\n]*)\n/.exec(decoder.decode(body));
      if (m) paxPath = m[1];
      continue;
    }
    if (type === 'g') continue;
    if (longName != null) { name = longName; longName = null; }
    if (paxPath != null) { name = paxPath; paxPath = null; }
    if (type === '0' || type === '\0' || type === '7') out.push([name.replace(/\/+$/, ''), body]);
  }
  return out;
}

// Recursively expand zip / gzip / tar blobs into Map{path: text}.
async function walk(data, name, out, tick) {
  await tick(name);
  if (isZip(data)) {
    const prefix = name ? name + '/' : '';
    const order = [];
    const entries = fflate.unzipSync(data, {
      filter: f => { if (f.name.endsWith('/') || SKIP_EXT.test(f.name)) return false; order.push(f.name); return true; },
    });
    for (const n of order) if (entries[n]) await walk(entries[n], prefix + n, out, tick);
    return;
  }
  if (isGzip(data)) {
    let raw;
    try { raw = fflate.gunzipSync(data); } catch (e) { return; }
    await walk(raw, name.replace(/\.gz$/, ''), out, tick);
    return;
  }
  if (isTar(data)) {
    const prefix = name ? name + '/' : '';
    for (const [n, body] of readTar(data)) await walk(body, prefix + n.replace(/^[./]+/, ''), out, tick);
    return;
  }
  if (isTextName(name)) out.set(name, decoder.decode(data));
}

const BUNDLE_EXT = ['.gz', '.zip'];

/* Group picked / dropped files into sources, like find_inputs() in Python.
 * items: [{path: 'folder/sub/file.gz' or 'file.gz', file: File}], where a
 * leading folder means the user picked or dropped that folder.
 * A folder that is itself an xTool Studio logs folder (date sub-folders) is one
 * source; other folders contribute the .gz / .zip bundles inside them. */
function findInputs(items) {
  const byTop = new Map();
  const sources = [];
  for (const it of items) {
    const parts = it.path.split('/');
    if (parts.length === 1) sources.push({ path: it.path, files: [it] });
    else setdefault(byTop, parts[0], []).push(it);
  }
  for (const [top, list] of byTop) {
    // a logs folder can be picked directly, or be the only thing inside the picked folder
    const rel = list.map(it => it.path.split('/').slice(1));
    const isLogs = r => r.length > 1 && /^\d{4}-\d\d-\d\d$/.test(r[0]);
    if (rel.some(isLogs)) {
      sources.push({ path: top, files: list.map((it, i) => ({ ...it, rel: rel[i].join('/') })) });
      continue;
    }
    // look one level down for a logs folder (e.g. a dropped folder containing "logs")
    const sub = new Map();
    for (let i = 0; i < list.length; i++) if (rel[i].length > 2 && /^\d{4}-\d\d-\d\d$/.test(rel[i][1])) setdefault(sub, rel[i][0], []).push(i);
    const used = new Set();
    for (const [name, idx] of sub) {
      if (name.startsWith('.')) continue;
      const pre = top + '/' + name + '/';
      const inside = list.map((it, i) => [it, i]).filter(([it]) => it.path.startsWith(pre));
      inside.forEach(([, i]) => used.add(i));
      sources.push({ path: top + '/' + name, files: inside.map(([it]) => ({ ...it, rel: it.path.slice(pre.length) })) });
    }
    list.forEach((it, i) => {
      if (used.has(i)) return;
      const r = rel[i];
      if (r.slice(0, -1).some(d => d.startsWith('.'))) return;
      const n = r[r.length - 1];
      if (BUNDLE_EXT.some(e => n.toLowerCase().endsWith(e)) && !n.startsWith('.')) sources.push({ path: it.path, files: [it] });
    });
  }
  const seen = new Map();
  for (const s of sources) seen.set(s.path, s);
  return [...seen.values()].sort((a, b) => cmp(a.path, b.path));
}

async function loadSource(src, tick) {
  const part = new Map();
  if (src.files.length === 1 && src.files[0].rel == null) {
    const data = new Uint8Array(await src.files[0].file.arrayBuffer());
    if (isZip(data)) await walk(data, '', part, tick);
    else await walk(data, basename(src.path), part, tick);
  } else {
    for (const it of src.files) {
      if (SKIP_EXT.test(it.rel)) continue;
      await walk(new Uint8Array(await it.file.arrayBuffer()), it.rel, part, tick);
    }
  }
  return part;
}

/* items as for findInputs; onProgress(fraction 0..1, label) */
async function loadSources(items, onProgress = () => {}) {
  const inputs = findInputs(items);
  const total = inputs.reduce((a, s) => a + s.files.reduce((b, f) => b + (f.file.size || 0), 0), 0) || 1;
  let done = 0, last = 0;
  const sources = [];
  for (const src of inputs) {
    const size = src.files.reduce((b, f) => b + (f.file.size || 0), 0);
    const tick = async label => {
      onProgress(Math.min(0.99, done / total), basename(src.path) + (label ? ': ' + label : ''));
      if (performance.now() - last > 60) { last = performance.now(); await new Promise(r => setTimeout(r, 0)); }
    };
    try {
      sources.push({ path: src.path, files: await loadSource(src, tick) });
    } catch (exc) {
      sources.push({ path: src.path, files: new Map(), error: String(exc && exc.message || exc) });
    }
    done += size;
  }
  return sources;
}

// ---- merging overlapping exports -------------------------------------------

const REC_START = /^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\]/;

/* Union of timestamped records from several copies / rotations of the same
 * device log, with exact duplicates removed and everything in time order.
 * Records written before the printer synced its clock (1970-...) are kept next
 * to the first real timestamp that follows them in their own file. */
function mergeDeviceLogs(texts) {
  const seen = new Set();
  const items = [];
  const stats = { records: 0, duplicates: 0 };
  texts.forEach((text, fi) => {
    const recs = [];
    for (const line of splitlines(text)) {
      if (REC_START.test(line) || !recs.length) recs.push([line]);
      else recs[recs.length - 1].push(line);
    }
    const keyed = new Array(recs.length);
    let anchor = '9999';
    for (let i = recs.length - 1; i >= 0; i--) {
      const m = REC_START.exec(recs[i][0]);
      if (m && !m[1].startsWith('1970')) anchor = m[1];
      keyed[i] = anchor;
    }
    const occurrence = new Map();
    recs.forEach((rec, i) => {
      const body = rec.join('\n');
      stats.records++;
      // the n-th copy of a record within a file only matches the n-th copy in
      // another file, so lines a file legitimately repeats are kept
      const n = (occurrence.get(body) || 0) + 1;
      occurrence.set(body, n);
      const key = n + '\u0000' + body;
      if (seen.has(key)) { stats.duplicates++; return; }
      seen.add(key);
      items.push([keyed[i], fi, i, body]);
    });
  });
  items.sort((a, b) => cmp(a[0], b[0]) || a[1] - b[1] || a[2] - b[2]);
  return [items.map(it => it[3]).join('\n'), stats];
}

/* entries: [[label, text]] for one date's main.log from various exports.
 * A copy that is a prefix of (or equal to) another copy is an older export of
 * the same computer's log and is dropped; different computers are all kept. */
function dedupeDesktopLogs(entries) {
  entries = entries.slice().sort((a, b) => b[1].length - a[1].length);   // stable, like sorted()
  const kept = [];
  for (const [label, text] of entries) {
    if (kept.some(k => k[1].startsWith(text))) continue;
    kept.push([label, text]);
  }
  return kept;
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

const DEV_LINE = /^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\] \[-(\w)-\] (?:\[([^\]]*)\] )?: ?(.*)$/;
const DESK_LINE = /^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\] \[(\w+)\]\s+(.*)$/;

// Naive timestamp string -> ms as if it were UTC (offset applied later); same float maths as Python.
function naiveMs(ts) {
  const m = /^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\.(\d{1,6})/.exec(ts.slice(0, 23));
  const secs = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) / 1000;
  const micro = +(m[7] + '000000').slice(0, 6);
  return Math.trunc((secs * 1e6 + micro) / 1e6 * 1000);
}

const validClock = ts => !ts.startsWith('1970');

// 'a=1 b=two' -> {a: '1', b: 'two'}
function kv(text) {
  const out = {};
  for (const m of text.matchAll(/(\w+)=([^\s,]+)/g)) out[m[1]] = m[2];
  return out;
}

function rotationKey(name) {
  // app-controller.2.log is older than .1.log which is older than .log
  const m = /\.(\d+)\.log$/.exec(name);
  return [name.replace(/\.\d+\.log$/, '.log'), -(m ? parseInt(m[1], 10) : 0)];
}

function filesMatching(files, pattern) {
  return [...files.keys()].filter(k => pattern.test(k)).sort((a, b) => cmpTuple(rotationKey(a), rotationKey(b)));
}

// Python str.partition
function partition(s, sep) {
  const i = s.indexOf(sep);
  return i < 0 ? [s, '', ''] : [s.slice(0, i), sep, s.slice(i + sep.length)];
}

// --------------------------------------------------------------------------
// Device: record.log (the printer's own high-level event journal)
// --------------------------------------------------------------------------

function classifyRecord(msg) {
  const [head, , rest] = partition(msg, ', ');
  const d = kv(rest);
  if (head.startsWith('print ')) return ['print', head, d];
  if (head.startsWith('clean ')) {
    d.inPrint = msg.includes('(in print)');
    const m = /action=(.+?) head=(\S+)/.exec(msg);
    if (m) { d.action = m[1]; d.head = m[2]; }
    return ['cleaning', head, d];
  }
  if (head.startsWith('white ') || head === 'low power maintain') return ['white-ink', head, d];
  if (['boot', 'time synced', 'upgrade completed'].includes(head)) return ['power', head, d];
  if (head.includes('sleep') || head.includes('low power')) return ['power', head, d];
  if (['calibration', 'measure', 'scan'].some(p => head.startsWith(p))) return ['calibration', head, d];
  if (head.startsWith('alarm')) {
    const m = /^alarm (\S+) (\S+)/.exec(msg);
    if (m) { d.code = m[1]; d.name = m[2]; }
    d.text = rest;
    return ['alarm', 'alarm', d];
  }
  if (head === 'config set') return ['config', head, { text: rest }];
  return ['other', head, { text: rest }];
}

function parseRecordLog(text) {
  const events = [];
  for (const line of splitlines(text)) {
    const m = DEV_LINE.exec(line);
    if (!m) continue;
    const ts = m[1], lvl = m[2], msg = m[4];
    const [cat, kind, d] = classifyRecord(msg);
    events.push({ ts, valid: validClock(ts), level: lvl, cat, kind, msg, d });
  }
  return events;
}

// --------------------------------------------------------------------------
// Device: app-controller logs (ink levels, counters, boots)
// --------------------------------------------------------------------------

const INK_RX = /ink_tank_status":(\{[^}]*\})/;
const MOIST_RX = /moisture_tank_status":(\{[^}]*\})/;
const BATCH_RX = /batch config: (\{.*\})/;

function parseAppController(texts) {
  const ink = [], moist = [], counters = [], boots = [];
  for (const text of texts) {
    for (const line of splitlines(text)) {
      if (!(line.includes('ink_tank_status') || line.includes('moisture_tank_status') ||
            line.includes('batch config') || line.includes('STARTED pid'))) continue;
      const m = DEV_LINE.exec(line);
      if (!m) continue;
      const ts = m[1], msg = m[4];
      if (msg.includes('STARTED pid')) { boots.push(ts); continue; }
      if (!validClock(ts)) continue;
      let x;
      if ((x = INK_RX.exec(msg))) {
        const j = JSON.parse(x[1]);
        if (truthy(j.exist)) ink.push([ts, j.route, j.tank_id, j.remain_ml, j.full_ml]);
      } else if ((x = MOIST_RX.exec(msg))) {
        const j = JSON.parse(x[1]);
        if (truthy(j.exist)) moist.push([ts, j.index, j.remain_ml, j.full_ml]);
      } else if ((x = BATCH_RX.exec(msg))) {
        try { counters.push([ts, JSON.parse(x[1])]); } catch (e) { /* skip */ }
      }
    }
  }
  return [ink, moist, counters, boots];
}

// ---- per-head time counters ------------------------------------------------

const HEAD_STAT_RX = /AppHeadTimeStat(?:::add| on print start:) head=(\d) counter=(\w+) (?:addSec=\d+ total=(\d+)|value=(\d+))/;
const THRESH_RX = /cache in-print clean thresholds: (.*)$/;
const SEVEN_DAYS = 7 * 24 * 3600;
const LINE_FILL_RX = /^\[(\d{4}-[^\]]+)\].*?\b(ink|white) pipeline fill consumed: route=\d+, ch=\d+, slot=(\d+), ml=([\d.]+)/gm;
const RATE_WINDOW_MS = 48 * 3600 * 1000;

/* The printer keeps per-head time counters (seconds): Idle (time not working
   since the head's last standard clean; keeps counting while asleep), NoRun,
   WhiteNoRun, ColorNoRun, FluNoRun, MoistNoRun, Moisrizing, ContinousWork.
   They are only logged when the printer changes state, so values are
   snapshots. Also returns the clean thresholds the firmware reports, and an
   estimate of when each head's Idle reaches 7 days, assuming the printer
   stays idle the same share of the time as in the last 48 h of the logs.
   (The 7-day moisturizing trigger is not shown in any log; it is an assumption.)
   Mirrors parse_head_timers in omni_parser.py. */
function parseHeadTimers(acText, devT, config) {
  const heads = {};
  const thresholds = {};
  for (const line of splitlines(acText)) {
    if (line.includes('AppHeadTimeStat')) {
      const m = DEV_LINE.exec(line);
      if (!m || !validClock(m[1])) continue;
      const s = HEAD_STAT_RX.exec(m[4]);
      if (!s) continue;
      const h = heads[s[1]] = heads[s[1]] || { counters: {}, idle: [] };
      const val = parseInt(s[3] != null ? s[3] : s[4], 10);
      const t = devT(m[1]);
      h.counters[s[2]] = { value: val, t };
      if (s[2] === 'Idle') h.idle.push([t, val]);
    } else if (line.includes('cache in-print clean thresholds')) {
      const x = THRESH_RX.exec(line);
      if (x) for (const k of x[1].matchAll(/(\w+(?:\([^)]*\))?)=([\d/]+)/g)) thresholds[k[1]] = k[2];
    }
  }
  if (config.uvPrintNorunTimeSwitchMoisturizing != null)
    thresholds.uvPrintNorunTimeSwitchMoisturizing = String(config.uvPrintNorunTimeSwitchMoisturizing);

  for (const h of Object.values(heads)) {
    const pts = h.idle;
    // last reset: a reading lower than the one before it
    h.lastReset = null;
    for (let i = 0; i + 1 < pts.length; i++) {
      const [, v0] = pts[i], [t1, v1] = pts[i + 1];
      if (v1 < v0) h.lastReset = v1 ? t1 - v1 * 1000 : t1;
    }
    if (!pts.length) { h.estimate = null; continue; }
    const end = pts[pts.length - 1][0];
    const start = end - RATE_WINDOW_MS;
    let idleSec = 0;
    for (let i = 0; i + 1 < pts.length; i++) {
      const [t0, v0] = pts[i], [t1, v1] = pts[i + 1];
      if (t1 <= start || t1 === t0) continue;
      let d = v1 >= v0 ? v1 - v0 : v1;
      if (t0 < start) d *= (t1 - start) / (t1 - t0);
      idleSec += d;
    }
    const span = (end - Math.max(start, pts[0][0])) / 1000;
    const frac = span > 0 ? Math.min(1, idleSec / span) : null;
    const latest = pts[pts.length - 1][1];
    const remaining = SEVEN_DAYS - latest;
    h.estimate = {
      windowStart: Math.max(start, pts[0][0]), windowEnd: end,
      idleFraction: frac, latest, latestT: end,
      remainingSec: remaining,
      reachT: frac && remaining > 0 ? end + remaining / frac * 1000 : (remaining <= 0 ? end : null),
    };
  }
  return { heads, thresholds, target: SEVEN_DAYS };
}

// --------------------------------------------------------------------------
// Desktop (xTool Studio) main.log: job configuration
// --------------------------------------------------------------------------

// Grab simple key: value pairs from a JS-style object dump.
function jsObjFields(s) {
  const out = {};
  for (const m of s.matchAll(/(\w+): ('[^']*'|-?[\d.]+|true|false)/g)) if (!has(out, m[1])) out[m[1]] = stripChars(m[2], "'");
  return out;
}

// lines[i] starts a multi-line JSON log entry '[ts] [info] ... [' -> parsed list.
function collectJsonBlock(lines, i) {
  const first = lines[i];
  const start = first.indexOf('[', first.includes(']=>') ? first.indexOf(']=>') : 30);
  const buf = [first.slice(start)];
  let j = i + 1;
  while (j < lines.length && !DESK_LINE.test(lines[j])) { buf.push(lines[j]); j++; }
  try { return [JSON.parse(buf.join('\n')), j]; } catch (e) { return [null, j]; }
}

const ELEMENT_RX = new RegExp(
  "id: '([0-9a-f-]{36})', isCompensate: \\w+, printThickness: ([\\d.]+), layerThicknessMm: ([\\d.]+), " +
  "inkMode: '(\\w*)', processingType: '(\\w*)', varnishType: '(\\w*)', whiteInkType: '(\\w*)'" +
  '(?:, inverted: \\w+, printPos: \\{ x: ([-\\d.]+), y: ([-\\d.]+), width: ([\\d.]+), height: ([\\d.]+) \\})?', 'g');

/* One entry per task created from this computer, with the most recent
 * preparation details that preceded it. */
function parseDesktopMain(text, fileLabel) {
  const lines = splitlines(text);
  const jobs = [];
  const tasksSeen = new Map();
  let ctx = {};
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const m = DESK_LINE.exec(line);
    if (!m) { i++; continue; }
    const ts = m[1];
    if (lines[i + 1 < lines.length ? i + 1 : i].includes('"write slice params json success"') && line.trimEnd().endsWith('[')) {
      const [block, nxt] = collectJsonBlock(lines, i);
      if (truthy(block) && block.length > 1) ctx.slice = Array.isArray(block[1]) && block[1].length > 1 ? block[1][1] : null;
      i = nxt;
      continue;
    }
    if (line.includes("'createdPrintSourceImage'")) ctx.bbox = jsObjFields(line.split("'createdPrintSourceImage'").slice(1).join("'createdPrintSourceImage'"));
    else if (line.includes("'write prn param json success'")) ctx.prn = line;
    else if (line.includes("'computeInkVolume workDes'")) ctx.inkplan = jsObjFields(line.slice(line.indexOf('workDes') + 7));
    else if (line.includes("'step:createTask.params'")) ctx.params = jsObjFields(line.slice(line.indexOf('createTask.params') + 17));
    else if (line.includes('step:createTask.apiOk')) {
      const tid = /taskId: '(\w+)'/.exec(line);
      if (tid) { jobs.push(buildDesktopJob(tid[1], ts, ctx)); ctx = {}; }
    }
    // Task history summaries reported by the device (name, passes, epoch times)
    if (line.includes('deviceHisTasks') || (line.includes('createdAt:') && line.includes('totalPrintPass'))) {
      for (const om of line.matchAll(/\{ clientId: '[^']*'[^{}]*\}/g)) {
        const f = jsObjFields(om[0]);
        if (has(f, 'id')) {
          const prev = tasksSeen.get(f.id) || {};
          for (const [k, v] of Object.entries(f)) if (v !== '' && v != null) prev[k] = v;
          tasksSeen.set(f.id, prev);
        }
      }
    }
    i++;
  }
  for (const j of jobs) j.desktopFile = fileLabel;
  return [jobs, tasksSeen];
}

const GENPD_RX = /\[GenPd\] eleIndex=(\d+), imgIndex=(\d+), color: (\w+),.*?\/atomm\/DT002\/task\/(\w+)\/+uv\/\d+_([0-9a-f]{8})\//g;

/* xTool Studio's slicer (algo-server) logs which ink each layer image is
 * printed with: HW/SW/W = white, V = varnish, C = colour. This is the only
 * place varnish layers can be told apart from white ones, since the slicer
 * parameters call both 'white'. Returns Map{taskId: {elementKey: [ink, ...]}}. */
function parseGenpd(texts) {
  const out = new Map();
  for (const text of texts) {
    for (const m of text.matchAll(GENPD_RX)) {
      const ele = parseInt(m[1], 10), img = parseInt(m[2], 10), ink = m[3], tid = m[4], key = m[5];
      setdefault(setdefault(out, tid, new Map()), key, new Map()).set(img, [ele, ink]);
    }
  }
  const res = new Map();
  for (const [tid, els] of out) {
    const o = {};
    for (const [k, v] of els) o[k] = [...v.keys()].sort((a, b) => a - b).map(i => v.get(i)[1]);
    res.set(tid, o);
  }
  return res;
}

function buildDesktopJob(taskId, ts, ctx) {
  const job = { taskId, createdLocal: ts };
  const p = ctx.params || {};
  job.name = get(p, 'name');
  job.totalPrintPass = num(get(p, 'totalPrintPass'));
  job.estimateTime = num(get(p, 'estimateTime'));
  job.processingMode = get(p, 'processingMode');
  const bb = ctx.bbox || {};
  if (truthy(bb)) job.size = { w: num(get(bb, 'width')), h: num(get(bb, 'height')), x: num(get(bb, 'x')), y: num(get(bb, 'y')) };
  const plan = ctx.inkplan || {};
  job.inkPlan = {};
  for (const [k, v] of Object.entries(plan)) if (k.startsWith('Ink_') && truthy(num(v))) job.inkPlan[k.slice(4)] = num(v);
  const prn = ctx.prn || '';
  const elems = new Map();
  for (const e of prn.matchAll(ELEMENT_RX)) {
    const eid = e[1];
    elems.set(eid.slice(0, 8), {
      id: eid, printThickness: num(e[2]), layerThicknessMm: num(e[3]),
      inkMode: e[4], processingType: e[5], varnishType: e[6], whiteInkType: e[7],
      pos: e[10] ? { x: num(e[8]), y: num(e[9]), w: num(e[10]), h: num(e[11]) } : null,
    });
  }
  for (const k of ['precision', 'speed', 'uvLightPower', 'inkSavingMode', 'halfToningType', 'renderingIntent', 'highSpeedPrint']) {
    const mm = new RegExp(k + ": ('[^']*'|\"[^\"]*\"|[\\w.]+)").exec(prn);
    if (mm) (job.settings = job.settings || {})[k] = stripChars(mm[1], "'\"");
  }
  const mm = /precision: '\{ x: (\d+), y: (\d+) \}'/.exec(prn);
  if (mm) (job.settings = job.settings || {}).dpi = `${mm[1]}x${mm[2]}`;
  // Layer stack per element from the slicer parameters
  const elements = [];
  for (const el of ctx.slice || []) {
    const key = String(get(el, 'input_dir', '')).split('_').pop().slice(0, 8);
    const layers = (get(el, 'layer_parameters', []) || []).map(lp => ({ mode: get(lp, 'color_mode'), count: get(lp, 'slice_count', 1), slice: get(lp, 'slice_mode') }));
    const info = { ...(elems.get(key) || {}) };
    info.key = key;
    info.layers = layers;
    elements.push(info);
  }
  // Elements known from prn param but not in slice (rare)
  for (const [key, info] of elems) if (!elements.some(e => e.key === key)) elements.push({ ...info, key, layers: [] });
  job.elements = elements;
  return job;
}

// --------------------------------------------------------------------------
// Clock offsets
// --------------------------------------------------------------------------

function roundOffset(diffMs) {
  const q = 15 * 60 * 1000;
  return pyRound(diffMs / q) * q;
}

// Device naive time - UTC, using the device's 'print finish' vs history finishTime.
function detectDeviceOffset(recordEvents, history) {
  const finishes = new Map();
  let current = null;
  for (const e of recordEvents) {
    if (e.kind === 'print start') current = get(e.d, 'taskId');
    else if (e.kind === 'print finish' && truthy(current)) { finishes.set(current, naiveMs(e.ts)); current = null; }
  }
  const diffs = [];
  for (const [tid, h] of history) {
    const ft = num(get(h, 'finishTime'));
    if (finishes.has(tid) && truthy(ft)) diffs.push(finishes.get(tid) - ft);
  }
  if (diffs.length) { diffs.sort((a, b) => a - b); return [roundOffset(diffs[Math.floor(diffs.length / 2)]), 'auto']; }
  return [8 * 3600 * 1000, 'default (UTC+8)'];
}

function detectDesktopOffset(desktopJobs, history) {
  const diffs = [];
  for (const j of desktopJobs) {
    const h = history.get(j.taskId) || {};
    const ca = num(get(h, 'createdAt'));
    if (truthy(ca)) diffs.push(naiveMs(j.createdLocal) - ca);
  }
  if (diffs.length) { diffs.sort((a, b) => a - b); return [roundOffset(diffs[Math.floor(diffs.length / 2)]), 'auto']; }
  return [null, 'unknown'];
}

// --------------------------------------------------------------------------
// Main analysis
// --------------------------------------------------------------------------

function analyze(sources) {
  const stats = { sources: [] };
  const recTexts = [], acTexts = [], algoTexts = [];
  const desktop = new Map();
  let newestInkcons = [null, ''];
  let files = [];
  for (const src of sources) {
    const f = src.files;
    const name = basename(src.path);
    files = files.concat([...f.keys()].sort().map(k => `${name}: ${k}`));
    const recs = [...f.keys()].filter(k => k.endsWith('record/record.log')).map(k => f.get(k));
    recTexts.push(...recs);
    acTexts.push(...filesMatching(f, /app-controller\/app-controller(\.\d+)?\.log$/).map(k => f.get(k)));
    const algo = [...f.keys()].filter(k => k.endsWith('algo-server/server.log'));
    algoTexts.push(...algo.map(k => f.get(k)));
    const days = [];
    for (const k of f.keys()) {
      const m = /(?:^|\/)(\d{4}-\d\d-\d\d)\/main\.log$/.exec(k);
      if (m) { setdefault(desktop, m[1], []).push([`${name}: ${k}`, f.get(k)]); days.push(m[1]); }
    }
    // the newest cartridge state comes from the export with the latest record.log entry
    let last = '';
    if (recs.length) {
      last = recs.map(t => {
        const tail = Array.from(t.slice(-8000)).slice(-4000).join('');
        const all = [...tail.matchAll(/^\[(\d{4}-[^\]]+)\]/gm)].map(m => m[1]);
        return all.length ? all[all.length - 1] : '';
      }).reduce((a, b) => (b > a ? b : a));
    }
    for (const k of f.keys()) if (k.endsWith('inkConsumption.json') && last >= newestInkcons[1]) newestInkcons = [f.get(k), last];
    stats.sources.push({ file: name, path: src.path, error: src.error || null, device: recs.length > 0, deviceLast: last || null,
      desktopDays: [...new Set(days)].sort(), slicerLogs: algo.length });
  }

  const [recordText, recordStats] = mergeDeviceLogs(recTexts);
  stats.record = recordStats;
  const record = parseRecordLog(recordText);
  const [acText, acStats] = mergeDeviceLogs(acTexts);
  stats.appController = acStats;
  const [ink, moist, counters, boots] = parseAppController([acText]);
  // the printer names each cartridge in its ink-tank replies, e.g. "Fluorescent Red"
  const deviceNames = {};
  for (const m of acText.matchAll(/"name":"([^"]+)","remain_ml":[-\d.]+,"serial":"A0000T(\w\w)/g)) deviceNames[m[2]] = m[1];

  let desktopJobs = [];
  const history = new Map();
  stats.desktopDropped = 0;
  for (const day of [...desktop.keys()].sort()) {
    const kept = dedupeDesktopLogs(desktop.get(day));
    stats.desktopDropped += desktop.get(day).length - kept.length;
    for (const [label, text] of kept) {
      const [jobs, hist] = parseDesktopMain(text, label);
      desktopJobs = desktopJobs.concat(jobs);
      for (const [tid, h] of hist) Object.assign(setdefault(history, tid, {}), h);
    }
  }
  // the same job can appear in two computers' logs only if both were open; keep the richest
  const byId = new Map();
  for (const j of desktopJobs) {
    const prev = byId.get(j.taskId);
    if (prev == null || (j.elements || []).length > (prev.elements || []).length) byId.set(j.taskId, j);
  }
  desktopJobs = [...byId.values()];

  const [devOff, devOffSrc] = detectDeviceOffset(record, history);
  const [deskOff, deskOffSrc] = detectDesktopOffset(desktopJobs, history);
  const devT = ts => naiveMs(ts) - devOff;

  // ---------- tank identities ----------
  const tankInfo = new Map();
  let moistTanks = [];
  if (newestInkcons[0]) {
    try {
      const ic = JSON.parse(newestInkcons[0]);
      for (const tank of get(ic, 'inkTanks', [])) {
        if (!has(tank, 'slot')) throw new Error('KeyError');
        const slot = tank.slot;
        const carts = get(tank, 'cartridges', []);
        if (carts.length && !has(carts[0], 'serial')) throw new Error('KeyError');
        const serial = carts.length ? carts[0].serial : '';
        const code = serial.length >= 8 ? serial.slice(6, 8) : `S${slot}`;
        tankInfo.set(`${Math.floor(slot / 6)}-${slot % 6}`, { code, name: deviceNames[code] || INK_NAMES[code] || code, serial, cartridges: carts });
      }
      moistTanks = get(ic, 'moistTanks', []);
    } catch (e) {
      moistTanks = [];
    }
  }

  // ---------- ink level series ----------
  const series = new Map();
  for (const [ts, route, tank, remain, full] of ink) {
    const key = `${route}-${tank}`;
    setdefault(series, key, []).push([devT(ts), remain, full]);
    if (!tankInfo.has(key)) tankInfo.set(key, { code: key, name: `Head ${route + 1} tank ${tank + 1}` });
  }
  for (const [key, pts] of series) {
    pts.sort(cmpTuple);
    tankInfo.get(key).full = pts[pts.length - 1][2];
    tankInfo.get(key).head = parseInt(key.split('-')[0], 10) + 1;
  }

  // ---------- events ----------
  const events = record.map(e => ({ t: e.valid ? devT(e.ts) : null, rawTs: e.ts, cat: e.cat, kind: e.kind, level: e.level, msg: e.msg, d: e.d }));

  // Pair start/finish into durations
  const open = new Map();
  for (const e of events) {
    const k = e.kind;
    const base = k.replace(/ (start|finish)$/, '');
    if (k.endsWith(' start') && base !== 'print' && base !== 'scan') open.set(base, e);
    else if (k.endsWith(' finish') && open.has(base)) {
      const s = open.get(base); open.delete(base);
      if (truthy(s.t) && truthy(e.t)) s.dur = (e.t - s.t) / 1000;
      e.paired = true;
    } else if (k.startsWith('clean finish') && open.has('clean')) {
      const s = open.get('clean'); open.delete('clean');
      if (truthy(s.t) && truthy(e.t)) s.dur = (e.t - s.t) / 1000;
      e.paired = true;
    }
  }

  // ---------- print jobs from device ----------
  const jobs = [];
  let cur = null;
  for (const e of events) {
    if (e.kind === 'print start') {
      cur = { taskId: get(e.d, 'taskId'), start: e.t, pauses: 0, cleans: [], alarms: [], end: null };
      jobs.push(cur);
    } else if (cur && cur.end == null) {
      if (e.kind === 'print finish') { cur.end = e.t; cur = null; }
      else if (e.kind === 'print pause') cur.pauses++;
      else if (e.cat === 'cleaning' && e.kind === 'clean start') cur.cleans.push(get(e.d, 'action'));
      else if (e.cat === 'alarm') cur.alarms.push(get(e.d, 'code'));
    }
  }

  const genpd = parseGenpd(algoTexts);
  const deskById = new Map(desktopJobs.map(j => [j.taskId, j]));
  for (const j of jobs) {
    const tid = j.taskId;
    j.isTest = tid == null || tid === 'taskNoQueue';
    j.dur = truthy(j.end) && truthy(j.start) ? (j.end - j.start) / 1000 : null;
    const h = history.get(tid) || {};
    j.name = get(h, 'name');
    j.passes = num(get(h, 'totalPrintPass'));
    j.estimate = num(get(h, 'estimateTime'));
    j.status = get(h, 'status');
    j.mode = get(h, 'processingMode');
    if (deskById.has(tid)) {
      const dj = deskById.get(tid);
      for (const k of ['size', 'inkPlan', 'elements', 'settings']) if (truthy(dj[k])) j[k] = dj[k];
      j.name = truthy(j.name) ? j.name : get(dj, 'name');
      j.passes = truthy(j.passes) ? j.passes : get(dj, 'totalPrintPass');
      j.estimate = truthy(j.estimate) ? j.estimate : get(dj, 'estimateTime');
    }
    applyLayerInks(j, genpd.get(tid));
    j.inkUsed = {};
    j.inkClean = {};
    j.inkPlanMl = {};
    for (const [k, v] of Object.entries(j.inkPlan || {})) j.inkPlanMl[k] = v / 1e9;
  }

  // ---------- ink usage attribution ----------
  const usageEvents = [];   // every drop between consecutive snapshots, tagged
  const refills = [];
  for (const [key, pts] of series) {
    for (let i = 0; i + 1 < pts.length; i++) {
      const [t0, r0] = pts[i], [t1, r1] = pts[i + 1];
      const d = r0 - r1;
      if (d < -1) refills.push({ t: t1, tank: key, from: r0, to: r1 });
      else if (d > 1e-4) {
        const [src, ji] = attribute(t0, t1, jobs, events);
        usageEvents.push({ t: t1, tank: key, ml: d, src, job: ji });
        if (ji != null) {
          const u = jobs[ji][src.startsWith('clean') ? 'inkClean' : 'inkUsed'];
          u[key] = (u[key] || 0) + d;
        }
      }
    }
  }

  const countersLatest = counters.length ? counters[counters.length - 1][1] : {};
  const configAll = {};
  for (const [, c] of counters) Object.assign(configAll, c);
  const headTimers = parseHeadTimers(acText, devT, configAll);
  // line-fill amounts the printer deducts when it first fills each ink line
  const lineFills = [...acText.matchAll(LINE_FILL_RX)].filter(m => validClock(m[1]))
    .map(m => ({ t: devT(m[1]), kind: m[2], tank: `${Math.floor(+m[3] / 6)}-${+m[3] % 6}`, ml: parseFloat(m[4]) }));
  const countersSeries = counters.map(([ts, c]) => [devT(ts), c]);
  const step = Math.max(1, Math.floor(countersSeries.length / 200));

  const moistSeries = {};
  for (const [ts, idx, remain, full] of moist) (moistSeries[String(idx)] = moistSeries[String(idx)] || []).push([devT(ts), remain, full]);

  const bootTs = [...new Set(boots)].sort();
  const fw = events.find(e => e.kind === 'upgrade completed');
  return {
    generated: new Date().toISOString(),
    deviceOffsetMs: devOff, deviceOffsetSource: devOffSrc,
    desktopOffsetMs: deskOff, desktopOffsetSource: deskOffSrc,
    firmware: fw ? get(fw.d, 'version') : null,
    tanks: Object.fromEntries(tankInfo),
    inkSeries: Object.fromEntries(series),
    moistSeries,
    moistTanks,
    usage: usageEvents,
    refills,
    jobs,
    events,
    counters: countersLatest,
    headTimers,
    lineFills,
    countersSeries: countersSeries.filter((_, i) => i % step === 0),
    serviceStarts: bootTs.length,
    files,
    stats,
    sources: stats.sources.map(s => s.file),
  };
}

/* Give each element its per-layer inks: from the slicer log when present,
 * otherwise inferred from the element's ink mode (V = varnish). */
function applyLayerInks(job, genpdEls) {
  const els = job.elements || [];
  if (truthy(genpdEls)) {
    const known = new Set(els.map(e => e.key));
    for (const key of Object.keys(genpdEls)) if (!known.has(key)) els.push({ key, layers: [] });
    job.elements = els;
  }
  for (const e of els) {
    if (truthy(genpdEls) && has(genpdEls, e.key)) {
      e.inks = genpdEls[e.key];
      e.inkSource = 'slicer';
    } else if (e.inkMode === 'V') {
      e.inks = new Array((e.layers || []).reduce((a, l) => a + get(l, 'count', 1), 0)).fill('V');
      e.inkSource = 'ink mode';
    }
  }
}

/* Decide what consumed the ink that disappeared between snapshots t0 and t1.
 * Returns [source label, index of the job it happened in or null].
 * A clean other than the periodic in-print "flash" spit wins, since the
 * printer snapshots right after each clean; flash cleans are interleaved with
 * printing every few minutes, so that ink stays with the print. */
function attribute(t0, t1, jobs, events) {
  let clean = null, maint = null;
  for (const e of events) {
    if (truthy(e.t) && t0 < e.t && e.t <= t1) {
      if (e.cat === 'cleaning' && e.kind === 'clean start' && get(e.d, 'action') !== 'flash') clean = e;
      else if (e.cat === 'cleaning' || e.cat === 'white-ink') maint = e;
    }
  }
  let job = null;
  jobs.forEach((j, i) => { if (truthy(j.start) && truthy(j.end) && j.start - 1 <= t1 && t1 <= j.end + 60000) job = i; });
  if (clean) return ['clean: ' + (get(clean.d, 'action') || '?'), truthy(get(clean.d, 'inPrint')) ? job : null];
  if (job != null) return [jobs[job].isTest ? 'test print' : 'print', job];
  if (maint) {
    if (maint.cat === 'cleaning') return ['clean: ' + (get(maint.d, 'action') || '?'), null];
    return ['white maintenance', null];
  }
  return ['other', null];
}

const api = { loadSources, analyze, findInputs, _internal: { splitlines, pyRound, num, naiveMs, readTar, mergeDeviceLogs, parseGenpd, parseHeadTimers } };
if (typeof module !== 'undefined' && module.exports) module.exports = api;
else root.OmniParser = api;
})(typeof self !== 'undefined' ? self : this);

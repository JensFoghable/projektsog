/* Klippe – the pet widget (SPEC §18). Follows the time tracking (/api/time/status), the import
 * helper and Resolve (SSE) and celebrates: every full hour, the day's goal, long focus, a card
 * that is safely in. It grows with all time ever logged: egg → baby → junior → pro → legend.
 *
 * Plain script like app.js: the first half holds pure helpers (tests/ui/*.test.js load them
 * under Node), the second half drives the page. It never takes the focus. */
(() => {
  'use strict';

  // ===========================================================================================
  // Pure helpers
  // ===========================================================================================

  const STAGES = [
    { key: 'egg', name: 'Æg', from: 0 },
    { key: 'baby', name: 'Baby', from: 5 },
    { key: 'junior', name: 'Junior', from: 25 },
    { key: 'pro', name: 'Pro', from: 100 },
    { key: 'legend', name: 'Legende', from: 300 },
  ];
  const FOCUS_STARS = [25, 50, 90];          // minutes of unbroken focus
  const BREAK_NUDGES = [90, 150, 210];       // gentle "take a break" reminders
  const DAY_MIN_S = 3600;                    // a day counts for the streak from 1 hour

  /** Growth from all time ever logged: the stage, a level and the hours to the next stage.
   *  `hatched` (hatched by hand, widget_hatched): at least a baby; the level stays the hours'. */
  function stageFor(hours, hatched = false) {
    const h = Math.max(0, Number(hours) || 0);
    let index = 0;
    for (let i = 0; i < STAGES.length; i += 1) if (h >= STAGES[i].from) index = i;
    if (hatched) index = Math.max(index, 1);
    const next = STAGES[index + 1] || null;
    return { ...STAGES[index], index, level: 1 + Math.floor(Math.sqrt(h * 2)),
      next: next ? next.name : null, toNext: next ? Math.max(0, next.from - h) : 0 };
  }

  /** How the pet feels about what the tracker sees. */
  function moodFor(status) {
    const s = status || {};
    if (!s.state || s.state === 'off' || s.enabled === false || s.state === 'no-resolve') return 'sleeping';
    if (s.state === 'recording') return 'working';
    if (s.state === 'away') return 'waiting';
    if (s.state === 'idle') return 'sleepy';
    return 'chill';
  }

  /** The outfit for the page you are on. */
  function outfitFor(status) {
    const s = status || {};
    if (s.state !== 'recording' && s.state !== 'away') return 'none';
    return { color: 'color', fusion: 'fusion', fairlight: 'audio', musik: 'audio', deliver: 'deliver' }[s.bucket] || 'none';
  }

  /** Whole hours passed between two totals of today: [1], [2, 3] … */
  function crossedHours(before, after) {
    const from = Math.floor(Math.max(0, before || 0) / 3600);
    const to = Math.floor(Math.max(0, after || 0) / 3600);
    const out = [];
    for (let h = from + 1; h <= to; h += 1) out.push(h);
    return out;
  }

  /** Marks (minutes) passed between two focus lengths. */
  function crossedMarks(marks, before, after) {
    return marks.filter((m) => (before || 0) < m && (after || 0) >= m);
  }

  /** Days in a row with at least an hour – ending today, or yesterday while today is young. */
  function dayStreak(dayTotals, todayIso) {
    const totals = dayTotals || {};
    const day = new Date(`${todayIso}T12:00:00`);
    if ((totals[todayIso] || 0) < DAY_MIN_S) day.setDate(day.getDate() - 1);
    let streak = 0;
    for (;;) {
      const iso = isoDate(day);
      if ((totals[iso] || 0) < DAY_MIN_S) return streak;
      streak += 1;
      day.setDate(day.getDate() - 1);
    }
  }

  function isoDate(date) {
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  /** "2:47" */
  function hm(seconds) {
    const minutes = Math.round(Math.max(0, Number(seconds) || 0) / 60);
    return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}`;
  }

  /** "2 t 47 min", "47 min" */
  function words(seconds) {
    const minutes = Math.round(Math.max(0, Number(seconds) || 0) / 60);
    const hours = Math.floor(minutes / 60);
    if (!hours) return `${minutes} min`;
    return minutes % 60 ? `${hours} t ${minutes % 60} min` : `${hours} t`;
  }

  const PAGE_NAMES = { edit: 'Edit', cut: 'Cut', color: 'Color', fusion: 'Fusion', fairlight: 'Fairlight',
    deliver: 'Deliver', media: 'Media', photo: 'Photo', musik: 'musik og lyd', ai: 'AI-video og -billeder' };

  /** The line under the pet. */
  function moodLine(status, name = 'Klippe') {
    const s = status || {};
    const mood = moodFor(s);
    const what = s.timeline || s.project || '';
    const page = PAGE_NAMES[s.bucket] || s.bucket_label || '';
    if (mood === 'working') {
      if (s.bucket === 'musik') return `Lytter efter musik til ${what} 🎧`;
      if (s.bucket === 'ai') return `Laver AI-video og -billeder til ${what} 🤖`;
      if (s.bucket === 'color') return `Gør ${what} smuk i Color 😎`;
      if (s.bucket === 'fusion') return `Laver magi i Fusion ✨`;
      if (s.bucket === 'deliver') return `Pakker ${what} til levering 📦`;
      return `Klipper løs på ${what}${page ? ` · ${page}` : ''}`;
    }
    if (mood === 'waiting') return `${name} venter på dig – tiden tæller stadig ⏳`;
    if (mood === 'sleepy') return 'Gaab … er du gået til kaffe? ☕';
    if (mood === 'chill') return `${name} slapper af, til du er tilbage i Resolve`;
    return s.enabled === false ? 'Tidsregistreringen er slået fra 💤' : `${name} sover, til Resolve vågner 💤`;
  }

  /** Klippe's job while a card is transferred: what it does (`phase`: copy = carries files from
   *  the card to the folder, verify = checks them, delete = empties the card) and the progress. */
  function transferInfo(job, now = Date.now()) {
    if (!job || !job.state) return null;
    const total = job.bytes_total || 0;
    const work = total ? ((job.copied || 0) + (job.verified || 0)) / (2 * total) : 1;
    const move = job.mode === 'move';
    const where = job.project_name || '';
    const files = `${job.files_done || 0} af ${job.files_total || 0} filer`;
    const eta = job.eta_s == null ? 'regner på tiden …'
      : job.eta_s < 60 ? 'under 1 min tilbage' : `ca. ${words(job.eta_s)} tilbage`;
    if (job.state === 'copying' || job.state === 'verifying') {
      return { busy: true, phase: job.state === 'verifying' ? 'verify' : 'copy', pct: Math.min(99, Math.floor(work * 100)),
        title: `${move ? 'Flytter' : 'Overfører'} ${job.camera}-kort → ${where}`, sub: `${files} · ${eta}` };
    }
    if (job.state === 'deleting') {
      return { busy: true, phase: 'delete', pct: 99, title: `Rydder kortet → ${where}`,
        sub: `${job.deleted || 0} af ${job.files_total || 0} filer slettet fra kortet – alt er kontrolleret` };
    }
    const recent = job.finished && now - job.finished * 1000 < 20 * 60e3;
    if (!recent) return null;
    if (job.state === 'done') {
      return { busy: false, done: true, phase: null, pct: 100, title: `${job.camera}-kortet er ${move ? 'flyttet' : 'overført'} ✓`,
        sub: `${files} i ${where} – kortet kan tages ud` };
    }
    return { busy: false, failed: true, phase: null, pct: Math.floor(work * 100),
      title: job.state === 'cancelled' ? 'Overførslen blev stoppet' : 'Overførslen stoppede',
      sub: job.state === 'cancelled' ? files : (job.error || files) };
  }

  /** What Klippe says about its job. */
  function jobLine(info, job) {
    if (!info || !info.busy) return null;
    if (info.phase === 'verify') return `Tjekker hver fil med lup 🔍 – ${info.pct} %`;
    if (info.phase === 'delete') return 'Rydder kortet – alt er sikkert på disken 🧹';
    return `Bærer ${job.camera}-filer over i ${job.project_name} 📦`;
  }

  /** Messages in the order Klippe shows them: what needs you first, quiet ones ("prioritet":
   *  "stille": a session that is done and needs no answer) last; newest first; expired gone. */
  function messageOrder(list, now = Date.now() / 1000) {
    const live = (Array.isArray(list) ? list : []).filter((m) => m && (!m.udloeber_ved || m.udloeber_ved > now));
    const rank = (m) => (m.prioritet === 'stille' ? 1 : 0);
    return live.map((m, i) => [m, i]).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(([m]) => m);
  }

  /** "12 / 20 dage", "0,4 / 1 TB" – how far a locked trophy has come ("" for yes/no ones). */
  function trophyProgress(t) {
    if (!t || t.unlocked || t.secret || !(t.goal > 1 || t.unit === 'TB')) return '';
    const fmt = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(1).replace('.', ','));
    const shown = t.unit === 't' || t.unit === 'TB' ? Math.floor(t.current * 10) / 10 : Math.floor(t.current);
    return `${fmt(shown)} / ${fmt(t.goal)}${t.unit ? ` ${t.unit}` : ''}`;
  }

  /** What Klippe says when something new is earned or found. */
  function progressLine(n) {
    const what = n.kind === 'fund' ? `🎁 Klippe fandt noget: ${n.name}!`
      : `🏆 ${n.name}${/[!?]$/.test(n.name) ? '' : '!'}${n.reward ? ` Ny ting: ${n.reward.name}` : ''}`;
    return n.rarity === 'legendarisk' ? `🌟 LEGENDARISK! ${what}` : what;
  }

  /** The `pynt` of a sprite sheet url ("hat:baret,haand:awp") – only plain names. */
  function parsePynt(text) {
    const out = {};
    for (const part of String(text || '').split(',')) {
      const [slot, item] = part.split(':');
      if (/^[a-z]+$/.test(slot || '') && /^[a-z0-9-]+$/.test(item || '')) out[slot] = item;
    }
    return out;
  }

  /** The folded line for quiet messages: "📬 2 beskeder · vis". */
  function quietLine(count) {
    return `📬 ${count === 1 ? '1 besked' : `${count} beskeder`} · vis`;
  }

  const CHEERS = {
    hour: (n) => [`${n} ${n === 1 ? 'time' : 'timer'} i dag! 🎉`, `${n} t i kassen – godt klippet! 🎬`, `Time nr. ${n}! Du er on fire 🔥`],
    goal: () => ['Dagens mål er nået! 🏆', 'MÅL! Hele dagens mål er i hus 🎆'],
    focus: (m) => (m >= 90 ? ['90 min fokus – du er i flow! 🌊'] : m >= 50 ? ['50 min i træk – stærkt! ⭐⭐'] : ['25 min fokus ⭐ Godt gået!']),
    rest: (m) => [`${m} min i træk – stræk lige benene og drik vand 🧘`, `${m} min uden pause. 5 minutters pause gør underværker ☕`],
    card: (job) => (job.mode === 'move'
      ? ['Kortet er tømt – alt ligger sikkert på disken! 🎉', 'Klip, kontrol og klap: kortet er tomt og alt er sikkert 🎬']
      : ['Kortet er i hus – alt er kontrolleret! 📼✨', `${job.camera}-kortet er overført 🎉`]),
    grow: (stage) => [`Se! Jeg er blevet ${stage.name}! 🐣🎉`],
    pet: () => ['Hihi 💕', 'Mere! 😊', 'Du er den bedste klipper 🎬', 'Klap klap! 👏'],
    played: () => ['Puha, det var sjovt! 😄', 'Wiii! Din mus er god at ride på 🐎', 'Så er jeg hjemme igen 🏠'],
    back: () => ['Hov! Du er tilbage 👋', 'Øh … jeg lånte bare lige musen 😇', 'Ups – din mus, din tur! 🖱️'],
  };

  function cheer(kind, arg, random = Math.random) {
    const lines = CHEERS[kind](arg);
    return lines[Math.floor(random() * lines.length) % lines.length];
  }

  const helpers = { stageFor, moodFor, outfitFor, crossedHours, crossedMarks, dayStreak, isoDate, hm, words,
    moodLine, cheer, transferInfo, jobLine, messageOrder, quietLine, trophyProgress, progressLine, parsePynt,
    STAGES, FOCUS_STARS, BREAK_NUDGES };
  if (typeof module === 'object' && module.exports) module.exports = helpers;
  if (typeof document === 'undefined') return;

  // ===========================================================================================
  // Sprite sheet: `?sprites=normal,happy,…&stage=baby&outfit=none&cell=240` shows the pet in
  // each pose side by side on a transparent page. Headless Edge takes a screenshot of it, and
  // the games out of the box (projektsog/petplay.py) are drawn from that – so they always look
  // exactly like the pet here.
  // ===========================================================================================

  const PARTY_POSES = new Set(['happy', 'cheer']);
  const query = new URLSearchParams(window.location.search);
  if (query.has('sprites')) {
    renderSprites(query);
    return;
  }

  function renderSprites(params) {
    const poses = (params.get('sprites') || 'normal').split(',').filter(Boolean);
    const stage = params.get('stage') === 'egg' ? 'baby' : (params.get('stage') || 'baby');
    const cell = Number(params.get('cell')) || 240;
    const pynt = parsePynt(params.get('pynt'));
    const svg = document.getElementById('pet');
    const copies = poses.map((_, i) => (i === 0 ? svg : svg.cloneNode(true)));   // the first keeps the ids
    const sheet = document.createElement('div');
    sheet.className = 'sheet';
    poses.forEach((pose, i) => {
      const box = document.createElement('div');
      box.className = PARTY_POSES.has(pose) ? 'sprite party' : 'sprite';
      Object.assign(box.dataset, { pose, stage, outfit: params.get('outfit') || 'none', mood: 'chill', ...pynt });
      box.style.width = `${cell}px`;
      box.style.height = `${cell}px`;
      if (i > 0) copies[i].removeAttribute('id');
      box.append(copies[i]);
      sheet.append(box);
    });
    document.documentElement.classList.add('sprites');
    document.body.replaceChildren(sheet);
  }

  // ===========================================================================================
  // Page
  // ===========================================================================================

  const STATUS_POLL_MS = 5000;
  const HISTORY_POLL_MS = 10 * 60e3;
  const BUBBLE_MS = 5500;
  const REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const $ = (id) => document.getElementById(id);
  const el = {
    app: $('app'), name: $('pet-name'), level: $('pet-level'), streak: $('pet-streak'), stage: $('stage'),
    bubble: $('bubble'), pet: $('pet'), hearts: $('hearts'), mood: $('mood'), today: $('today'), goal: $('goal'),
    todayBar: $('today-bar'), focus: $('focus'), focusBar: $('focus-bar'), grow: $('grow'), fx: $('fx'),
    messages: $('messages'), trophies: $('trophies'), trophyCount: $('trophy-count'), panel: $('panel'),
    panelBody: $('panel-body'), panelClose: $('panel-close'), panelPlay: $('panel-play'),
    transfer: $('transfer'), transferTitle: $('transfer-title'), transferPct: $('transfer-pct'),
    transferBar: $('transfer-bar'), transferSub: $('transfer-sub'),
  };

  const state = {
    status: null, todayS: null, focusStart: null, focusMin: 0, hours: 0, stage: null, streak: 0,
    name: 'Klippe', goalH: 6, bubbles: [], bubbleTimer: 0, seenJobs: new Set(), cards: null, project: null,
    job: null, jobsStarted: new Set(), lookSent: '', lookTimer: 0, hatched: false, hatching: false,
    messages: [], messagesSeen: new Set(), messagesLoaded: false, messageTag: null, messagesOpen: false,
    pet: null, panelTab: 'trophies',
  };

  // ---------------------------------------------------------------- memory (per day)
  const MEMORY_KEY = 'klippe.memory';

  function memory() {
    try {
      const data = JSON.parse(localStorage.getItem(MEMORY_KEY) || '{}');
      return data && typeof data === 'object' ? data : {};
    } catch {
      return {};
    }
  }

  function remember(data) {
    try {
      localStorage.setItem(MEMORY_KEY, JSON.stringify(data));
    } catch { /* storage unavailable: celebrations may repeat after a reload */ }
  }

  /** True the first time `key` is celebrated today (and remembers it). */
  function firstToday(key) {
    const today = isoDate(new Date());
    const data = memory();
    if (data.day !== today) Object.assign(data, { day: today, done: [] });
    if (data.done.includes(key)) return false;
    data.done.push(key);
    remember(data);
    return true;
  }

  // ---------------------------------------------------------------- API
  async function get(path) {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  }

  async function loadSettings() {
    try {
      applySettings((await get('/api/settings')).settings);
    } catch { /* keep the defaults */ }
  }

  function applySettings(settings) {
    if (!settings) return;
    state.name = settings.widget_pet_name || 'Klippe';
    state.goalH = Number(settings.widget_daily_goal_hours) || 6;
    el.name.textContent = state.name;
    renderStats();
    const hatched = Boolean(settings.widget_hatched);
    if (hatched !== state.hatched) {
      state.hatched = hatched;
      if (state.stage) applyStage();
    }
  }

  async function pollStatus() {
    try {
      applyStatus(await get('/api/time/status'));
    } catch { /* Projektsøg restarting: try again */ }
  }

  async function loadHistory() {
    const today = new Date();
    const from = new Date(today);
    from.setDate(from.getDate() - 399);
    try {
      const data = await get(`/api/time?from=${isoDate(from)}&to=${isoDate(today)}`);
      const totals = {};
      for (const p of data.report.projects || []) {
        for (const [day, secs] of Object.entries(p.days || {})) totals[day] = (totals[day] || 0) + secs;
      }
      state.hours = (data.report.total_s || 0) / 3600;
      state.streak = dayStreak(totals, isoDate(today));
      applyStage();
    } catch { /* next time */ }
  }

  // ---------------------------------------------------------------- state → pet
  function applyStatus(status) {
    const before = state.todayS;
    const prevFocus = state.focusMin;
    state.status = status;
    state.todayS = status.today_s || 0;
    const mood = moodFor(status);
    if (mood === 'working' || mood === 'waiting') {
      if (state.focusStart == null) state.focusStart = status.since ? status.since * 1000 : Date.now();
    } else {
      state.focusStart = null;
    }
    state.focusMin = state.focusStart == null ? 0 : (Date.now() - state.focusStart) / 60000;
    el.app.dataset.mood = mood;
    el.app.dataset.outfit = outfitFor(status);
    renderMoodLine();
    if (status.project && state.project && status.project !== state.project && mood === 'working') {
      say(`Nyt projekt: ${status.project} 👋`);
      jump();
    }
    if (status.project) state.project = status.project;
    renderStats();
    if (before == null) {          // first look: what is already reached is not news
      for (const h of crossedHours(0, state.todayS)) firstToday(`hour:${h}`);
      if (state.todayS >= state.goalH * 3600) firstToday('goal');
      return;
    }
    for (const h of crossedHours(before, state.todayS)) {
      if (firstToday(`hour:${h}`)) celebrate(cheer('hour', h), h >= 3 ? 'fireworks' : 'confetti');
    }
    if (before < state.goalH * 3600 && state.todayS >= state.goalH * 3600 && firstToday('goal')) {
      celebrate(cheer('goal'), 'fireworks', 4);
    }
    for (const m of crossedMarks(FOCUS_STARS, prevFocus, state.focusMin)) {
      if (firstToday(`focus:${m}:${state.focusStart}`)) celebrate(cheer('focus', m), 'stars');
    }
    for (const m of crossedMarks(BREAK_NUDGES, prevFocus, state.focusMin)) {
      if (firstToday(`rest:${m}:${state.focusStart}`)) say(cheer('rest', m), 9000);
    }
    reportLook();
  }

  function applyStage() {
    const stage = stageFor(state.hours, state.hatched);
    const data = memory();
    const grew = state.stage && stage.index > state.stage.index
      || (data.stage != null && stage.index > data.stage);
    const hatches = grew && stage.index === 1 && !REDUCED;
    state.stage = stage;
    if (!state.hatching) el.app.dataset.stage = hatches ? 'egg' : stage.key;
    el.level.textContent = `${stage.name} · Lv ${stage.level}`;
    el.streak.hidden = state.streak < 2;
    el.streak.textContent = `🔥 ${state.streak} dage`;
    const total = state.hours >= 10 ? `${Math.floor(state.hours)} t` : words(state.hours * 3600);
    el.grow.textContent = stage.next ? `${words(stage.toNext * 3600)} til ${stage.next} · ${total} i alt`
      : `${total} i alt – en ægte legende 👑`;
    if (data.stage !== stage.index) {
      data.stage = stage.index;
      remember(data);
    }
    if (hatches) {
      hatch();
    } else if (grew) {
      celebrate(cheer('grow', stage), 'fireworks', 4);
    }
    reportLook();
  }

  /** The egg hatches: it shakes and cracks, then the baby pops out – with fireworks. */
  function hatch() {
    if (state.hatching) return;
    state.hatching = true;
    say('Hov … der sker noget! 🥚', 1600);
    el.app.classList.add('hatching');
    setTimeout(() => {
      state.hatching = false;
      el.app.classList.remove('hatching');
      el.app.dataset.stage = state.stage.key;
      el.app.classList.add('hatched');
      setTimeout(() => el.app.classList.remove('hatched'), 1000);
      celebrate(cheer('grow', state.stage), 'fireworks', 4);
      reportLook();
    }, 1800);
  }

  function renderMoodLine() {
    if (el.app.dataset.play === 'out') {
      el.mood.textContent = `${state.name} er ude at lege med musen 🎈 Rør den, så kommer ${state.name} hjem`;
      return;
    }
    const info = transferInfo(state.job);
    el.mood.textContent = jobLine(info, state.job) || moodLine(state.status, state.name);
  }

  // ---------------------------------------------------------------- the games (petplay.py)
  /** How the pet looks and where it sits (CSS pixels): its games out of the box start and end
   *  right here. Sent when it changes – and every minute, for a restarted Projektsøg. */
  function lookReport() {
    const r = el.pet.getBoundingClientRect();
    const round = (n) => Math.round(n * 10) / 10;
    return { stage: (state.stage && state.stage.key) || 'egg', outfit: el.app.dataset.outfit || 'none',
      pet: { x: round(r.left), y: round(r.top), w: round(r.width), h: round(r.height) },
      view: { w: window.innerWidth, h: window.innerHeight } };
  }

  function reportLook(force = false) {
    clearTimeout(state.lookTimer);
    state.lookTimer = setTimeout(async () => {
      const look = lookReport();
      const text = JSON.stringify(look);
      if ((!force && text === state.lookSent) || look.pet.w <= 0 || look.pet.h <= 0) return;
      try {
        const response = await fetch('/api/widget/look', { method: 'POST', cache: 'no-store', body: text,
          headers: { 'Content-Type': 'application/json', 'X-Projektsog': '1' } });
        if (response.ok) state.lookSent = text;
      } catch { /* Projektsøg is restarting: next time */ }
    }, 300);
  }

  /** `pet` events: "out" – Klippe broke out of the box (only the hole is left); anything else –
   *  it is home again. */
  function applyPlay(data) {
    const out = Boolean(data && data.state === 'out');
    const was = el.app.dataset.play === 'out';
    if (out) {
      el.app.dataset.play = 'out';
    } else {
      delete el.app.dataset.play;
    }
    renderMoodLine();
    if (!out && was) {
      jump();
      say(cheer(data && data.reason === 'touched' ? 'back' : 'played'), 4500);
    }
  }

  // ---------------------------------------------------------------- messages (SPEC §19)
  /** Messages from other programs – the Claude sessions' Resolve queue ("Mette vil bruge
   *  Resolve: Byg nu / Ikke nu"). A button opens its uri through Projektsøg; × just closes. */
  function messageKey(m) {
    return `${m.tag}|${m.titel}|${m.tekst}`;
  }

  /** One card at a time (never a long list): the one that needs you, ‹ 1/3 › to the others.
   *  Quiet messages alone only show a small "📬 2 beskeder · vis" line. */
  function renderMessages(list) {
    const messages = messageOrder(list);
    const fresh = messages.filter((m) => !state.messagesSeen.has(messageKey(m)));
    for (const m of messages) state.messagesSeen.add(messageKey(m));
    state.messages = messages;
    const loud = fresh.find((m) => m.prioritet !== 'stille');
    if (loud) state.messageTag = loud.tag;               // what needs you comes to the front
    let index = messages.findIndex((m) => m.tag === state.messageTag);
    // A quiet one stays in front only while you are looking at the quiet ones.
    if (index < 0 || (messages[index].prioritet === 'stille' && !state.messagesOpen)) index = 0;
    state.messageTag = messages.length ? messages[index].tag : null;
    if (!messages.length || loud) state.messagesOpen = false;   // quiet ones fold up again
    const folded = messages.length && messages.every((m) => m.prioritet === 'stille') && !state.messagesOpen;
    if (!messages.length) {
      el.messages.replaceChildren();
    } else if (folded) {
      const line = document.createElement('button');
      line.type = 'button';
      line.className = 'messages__quiet';
      line.textContent = quietLine(messages.length);
      line.addEventListener('click', () => {
        state.messagesOpen = true;
        renderMessages(state.messages);
      });
      el.messages.replaceChildren(line);
    } else {
      el.messages.replaceChildren(messageCard(messages[index], fresh.includes(messages[index]), index, messages.length));
    }
    el.messages.hidden = !messages.length;
    if (loud && loud.lyd !== false && state.messagesLoaded) {
      jump();
      pulse('clap', 950);
    }
    state.messagesLoaded = true;
    reportLook();
  }

  function showMessage(step) {
    const count = state.messages.length;
    const index = state.messages.findIndex((m) => m.tag === state.messageTag);
    const next = state.messages[(Math.max(0, index) + step + count) % count];
    state.messageTag = next.tag;
    if (next.prioritet === 'stille') state.messagesOpen = true;
    renderMessages(state.messages);
  }

  function smallButton(className, text, label, onClick) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = text;
    button.title = label;
    button.setAttribute('aria-label', label);
    button.addEventListener('click', onClick);
    return button;
  }

  function messageCard(m, isNew, index = 0, count = 1) {
    const card = document.createElement('article');
    card.className = `message${isNew ? ' is-new' : ''}${m.prioritet === 'stille' ? ' is-quiet' : ''}`;
    card.dataset.tag = m.tag;
    const title = document.createElement('p');
    title.className = 'message__title';
    title.textContent = m.titel || m.session || 'Besked';
    card.append(title);
    if (m.tekst) {
      const text = document.createElement('p');
      text.className = 'message__text';
      text.textContent = m.tekst;
      card.append(text);
    }
    if (m.knapper && m.knapper.length) {
      const row = document.createElement('div');
      row.className = 'message__buttons';
      m.knapper.forEach((b, i) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'message__button';
        button.textContent = b.tekst;
        button.addEventListener('click', () => answerMessage(m.tag, i, row));
        row.append(button);
      });
      card.append(row);
    }
    card.append(smallButton('message__close', '×', 'Luk beskeden', () => closeMessage(m.tag)));
    const quietOnly = state.messages.every((x) => x.prioritet === 'stille');
    if (count > 1 || quietOnly) {
      const pager = document.createElement('div');
      pager.className = 'message__pager';
      if (count > 1) {
        const where = document.createElement('span');
        where.className = 'message__where';
        where.textContent = `${index + 1} / ${count}`;
        pager.append(smallButton('message__step', '‹', 'Forrige besked', () => showMessage(-1)), where,
          smallButton('message__step', '›', 'Næste besked', () => showMessage(1)));
      }
      if (quietOnly) {
        pager.append(smallButton('message__fold', '▾', 'Fold beskederne sammen', () => {
          state.messagesOpen = false;
          renderMessages(state.messages);
        }));
      }
      card.append(pager);
    }
    return card;
  }

  async function send(method, path, body) {
    const response = await fetch(path, { method, cache: 'no-store', body: JSON.stringify(body),
      headers: { 'Content-Type': 'application/json', 'X-Projektsog': '1' } });
    let data = null;
    try {
      data = await response.json();
    } catch { /* no body */ }
    if (!response.ok) throw new Error((data && data.error) || `Projektsøg svarede ${response.status}`);
    return data;
  }

  async function answerMessage(tag, index, row) {
    for (const button of row.querySelectorAll('button')) button.disabled = true;
    try {
      await send('POST', '/api/messages/click', { tag, knap: index });
    } catch (err) {
      say(`Øv – ${err.message}`, 6000);
      loadMessages();
    }
  }

  async function closeMessage(tag) {
    try {
      await send('DELETE', '/api/messages', { tag });
    } catch {
      loadMessages();
    }
  }

  async function loadMessages() {
    try {
      renderMessages((await get('/api/messages')).messages);
    } catch { /* not there (yet) */ }
  }

  // ---------------------------------------------------------------- trophies and wardrobe (achievements.py)
  const SLOT_KEYS = ['farve', 'striber', 'hat', 'briller', 'mund', 'haand', 'aura'];

  /** What Klippe wears: data-farve, data-hat … on the page (the CSS draws it). */
  function applyWardrobe(equipped) {
    for (const slot of SLOT_KEYS) {
      if (equipped && equipped[slot]) el.app.dataset[slot] = equipped[slot];
    }
  }

  async function loadPet() {
    try {
      state.pet = await get('/api/pet');
    } catch {
      return;
    }
    applyWardrobe(state.pet.equipped);
    el.trophyCount.textContent = String(state.pet.unlocked);
    if (!el.panel.hidden) renderPanel();
  }

  function node(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text != null) n.textContent = text;
    return n;
  }

  function trophyNodes(pet) {
    const nodes = [node('p', 'panel__summary', `${pet.unlocked} af ${pet.total} trofæer`)];
    let group = null;
    for (const t of pet.trophies) {
      if (t.group !== group) {
        group = t.group;
        nodes.push(node('p', 'panel__group', group));
      }
      const row = node('div', `trophy${t.unlocked ? '' : ' is-locked'}`);
      row.dataset.trophy = t.id;
      row.append(node('span', 'trophy__icon', t.unlocked ? '🏆' : t.secret ? '❔' : '🔒'), node('p', 'trophy__name', t.name));
      const progress = trophyProgress(t);
      row.append(node('p', 'trophy__text', progress ? `${t.text} · ${progress}` : t.text));
      if (progress) {
        const bar = node('div', 'bar');
        const fill = node('div', 'bar__fill');
        fill.style.width = `${Math.round(t.progress * 100)}%`;
        bar.append(fill);
        row.append(bar);
      }
      if (t.reward) row.append(node('p', 'trophy__reward', `${t.unlocked ? '✓' : '→'} ${t.reward.name}`));
      nodes.push(row);
    }
    return nodes;
  }

  function wardrobeNodes(pet) {
    const nodes = [node('p', 'panel__summary', 'Vælg, hvad Klippe har på. Hold musen over en låst ting for at se, hvordan den fås.')];
    for (const slot of pet.slots) {
      const box = node('div', 'slot');
      box.append(node('p', 'slot__name', slot.name));
      const row = node('div', 'slot__items');
      for (const item of pet.items.filter((i) => i.slot === slot.id)) {
        const rarity = item.rarity === 'legendarisk' ? ' is-legendary' : item.rarity === 'sjælden' ? ' is-rare' : '';
        const button = node('button', `item${rarity}`, item.owned ? item.name : `🔒 ${item.name}`);
        button.type = 'button';
        button.title = item.owned ? `${item.name} – ${item.how}` : item.how;
        button.disabled = !item.owned;
        button.dataset.item = item.id;
        button.setAttribute('aria-pressed', String(pet.equipped[slot.id] === item.id));
        button.addEventListener('click', () => equip(slot.id, item.id));
        row.append(button);
      }
      box.append(row);
      nodes.push(box);
    }
    return nodes;
  }

  function renderPanel() {
    for (const tab of el.panel.querySelectorAll('[data-panel-tab]')) {
      tab.setAttribute('aria-selected', String(tab.dataset.panelTab === state.panelTab));
    }
    if (!state.pet) {
      el.panelBody.replaceChildren(node('p', 'panel__summary', 'Henter …'));
      return;
    }
    const top = el.panelBody.scrollTop;
    el.panelBody.replaceChildren(...(state.panelTab === 'wardrobe' ? wardrobeNodes(state.pet) : trophyNodes(state.pet)));
    el.panelBody.scrollTop = top;
  }

  function openPanel(tab) {
    if (tab) state.panelTab = tab;
    el.panel.hidden = false;
    renderPanel();
    loadPet();
  }

  async function equip(slot, item) {
    try {
      const answer = await send('POST', '/api/pet/equip', { slot, item });
      applyWardrobe(answer.equipped);
      if (state.pet) state.pet.equipped = answer.equipped;
      renderPanel();
      jump();
    } catch (err) {
      say(`Øv – ${err.message}`, 5000);
    }
  }

  function celebrateProgress(data) {
    const news = (data && data.nye) || [];
    if (!news.length) return;
    if (data.foerste) {
      celebrate(`🏆 Du har allerede ${data.unlocked} trofæer! Se dem under 🏆 ovenfor`, 'confetti', 3);
    } else {
      for (const n of news) {
        const legendary = n.rarity === 'legendarisk';
        celebrate(progressLine(n), legendary ? 'fireworks' : 'confetti', legendary ? 5 : 2.5);
      }
    }
    if (data.unlocked != null) el.trophyCount.textContent = String(data.unlocked);
    loadPet();
  }

  el.trophies.addEventListener('click', () => (el.panel.hidden ? openPanel() : (el.panel.hidden = true)));
  el.panelClose.addEventListener('click', () => {
    el.panel.hidden = true;
  });
  // "Vis legen nu" right here: the panel closes (so the box is seen), Klippe comes out as soon as
  // the mouse has been still for a moment.
  el.panelPlay.addEventListener('click', async () => {
    el.panel.hidden = true;
    try {
      await send('POST', '/api/widget/play', {});
      say('Slip musen … så kommer jeg ud! 🎈', 4000);
    } catch (err) {
      say(`Øv – ${err.message}`, 5000);
    }
  });
  for (const tab of el.panel.querySelectorAll('[data-panel-tab]')) {
    tab.addEventListener('click', () => {
      state.panelTab = tab.dataset.panelTab;
      renderPanel();
    });
  }
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !el.panel.hidden) el.panel.hidden = true;
  });

  async function loadPlay() {
    try {
      applyPlay(await get('/api/widget/play'));
    } catch { /* not started yet */ }
  }

  /** A finished job's box stays only while its card is still in the reader: taking the card out
   *  clears it (it would just take room). A failure stays a while – its card may be the reason. */
  function showsJob(job, info) {
    if (!info) return false;
    if (info.busy || info.failed) return true;
    return !state.cards || state.cards.has(job.card);
  }

  /** Klippe's job: a card being transferred (progress from the import's SSE events). */
  function applyJob(job) {
    state.job = job || null;
    const info = transferInfo(state.job);
    if (info && info.busy) {
      el.app.dataset.transfer = info.phase;
    } else {
      delete el.app.dataset.transfer;
    }
    el.transfer.hidden = !showsJob(state.job, info);
    if (info) {
      el.transfer.classList.toggle('is-done', Boolean(info.done));
      el.transfer.classList.toggle('is-failed', Boolean(info.failed));
      el.transferTitle.textContent = info.title;
      el.transferTitle.title = info.title;
      el.transferPct.textContent = `${info.pct} %`;
      el.transferBar.style.width = `${info.pct}%`;
      el.transferSub.textContent = info.sub;
    }
    reportLook();
    if (job && info && info.busy && !state.jobsStarted.has(job.id)) {
      state.jobsStarted.add(job.id);
      jump();
      say(job.mode === 'move' ? `Jeg flytter ${job.camera}-kortet for dig – og sletter først, når alt er tjekket 💪`
        : `Jeg bærer ${job.camera}-kortet over til ${job.project_name}! 📦`);
    }
    if (job && info && info.failed && !state.seenJobs.has(job.id)) {
      state.seenJobs.add(job.id);
      say(`Øv – ${info.sub}`, 9000);
    }
    renderMoodLine();
  }

  async function loadJob() {
    try {
      const data = await get('/api/import');
      state.cards = new Set((data.cards || []).map((c) => c.id));
      const job = data.job;
      if (job && job.state === 'done') state.seenJobs.add(job.id);   // finished before: no party now
      applyJob(job);
    } catch { /* no import helper */ }
  }

  function renderStats() {
    const today = state.todayS || 0;
    el.today.textContent = hm(today);
    el.goal.textContent = `/ ${state.goalH} t`;
    el.todayBar.style.width = `${Math.min(100, (100 * today) / (state.goalH * 3600))}%`;
    el.todayBar.parentElement.classList.toggle('goal-done', today >= state.goalH * 3600);
    const focus = state.focusMin || 0;
    const nextStar = FOCUS_STARS.find((m) => m > focus) || 90;
    el.focus.textContent = focus >= 1 ? `${words(focus * 60)}${focus < 90 ? ` · ⭐ ved ${nextStar} min` : ' · i flow 🌊'}` : '–';
    el.focusBar.style.width = `${Math.min(100, (100 * focus) / 90)}%`;
  }

  // ---------------------------------------------------------------- reactions
  function say(text, ms = BUBBLE_MS) {
    state.bubbles.push({ text, ms });
    if (!state.bubbleTimer) nextBubble();
  }

  function nextBubble() {
    const item = state.bubbles.shift();
    if (!item) {
      el.bubble.hidden = true;
      state.bubbleTimer = 0;
      return;
    }
    el.bubble.textContent = item.text;
    el.bubble.hidden = false;
    state.bubbleTimer = setTimeout(nextBubble, item.ms);
  }

  function pulse(className, ms) {
    el.pet.classList.remove(className);
    void el.pet.getBoundingClientRect();   // restart the animation
    el.pet.classList.add(className);
    setTimeout(() => el.pet.classList.remove(className), ms);
  }

  function jump() {
    pulse('jump', 750);
  }

  function party(ms = 2600) {
    el.app.classList.add('party');
    clearTimeout(state.partyTimer);
    state.partyTimer = setTimeout(() => el.app.classList.remove('party'), ms);
  }

  function floatEmoji(emoji, count = 1) {
    for (let i = 0; i < count; i += 1) {
      const node = document.createElement('span');
      node.className = 'float';
      node.textContent = emoji;
      node.style.left = `${15 + Math.random() * 70}%`;
      node.style.animationDelay = `${i * 0.12}s`;
      el.hearts.append(node);
      setTimeout(() => node.remove(), 2200 + i * 120);
    }
  }

  /** A celebration: words, a happy pet, the clapper claps, and an effect. */
  function celebrate(text, effect = 'confetti', seconds = 2.5) {
    say(text, Math.max(BUBBLE_MS, seconds * 1000 + 1500));
    party(seconds * 1000 + 500);
    pulse('clap', 950);
    jump();
    el.app.classList.remove('flash');
    void el.app.offsetWidth;
    el.app.classList.add('flash');
    if (REDUCED) {
      floatEmoji(effect === 'stars' ? '⭐' : '🎉', 1);
      return;
    }
    if (effect === 'stars') floatEmoji('⭐', 5);
    fx.start(effect === 'fireworks' ? 'fireworks' : 'confetti', seconds);
  }

  el.pet.addEventListener('click', () => {
    jump();
    floatEmoji('💕', 3);
    const today = state.todayS || 0;
    const lines = [cheer('pet')];
    if (today >= 60) lines.push(`I dag: ${words(today)} 💪`);
    say(lines[Math.floor(Math.random() * lines.length)], 3500);
  });

  // ---------------------------------------------------------------- effects (canvas)
  const fx = (() => {
    const canvas = el.fx;
    const ctx = canvas.getContext('2d');
    const colors = ['#f0a45b', '#ffcf4a', '#5fcf8f', '#7f9cff', '#ef7166', '#b59cff', '#ffffff'];
    let parts = [];
    let until = 0;
    let running = false;

    function resize() {
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.round(canvas.clientWidth * ratio);
      canvas.height = Math.round(canvas.clientHeight * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    }

    function confetti() {
      const w = canvas.clientWidth;
      for (let i = 0; i < 90; i += 1) {
        parts.push({ x: Math.random() * w, y: -10 - Math.random() * 80, vx: (Math.random() - 0.5) * 2.4,
          vy: 1.5 + Math.random() * 2.5, size: 4 + Math.random() * 5, rot: Math.random() * 6, vr: (Math.random() - 0.5) * 0.3,
          color: colors[i % colors.length], kind: 'paper', life: 1 });
      }
    }

    function burst() {
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      const x = w * (0.2 + Math.random() * 0.6);
      const y = h * (0.15 + Math.random() * 0.35);
      const color = colors[Math.floor(Math.random() * colors.length)];
      for (let i = 0; i < 46; i += 1) {
        const angle = (Math.PI * 2 * i) / 46;
        const speed = 1.6 + Math.random() * 2.2;
        parts.push({ x, y, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed, size: 2.4, color,
          kind: 'spark', life: 1 });
      }
    }

    function frame() {
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      ctx.clearRect(0, 0, w, h);
      if (state.fxKind === 'fireworks' && performance.now() < until && Math.random() < 0.06) burst();
      parts = parts.filter((p) => p.life > 0 && p.y < h + 20);
      for (const p of parts) {
        if (p.kind === 'paper') {
          p.x += p.vx;
          p.y += p.vy;
          p.rot += p.vr;
          p.vx *= 0.995;
          if (performance.now() > until) p.life -= 0.02;
          ctx.save();
          ctx.globalAlpha = Math.max(0, p.life);
          ctx.translate(p.x, p.y);
          ctx.rotate(p.rot);
          ctx.fillStyle = p.color;
          ctx.fillRect(-p.size / 2, -p.size / 3, p.size, p.size * 0.66);
          ctx.restore();
        } else {
          p.x += p.vx;
          p.y += p.vy;
          p.vy += 0.03;
          p.vx *= 0.985;
          p.life -= 0.012;
          ctx.globalAlpha = Math.max(0, p.life);
          ctx.fillStyle = p.color;
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
          ctx.fill();
        }
      }
      ctx.globalAlpha = 1;
      if (parts.length || performance.now() < until) {
        requestAnimationFrame(frame);
      } else {
        running = false;
        ctx.clearRect(0, 0, w, h);
      }
    }

    function start(kind, seconds) {
      resize();
      state.fxKind = kind;
      until = performance.now() + seconds * 1000;
      if (kind === 'fireworks') {
        burst();
        burst();
      } else {
        confetti();
      }
      if (!running) {
        running = true;
        requestAnimationFrame(frame);
      }
    }

    window.addEventListener('resize', resize);
    return { start, count: () => parts.length };
  })();
  window.__klippe = { celebrate, fx, state, applyStatus, applyStage, applyJob, applyPlay, lookReport,
    renderMessages, applySettings, celebrateProgress, openPanel };   // for tests

  // ---------------------------------------------------------------- events
  function connectEvents() {
    let source;
    try {
      source = new EventSource('/api/events');
    } catch {
      setTimeout(connectEvents, 5000);
      return;
    }
    source.addEventListener('import', (event) => {
      let job;
      try {
        job = JSON.parse(event.data);
      } catch {
        return;
      }
      applyJob(job);
      if (!job || job.state !== 'done' || state.seenJobs.has(job.id)) return;
      state.seenJobs.add(job.id);
      if (firstToday(`import:${job.id}`)) celebrate(cheer('card', job), 'confetti', 3);
    });
    source.addEventListener('cards', (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      const ids = new Set((data.cards || []).map((c) => c.id));
      const known = state.cards;
      state.cards = ids;
      applyJob(state.job);              // a card taken out clears its finished job
      if (!known) return;
      for (const card of data.cards || []) {
        if (known.has(card.id)) continue;
        jump();
        say(card.found && card.found.complete ? `${card.camera}-kortet kender jeg – det er allerede overført ✅`
          : `Uh, et ${card.camera}-kort! 📼`);
      }
    });
    source.addEventListener('pet_look', (event) => {
      try {
        applyWardrobe(JSON.parse(event.data).equipped);
      } catch { /* ignore */ }
    });
    source.addEventListener('pet_progress', (event) => {
      try {
        celebrateProgress(JSON.parse(event.data));
      } catch { /* ignore */ }
    });
    source.addEventListener('say', (event) => {
      try {
        say(JSON.parse(event.data).tekst, 6000);
      } catch { /* ignore */ }
    });
    source.addEventListener('messages', (event) => {
      try {
        renderMessages(JSON.parse(event.data).messages);
      } catch { /* ignore */ }
    });
    source.addEventListener('pet', (event) => {
      try {
        applyPlay(JSON.parse(event.data));
      } catch { /* ignore */ }
    });
    source.addEventListener('settings', (event) => {
      try {
        applySettings(JSON.parse(event.data));
      } catch { /* ignore */ }
    });
    source.onerror = () => {
      source.close();
      setTimeout(connectEvents, 5000);
    };
  }

  // ---------------------------------------------------------------- start
  async function init() {
    await loadSettings();
    await pollStatus();
    await loadJob();
    loadHistory();
    setInterval(pollStatus, STATUS_POLL_MS);
    setInterval(loadHistory, HISTORY_POLL_MS);
    setInterval(() => applyJob(state.job), 60e3);   // a finished job's line goes away after a while
    setInterval(() => reportLook(true), 60e3);
    setInterval(() => renderMessages(state.messages), 60e3);   // expired messages go
    await loadMessages();
    loadPet();
    window.addEventListener('resize', () => reportLook());
    loadPlay();
    connectEvents();
    setTimeout(() => say(`Hej! Jeg er ${state.name} 👋`), 600);
  }

  init();
})();

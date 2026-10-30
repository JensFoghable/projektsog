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

  // ---------------------------------------------------------------- food and hunger (SPEC §18.6)
  /** maet ≥ 70 · fin ≥ 35 · sulten ≥ 12 · skrubsulten below (null: not known yet). */
  function hungerLevel(maet) {
    if (maet == null || maet === '' || !Number.isFinite(Number(maet))) return null;
    const m = Number(maet);
    if (m >= 70) return 'maet';
    if (m >= 35) return 'fin';
    return m >= 12 ? 'sulten' : 'skrubsulten';
  }

  /** What Klippe dreams of, as it would say it: "en durum 🌯". */
  const CRAVINGS = { durum: 'en durum 🌯', bigmac: 'en Big Mac 🍔', nuggets: 'McNuggets 🐔', pommes: 'pommes frites 🍟',
    booster: 'en Faxe Kondi Booster ⚡', mangoloco: 'en Monster Mango Loco 🥭', drop: 'et Booster-drop 💉' };

  /** The line under the pet about food, or null: starving beats everything, then an energy rush,
   *  then hunger (not while you work – the work line stays, the dream bubble says it). */
  function foodLine(food, mood, name = 'Klippe', craving = null) {
    if (!food) return null;
    const level = hungerLevel(food.maet);
    const dream = CRAVINGS[craving];
    if (level === 'skrubsulten') {
      return dream ? `${name} er skrubsulten og drømmer om ${dream}` : `${name} er skrubsulten! Giv den noget at spise 🍔`;
    }
    if (food.energi && mood !== 'working') {
      return food.energi.item === 'drop' ? `💉 ${name} ligger i Booster-drop – fuld fart ⚡`
        : `⚡ ${name} er helt oppe at køre på ${food.energi.name}`;
    }
    if (level !== 'sulten' || mood === 'working') return null;
    if (mood === 'sleeping') return `${name} sover og drømmer om ${dream || 'mad 🍔'} 💤`;
    return dream ? `${name} er sulten – drømmer om ${dream}` : `${name} er sulten 🍔`;
  }

  const FOOD_LINES = {
    durum: ['Mmm, durum med det hele! 🌯', 'Ekstra hvidløg, tak! 😋', 'Byens bedste durum 🤤'],
    bigmac: ['Big Mac – det var lige sagen! 🍔', 'Nom nom nom 🍔😋'],
    nuggets: ['Nuggets! Hvor er dippen? 🐔', 'Sprøde nuggets 😋'],
    pommes: ['Sprøde pommes! 🍟', 'Med ketchup næste gang 🍟😋'],
    booster: ['BØVS! ⚡ Nu kører det!', 'Booster! Nu kan jeg klippe en hel spillefilm ⚡'],
    mangoloco: ['BØVS! 🥭 Mango Loco!', 'Jeg kan høre farver 🥭⚡'],
    drop: ['Booster direkte i blodet! 💉⚡', 'Intravenøs turbo 💉 – nu klipper vi!'],
    maet: ['Jeg er proppet! 🤢 Måske senere', 'Ikke en bid mere … 😵'],
    hjerte: ['Mit hjerte hamrer 💓 – ikke flere energidrikke lige nu', 'Puha … vand nu, tak 💧'],
    sulten: ['Min mave knurrer … 🍔', 'Er det ikke snart frokost? 🌯'],
    skrubsulten: ['Jeg er SKRUBSULTEN! 😫', 'Mad … nu … tak … 🥺'],
  };

  /** What Klippe says about food: `key` = a menu item it ate, a refusal (maet, hjerte) or hunger. */
  function foodSay(key, random = Math.random) {
    const lines = FOOD_LINES[key] || ['Mmm! 😋'];
    return lines[Math.floor(random() * lines.length) % lines.length];
  }

  /** The hint under a dish in the tray: "+60" or "⚡ 20 min" (an energy drink or the drip). */
  function foodHint(food) {
    return food.energy_min > 0 ? `⚡ ${Math.round(food.energy_min)} min` : `+${Math.round(food.points)}`;
  }

  /** How full the drip's bag still is (0.05–1): `bag` = {fra, til} of the drip. */
  function dropLeft(bag, now = Date.now() / 1000) {
    if (!bag || typeof bag.til !== 'number' || typeof bag.fra !== 'number' || bag.til <= bag.fra) return 1;
    return Math.max(0.05, Math.min(1, (bag.til - now) / (bag.til - bag.fra)));
  }

  /** The folded line for quiet messages: "📬 2 beskeder · vis". */
  function quietLine(count) {
    return `📬 ${count === 1 ? '1 besked' : `${count} beskeder`} · vis`;
  }

  // ---------------------------------------------------------------- the phone and the robot crew (SPEC §21)
  /** A call that still waits for an answer: Klippe shows a phone instead of its card. */
  function isCall(m) {
    return Boolean(m && m.opkald && !m.besvaret);
  }

  /** Who calls: the session, else the name in "🎬 Mette vil bruge Resolve", else "Claude". */
  function callerName(m) {
    const session = String((m && m.session) || '').trim();
    if (session) return session;
    const match = /^\s*(?:🎬\s*)?(.+?)\s+vil bruge Resolve/u.exec(String((m && m.titel) || ''));
    return match ? match[1] : 'Claude';
  }

  /** "📞 Mette ringer" – or, once it has stopped ringing, "📞 Ubesvaret opkald fra Mette". */
  function callLine(m) {
    return m && m.ringer ? `📞 ${callerName(m)} ringer` : `📞 Ubesvaret opkald fra ${callerName(m)}`;
  }

  /** The robot sprite sheet's poses (crew.py ROBOT_POSES), in that order. */
  const ROBOT_POSES = ['robot-a', 'robot-b', 'robot-baer', 'robot-klip', 'robot-hop', 'robot-fraek', 'robot-panik'];
  const ROBOT_COUNT = { egg: 4, baby: 5, junior: 7, pro: 9, legend: 12 };   // the crew out on the screen
  const BODY_SCALE = { baby: 0.74, junior: 0.86, pro: 0.95, legend: 1 };     // .body in widget.css

  /** `?sprites=` asks for the robots' sheet (every pose a robot pose), not Klippe's. */
  function isRobotSheet(poses) {
    return poses.length > 0 && poses.every((pose) => pose.startsWith('robot'));
  }

  /** The end of the barrel in Klippe's aim pose (data-sigter), in pet SVG units (0…200): where the
   *  robots' helper starts the laser – mirrored when it aims to the left (the robots work to the
   *  left of the widget), as drawn to the right. null for an egg – it has no hands. */
  function muzzle(stage, direction = 'venstre') {
    const s = BODY_SCALE[stage];
    return s ? { x: 100 + (direction === 'hoejre' ? 92 : -92) * s, y: 180 - 54.75 * s } : null;
  }

  const DIRECTOR_LINES = ['Action! 🎬', 'Klip! ✂️', 'Mere tempo! 📣', 'Flot, robot nr. {n}! 🤖', 'Pas på tidslinjen! 😬',
    'Tag den fra toppen! 🔁', 'Ro på settet! 🤫', 'Den her bliver en klassiker! 🏆'];

  /** What Klippe shouts through the megaphone now and then; `robots`: how many there are. */
  function directorLine(random = Math.random, robots = 3) {
    const line = DIRECTOR_LINES[Math.floor(random() * DIRECTOR_LINES.length) % DIRECTOR_LINES.length];
    return line.replace('{n}', String(1 + (Math.floor(random() * robots) % robots)));
  }

  function shorten(text, max = 48) {
    const t = String(text || '').trim();
    return t.length > max ? `${t.slice(0, max - 1)}…` : t;
  }

  /** The line under the pet while a session builds (`bygger` state), or null. */
  function buildLine(b, name = 'Klippe') {
    if (!b || !b.aktiv) return null;
    if (b.ude) return '🤖 Robotterne klipper ude på skærmen – rør musen, så går de ind';
    if (b.demo) return `🤖 ${shorten(b.projekt || b.opgave) || 'Robotterne øver sig'} – ${name} dirigerer`;
    const task = shorten(b.opgave);
    const what = task ? `„${task}“` : b.projekt ? `i ${shorten(b.projekt)}` : 'i Resolve';
    return `🤖 ${b.navn || 'Claude'} bygger ${what} – ${name} dirigerer robotterne`;
  }

  /** What Klippe says when a build is done. */
  function doneLine(b) {
    return b && b.demo ? 'Robotterne er færdige med at øve! 🎉' : `✅ ${(b && b.navn) || 'Claude'} er færdig – klar til at klippe!`;
  }

  // ---------------------------------------------------------------- renders, office Klippes and the delivery party (SPEC §22)
  /** The progress line under the pet while Resolve renders (`render` state): `what` "Renderer
   *  Portræt_v3.mp4", `tal` "47 % · ca. 3 min" and `text`, the whole line – or null. */
  function renderProgress(r) {
    if (!r || !r.aktiv) return null;
    const pct = Number.isFinite(r.pct) ? Math.max(0, Math.min(100, Math.round(r.pct))) : null;
    const eta = !Number.isFinite(r.eta_s) || r.eta_s < 0 ? null : r.eta_s < 60 ? 'under 1 min' : `ca. ${words(r.eta_s)}`;
    const what = `Renderer ${shorten(r.navn || r.tidslinje, 40) || 'tidslinjen'}`;
    const tal = [pct == null ? null : `${pct} %`, eta].filter(Boolean).join(' · ') || 'går i gang …';
    return { what, tal, pct, text: `${what} · ${tal}` };
  }

  /** The line under the pet while Resolve renders, or null. */
  function renderLine(r, name = 'Klippe') {
    if (!r || !r.aktiv) return null;
    const what = shorten(r.tidslinje || r.navn || r.projekt, 40) || 'tidslinjen';
    return r.af_claude ? `🎬 ${shorten(r.af_claude, 20)} renderer ${what} – robotterne fodrer maskinen`
      : `🎬 Robotterne renderer ${what} – ${name} holder øje`;
  }

  /** What Klippe says when a render has ended (`faerdig`) – null for a job that is just gone. */
  function renderDoneLine(f) {
    if (!f) return null;
    if (f.udfald === 'done') return 'Renderen er færdig! 🎬';
    if (f.udfald === 'failed') return `Renderen fejlede 😟${f.fejl ? ` – ${shorten(f.fejl, 90)}` : ''}`;
    return f.udfald === 'cancelled' ? 'Renderen blev stoppet ✋' : null;
  }

  /** One finished render, the same in every event and after a reload (null: none). */
  function renderKey(f) {
    return f && f.seq != null ? `${f.seq}|${f.udfald}|${f.sti || f.fil || ''}` : null;
  }

  const SLOT_KEYS = ['farve', 'striber', 'hat', 'briller', 'mund', 'hals', 'haand', 'aura'];
  const OUTFITS = ['none', 'color', 'fusion', 'audio', 'deliver'];
  const KOSTUMER = ['vampyr', 'spoegelse', 'skelet', 'heks'];      // achievements.KOSTUMER

  /** How a colleague's Klippe looks (SSE `besoeg`): only known stages and outfits, only the known
   *  wardrobe slots with plain item names – they only ever become data-* attributes that the CSS
   *  matches. A guest from a delivery party wears the Deliver cap; a trick-or-treater its costume
   *  (SPEC §23.3), a postman an envelope. */
  function guestLook(b) {
    const v = b && typeof b === 'object' ? b : {};
    const stage = STAGES.some((s) => s.key === v.stage) ? v.stage : 'baby';
    const outfit = v.type === 'fest' ? 'deliver' : OUTFITS.includes(v.outfit) ? v.outfit : 'none';
    const pynt = {};
    if (v.pynt && typeof v.pynt === 'object') {
      for (const slot of SLOT_KEYS) {
        const item = v.pynt[slot];
        if (typeof item === 'string' && /^[a-z0-9-]{1,32}$/.test(item)) pynt[slot] = item;
      }
    }
    const kostume = v.type === 'slik' && KOSTUMER.includes(v.kostume) ? v.kostume : null;
    return { stage, outfit, pynt, kostume, post: v.type === 'post' };
  }

  /** What the guest says: "Hej fra STUDIO-PC! Jeg fik 🏆 Durumkongen". */
  function visitLine(b) {
    const v = b || {};
    const from = `Hej fra ${shorten(v.pc, 15) || 'kontoret'}!`;
    if (v.type === 'fest') return `${from} Vi har leveret! 🎉`;
    if (v.type === 'slik') return 'Slik eller ballade! 🎃';
    if (v.type === 'post') return `${from} ✉️ Post til dig!`;
    if (v.type === 'aar') return `${from} ${yearShareLine(v.aar)}`;
    const t = v.trofae;
    if (!t || !t.name) return `${from} Jeg har fået et nyt trofæ 🏆`;
    return t.kind === 'fund' ? `${from} Jeg fandt 🎁 ${shorten(t.name, 32)}` : `${from} Jeg fik 🏆 ${shorten(t.name, 32)}`;
  }

  /** What Klippe answers its guest. */
  function visitReply(b) {
    const name = shorten(b && b.navn, 20) || 'du';
    if (b && b.type === 'post') return `Tak for posten, ${name}! 📬`;
    if (b && b.type === 'aar') return `Flot år, ${name}! 🎉`;
    return b && b.type === 'fest' ? `Hej ${name}! 👋 Tillykke med leveringen!` : `Hej ${name}! 👋 Flot klaret!`;
  }

  /** A colleague's year in one line (SSE `besoeg` type "aar", §23.7): "📊 Mit Klippet 2026: 312 timer ·
   *  mest i Color · 31 durum!" – only numbers and a page, never a project. */
  function yearShareLine(x) {
    const v = x && typeof x === 'object' ? x : {};
    const year = Number.isInteger(v.aar) ? v.aar : '';
    const parts = [];
    if (Number.isFinite(v.timer)) parts.push(`${Math.round(v.timer)} ${Math.round(v.timer) === 1 ? 'time' : 'timer'}`);
    if (PAGE_NAMES[v.side]) parts.push(`mest i ${PAGE_NAMES[v.side]}`);
    if (Number.isFinite(v.durum) && v.durum > 0) parts.push(`${Math.round(v.durum)} durum`);
    return `📊 Mit Klippet ${year}${parts.length ? `: ${parts.join(' · ')}` : ''}!`;
  }

  /** The line of a delivery party (SSE `levering`): "Leveret: Portræt_v3.mp4 🎉". */
  function deliveryLine(d) {
    return `Leveret: ${shorten(d && (d.fil || d.projekt), 40) || 'filen'} 🎉`;
  }

  const FESTKAT_FRAMES = 103;                // GIPHY's party cat (levering.FESTKAT_URL)

  /** Which frames of the party cat stand and which spin ([first, last]): GIPHY's cat stands on
   *  0–23 and spins on 24–69; any other GIF stands on its first frame and spins through them all. */
  function catFrames(count) {
    if (count === FESTKAT_FRAMES) return { stand: [0, 23], spin: [24, 69] };
    return { stand: [0, 0], spin: [0, Math.max(0, count - 1)] };
  }

  /** The time until the cat's next spin: 2.5–6 s. */
  function nextSpin(random = Math.random) {
    return 2500 + random() * 3500;
  }

  /** Keys out the green screen of one RGBA frame in place (`p`: its pixels, `width` in pixels):
   *  clearly green (green over the larger of red and blue by more than 60) is transparent, the soft
   *  edge (20–60) fades out with its green taken away. Returns the lowest row that still shows
   *  something (−1: none) – where the cat stands. */
  function keyGreen(p, width) {
    let bottom = -1;
    for (let j = 0; j < p.length; j += 4) {
      const green = p[j + 1] - Math.max(p[j], p[j + 2]);
      if (green > 60) {
        p[j + 3] = 0;
      } else if (green > 20) {
        p[j + 3] = Math.round((p[j + 3] * (60 - green)) / 40);
        p[j + 1] = Math.max(p[j], p[j + 2]);
      }
      if (p[j + 3] > 127) bottom = Math.floor(j / 4 / width);
    }
    return bottom;
  }

  /** `url(#stripes)` → `url(#stripes-gaest)` for the ids in `map` (a guest's own copies). */
  function renameRefs(value, map) {
    return String(value).replace(/url\(#([^)]+)\)/g, (all, id) => (Object.hasOwn(map, id) ? `url(#${map[id]})` : all));
  }

  // ---------------------------------------------------------------- fun in the office: the clock (SPEC §23)
  const MORNING_MIN = 6 * 60;                // Natte-Klippe sleeps until 06:00
  const pad2 = (n) => String(n).padStart(2, '0');

  /** "16:00" → 960 (minutes of the day); null for "" or nonsense. */
  function clockMinutes(text) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(text == null ? '' : text).trim());
    if (!m) return null;
    const hours = Number(m[1]);
    const minutes = Number(m[2]);
    return hours < 24 && minutes < 60 ? hours * 60 + minutes : null;
  }

  /** Natte-Klippe (SPEC §23.2): `gaber` for [F, F+20), `doeser` for [F+20, F+45), `sover` from F+45
   *  until 06:00 (F = the fyraften); null in the day – and when fyraften is "" (off). */
  function natFase(now, fyraften) {
    const f = clockMinutes(fyraften);
    if (f == null) return null;
    const day = now instanceof Date ? now : new Date(now);
    const t = day.getHours() * 60 + day.getMinutes();
    if (t >= MORNING_MIN && t < f) return null;
    const since = (t - f + 1440) % 1440;
    if (since < 20) return 'gaber';
    return since < 45 ? 'doeser' : 'sover';
  }

  /** How Klippe says the fyraften: "16" or "16.30". */
  function fyraftenLabel(fyraften) {
    const f = clockMinutes(fyraften);
    if (f == null) return '16';
    return f % 60 ? `${Math.floor(f / 60)}.${pad2(f % 60)}` : String(Math.floor(f / 60));
  }

  /** The line under the pet after fyraften (null: none) – only while nothing else has a say. */
  function natLine(fase, mood, fyraften, name = 'Klippe') {
    if (fase === 'sover') {
      return mood === 'working' ? `💤 ${name} sover – fyraften var kl. ${fyraftenLabel(fyraften)}. Gå hjem! 🏠` : '💤 Godnat …';
    }
    if ((fase === 'gaber' || fase === 'doeser') && mood === 'working') return '🥱 Det er fyraften – snart godnat';
    return null;
  }

  const YAWN_LINES = ['Gaaab … 🥱', 'Puha, det har været en lang dag', 'Skal vi ikke gå hjem? 🏠', 'Mine øjne bliver så tunge … 😴'];
  const DANCE_LINES = ['🎧 Den her har groove!', 'Skru op! 🔊', 'Den skal med i filmen 🎶'];

  /** Monday–Sunday of the ISO week that holds 31 October (achievements.halloween_week). */
  function halloweenWeek(now) {
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const oct31 = new Date(day.getFullYear(), 9, 31);
    const monday = new Date(oct31);
    monday.setDate(oct31.getDate() - ((oct31.getDay() + 6) % 7));
    const sunday = new Date(monday);
    sunday.setDate(monday.getDate() + 6);
    return day >= monday && day <= sunday;
  }

  /** "halloween" (Halloween week), "jul" (December) or null. */
  function seasonFor(now) {
    if (halloweenWeek(now)) return 'halloween';
    return now.getMonth() === 11 ? 'jul' : null;
  }

  /** The lunch bell is offered on weekdays 10:45–13:30 (SPEC §23.12). */
  function lunchTime(now) {
    const t = now.getHours() * 60 + now.getMinutes();
    return now.getDay() >= 1 && now.getDay() <= 5 && t >= 10 * 60 + 45 && t < 13 * 60 + 30;
  }

  /** Klippet ‹år› in December (this year) and January (last year); null otherwise. */
  function yearInReview(now) {
    if (now.getMonth() === 11) return now.getFullYear();
    return now.getMonth() === 0 ? now.getFullYear() - 1 : null;
  }

  /** "14:32" (local time) of an epoch in seconds. */
  function clockLabel(epochS) {
    const d = new Date(Number(epochS) * 1000);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }

  // ---------------------------------------------------------------- render bets (SPEC §23.7, §23.12)
  const GUESS_MINUTES = [5, 10, 20, 40];

  /** The bet card's quick choices: now + 5, 10, 20, 40 min, rounded to 5 min (never before now + 1 min). */
  function guessChoices(nowMs) {
    return GUESS_MINUTES.map((min) => {
      let at = Math.round((nowMs + min * 60e3) / 300e3) * 300e3;
      if (at <= nowMs + 60e3) at += 300e3;
      return { min, at: Math.round(at / 1000), label: clockLabel(at / 1000) };
    });
  }

  /** "14:35" from the time field → epoch seconds: today, or tomorrow when that is not after now. */
  function guessFromField(text, nowMs) {
    const minutes = clockMinutes(text);
    if (minutes == null) return null;
    const day = new Date(nowMs);
    day.setHours(Math.floor(minutes / 60), minutes % 60, 0, 0);
    if (day.getTime() <= nowMs) day.setDate(day.getDate() + 1);
    return Math.round(day.getTime() / 1000);
  }

  /** One bet: its PC, its number and when its render began – a PC restarted counts its bets from 0 again. */
  function betKey(bet) {
    return `${bet && bet.pc}:${bet && bet.vid}:${bet && bet.start}`;
  }

  const BET_STALE_S = 3 * 3600;              // a colleague's bet we never heard the end of

  /** A colleague's bet that never ended (its PC went to sleep, the last word was lost): gone 3 h after it closed. */
  function betStale(bet, nowS) {
    return Boolean(bet) && !bet.egen && (bet.status === 'aaben' || bet.status === 'lukket')
      && nowS - (Number(bet.lukker) || 0) >= BET_STALE_S;
  }

  /** The bet the card shows: an open one first (the newest), then one whose guesses are closed, then
   *  one that ended less than 2 minutes ago (`endedAt`, epoch s, stamped by the page). */
  function pickBet(bets, nowS) {
    const list = (Array.isArray(bets) ? bets : []).filter((b) => b && typeof b === 'object' && !betStale(b, nowS));
    const newest = (a, b) => (b.start || 0) - (a.start || 0);
    const open = list.filter((b) => b.status === 'aaben').sort(newest);
    if (open.length) return open[0];
    const closed = list.filter((b) => b.status === 'lukket').sort(newest);
    if (closed.length) return closed[0];
    const ended = list.filter((b) => (b.status === 'slut' || b.status === 'aflyst')
      && nowS - (b.endedAt || nowS) < 120).sort((a, b) => (b.endedAt || 0) - (a.endedAt || 0));
    return ended[0] || null;
  }

  /** "🎲 GRAFIK-PC renderer Portræt_v3.mp4 (12 %) – hvornår er den færdig?" (own: "Du renderer …"). */
  function betTitle(bet) {
    const fil = shorten(bet && bet.fil, 32) || 'en film';
    if (bet && bet.egen) return `🎲 Du renderer ${fil} – kollegerne gætter med!`;
    const pct = Number.isFinite(bet && bet.pct) ? ` (${Math.round(bet.pct)} %)` : '';
    return `🎲 ${shorten(bet && (bet.maskine || bet.pc), 15) || 'En kollega'} renderer ${fil}${pct} – hvornår er den færdig?`;
  }

  /** "🎲 Dit gæt: 14:32 · 3 har gættet · lukker om 2 min" (after a guess, or once guesses are closed). */
  function betStatus(bet, nowS) {
    const parts = [];
    if (bet.mit_gaet) parts.push(`🎲 Dit gæt: ${clockLabel(bet.mit_gaet)}`);
    else if (bet.status === 'lukket') parts.push('🎲 Gættene er lukket');
    parts.push(`${Math.max(0, Math.round(Number(bet.antal_gaet) || 0))} har gættet`);
    if (bet.status === 'aaben') {
      const left = Math.max(0, (Number(bet.lukker) || 0) - nowS);
      parts.push(left < 60 ? 'lukker om under 1 min' : `lukker om ${Math.round(left / 60)} min`);
    } else if (bet.status === 'lukket') {
      parts.push('venter på renderen …');
    }
    return parts.join(' · ');
  }

  /** How a bet ended – or null while it runs. */
  function betResult(bet) {
    if (!bet) return null;
    if (bet.status === 'aflyst') return 'Renderen stoppede – væddemålet er aflyst';
    const r = bet.resultat;
    if (bet.status !== 'slut' || !r) return null;
    if (r.mig) return `Du vandt render-væddemålet! 🔮 Gættede ${clockLabel(r.tid)} – færdig ${clockLabel(r.slut)}`;
    if (!r.vinder_pc) return `🎲 Ingen gættede – renderen blev færdig ${clockLabel(r.slut)}`;
    const who = r.vinder_navn ? `${shorten(r.vinder_navn, 20)} (${shorten(r.vinder_pc, 15)})` : shorten(r.vinder_pc, 15);
    return `🏆 ${who} vandt! Gættede ${clockLabel(r.tid)} – færdig ${clockLabel(r.slut)}`;
  }

  // ---------------------------------------------------------------- lunch, post, trick or treat (SPEC §23.3, §23.12)
  /** "🍽️ FROKOST! (Bobby ringede fra KLIPPER-PC)" – or "🔔 Du ringede til frokost!". */
  function lunchLine(ev) {
    if (ev && ev.selv) return '🔔 Du ringede til frokost!';
    return `🍽️ FROKOST! (${shorten(ev && ev.navn, 20) || 'Klippe'} ringede fra ${shorten(ev && ev.pc, 15) || 'kontoret'})`;
  }

  const POST_NOTES = ['Kan du lige kigge på det her? 👀', 'Frokost? 🍔', 'Kaffe? ☕', 'Renderen er færdig ✅', 'Ring lige 📞',
    'Godt gået! 🎉'];
  const POST_MAX = 120;

  /** "🎃 Bobby fra KLIPPER-PC vil have slik!" */
  function slikLine(v) {
    return `🎃 ${shorten(v && v.navn, 20) || 'Klippe'} fra ${shorten(v && v.pc, 15) || 'kontoret'} vil have slik!`;
  }

  /** The countdown under it: "… ellers bliver der ballade om 20 s". */
  function slikCountdown(seconds) {
    return `… ellers bliver der ballade om ${Math.max(0, Math.ceil(seconds))} s`;
  }

  const SLIK_SERVER_S = 55;                  // the server takes an answer 60 s after the guest came (kontor.py)
  const SLIK_MIN_S = 8;                      // less left than this: the guest has gone on

  /** How long a trick-or-treater waits for its Booster once it is in: its own 25 s, but never past what the
   *  server still takes after `waitedMs` in the queue (another guest, a meal, a game) – < SLIK_MIN_S: it left. */
  function slikSeconds(tilS, waitedMs = 0) {
    return Math.min(Number(tilS) || 25, SLIK_SERVER_S - Math.max(0, Number(waitedMs) || 0) / 1000);
  }

  /** data-ballade: "hat", "toiletpapir" or "hat toiletpapir" (a second prank keeps the hat away), or "". */
  function balladeTokens(prank) {
    if (!prank) return '';
    return [prank.hat ? 'hat' : null, prank.toiletpapir ? 'toiletpapir' : null].filter(Boolean).join(' ');
  }

  /** "Lars'" (a name ending in s, x or z), else "Lises". */
  function possessive(name) {
    const n = String(name || '').trim();
    return /[sxz]$/i.test(n) ? `${n}'` : `${n}s`;
  }

  /** "dit" before a neuter thing (et gevir), "din" before the rest (en hat, en hue, en hjelm …). */
  const NEUTER_ITEMS = new Set(['gevir']);

  function yourItem(item) {
    return NEUTER_ITEMS.has(item) ? 'dit' : 'din';
  }

  /** What Klippe says when a guest played a prank on it – the hat's name in lower case. */
  function balladeLine(navn, prank, hatName) {
    const who = shorten(navn, 20) || 'Gæsten';
    if (prank && prank.hat) {
      return `Ballade! 😈 ${who} stak af med ${yourItem(prank.hat)} ${String(hatName || prank.hat).toLowerCase()}!`;
    }
    return `Ballade! 😈 ${who} pakkede mig ind i toiletpapir! 🧻`;
  }

  /** What the trick-or-treater's Klippe says (SSE `slik_svar`). */
  function slikSvarLine(d, hatName) {
    const name = shorten(d && d.navn, 20) || 'Klippe';
    const who = `${name} (${shorten(d && d.pc, 15) || 'kontoret'})`;
    if (d && d.type === 'godbid') return `🍬 ${who} gav mig en Booster!`;
    if (d && d.hat) return `😈 Hehe – jeg fik ${possessive(name)} ${String(hatName || d.hat).toLowerCase()}!`;
    return `😈 Hehe – jeg pakkede ${name} ind i toiletpapir! 🧻`;
  }

  // ---------------------------------------------------------------- the Christmas calendar (SPEC §23.4)
  const GIFT_ICONS = { halstoerklaede: '🧣', julesloejfe: '🎀', gevir: '🦌', glorie: '😇', nissehue: '🎅', julebriller: '🤓',
    slikstok: '🍭', julegave: '🎁', snefnug: '❄️', julelys: '✨', julerod: '❤️', polkagris: '🍬', julestjerne: '⭐',
    aebleskiver: '🥞', risalamande: '🍚', pebernoedder: '🍪', brunkager: '🍪', klejner: '🥨', julesodavand: '🥤',
    sne1: '❄️', lucia: '🕯️' };
  const GIFT_KIND_ICONS = { ting: '🎁', sne: '❄️', mad: '🍪', vits: '😂' };

  function giftIcon(gave) {
    if (!gave) return '🎁';
    return GIFT_ICONS[gave.id] || GIFT_ICONS[gave.item] || GIFT_KIND_ICONS[gave.kind] || '🎁';
  }

  /** One door of the grid: `klasse` is-aaben (its gift), is-klar (can be opened) or is-laast ("Om 3 dage"). */
  function doorView(door, dag, demo = false) {
    const d = door || {};
    if (d.aaben && d.gave) return { klasse: 'is-aaben', ikon: giftIcon(d.gave), tekst: shorten(d.gave.name, 18) };
    if (demo || d.kan) return { klasse: 'is-klar', ikon: '', tekst: 'Åbn' };
    const days = dag ? d.nr - dag : null;
    const tekst = days == null || days < 1 ? `${d.nr}. dec.` : days === 1 ? 'I morgen' : `Om ${days} dage`;
    return { klasse: 'is-laast', ikon: '', tekst };
  }

  /** What Klippe says when a door's gift comes out. */
  function giftLine(gave, nr) {
    if (!gave) return `🎁 Låge ${nr}!`;
    if (gave.kind === 'ting') return `🎁 Låge ${nr}: ${gave.name}!`;
    if (gave.kind === 'mad') return `${gave.name}! 😋`;
    if (gave.kind === 'sne') return gave.tekst || '❄️ Det sner!';
    return gave.tekst || gave.name || '🎁';
  }

  // ---------------------------------------------------------------- easter eggs, the bath, memories (SPEC §23.6, §23.8, §23.9)
  const EGGS = {
    kaffe: { line: 'Værsgo! ☕', haand: 'kaffe', ms: 8000 },
    fredag: { celebrate: true },
    deadline: { line: 'DEADLINE?! 😱', attr: 'panik', ms: 4000 },
    klippe: { line: 'Det er mig! 👋', attr: 'vink', ms: 2500 },
    durum: { line: 'Durum?! 🤤 Ja tak!', attr: 'savler', ms: 4000 },
    booster: { line: '⚡ BOOSTER!', energi: 'booster', ms: 4000 },
    festkat: { cat: true, ms: 6500 },
    hjaelp: { line: 'Klippe til undsætning! 🦸', attr: 'helt', ms: 5000 },
    fyraften: { line: 'Fyraften? Jaaa! 🏠', nat: true, ms: 5000 },
    sne: { line: 'Sne! ❄️', attr: 'snestorm', ms: 6000 },
  };

  /** How Klippe reacts to an easter egg from the search field (null: not one). */
  function eggPlan(ord, now = new Date()) {
    if (!Object.hasOwn(EGGS, ord)) return null;
    const plan = { ord, ...EGGS[ord] };
    if (ord === 'fredag') plan.line = now.getDay() === 5 ? 'Det ER fredag! 🎉' : 'Fredag? Snart! 🎉';
    return plan;
  }

  const DIRT_LEVELS = ['lidt', 'beskidt', 'meget'];

  /** data-beskidt from the bath's state (null: clean, or the bath is switched off). */
  function dirtLevel(rod, enabled = true) {
    if (!enabled || !rod || !rod.aktiv || !(rod.antal > 0)) return null;
    return DIRT_LEVELS.includes(rod.niveau) ? rod.niveau : null;
  }

  /** "Uh, der ligger 7 løse filer i roden af Rikke Lindholm – jeg bliver helt beskidt 🫣 Se forslagene i Projektsøg". */
  function dirtLine(rod) {
    const level = dirtLevel(rod);
    if (!level) return null;
    const files = rod.antal === 1 ? '1 løs fil' : `${rod.antal} løse filer`;
    const how = { lidt: 'lidt beskidt', beskidt: 'beskidt', meget: 'helt beskidt' }[level];
    const where = shorten(rod.projekt, 32) || 'projektmappen';
    return `Uh, der ligger ${files} i roden af ${where} – jeg bliver ${how} 🫣 Se ${rod.antal === 1 ? 'forslaget' : 'forslagene'}`
      + ' i Projektsøg';
  }

  const MEMORY_KINDS = { klip: 'klip', billeder: 'billeder', lydfiler: 'lydfiler', filer: 'filer' };

  /** "📸 I dag for 3 år siden: 112 klip i Rikke Lindholm" (GET /api/minder; null: no memory). */
  function memoryLine(item) {
    const m = item && item.minde;
    if (!m || !(m.antal > 0) || !(m.aar_siden > 0)) return null;
    const ago = m.aar_siden === 1 ? 'et år' : `${m.aar_siden} år`;
    return `📸 I dag for ${ago} siden: ${m.antal} ${MEMORY_KINDS[m.slags] || 'filer'} i ${shorten(item.name, 40) || 'et projekt'}`;
  }

  // ---------------------------------------------------------------- the render game (SPEC §23.10)
  // An endless runner in units: the canvas is 160 units high (its width follows the canvas), the track's
  // top at y 132; heights are measured up from the track. Klippe stands at x 44.
  const GAME = { w: 210, h: 160, ground: 132, kx: 40, kw: 24, kh: 30, gravity: 1650, jump: 560, jump2: 470, speed0: 140,
    speedMax: 360, ramp: 5, fallTo: -60 };

  /** A small seeded random generator (mulberry32): the same seed, the same course. */
  function seeded(seed) {
    let a = (Number(seed) >>> 0) || 1;
    return () => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /** A new round, `w` units wide. */
  function gameNew(seed = 1, w = 320) {
    return { w, t: 0, dist: 0, speed: GAME.speed0, y: 0, vy: 0, jumps: 0, ground: true, falling: false, over: false,
      why: null, obstacles: [], frames: [], taken: 0, score: 0, next: w * 0.9, rng: seeded(seed) };
  }

  /** A jump – a second one in the air (once). False when it cannot. */
  function gameJump(g) {
    if (g.over || g.falling) return false;
    if (g.ground) {
      Object.assign(g, { vy: GAME.jump, ground: false, jumps: 1 });
      return true;
    }
    if (g.jumps < 2) {
      Object.assign(g, { vy: GAME.jump2, jumps: 2 });
      return true;
    }
    return false;
  }

  /** Two boxes {x, w, b (bottom, up from the track), h} overlap. */
  function gameHits(a, o) {
    return a.x < o.x + o.w && a.x + a.w > o.x && a.b < o.b + o.h && a.b + a.h > o.b;
  }

  /** Klippe's box – a little smaller than its drawing, to be fair. */
  function gameKlippe(g) {
    return { x: GAME.kx + 4, w: GAME.kw - 8, b: g.y + 1, h: GAME.kh - 5 };
  }

  const GAME_KINDS = ['offline', 'sort', 'glitch', 'glimt', 'hul'];

  /** The next klippefejl at the right edge, and maybe a film frame to catch. */
  function gameSpawn(g) {
    const r = g.rng;
    const pick = r();
    const kind = g.t < 6 ? (pick < 0.55 ? 'offline' : 'sort') : pick < 0.3 ? 'offline' : pick < 0.5 ? 'sort'
      : pick < 0.7 ? 'glitch' : pick < 0.85 ? 'glimt' : 'hul';
    const x = g.w + 10;
    const sizes = { offline: [20 + r() * 8, 20 + r() * 8, 0], sort: [12 + r() * 6, 18 + r() * 14, 0],
      glitch: [16 + r() * 8, 34 + r() * 10, 0], glimt: [26 + r() * 10, 14, 40 + r() * 8], hul: [30 + r() * 16, 0, 0] };
    const [w, h, b] = sizes[kind];
    g.obstacles.push({ kind, x, w, h, b });
    if (r() < 0.6) g.frames.push({ x: x + w + 30 + r() * 40, b: [8, 30, 62][Math.floor(r() * 3)], w: 10, h: 9, taken: false });
    // The faster it goes, the more room between them (0.85–1.6 s apart).
    g.next = Math.max(110, g.speed * (0.85 + r() * 0.75)) + w;
  }

  /** One step of `dt` seconds; returns what happened: "frame" (a film frame caught), "crash", "fall". */
  function gameStep(g, dt) {
    const events = [];
    if (g.over) return events;
    const step = Math.max(0, Math.min(0.05, Number(dt) || 0));
    g.t += step;
    if (g.falling) {
      g.vy -= GAME.gravity * step;
      g.y += g.vy * step;
      if (g.y <= GAME.fallTo) Object.assign(g, { over: true, why: 'hul' });
      return events;
    }
    g.speed = Math.min(GAME.speedMax, GAME.speed0 + GAME.ramp * g.t);
    const dx = g.speed * step;
    g.dist += dx;
    for (const o of g.obstacles) o.x -= dx;
    for (const f of g.frames) f.x -= dx;
    g.obstacles = g.obstacles.filter((o) => o.x + o.w > -20);
    g.frames = g.frames.filter((f) => f.x + f.w > -20 && !f.taken);
    g.next -= dx;
    if (g.next <= 0) gameSpawn(g);
    if (!g.ground) {
      g.vy -= GAME.gravity * step;
      g.y += g.vy * step;
    }
    const feet = GAME.kx + GAME.kw / 2;
    const hole = g.obstacles.find((o) => o.kind === 'hul' && feet > o.x + 3 && feet < o.x + o.w - 3);
    if (g.y <= 0) {
      if (hole) {
        Object.assign(g, { falling: true, ground: false, vy: Math.min(g.vy, 0) });
        events.push('fall');
        return events;
      }
      Object.assign(g, { y: 0, vy: 0, ground: true, jumps: 0 });
    }
    const me = gameKlippe(g);
    if (g.obstacles.some((o) => o.kind !== 'hul' && gameHits(me, o))) {
      Object.assign(g, { over: true, why: 'fejl' });
      events.push('crash');
    }
    for (const f of g.frames) {
      if (!f.taken && gameHits(me, f)) {
        f.taken = true;
        g.taken += 1;
        events.push('frame');
      }
    }
    g.score = gameScore(g);
    return events;
  }

  /** distance/10 + 10 per film frame. */
  function gameScore(g) {
    return Math.floor(g.dist / 10) + g.taken * 10;
  }

  /** Two boxes {x, y (top, down from the top), w, h} overlap. */
  function boxHits(a, b) {
    return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
  }

  // Fejljagt (SPEC §23.10): Klippe stands on the timeline at the bottom and claps klippefejl out of the
  // sky – they drift down in a swaying flock and drop noise. In units, y down: the field is at least 160
  // high (all of a taller canvas), the timeline's top 10 above its bottom. Three takes; a flock that
  // reaches Klippe's height ends the round.
  const SKYD = { h: 160, ground: 10, kw: 20, kh: 22, speed: 130, shot: 240, reload: 0.25, fw: 16, fh: 11, gapX: 8,
    gapY: 7, top: 14, takes: 3, safe: 1.2, pause: 1.2 };
  const SKYD_POINTS = { sort: 10, glitch: 20, offline: 30 };

  function skydNew(seed = 1, w = 320, h = SKYD.h) {
    const fieldH = Math.max(SKYD.h, Number(h) || 0);
    const g = { w, h: fieldH, floor: fieldH - SKYD.ground, t: 0, x: w / 2, dir: 0, aim: null, fire: false, cool: 0, shots: [], noise: [], frames: [],
      fejl: [], wave: 0, fy: 0, rows: 0, cols: 0, takes: SKYD.takes, safe: 0, pause: 0, over: false, why: null,
      score: 0, kills: 0, next: 1.5, rng: seeded(seed) };
    skydWave(g);
    return g;
  }

  /** The next flock: more rows as the waves go by (bottom row "sort", then glitch, then Media Offline). */
  function skydWave(g) {
    g.wave += 1;
    g.rows = Math.min(2 + Math.floor((g.wave - 1) / 2), 4);
    g.cols = Math.max(4, Math.min(8, Math.floor((g.w - 30) / (SKYD.fw + SKYD.gapX))));
    g.fejl = [];
    for (let r = 0; r < g.rows; r += 1) {
      const fromBottom = g.rows - 1 - r;
      const kind = fromBottom === 0 ? 'sort' : fromBottom === 1 ? 'glitch' : 'offline';
      for (let c = 0; c < g.cols; c += 1) {
        g.fejl.push({ kind, r, c, hp: kind === 'offline' ? 2 : 1, x: 0, y: 0, w: SKYD.fw, h: SKYD.fh, ph: g.rng() * 6.3 });
      }
    }
    g.fy = -g.rows * (SKYD.fh + SKYD.gapY) - 4;                  // it comes in from above
    skydPlace(g);
  }

  /** Where each klippefejl is: the flock sways side to side (glitch ones shiver too). */
  function skydPlace(g) {
    const width = g.cols * SKYD.fw + (g.cols - 1) * SKYD.gapX;
    const amp = Math.max(0, (g.w - width) / 2 - 6);
    const sway = Math.sin(g.t * (0.6 + 0.08 * g.wave)) * amp;
    const left = (g.w - width) / 2 + sway;
    for (const f of g.fejl) {
      f.x = left + f.c * (SKYD.fw + SKYD.gapX) + (f.kind === 'glitch' ? Math.sin(g.t * 7 + f.ph) * 1.5 : 0);
      f.y = SKYD.top + g.fy + f.r * (SKYD.fh + SKYD.gapY);
    }
  }

  /** Klippe's box – a little smaller than its drawing. */
  function skydKlippe(g) {
    return { x: g.x - SKYD.kw / 2 + 3, y: g.floor - SKYD.kh + 4, w: SKYD.kw - 6, h: SKYD.kh - 4 };
  }

  /** A clap upwards – when the clapper is ready again. */
  function skydFire(g) {
    if (g.over || g.cool > 0) return false;
    g.shots.push({ x: g.x - 1, y: g.floor - SKYD.kh - 6, w: 2, h: 6 });
    g.cool = SKYD.reload;
    return true;
  }

  /** One step of `dt` seconds; returns what happened: "shot", "hit", "kill", "frame", "hurt", "wave". */
  function skydStep(g, dt) {
    const events = [];
    if (g.over) return events;
    const step = Math.max(0, Math.min(0.05, Number(dt) || 0));
    g.t += step;
    g.cool = Math.max(0, g.cool - step);
    g.safe = Math.max(0, g.safe - step);
    const half = SKYD.kw / 2 + 2;
    if (g.aim !== null && Number.isFinite(g.aim)) {
      const d = g.aim - g.x;
      g.x += Math.sign(d) * Math.min(Math.abs(d), SKYD.speed * step);
    } else {
      g.x += g.dir * SKYD.speed * step;
    }
    g.x = Math.max(half, Math.min(g.w - half, g.x));
    if (g.fire && skydFire(g)) events.push('shot');
    for (const s of g.shots) s.y -= SKYD.shot * step;
    for (const n of g.noise) n.y += n.v * step;
    for (const f of g.frames) f.y += 40 * step;
    if (g.pause > 0) {                                          // a breath between two flocks
      g.pause -= step;
      if (g.pause <= 0) skydWave(g);
    } else {
      const alive = g.fejl.filter((f) => f.hp > 0).length;
      const total = g.rows * g.cols;
      // as fast down a tall field as down a short one
      const speed = g.fy < 0 ? 30 : (1.4 + 0.45 * g.wave) * (1 + 1.2 * (1 - alive / total)) * (g.h / SKYD.h);
      g.fy += speed * step;
      skydPlace(g);
      g.next -= step;
      if (g.next <= 0 && g.fy >= 0) {
        const lowest = new Map();
        for (const f of g.fejl) if (f.hp > 0 && (!lowest.has(f.c) || lowest.get(f.c).r < f.r)) lowest.set(f.c, f);
        const from = [...lowest.values()];
        if (from.length) {
          const f = from[Math.floor(g.rng() * from.length) % from.length];
          g.noise.push({ x: f.x + f.w / 2 - 1.5, y: f.y + f.h, w: 3, h: 6, v: 60 + 6 * g.wave });
        }
        g.next = Math.max(0.45, 1.5 - 0.1 * g.wave) * (0.6 + g.rng() * 0.8);
      }
    }
    for (const s of g.shots) {
      const f = g.fejl.find((o) => o.hp > 0 && boxHits(s, o));
      if (f) {
        s.y = -99;
        f.hp -= 1;
        if (f.hp > 0) {
          events.push('hit');
        } else {
          g.kills += 1;
          g.score += SKYD_POINTS[f.kind];
          events.push('kill');
          if (g.rng() < 0.2) g.frames.push({ x: f.x + 3, y: f.y, w: 10, h: 9 });
        }
        continue;
      }
      const n = g.noise.find((o) => boxHits(s, o));
      if (n) {
        s.y = -99;
        n.y = 999;
        g.score += 2;
      }
    }
    g.shots = g.shots.filter((s) => s.y + s.h > -10);
    const me = skydKlippe(g);
    g.frames = g.frames.filter((f) => {
      if (boxHits(me, f)) {
        g.score += 25;
        events.push('frame');
        return false;
      }
      return f.y < g.h;
    });
    if (g.safe <= 0 && g.noise.some((n) => boxHits(me, n))) {
      g.takes -= 1;
      g.noise = [];
      g.safe = SKYD.safe;
      events.push('hurt');
      if (g.takes <= 0) Object.assign(g, { over: true, why: 'stoej' });
    }
    g.noise = g.noise.filter((n) => n.y < g.h);
    if (!g.over && g.pause <= 0) {
      if (g.fejl.some((f) => f.hp > 0 && f.y + f.h >= g.floor - SKYD.kh)) {
        Object.assign(g, { over: true, why: 'nede', takes: 0 });
      } else if (g.fejl.every((f) => f.hp <= 0)) {
        g.score += 50 * g.wave;
        g.pause = SKYD.pause;
        g.noise = [];
        events.push('wave');
      }
    }
    return events;
  }

  // Over tidslinjen (SPEC §23.10): Klippe hops from the Media Pool at the bottom over three audio
  // tracks, the marker strip and three video tracks to Deliver at the top, while clips slide along
  // the tracks. In units, y down: row 0 (bottom) to row 8 (Deliver), 16 high each, from y 16.
  // A clip knocks Klippe back to the last safe row (the Media Pool or the markers); three takes.
  const KRYDS = { h: 160, rows: 9, lane: 16, top: 16, kw: 14, kh: 13, hop: 0.12, stepX: 14, takes: 3, safe: 1.0,
    markers: 4, goal: 8 };
  const KRYDS_TRACKS = { 1: ['A1', 18, 1], 2: ['A2', 26, -1], 3: ['A3', 22, 1], 5: ['V1', 30, -1], 6: ['V2', 20, 1],
    7: ['V3', 34, -1] };

  function krydsNew(seed = 1, w = 320) {
    const g = { w, t: 0, row: 0, x: w / 2, from: null, hopT: 0, best: 0, level: 1, takes: KRYDS.takes, safe: 0,
      over: false, why: null, score: 0, delivered: 0, lanes: [], frame: null, nextFrame: 3, rng: seeded(seed) };
    for (const [row, [name, , dir]] of Object.entries(KRYDS_TRACKS)) {
      const lane = { row: Number(row), name, dir, kind: name[0] === 'A' ? 'lyd' : 'video', clips: [] };
      let x = -60 + g.rng() * 30;
      while (x < w + 60) {
        const clipW = krydsClipWidth(g, lane);
        lane.clips.push({ x, w: clipW });
        x += clipW + krydsGap(g);
      }
      g.lanes.push(lane);
    }
    return g;
  }

  function krydsClipWidth(g, lane) {
    return lane.kind === 'lyd' ? 30 + g.rng() * 30 : 22 + g.rng() * 24;
  }

  function krydsGap(g) {
    return 30 + g.rng() * 44;
  }

  /** How fast a track's clips slide – faster for each delivery. */
  function krydsSpeed(g, row) {
    return KRYDS_TRACKS[row][1] * (1 + 0.18 * (g.level - 1));
  }

  /** The top of a row (y down). */
  function krydsRowY(row) {
    return KRYDS.top + (KRYDS.goal - row) * KRYDS.lane;
  }

  /** A hop: "op", "ned", "venstre" or "hoejre" – not while it is in the air. */
  function krydsHop(g, dir) {
    if (g.over || g.hopT > 0) return false;
    const half = KRYDS.kw / 2 + 1;
    let { row, x } = g;
    if (dir === 'op') row += 1;
    else if (dir === 'ned') row -= 1;
    else if (dir === 'venstre') x -= KRYDS.stepX;
    else if (dir === 'hoejre') x += KRYDS.stepX;
    else return false;
    row = Math.max(0, Math.min(KRYDS.goal, row));
    x = Math.max(half, Math.min(g.w - half, x));
    if (row === g.row && x === g.x) return false;
    g.from = { row: g.row, x: g.x };
    Object.assign(g, { row, x, hopT: KRYDS.hop });
    return true;
  }

  function krydsKlippe(g) {
    return { x: g.x - KRYDS.kw / 2 + 2, y: krydsRowY(g.row) + 2, w: KRYDS.kw - 4, h: KRYDS.lane - 4 };
  }

  /** One step of `dt` seconds; returns what happened: "step" (a new row), "frame", "hurt", "leveret". */
  function krydsStep(g, dt) {
    const events = [];
    if (g.over) return events;
    const step = Math.max(0, Math.min(0.05, Number(dt) || 0));
    g.t += step;
    g.safe = Math.max(0, g.safe - step);
    for (const lane of g.lanes) {
      const dx = lane.dir * krydsSpeed(g, lane.row) * step;
      for (const c of lane.clips) c.x += dx;
      lane.clips = lane.clips.filter((c) => c.x < g.w + 80 && c.x + c.w > -80);
      if (lane.dir > 0) {
        const first = lane.clips.reduce((a, c) => (a && a.x < c.x ? a : c), null);
        const edge = first ? first.x : -10;                     // an empty track fills from outside
        if (edge > -50) {
          const clipW = krydsClipWidth(g, lane);
          lane.clips.push({ x: edge - krydsGap(g) - clipW, w: clipW });
        }
      } else {
        const last = lane.clips.reduce((a, c) => (a && a.x + a.w > c.x + c.w ? a : c), null);
        const edge = last ? last.x + last.w : g.w + 10;
        if (edge < g.w + 50) lane.clips.push({ x: edge + krydsGap(g), w: krydsClipWidth(g, lane) });
      }
    }
    if (g.frame) {
      g.frame.t -= step;
      if (g.frame.t <= 0) g.frame = null;
    } else {
      g.nextFrame -= step;
      if (g.nextFrame <= 0) {
        const rows = Object.keys(KRYDS_TRACKS).map(Number);
        g.frame = { row: rows[Math.floor(g.rng() * rows.length) % rows.length], x: 10 + g.rng() * (g.w - 30), w: 10,
          h: 9, t: 6 };
        g.nextFrame = 3 + g.rng() * 3;
      }
    }
    if (g.hopT > 0) {
      g.hopT = Math.max(0, g.hopT - step);
      if (g.hopT > 0) return events;
      g.from = null;
      if (g.row > g.best) {
        g.best = g.row;
        g.score += 10;
        events.push('step');
      }
      if (g.row === KRYDS.goal) {
        g.score += 50 + 20 * g.level;
        g.delivered += 1;
        g.level += 1;
        Object.assign(g, { row: 0, best: 0, x: g.w / 2 });
        events.push('leveret');
        return events;
      }
    }
    const me = krydsKlippe(g);
    if (g.frame && g.frame.row === g.row && boxHits(me, { x: g.frame.x, y: krydsRowY(g.row) + 3, w: g.frame.w, h: g.frame.h })) {
      g.frame = null;
      g.score += 25;
      events.push('frame');
    }
    const lane = g.lanes.find((l) => l.row === g.row);
    if (lane && g.safe <= 0 && lane.clips.some((c) => me.x < c.x + c.w && me.x + me.w > c.x)) {
      g.takes -= 1;
      g.safe = KRYDS.safe;
      events.push('hurt');
      if (g.takes <= 0) {
        Object.assign(g, { over: true, why: 'klip' });
      } else {
        g.row = g.row > KRYDS.markers ? KRYDS.markers : 0;      // back to the last safe row
      }
    }
    return events;
  }

  /** The render game's games, in the order of the picker. */
  const GAME_IDS = ['hop', 'skyd', 'kryds'];

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
    hungerLevel, foodLine, foodSay, foodHint, dropLeft, isCall, callerName, callLine, isRobotSheet, muzzle,
    directorLine, buildLine, doneLine, renderProgress, renderLine, renderDoneLine, renderKey, guestLook, visitLine,
    visitReply, yearShareLine, deliveryLine, catFrames, nextSpin, keyGreen, renameRefs, clockMinutes, natFase,
    fyraftenLabel, natLine, halloweenWeek, seasonFor, lunchTime, yearInReview, clockLabel, guessChoices, guessFromField,
    betKey, betStale, pickBet, betTitle, betStatus, betResult, lunchLine, slikLine, slikCountdown, slikSeconds,
    balladeTokens, possessive, yourItem, balladeLine, slikSvarLine, BET_STALE_S, SLIK_MIN_S,
    giftIcon, doorView, giftLine, eggPlan, dirtLevel, dirtLine, memoryLine, seeded, gameNew, gameJump, gameHits,
    gameKlippe, gameSpawn, gameStep, gameScore, boxHits, skydNew, skydWave, skydFire, skydStep, skydKlippe, krydsNew,
    krydsHop, krydsStep, krydsRowY, krydsKlippe, SKYD, SKYD_POINTS, KRYDS, KRYDS_TRACKS, GAME_IDS, STAGES, FOCUS_STARS, BREAK_NUDGES, ROBOT_POSES, ROBOT_COUNT, BODY_SCALE,
    DIRECTOR_LINES, SLOT_KEYS, OUTFITS, KOSTUMER, FESTKAT_FRAMES, POST_NOTES, POST_MAX, EGGS, GAME, GAME_KINDS,
    YAWN_LINES, DANCE_LINES };
  if (typeof module === 'object' && module.exports) module.exports = helpers;
  if (typeof document === 'undefined') return;

  // ===========================================================================================
  // Sprite sheet: `?sprites=normal,happy,…&stage=baby&outfit=none&cell=240` shows the pet in
  // each pose side by side on a transparent page. Headless Edge takes a screenshot of it, and
  // the games out of the box (projektsog/petplay.py) are drawn from that – so they always look
  // exactly like the pet here.
  // ===========================================================================================

  const PARTY_POSES = new Set(['happy', 'cheer']);
  const SVG_NS = 'http://www.w3.org/2000/svg';
  // The drip's falling drop and the robots' antenna light are SMIL (it has to run inside <use>),
  // which reduced motion does not stop.
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    for (const animate of document.querySelectorAll('#mad-drop animate, #robot-tegning animate')) animate.remove();
  }
  const query = new URLSearchParams(window.location.search);
  if (query.has('sprites')) {
    renderSprites(query);
    return;
  }

  function renderSprites(params) {
    const poses = (params.get('sprites') || 'normal').split(',').filter(Boolean);
    if (isRobotSheet(poses)) {
      renderRobotSprites(poses, Number(params.get('cell')) || 120);
      return;
    }
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

  /** The robot crew's sheet (crew.py): each pose in a `cell`-px cell, the 60-unit drawing filling
   *  it (2 CSS px per unit at 120), facing right. Deep copies, not <use>: the pose CSS reaches in. */
  function renderRobotSprites(poses, cell) {
    const drawing = document.getElementById('robot-tegning');
    const sheet = document.createElement('div');
    sheet.className = 'sheet';
    for (const pose of poses) {
      const box = document.createElement('div');
      box.className = 'sprite sprite--robot';
      box.dataset.pose = pose;
      box.style.width = `${cell}px`;
      box.style.height = `${cell}px`;
      const svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('class', 'robot-ark');
      svg.setAttribute('viewBox', '0 0 60 60');
      svg.setAttribute('aria-hidden', 'true');
      const copy = drawing.cloneNode(true);
      copy.removeAttribute('id');
      for (const animate of copy.querySelectorAll('animate')) animate.remove();   // nothing moves
      svg.append(copy);
      box.append(svg);
      sheet.append(box);
    }
    document.documentElement.classList.add('sprites');
    document.body.replaceChildren(sheet);
  }

  // ===========================================================================================
  // Page
  // ===========================================================================================

  const STATUS_POLL_MS = 5000;
  const HISTORY_POLL_MS = 10 * 60e3;
  const BUBBLE_MS = 5500;
  const ANSWER_MS = 1600;                    // "Hallo? 📞" – the receiver at the ear, then the card
  const AIM_MAX_MS = 20e3;                   // a lost "sigter-slut" never leaves Klippe aiming
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
    hunger: $('hunger'), maet: $('maet'), maetBar: $('maet-bar'), energi: $('energi'), feed: $('feed'),
    tray: $('tray'), trayGrid: $('tray-grid'), trayClose: $('tray-close'), dream: $('drom-mad'),
    eatBite: $('mad-bid'), eatThing: $('mad-ting'), phone: $('telefon'), phoneCount: $('telefon-antal'),
    demoRobots: $('demo-robots'), demoCall: $('demo-call'), demoParty: $('demo-party'), demoVisit: $('demo-visit'),
    renderStatus: $('render-status'), renderWhat: $('render-what'), renderTal: $('render-tal'), renderBar: $('render-bar'),
    // fun in the office (SPEC §23)
    panelProev: $('panel-proev'), frokostKnap: $('frokost-knap'), postKnap: $('post-knap'), julKnap: $('jul-knap'),
    julAntal: $('jul-antal'), aarKnap: $('aar-knap'), frokostSkilt: $('frokost-skilt'),
    postPanel: $('post-panel'), postTil: $('post-til'), postHurtig: $('post-hurtig'), postTekst: $('post-tekst'),
    postSend: $('post-send'), postFejl: $('post-fejl'), postLuk: $('post-luk'),
    slik: $('slik'), slikTekst: $('slik-tekst'), slikGiv: $('slik-giv'), slikTid: $('slik-tid'),
    vm: $('vaeddemaal'), vmTitel: $('vm-titel'), vmValg: $('vm-valg'), vmTid: $('vm-tid'), vmGaet: $('vm-gaet'),
    vmStatus: $('vm-status'),
    spil: $('spil'), spilCanvas: $('spil-canvas'), spilPoint: $('spil-point'), spilBedste: $('spil-bedste'),
    spilRekord: $('spil-rekord'), spilRender: $('spil-render'), spilStart: $('spil-start'), spilLuk: $('spil-luk'),
    spilBesked: $('spil-besked'), spilKnap: $('spil-knap'), spilValg: $('spil-valg'),
  };
  el.box = el.pet.querySelector('.pakke');

  const state = {
    status: null, todayS: null, focusStart: null, focusMin: 0, hours: 0, stage: null, streak: 0,
    name: 'Klippe', goalH: 6, bubbles: [], bubbleTimer: 0, seenJobs: new Set(), cards: null, project: null,
    job: null, jobsStarted: new Set(), lookSent: '', lookTimer: 0, hatched: false, hatching: false,
    messages: [], messagesSeen: new Set(), messagesLoaded: false, messageTag: null, messagesOpen: false,
    pet: null, panelTab: 'trophies', looks: 0, equipped: null,
    food: null, menu: [], hunger: null, energy: null, craving: null, eating: null, feeding: false, laterProgress: [],
    answering: null, revealTag: null, callTags: new Set(),
    build: null, directorTimer: 0, naughtyTimer: 0, naughty: null, aimTimer: 0,
    render: null, renderSeen: null, peers: [], visit: null, visitWaiting: null, fest: null, festWaiting: null,
    stageTimer: 0, festkat: null,
    // fun in the office (SPEC §23)
    settings: {}, wear: {}, tmpHaand: null, nat: null, natWakeUntil: 0, natDemo: null, natEggUntil: 0, yawnTimer: 0,
    nodTimer: 0, dans: false, dansDemoUntil: 0, dansLineAt: 0, season: null, halloweenDemoUntil: 0, saeson: null,
    balladeLocal: null, jul: null, julDemo: false, julDemoOpen: {}, julOpening: false, julPeek: false, rod: null,
    bathWaiting: null, bath: null, lunch: null, postTil: null, postOut: null, bets: new Map(), betsCelebrated: new Set(),
    betChoice: null, slik: null, eggWaiting: null, egg: null, cameo: null, attrTimers: {}, game: null, spil: null,
    snowTimer: 0, betEdit: null, ready: false,       // ready: the hello is said – the fun things may speak now
    sseSeq: {}, day: null, visitWaitingAt: 0, postSvarTil: null,
    tempo: 1,                                // tests speed up the visits and the party (their timelines × tempo)
  };

  // ---------------------------------------------------------------- the clock (SPEC §23)
  // Time-of-day things (Natte-Klippe, the seasons, the lunch bell) follow clock(); `?nu=2026-10-12T16:05`
  // starts it at another moment (the tests' fixed day), from where it runs on.
  const CLOCK_OFFSET = (() => {
    const fixed = Date.parse(query.get('nu') || '');
    return Number.isFinite(fixed) ? fixed - Date.now() : 0;
  })();

  function clock() {
    return new Date(Date.now() + CLOCK_OFFSET);
  }

  /** `fn` after `ms` × tempo. */
  function after(ms, fn) {
    return setTimeout(fn, ms * state.tempo);
  }

  function show(node, on) {
    if (node) node.hidden = !on;
  }

  function on(node, type, fn) {
    if (node) node.addEventListener(type, fn);
  }

  /** A data-* attribute on #app for `ms` (a pose that comes and goes). */
  function tempAttr(name, ms, value = '') {
    clearTimeout(state.attrTimers[name]);
    el.app.dataset[name] = value;
    state.attrTimers[name] = after(ms, () => {
      delete el.app.dataset[name];
      delete state.attrTimers[name];
    });
  }

  function setting(key, fallback = true) {
    const value = state.settings[key];
    return value === undefined ? fallback : value;
  }

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

  /** A GET that a newer SSE event of `type` wins over (on load and whenever the stream (re)opens): its
   *  answer, or null when such an event came while it was on its way – that event is newer. */
  async function fresh(type, path) {
    const seq = state.sseSeq[type] || 0;
    const data = await get(path);
    return (state.sseSeq[type] || 0) === seq ? data : null;
  }

  async function loadSettings() {
    try {
      applySettings((await get('/api/settings')).settings);
    } catch { /* keep the defaults */ }
  }

  function applySettings(settings) {
    if (!settings) return;
    state.settings = { ...state.settings, ...settings };
    state.name = settings.widget_pet_name || 'Klippe';
    state.goalH = Number(settings.widget_daily_goal_hours) || 6;
    el.name.textContent = state.name;
    renderStats();
    const hatched = Boolean(settings.widget_hatched);
    if (hatched !== state.hatched) {
      state.hatched = hatched;
      if (state.stage) applyStage();
    }
    funTick();                                 // a switch for the fun things (SPEC §23.0) takes effect at once
    if (state.rod) applyRod(state.rod, true);
    renderBetCard();
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
    updateDance();
    lunchCheck(mood);
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
    renderHunger();
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
      renderHunger();
      reportLook();
    }, 1800);
  }

  function renderMoodLine() {
    if (el.app.dataset.play === 'out') {
      el.mood.textContent = `${state.name} er ude at lege med musen 🎈 Rør den, så kommer ${state.name} hjem`;
      return;
    }
    if (state.eating) {
      const verb = { drik: 'drikker', drop: 'får' }[state.eating.kind] || 'spiser';
      el.mood.textContent = `${state.name} ${verb} ${state.eating.name} ${state.eating.kind === 'drop' ? '💉' : '😋'}`;
      return;
    }
    if (state.fest) {
      const file = shorten(state.fest.d.fil || state.fest.d.projekt, 36) || 'leveringen';
      el.mood.textContent = state.fest.stamped ? `🎉 ${state.name} fejrer ${file}` : `📦 ${state.name} pakker ${file} …`;
      return;
    }
    if (state.lunch && state.lunch.phase === 'ude') {
      el.mood.textContent = `🍽️ ${state.name} er til frokost – klik på skiltet, så kommer ${state.name} hjem`;
      return;
    }
    const rendering = renderLine(state.render, state.name);
    const building = buildLine(state.build, state.name);
    const job = jobLine(transferInfo(state.job), state.job);
    const mood = moodFor(state.status);
    const night = natLine(state.nat, mood, setting('widget_fyraften', '16:00'), state.name);
    const food = hatchedFood();
    const hungry = foodLine(food, mood, state.name, state.craving);
    const rush = food && food.energi && mood === 'working' ? '⚡ ' : '';   // turbo editing
    el.mood.textContent = rendering || building || job || night || hungry || `${rush}${moodLine(state.status, state.name)}`;
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

  /** A call shows as a phone until it is answered – and while Klippe says "Hallo?". */
  function shownAsCall(m) {
    return isCall(m) || m.tag === state.answering;
  }

  /** One card at a time (never a long list): the one that needs you, ‹ 1/3 › to the others.
   *  Quiet messages alone only show a small "📬 2 beskeder · vis" line. An unanswered call is a
   *  ringing phone and one line instead of its card (SPEC §21.1). */
  function renderMessages(list) {
    const messages = messageOrder(list);
    const fresh = messages.filter((m) => !state.messagesSeen.has(messageKey(m)));
    for (const m of messages) state.messagesSeen.add(messageKey(m));
    state.messages = messages;
    // A call answered – here after Klippe's "Hallo?", or anywhere else – shows its real card, glowing.
    const answered = messages.filter((m) => !shownAsCall(m)
      && (m.tag === state.revealTag || state.callTags.has(m.tag)));
    state.revealTag = null;
    state.callTags = new Set(messages.filter(shownAsCall).map((m) => m.tag));
    const loud = fresh.find((m) => m.prioritet !== 'stille') || answered[0];
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
      const m = messages[index];
      el.messages.replaceChildren(shownAsCall(m) ? callCard(m, index, messages.length)
        : messageCard(m, fresh.includes(m) || answered.includes(m), index, messages.length));
    }
    el.messages.hidden = !messages.length;
    renderPhone(messages);
    if (messages.some((m) => isCall(m) && m.ringer)) comeHome();      // a call brings Klippe back from lunch
    // No jump and clap for a call: the phone is the alarm.
    if (loud && loud.lyd !== false && !shownAsCall(loud) && !answered.includes(loud) && state.messagesLoaded) {
      jump();
      pulse('clap', 950);
    }
    state.messagesLoaded = true;
    reportLook();
  }

  /** data-telefon: ringer (an unanswered call rings), ubesvaret (missed: a red badge), svarer. */
  function renderPhone(messages) {
    const calls = messages.filter(isCall);
    const phone = state.answering ? 'svarer' : calls.some((m) => m.ringer) ? 'ringer' : calls.length ? 'ubesvaret' : null;
    if (phone) {
      el.app.dataset.telefon = phone;
    } else {
      delete el.app.dataset.telefon;
    }
    el.phoneCount.textContent = String(Math.min(9, Math.max(1, calls.length)));
  }

  /** The line a call shows instead of its card: "📞 Mette ringer" · Tag telefonen · ×. */
  function callCard(m, index = 0, count = 1) {
    const answering = m.tag === state.answering;
    const card = node('article', `message message--call${answering ? '' : m.ringer ? ' is-ringing' : ' is-missed'}`);
    card.dataset.tag = m.tag;
    card.append(node('p', 'message__title', answering ? `📞 ${state.name} tager telefonen …` : callLine(m)));
    if (!answering) {
      const row = node('div', 'message__buttons');
      const button = node('button', 'message__button', 'Tag telefonen');
      button.type = 'button';
      button.addEventListener('click', () => answerCall(m.tag));
      row.append(button);
      card.append(row);
    }
    card.append(smallButton('message__close', '×', 'Afvis opkaldet', () => closeMessage(m.tag)));
    if (count > 1) card.append(messagePager(index, count, false));
    return card;
  }

  /** Klippe answers the call (the button or the phone itself): the receiver to its ear and
   *  "Hallo? 📞" – then the call's real card comes up, glowing, with its "Byg nu". */
  async function answerCall(tag) {
    if (state.answering || !tag) return;
    state.answering = tag;
    renderMessages(state.messages);
    say('Hallo? 📞', ANSWER_MS, true);
    const held = new Promise((resolve) => setTimeout(resolve, ANSWER_MS));
    let ok = true;
    try {
      await send('POST', '/api/messages/svar', { tag });
    } catch (err) {
      ok = false;
      say(`Øv – ${err.message}`, 6000);
    }
    if (ok) {
      const m = state.messages.find((x) => x.tag === tag);
      if (m) Object.assign(m, { besvaret: true, ringer: false });    // its `messages` event may come later
      await held;
      state.revealTag = tag;
    }
    state.answering = null;
    renderMessages(state.messages);
    if (!ok) loadMessages();
  }

  /** The call the phone in Klippe's hand answers: the one on show, else the newest. */
  function phoneCall() {
    const shown = state.messages.find((m) => m.tag === state.messageTag);
    return shown && isCall(shown) ? shown : state.messages.find(isCall);
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
    if (count > 1 || quietOnly) card.append(messagePager(index, count, quietOnly));
    return card;
  }

  /** "‹ 1 / 3 ›" under a card – and ▾ to fold quiet messages up again. */
  function messagePager(index, count, quietOnly) {
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
    return pager;
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

  el.phone.addEventListener('click', (event) => {
    event.stopPropagation();                 // the phone, not a pat on the head
    const call = phoneCall();
    if (call) answerCall(call.tag);
  });

  // ---------------------------------------------------------------- the robot crew (SPEC §21.3)
  /** `bygger` (SSE and GET /api/bygger): a Claude session builds. Klippe directs with a megaphone;
   *  the robots work in the box (three minis on a little timeline) or out on the screen (`ude`:
   *  only the open hatch is left). `faerdig` ends it with fireworks. */
  function applyBuild(data) {
    if (!data || typeof data !== 'object') return;
    const before = state.build;
    const on = Boolean(data.aktiv);
    state.build = on ? data : null;
    if (on) {
      el.app.dataset.bygger = data.demo ? 'demo' : 'koe';
    } else {
      delete el.app.dataset.bygger;
    }
    if (on && data.ude) {
      el.app.dataset.ude = data.retning === 'hoejre' ? 'hoejre' : 'venstre';    // the hatch they used
    } else {
      delete el.app.dataset.ude;
    }
    if (!on || data.ude) stopNaughty();       // no mini robots in the box to shoot at
    if (on && !data.ude) scheduleNaughty();
    if (!on) {
      clearTimeout(state.directorTimer);
      state.directorTimer = 0;
      aim(false);
    } else if (!state.directorTimer) {
      scheduleDirector();
    }
    if (on && !before) say(directorLine(() => 0), 3500);                 // "Action! 🎬"
    if (data.faerdig) celebrate(doneLine(data), 'fireworks', 4);
    renderMoodLine();
  }

  function robotCount() {
    return ROBOT_COUNT[el.app.dataset.stage] || ROBOT_COUNT.egg;
  }

  /** A director's line every 20–40 s while the robots build – unless Klippe is saying something. */
  function scheduleDirector() {
    clearTimeout(state.directorTimer);
    state.directorTimer = setTimeout(() => {
      state.directorTimer = 0;
      if (!state.build) return;
      if (!state.bubbleTimer && !state.eating) say(directorLine(Math.random, robotCount()), 4000);
      scheduleDirector();
    }, 20e3 + Math.random() * 20e3);
  }

  /** data-sigter: Klippe aims – towards the robots: "venstre" (mirrored) or "hoejre" (widget.css). */
  function aim(on, direction = 'venstre') {
    clearTimeout(state.aimTimer);
    if (!on) {
      delete el.app.dataset.sigter;
      return;
    }
    el.app.dataset.sigter = direction === 'hoejre' ? 'hoejre' : 'venstre';
    state.aimTimer = setTimeout(() => aim(false), AIM_MAX_MS);
  }

  /** A shot: the AWP kicks back and the muzzle flashes. */
  function shoot() {
    pulse('skud', 350);
  }

  /** `robot` events from the robots out on the screen: sigter (Klippe turns and aims), skud (each
   *  shot, `ram` = a hit), sigter-slut. */
  function applyRobot(data) {
    const event = data && data.haendelse;
    if (event === 'sigter' || event === 'skud') {
      stopNaughty();
      if (event === 'sigter' || !el.app.dataset.sigter) aim(true, data.retning);
      if (event === 'skud') {
        shoot();
        if (data.ram) floatEmoji('💥', 1, 10);
      }
    } else if (event === 'sigter-slut') {
      aim(false);
    }
  }

  /** In the box, with the AWP: every 45–90 s one mini robot gets naughty – it dances with red eyes,
   *  Klippe aims and shoots, the robot bursts and is back 2 s later. */
  function scheduleNaughty() {
    if (state.naughtyTimer || state.naughty) return;
    state.naughtyTimer = setTimeout(naughtyRobot, 45e3 + Math.random() * 45e3);
  }

  function naughtyRobot() {
    state.naughtyTimer = 0;
    if (!state.build || state.build.ude) return;
    const ready = el.app.dataset.haand === 'awp' && el.app.dataset.stage !== 'egg' && !REDUCED
      && !el.app.dataset.sigter && el.app.dataset.play !== 'out'
      && !el.app.dataset.transfer && !state.eating && !state.answering;   // the AWP is put away then
    // the ones on show: the crew's three, or the render box's two while Resolve renders
    const minis = [...el.pet.querySelectorAll('.mini')].filter((m) => m.getBoundingClientRect().width > 0);
    if (!ready || !minis.length) {
      scheduleNaughty();
      return;
    }
    const run = { mini: minis[Math.floor(Math.random() * minis.length)], timers: [] };
    const later = (ms, fn) => run.timers.push(setTimeout(() => {
      if (state.naughty === run) fn();
    }, ms));
    state.naughty = run;
    run.mini.classList.add('is-fraek');
    later(1400, () => aim(true));
    later(2500, () => {
      shoot();
      floatEmoji('💥', 1, 12);
      run.mini.classList.replace('is-fraek', 'is-poff');
    });
    later(3000, () => aim(false));
    later(4500, () => run.mini.classList.replace('is-poff', 'is-tilbage'));
    later(5100, () => {
      run.mini.classList.remove('is-tilbage');
      state.naughty = null;
      scheduleNaughty();
    });
  }

  function stopNaughty() {
    clearTimeout(state.naughtyTimer);
    state.naughtyTimer = 0;
    const run = state.naughty;
    if (!run) return;
    run.timers.forEach(clearTimeout);
    run.mini.classList.remove('is-fraek', 'is-poff', 'is-tilbage');
    state.naughty = null;
    aim(false);
  }

  async function loadBuild() {
    try {
      applyBuild({ ...(await get('/api/bygger')), faerdig: false });
    } catch { /* no crew (yet) */ }
  }

  /** "🤖 Vis robotterne" / "📞 Prøv telefonen" in the 🏆 panel: the panel closes, so the box is seen. */
  async function demo(call) {
    el.panel.hidden = true;
    try {
      await send('POST', '/api/bygger/demo', { opkald: call });
      if (!call) say('Robotterne øver sig – slip musen, så kommer de ud! 🤖', 4500);
    } catch (err) {
      say(`Øv – ${err.message}`, 5000);
    }
  }

  /** "🎉 Prøv leveringsfesten" / "👋 Prøv et besøg" in the 🏆 panel: the panel closes, so it is seen. A
   *  demo that cannot run now (`{ok: false, grund}` – no snow while something is full screen) says why. */
  async function demoNow(path) {
    el.panel.hidden = true;
    try {
      const answer = await send('POST', path, {});
      if (answer && answer.ok === false) say(`Øv – ${answer.grund || 'det kan ikke lige nu'}`, 5000);
    } catch (err) {
      say(`Øv – ${err.message}`, 5000);
    }
  }

  // ---------------------------------------------------------------- Resolve renders (SPEC §22.1)
  /** Remembers a finished render (the last 20, over reloads); true when it was known already. */
  function knownRender(key) {
    const data = memory();
    const seen = Array.isArray(data.renders) ? data.renders : [];
    if (seen.includes(key)) return true;
    data.renders = [...seen, key].slice(-20);
    remember(data);
    return false;
  }

  /** `render` (SSE, and GET /api/render on load and whenever the event stream (re)opens): while
   *  Resolve renders the mini robots feed the render box and the line under the pet counts; a render
   *  that ended is celebrated (or mourned) once. `loaded`: from the GET – a render this widget saw end
   *  before a reload stays quiet then. */
  function applyRender(data, loaded = false) {
    if (!data || typeof data !== 'object') return;
    const info = renderProgress(data);
    state.render = info ? data : null;
    if (info) {
      el.app.dataset.render = '';
      el.renderWhat.textContent = info.what;
      el.renderTal.textContent = `· ${info.tal}`;
      el.renderStatus.title = info.text;
      el.renderBar.style.width = `${info.pct || 0}%`;
      el.app.style.setProperty('--render', String((info.pct || 0) / 100));
    } else {
      delete el.app.dataset.render;
    }
    el.renderStatus.hidden = !info;
    show(el.spilKnap, Boolean(info) && setting('widget_renderspil'));     // the render game (SPEC §23.10)
    gameRender(info);
    const key = renderKey(data.faerdig);
    if (key && key !== state.renderSeen) {
      state.renderSeen = key;
      if (!knownRender(key) || !loaded) renderEnded(data.faerdig);
    }
    renderMoodLine();
    reportLook();
  }

  /** Done: fireworks. Failed: a sad face and the reason. Stopped: a word. Gone: nothing. */
  function renderEnded(f) {
    const line = renderDoneLine(f);
    if (!line) return;
    comeHome();                                  // back from lunch for it (SPEC §23.12)
    if (f.udfald === 'done') {
      celebrate(line, 'fireworks', 4);
    } else if (f.udfald === 'failed') {
      pulse('trist', 6000);
      say(line, 9000);
    } else {
      say(line, 5000);
    }
  }

  async function loadRender() {
    try {
      applyRender(await get('/api/render'), true);
    } catch { /* no Resolve bridge (yet) */ }
  }

  // ---------------------------------------------------------------- office Klippes (SPEC §22.3)
  /** GET /api/kontor: the colleagues' Klippes about now – only to know them (visits are never
   *  replayed; why Windows may ask about the network, kontor.py says once by itself). */
  async function loadKontor() {
    let data;
    try {
      data = await get('/api/kontor');
    } catch {
      return;                                // the office has not started
    }
    state.peers = Array.isArray(data.peers) ? data.peers.filter((p) => p && typeof p === 'object'
      && typeof p.pc === 'string') : [];
    const names = state.peers.map((p) => `${shorten(p.navn, 20) || 'Klippe'} (${shorten(p.pc, 15)})`);
    el.demoVisit.title = names.length ? `Kollegernes Klipper lige nu: ${names.join(', ')}`
      : 'En kollegas Klippe kigger forbi – som når den har fået et trofæ';
    renderHeaderIcons();
    if (el.postPanel && !el.postPanel.hidden) renderPost();
  }

  /** SSE `besoeg`: a colleague's Klippe comes by (the newest waits – one at most; when it came counts for
   *  a trick-or-treater: the server takes its answer only so long). */
  function visit(data) {
    if (!data || typeof data !== 'object') return;
    state.visitWaiting = data;
    state.visitWaitingAt = Date.now();
    nextOnStage();
  }

  /** Klippe is not free for a guest, a party, a bath or an easter egg: it eats, plays out of the box,
   *  rings the lunch bell or runs to (or from) lunch. */
  function stageBusy() {
    return Boolean(state.eating) || el.app.dataset.play === 'out'
      || Boolean(state.lunch && state.lunch.phase !== 'ude') || Boolean(state.bath);
  }

  /** The next party or guest once the stage is free – never while Klippe eats or plays out of the
   *  box, a guest not while a card is transferred or the render game covers the box either; a party
   *  goes first. Klippe at lunch comes home for it first; then a bath and an easter egg (SPEC §23.6,
   *  §23.8) get their turn – an egg not while Klippe is out at lunch. */
  function nextOnStage() {
    clearTimeout(state.stageTimer);
    state.stageTimer = 0;
    const waiting = state.visitWaiting || state.festWaiting || state.bathWaiting || state.eggWaiting;
    if (state.visit || state.fest || !waiting) return;
    if (state.lunch && state.lunch.phase === 'ude' && (state.visitWaiting || state.festWaiting)) comeHome();
    const busy = stageBusy() || Boolean(state.lunch);
    if (state.festWaiting && !busy) {
      const data = state.festWaiting;
      state.festWaiting = null;
      startParty(data);
    } else if (state.visitWaiting && !busy && !el.app.dataset.transfer && !state.game) {
      const data = state.visitWaiting;
      const waited = Date.now() - (state.visitWaitingAt || Date.now());
      state.visitWaiting = null;
      startVisit(data, waited);
    } else if (state.bathWaiting && !busy) {
      const data = state.bathWaiting;
      state.bathWaiting = null;
      startBath(data);
    } else if (state.eggWaiting && !stageBusy() && !state.lunch && !state.game) {
      const egg = state.eggWaiting;
      state.eggWaiting = null;
      if (Date.now() - egg.at < 20e3) runEgg(egg.ord);
      nextOnStage();
    } else {
      if (state.eggWaiting && Date.now() - state.eggWaiting.at >= 20e3) state.eggWaiting = null;   // waited too long
      state.stageTimer = setTimeout(nextOnStage, 1000);
    }
  }

  /** `fn` after `ms` (× tempo), while `run` (a visit or a party) is still on. */
  function later(run, ms, fn) {
    run.timers.push(setTimeout(() => {
      if (state.visit === run || state.fest === run) fn();
    }, ms * state.tempo));
  }

  /** Where Klippe stands on the page: the floor (the pet's y 180) and its middle. */
  function floor() {
    const r = el.pet.getBoundingClientRect();
    const k = r.height / 200;
    return { y: r.top + 180 * k, x: r.left + 100 * k };
  }

  /** `node` walks from x `from` to `to` (px) in `ms` – or just stands there (reduced motion). */
  function walk(node, from, to, ms) {
    if (REDUCED) {
      node.style.transform = `translateX(${to}px)`;
      return;
    }
    node.classList.add('gaar');
    node.animate([{ transform: `translateX(${from}px)` }, { transform: `translateX(${to}px)` }],
      { duration: ms * state.tempo, easing: 'linear', fill: 'forwards' })
      .finished.then(() => node.classList.remove('gaar'), () => {});
  }

  const GUEST_PX = 110;
  const GUEST_IDS = { stripes: 'stripes-gaest', glow: 'glow-gaest' };
  // What a guest does not bring along: Klippe's own jobs, its phone, its food and its hole – nor its
  // pumpkin, bathtub, lunch bell and door, dirt, toilet paper, music notes and cape (SPEC §23). Its
  // costume and an envelope it carries stay.
  const GUEST_LEAVE_OUT = '.crew, .luge, .render, .pakke, .telefon, .carry, .hole, .zzz, .drop, .drop__tape, .megafon,'
    + ' .drom, .knurr, .savl, .mad, .graeskar-lygte, .badekar, .frokostklokke, #frokost-doer, .frokost-doer, .snavs,'
    + ' .fluer, .stank, .toiletpapir, .noder, .kappe, .sne-lag, .flagermus, .panik-draaber';

  /** A copy of the pet's drawing for a guest: its own stripes and glow (renamed – the colours come
   *  from the guest's data-*, where the copy sits), no other ids, none of Klippe's jobs. */
  function guestDrawing() {
    const svg = el.pet.cloneNode(true);
    svg.removeAttribute('id');
    svg.setAttribute('class', 'pet');
    for (const n of svg.querySelectorAll('defs > *')) {
      if (!Object.hasOwn(GUEST_IDS, n.id)) n.remove();
    }
    for (const n of svg.querySelectorAll(GUEST_LEAVE_OUT)) n.remove();
    for (const n of svg.querySelectorAll('[id]')) {
      if (Object.hasOwn(GUEST_IDS, n.id)) {
        n.id = GUEST_IDS[n.id];
      } else {
        n.removeAttribute('id');
      }
    }
    for (const n of svg.querySelectorAll('[fill], [stroke], [mask], [clip-path], [filter]')) {
      for (const name of ['fill', 'stroke', 'mask', 'clip-path', 'filter']) {
        const value = n.getAttribute(name);
        if (value && value.includes('url(')) n.setAttribute(name, renameRefs(value, GUEST_IDS));
      }
    }
    return svg;
  }

  /** A guest by the left edge of the page, its bubble hidden: a copy of the pet in the guest's look. */
  function makeGuest(data) {
    const look = guestLook(data);
    const guest = node('div', 'gaest');
    Object.assign(guest.dataset, { stage: look.stage, outfit: look.outfit, mood: 'chill', ...look.pynt });
    if (look.kostume) guest.dataset.kostume = look.kostume;
    if (look.post) guest.dataset.post = '';
    const bubble = node('p', 'gaest__boble', visitLine(data));
    bubble.setAttribute('role', 'status');
    bubble.hidden = true;
    guest.append(bubble, guestDrawing());
    guest.style.left = '-6px';
    document.body.append(guest);
    fitGuest(guest);
    return { guest, bubble };
  }

  /** The guest stands on Klippe's floor – never bigger than Klippe's own drawing (a card under the pet,
   *  like the trick-or-treat card, leaves less room for the stage). */
  function fitGuest(guest) {
    const size = Math.round(Math.min(GUEST_PX, el.pet.getBoundingClientRect().height || GUEST_PX));
    const svg = guest.querySelector('svg');
    if (svg && size < GUEST_PX) {
      svg.style.width = `${size}px`;
      svg.style.height = `${size}px`;
    }
    guest.style.top = `${Math.round(floor().y - size * 0.9)}px`;
  }

  /** The guest walks in from the left, says its line, both jump and clap (twice), and after ~6 s it
   *  walks out again. Everything from the packet goes into textContent or data-* only. A
   *  trick-or-treater (SPEC §23.3) has its own visit. */
  function startVisit(data, waited = 0) {
    if (data.type === 'slik') {
      startSlik(data, waited);
      return;
    }
    const { guest, bubble } = makeGuest(data);
    const run = { data, guest, timers: [] };
    state.visit = run;
    walk(guest, -GUEST_PX - 20, 0, 1400);
    later(run, 1400, () => {
      bubble.hidden = false;
    });
    if (data.type === 'post') later(run, 2200, () => delete guest.dataset.post);   // the letter is handed over
    later(run, 1700, () => {
      cheerTogether(guest);
      if (data.trofae && data.trofae.rarity === 'legendarisk') floatEmoji('⭐', 4);
    });
    later(run, 2600, () => say(visitReply(data), 3200));
    later(run, 3400, () => cheerTogether(guest));
    later(run, 5600, () => {
      bubble.hidden = true;
      walk(guest, 0, -GUEST_PX - 20, 1400);
    });
    later(run, 7100, () => endVisit(run));
  }

  /** Both jump and clap – and they are glad to see each other. */
  function cheerTogether(guest) {
    jump();
    pulse('clap', 950);
    party(1600);
    const svg = guest.querySelector('svg');
    svg.classList.remove('jump', 'clap');
    void svg.getBoundingClientRect();          // restart the animations
    svg.classList.add('jump', 'clap');
    guest.classList.add('party');
    guest.dataset.pose = 'cheer';
    setTimeout(() => {
      svg.classList.remove('jump', 'clap');
      guest.classList.remove('party');
      delete guest.dataset.pose;
    }, 1000);
  }

  function endVisit(run) {
    run.timers.forEach(clearTimeout);
    run.guest.remove();
    if (state.visit === run) state.visit = null;
    nextOnStage();
  }

  // ---------------------------------------------------------------- the delivery party (SPEC §22.4)
  /** SSE `levering`: a render into Final, or a new file in the project's Final – a party (the
   *  newest waits, one at most). */
  function deliver(data) {
    if (!data || typeof data !== 'object') return;
    state.festWaiting = data;
    nextOnStage();
  }

  /** ~12 s: Klippe puts on the Deliver cap and packs the reel into a box – flaps, tape, the
   *  "LEVERET ✓" stamp – then confetti and fireworks, and the party cat walks in, hops, spins now
   *  and then and walks out again. No sound here (the main process rings once). */
  function startParty(data) {
    const run = { d: data, timers: [], cat: null, stamped: false };
    state.fest = run;
    el.box.classList.remove('is-lukket', 'is-tapet', 'is-stemplet', 'is-vaek');
    delete el.app.dataset.fest;
    void el.app.offsetWidth;                 // restart the cap's and the box's animations
    el.app.dataset.fest = data.demo ? 'demo' : data.kilde === 'fil' ? 'fil' : 'render';
    const cat = festkat();                   // fetched and keyed while the box is packed
    renderMoodLine();
    later(run, 1700, () => el.box.classList.add('is-lukket'));
    later(run, 2300, () => el.box.classList.add('is-tapet'));
    later(run, 3000, () => {
      el.box.classList.add('is-stemplet');
      run.stamped = true;
      renderMoodLine();
      if (!REDUCED) fx.start('confetti', 5);
      celebrate(deliveryLine(data), 'fireworks', 5);
      if (data.demo) say('Bare en prøve – sådan fejrer jeg, når en film er leveret 😉', 5000);
    });
    later(run, 3400, () => catEnters(run, cat));
    later(run, 10600, () => catLeaves(run));
    later(run, 12000, () => el.box.classList.add('is-vaek'));
    later(run, 12500, () => endParty(run));
  }

  function endParty(run) {
    run.timers.forEach(clearTimeout);
    stopCat(run);
    if (state.fest === run) state.fest = null;
    delete el.app.dataset.fest;
    el.box.classList.remove('is-lukket', 'is-tapet', 'is-stemplet', 'is-vaek');
    renderMoodLine();
    nextOnStage();
  }

  /** The party cat (GET /api/festkat), every frame decoded once and its green screen keyed out:
   *  {frames: [{bitmap, ms}], plan, w, h, bottom} – or null without the GIF or ImageDecoder (then
   *  the drawn cat comes, and the GIF is tried again at the next party). */
  function festkat() {
    if (!state.festkat) {
      state.festkat = decodeFestkat().catch(() => null).then((cat) => {
        if (!cat) state.festkat = null;
        return cat;
      });
    }
    return state.festkat;
  }

  async function decodeFestkat() {
    if (typeof ImageDecoder === 'undefined') return null;
    const response = await fetch('/api/festkat');
    if (!response.ok) return null;
    const decoder = new ImageDecoder({ data: await response.arrayBuffer(), type: 'image/gif' });
    try {
      await decoder.tracks.ready;
      await decoder.completed;
      const count = decoder.tracks.selectedTrack.frameCount;
      const plan = catFrames(count);
      const work = document.createElement('canvas');
      const ctx = work.getContext('2d', { willReadFrequently: true });
      const frames = [];
      let bottom = -1;
      for (let i = 0; i < count; i += 1) {
        const { image } = await decoder.decode({ frameIndex: i });
        work.width = image.displayWidth;       // (also clears it)
        work.height = image.displayHeight;
        ctx.drawImage(image, 0, 0);
        const pixels = ctx.getImageData(0, 0, work.width, work.height);
        const low = keyGreen(pixels.data, work.width);
        if (i >= plan.stand[0] && i <= plan.stand[1]) bottom = Math.max(bottom, low);
        ctx.putImageData(pixels, 0, 0);
        frames.push({ bitmap: await createImageBitmap(work), ms: Math.max(20, (image.duration || 1e5) / 1000) });
        image.close();
      }
      return frames.length ? { frames, plan, w: work.width, h: work.height, bottom } : null;
    } finally {
      decoder.close();
    }
  }

  const CAT_PX = 120;                        // the GIF's picture, high (the cat itself is about half)
  const DRAWN_CAT_PX = 74;                   // the drawn cat's 100 units

  /** The cat walks in from the right and stands beside Klippe (its feet on Klippe's floor) – the
   *  GIF once it is decoded (≤ 2 s more), else the drawn cat. `run` is a party – or the easter egg's
   *  short visit (`state.cameo`, its first spin soon: `firstSpin` ms). */
  async function catEnters(run, frames, firstSpin = null) {
    const cat = await Promise.race([frames, new Promise((resolve) => setTimeout(resolve, 2000, null))]);
    if ((state.fest !== run && state.cameo !== run) || run.cat) return;
    const box = node('div', 'festkat');
    box.setAttribute('aria-hidden', 'true');
    let figure;
    let width;
    let height;
    let feet;                                // 0…1: where in its picture the cat stands
    if (cat) {
      figure = document.createElement('canvas');
      figure.width = cat.w;
      figure.height = cat.h;
      height = CAT_PX;
      width = (CAT_PX * cat.w) / cat.h;
      feet = cat.bottom >= 0 ? (cat.bottom + 1) / cat.h : 1;
    } else {
      figure = document.createElementNS(SVG_NS, 'svg');
      figure.setAttribute('viewBox', '0 0 100 100');
      const use = document.createElementNS(SVG_NS, 'use');
      use.setAttribute('href', '#kat-tegning');
      figure.append(use);
      height = width = DRAWN_CAT_PX;
      feet = 0.95;
    }
    figure.classList.add('festkat__kat', 'hopper');
    figure.style.width = `${width}px`;
    figure.style.height = `${height}px`;
    box.append(figure);
    const ground = floor();
    box.style.left = `${Math.round(Math.min(window.innerWidth - width * 0.55, ground.x + 85 - width / 2))}px`;
    box.style.top = `${Math.round(ground.y - height * feet)}px`;
    document.body.append(box);
    run.cat = { box, figure, cat, width, timer: 0, spins: 0 };
    walk(box, width + 20, 0, 1500);
    if (cat) {
      playCat(run, firstSpin);
    } else if (!REDUCED) {
      run.cat.timer = setTimeout(() => spinDrawnCat(run), (firstSpin == null ? 1800 : firstSpin) * state.tempo);
    }
  }

  /** The GIF: it stands and hops (the stand frames over and over), and every 2.5–6 s it spins once
   *  (the spin frames, no hop meanwhile). Reduced motion: one standing frame. */
  function playCat(run, firstSpin = null) {
    const c = run.cat;
    const { frames, plan } = c.cat;
    const ctx = c.figure.getContext('2d');
    let index = plan.stand[0];
    let spinning = false;
    let spinAt = performance.now() + (firstSpin == null ? nextSpin() : firstSpin) * state.tempo;
    const show = () => {
      if (run.cat !== c) return;
      ctx.clearRect(0, 0, c.figure.width, c.figure.height);
      ctx.drawImage(frames[index].bitmap, 0, 0);
      if (REDUCED) return;
      const wait = frames[index].ms;
      if (spinning) {
        if (index >= plan.spin[1]) {
          spinning = false;
          index = plan.stand[0];
          c.figure.classList.add('hopper');
          spinAt = performance.now() + nextSpin() * state.tempo;
        } else {
          index += 1;
        }
      } else if (performance.now() >= spinAt) {
        spinning = true;
        c.spins += 1;
        index = plan.spin[0];
        c.figure.classList.remove('hopper');
      } else {
        index = index >= plan.stand[1] ? plan.stand[0] : index + 1;
      }
      c.timer = setTimeout(show, wait);
    };
    show();
  }

  /** The drawn cat spins with CSS (three quick turns), then hops again until the next spin. */
  function spinDrawnCat(run) {
    const c = run.cat;
    if (!c) return;
    c.spins += 1;
    c.figure.classList.replace('hopper', 'snurrer');
    c.timer = setTimeout(() => {
      c.figure.classList.replace('snurrer', 'hopper');
      c.timer = setTimeout(() => spinDrawnCat(run), nextSpin() * state.tempo);
    }, 1400);
  }

  function catLeaves(run) {
    if (run.cat) walk(run.cat.box, 0, run.cat.width + 20, 1400);
  }

  function stopCat(run) {
    if (!run.cat) return;
    clearTimeout(run.cat.timer);
    run.cat.box.remove();
    run.cat = null;
  }

  // ---------------------------------------------------------------- trophies and wardrobe (achievements.py)
  /** What Klippe wears: data-farve, data-hat … on the page (the CSS draws it). */
  function applyWardrobe(equipped) {
    for (const slot of SLOT_KEYS) {
      if (equipped && equipped[slot]) state.wear[slot] = equipped[slot];
    }
    applyLook();
  }

  /** The wardrobe plus what is borrowed for a while: a hat won at Halloween (`bytte`, SPEC §23.3) and
   *  the coffee cup of an easter egg. */
  function applyLook() {
    const wear = { ...state.wear };
    const bytte = state.saeson && state.saeson.bytte;
    if (bytte && bytte.hat && !(bytte.til && bytte.til * 1000 <= Date.now())) wear.hat = bytte.hat;
    if (state.tmpHaand && state.tmpHaand.until > Date.now()) wear.haand = state.tmpHaand.item;
    for (const slot of SLOT_KEYS) {
      if (wear[slot]) {
        el.app.dataset[slot] = wear[slot];
      } else {
        delete el.app.dataset[slot];
      }
    }
  }

  async function loadPet() {
    const looks = state.looks;
    try {
      state.pet = await get('/api/pet');
    } catch {
      return;
    }
    // A change of clothes (SSE pet_look) that came while this was on its way is newer than it.
    if (looks === state.looks) {
      applyWardrobe(state.pet.equipped);
    } else if (state.equipped) {
      state.pet.equipped = { ...state.pet.equipped, ...state.equipped };
    }
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
    if (state.panelTab === 'jul' && !julTabShown()) state.panelTab = 'trophies';
    for (const tab of el.panel.querySelectorAll('[data-panel-tab]')) {
      tab.setAttribute('aria-selected', String(tab.dataset.panelTab === state.panelTab));
      if (tab.dataset.panelTab === 'jul') tab.hidden = !julTabShown();
    }
    // The Prøv tab is static markup (every demo button with its hint); the others are built here.
    const trying = state.panelTab === 'proev' && Boolean(el.panelProev);
    show(el.panelProev, trying);
    el.panelBody.hidden = trying;
    if (trying) return;
    if (state.panelTab === 'jul') {
      const top = el.panelBody.scrollTop;
      el.panelBody.replaceChildren(...calendarNodes());
      el.panelBody.scrollTop = top;
      return;
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
    const all = (data && data.nye) || [];
    if (!all.length) return;
    // A gift from the Christmas calendar is celebrated by its door (SPEC §23.4) – only the wardrobe learns it.
    const news = all.filter((n) => n && n.kind !== 'jul');
    if (!news.length) {
      if (data.unlocked != null) el.trophyCount.textContent = String(data.unlocked);
      loadPet();
      return;
    }
    if (state.eating) {                 // a food trophy arrives while it eats: party after the meal
      state.laterProgress.push(data);
      return;
    }
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

  el.trophies.addEventListener('click', () => {
    closeTray();
    closePost();
    if (el.panel.hidden) {
      openPanel();
    } else {
      el.panel.hidden = true;
    }
  });
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
  el.demoRobots.addEventListener('click', () => demo(false));
  el.demoCall.addEventListener('click', () => demo(true));
  el.demoParty.addEventListener('click', () => demoNow('/api/levering/demo'));
  el.demoVisit.addEventListener('click', () => demoNow('/api/kontor/demo'));
  // The fun in the office (SPEC §23.12): every demo sits in the Prøv tab too.
  on($('demo-halloween'), 'click', () => {
    state.halloweenDemoUntil = Date.now() + 60e3;
    funTick();
    demoNow('/api/kontor/slik/demo');
  });
  on($('demo-jul'), 'click', () => {
    state.julDemo = true;
    state.julPeek = false;
    openPanel('jul');
  });
  on($('demo-sne'), 'click', () => demoNow('/api/sne/demo'));
  on($('demo-frokost'), 'click', () => demoNow('/api/kontor/frokost/demo'));
  on($('demo-vaeddemaal'), 'click', () => demoNow('/api/kontor/vaeddemaal/demo'));
  on($('demo-spil'), 'click', () => {
    el.panel.hidden = true;
    openGame(true);
  });
  on($('demo-bad'), 'click', () => demoNow('/api/rod/demo'));
  on($('demo-nat'), 'click', () => {
    el.panel.hidden = true;
    natDemo();
  });
  on($('demo-dans'), 'click', () => {
    el.panel.hidden = true;
    state.dansDemoUntil = Date.now() + 10e3;
    updateNat();                                   // awake for the demo, even after fyraften
    updateDance();
    setTimeout(() => {
      updateNat();
      updateDance();
    }, 10.2e3);
  });
  on($('demo-aar'), 'click', () => {
    el.panel.hidden = true;
    openYear(clock().getFullYear());
  });
  on($('demo-post'), 'click', () => demoNow('/api/kontor/post/demo'));
  for (const tab of el.panel.querySelectorAll('[data-panel-tab]')) {
    tab.addEventListener('click', () => {
      state.panelTab = tab.dataset.panelTab;
      renderPanel();
    });
  }
  window.addEventListener('keyup', (event) => {
    if (state.game && !el.spil.hidden) gameKeyUp(event);
  });
  window.addEventListener('keydown', (event) => {
    if (state.game && !el.spil.hidden && gameKey(event)) return;
    if (event.key !== 'Escape') return;
    el.panel.hidden = true;
    closeTray();
    closePost();
    if (state.game) closeGame();
  });

  // ---------------------------------------------------------------- food and hunger (SPEC §18.6)
  /** The food state – unless Klippe is still an egg (an egg is never hungry). */
  function hatchedFood() {
    return el.app.dataset.stage === 'egg' ? null : state.food;
  }

  /** The dish Klippe dreams of while it is hungry: picked once per hunger (kept over a reload). */
  function pickCraving(level) {
    const data = memory();
    if (level !== 'sulten' && level !== 'skrubsulten') {
      if (data.craving) {
        delete data.craving;
        remember(data);
      }
      return null;
    }
    const ids = state.menu.map((m) => m.id);
    if (!ids.includes(data.craving) && ids.length) {
      data.craving = ids[Math.floor(Math.random() * ids.length)];
      remember(data);
    }
    return data.craving || null;
  }

  /** `GET /api/pet/mad`, SSE `pet_mad` and a meal: how full, a rush, the menu. */
  function applyFood(food) {
    if (!food || typeof food !== 'object') return;
    const before = state.food ? state.hunger : undefined;
    const hadEnergy = state.energy;
    state.food = food;
    if (Array.isArray(food.menu) && food.menu.length) state.menu = food.menu;
    state.hunger = hungerLevel(food.maet);
    state.energy = food.energi ? food.energi.item : null;
    state.craving = pickCraving(state.hunger);
    renderHunger();
    if (!hatchedFood() || state.eating) return;
    const worse = ['maet', 'fin', 'sulten', 'skrubsulten'];
    if (before !== undefined && worse.indexOf(state.hunger) > worse.indexOf(before)
        && (state.hunger === 'sulten' || state.hunger === 'skrubsulten')) {
      const dream = CRAVINGS[state.craving];
      say(dream && state.hunger === 'sulten' ? `Jeg kunne godt spise ${dream}` : foodSay(state.hunger), 7000);
    }
    if (hadEnergy && !state.energy) say('Sukkerkrak … 🥱', 5000);
  }

  function renderHunger() {
    const food = hatchedFood();
    el.hunger.hidden = !food;
    if (food) {
      el.app.dataset.sult = state.hunger || 'fin';
    } else {
      delete el.app.dataset.sult;
    }
    if (food && food.energi) {
      el.app.dataset.energi = food.energi.item;
    } else {
      delete el.app.dataset.energi;
    }
    if (food && food.drop) {                 // the drip stays until its own bag is empty
      el.app.dataset.drop = '';
    } else {
      delete el.app.dataset.drop;
    }
    const newDrip = state.eating && state.eating.kind === 'drop';    // a new bag is full
    el.app.style.setProperty('--drop', String(newDrip ? 1 : dropLeft(food && food.drop)));
    if (food) {
      const maet = Math.max(0, Math.min(100, Number(food.maet) || 0));
      el.maetBar.style.width = `${maet}%`;
      el.maet.title = `Mæthed ${Math.round(maet)} / 100 – Klippe bliver sulten, mens du arbejder`;
      const left = food.energi ? Math.max(1, Math.round((food.energi.til - Date.now() / 1000) / 60)) : 0;
      el.energi.hidden = !food.energi;
      el.energi.textContent = food.energi ? `⚡ ${left} min` : '';
      el.energi.title = food.energi ? `${food.energi.name}: ${left} min mere` : '';
    }
    if (state.craving) el.dream.setAttribute('href', `#mad-${state.craving}`);
    el.feed.disabled = Boolean(state.eating);
    renderMoodLine();
  }

  async function loadFood() {
    try {
      const food = await get('/api/pet/mad');
      if (!state.feeding) applyFood(food);      // a meal brings its own state when it is eaten
    } catch { /* not there (yet) */ }
  }

  function dishIcon(id) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '-23 -23 46 46');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', `#mad-${id}`);
    svg.append(use);
    return svg;
  }

  function renderTray() {
    const buttons = state.menu.map((food) => {
      const button = node('button', `food${food.id === state.craving ? ' is-craved' : ''}`);
      button.type = 'button';
      button.dataset.food = food.id;
      button.title = food.energy_min > 0 ? `${food.name} – energi i ${Math.round(food.energy_min)} min`
        : `${food.name} – mætter ${Math.round(food.points)}`;
      button.append(dishIcon(food.id), node('span', 'food__name', food.name), node('span', 'food__hint', foodHint(food)));
      button.addEventListener('click', () => feed(food.id));
      return button;
    });
    el.trayGrid.replaceChildren(...(buttons.length ? buttons : [node('p', 'panel__summary', 'Henter menuen …')]));
  }

  function openTray() {
    el.panel.hidden = true;
    closePost();
    renderTray();
    el.tray.hidden = false;
    loadFood().then(() => {
      if (!el.tray.hidden) renderTray();
    });
  }

  function closeTray() {
    el.tray.hidden = true;
  }

  /** The meal itself: the food flies from the hand to the mouth and is eaten (CSS, widget.css). */
  function eat(food) {
    return new Promise((resolve) => {
      if (REDUCED) {
        floatEmoji(food.kind === 'mad' ? '😋' : '⚡', 1);
        resolve();
        return;
      }
      state.eating = food;
      renderHunger();
      el.eatThing.setAttribute('href', `#mad-${food.id}`);
      if (food.kind === 'mad') {
        el.eatBite.setAttribute('mask', food.id === 'bigmac' ? 'url(#bid-side)' : 'url(#bid-top)');
      } else {
        el.eatBite.removeAttribute('mask');
      }
      delete el.app.dataset.spiser;
      void el.app.offsetWidth;              // restart the animations
      el.app.dataset.spiseArt = food.kind;
      el.app.dataset.spiser = food.id;
      setTimeout(() => {
        delete el.app.dataset.spiser;
        delete el.app.dataset.spiseArt;
        state.eating = null;
        renderHunger();                       // the 🍔 button works again – whoever gave the food
        resolve();
      }, { drik: 5400, drop: 2800 }[food.kind] || 5200);
    });
  }

  async function feed(id) {
    closeTray();
    if (state.feeding) return;
    if (el.app.dataset.play === 'out') {
      say(`${state.name} er ude at lege – rør musen, så kommer den hjem og spiser 🎈`, 4500);
      return;
    }
    state.feeding = true;                   // SSE pet_mad of this meal waits for the meal itself
    let answer;
    try {
      answer = await send('POST', '/api/pet/mad', { item: id });
    } catch (err) {
      state.feeding = false;
      say(`Øv – ${err.message}`, 5000);
      return;
    }
    const food = answer.item || state.menu.find((m) => m.id === id) || { id, name: id, kind: 'mad' };
    if (!answer.spiste) {
      state.feeding = false;
      applyFood(answer.mad);
      pulse('nej', 700);
      say(foodSay(answer.grund), 5000);
      return;
    }
    const craved = state.craving === food.id;
    await eat(food);
    state.feeding = false;
    applyFood(answer.mad);
    jump();
    floatEmoji(craved ? '😍' : food.kind === 'mad' ? '😋' : '⚡', craved ? 4 : 2);
    say(craved ? 'Præcis hvad jeg drømte om! 😍' : foodSay(food.id), 4500);
    for (const later of state.laterProgress.splice(0)) celebrateProgress(later);
  }

  el.feed.addEventListener('click', () => (el.tray.hidden ? openTray() : closeTray()));
  el.trayClose.addEventListener('click', closeTray);

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
  /** A speech bubble, after the ones already waiting – or `now`, cutting in. */
  function say(text, ms = BUBBLE_MS, now = false) {
    if (now) {
      state.bubbles.unshift({ text, ms });
      clearTimeout(state.bubbleTimer);
      nextBubble();
      return;
    }
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

  /** Emojis floating up from the stage – somewhere, or from `at` (% from the left). */
  function floatEmoji(emoji, count = 1, at = null) {
    for (let i = 0; i < count; i += 1) {
      const node = document.createElement('span');
      node.className = 'float';
      node.textContent = emoji;
      node.style.left = `${at == null ? 15 + Math.random() * 70 : at}%`;
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
    if (state.nat === 'sover' && !state.natDemo) {          // woken after fyraften: 30 s, then asleep again
      state.natWakeUntil = Date.now() + 30e3;
      updateNat();
      jump();
      say('Hva\'? 😴 … Er det ikke fyraften?', 4000);
      return;
    }
    jump();
    floatEmoji('💕', 3);
    const today = state.todayS || 0;
    const lines = [cheer('pet')];
    if (today >= 60) lines.push(`I dag: ${words(today)} 💪`);
    if (hatchedFood() && (state.hunger === 'sulten' || state.hunger === 'skrubsulten')) {
      const dream = CRAVINGS[state.craving];
      lines.splice(0, lines.length, dream ? `Jeg kunne godt spise ${dream}` : foodSay(state.hunger));
    }
    say(lines[Math.floor(Math.random() * lines.length)], 3500);
  });

  /** What the right hand holds turns with the arm: .figur__haand gets every animation .arm--right gets
   *  (widget.css), from the same change – yet the browser may start the two a frame apart (one on the
   *  compositor, the other not), and the thing would trail the hand. So whenever one of them starts,
   *  the hand's animation is set to start exactly when the arm's did. */
  const armRight = el.pet.querySelector('.arm--right');
  const handGroup = el.pet.querySelector('.figur__haand');
  function handInStep() {
    const [arm] = armRight.getAnimations();
    const [hand] = handGroup.getAnimations();
    if (arm && hand && arm.animationName === hand.animationName && arm.startTime != null
        && hand.startTime !== arm.startTime) hand.startTime = arm.startTime;
  }
  el.pet.addEventListener('animationstart', (event) => {
    if (event.target === armRight || event.target === handGroup) handInStep();
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

  // ===========================================================================================
  // Fun in the office (SPEC §23)
  // ===========================================================================================

  /** Every 15 s, and whenever the settings change: what follows the clock. */
  function funTick() {
    newDay();
    updateNat();
    updateSeason();
    updateDance();
    renderBallade();
    applyLook();
    renderHeaderIcons();
    renderBetCard();
    renderGameSwitch();
    greetJul();
  }

  /** A new day by the widget's clock (a PC left on overnight): the calendar has a new door – or, in
   *  January, is over. */
  function newDay() {
    const day = isoDate(clock());
    if (state.day === day) return;
    const first = state.day == null;
    state.day = day;
    if (!first && (clock().getMonth() === 11 || state.jul)) loadJul();
  }

  /** `widget_renderspil` off: no 🎮, and a game that is open closes at once. */
  function renderGameSwitch() {
    const on = setting('widget_renderspil');
    show(el.spilKnap, on && Boolean(renderProgress(state.render)));
    if (!on && state.game && !state.game.demo) closeGame();
  }

  function itemName(id) {
    const item = id && state.pet && Array.isArray(state.pet.items) ? state.pet.items.find((i) => i.id === id) : null;
    return item ? item.name : id;
  }

  async function demoAnswer(promise) {
    try {
      return await promise;
    } catch (err) {
      say(`Øv – ${err.message}`, 5000);
      return null;
    }
  }

  // ---------------------------------------------------------------- Natte-Klippe and the dance (SPEC §23.2)
  /** data-nat: gaber → doeser → sover after fyraften (the nightcap, yawns, nodding, sleep). A click
   *  wakes it from its sleep for 30 s; the demo runs through the three. */
  function updateNat() {
    const before = state.nat;
    const fyraften = setting('widget_fyraften', '16:00');
    const real = natFase(clock(), fyraften);
    const phase = state.natDemo ? state.natDemo.phase
      : state.dansDemoUntil > Date.now() ? null                        // the dance demo is awake, whatever the hour
        : real === 'sover' && state.natWakeUntil > Date.now() ? 'gaber' : real;
    state.nat = phase;
    const shown = phase || (state.natEggUntil > Date.now() ? 'gaber' : null);
    if (shown) {
      el.app.dataset.nat = shown;
    } else {
      delete el.app.dataset.nat;
    }
    // Once a day, when it gets tired (also when the widget starts then) – never for a nap that was woken.
    if ((real === 'gaber' || real === 'doeser') && !state.natDemo && state.ready && firstToday('fyraften')) {
      pulse('gaab', 1600);
      say(`Klokken er ${fyraftenLabel(fyraften)} – fyraften! 🏠🥱`, 6000);
    }
    if (phase === before) return;
    scheduleYawn();
    scheduleNod();
    updateDance();
    renderMoodLine();
  }

  /** A yawn every 4–6 min while it gets tired – with a line when it is not saying something else. */
  function scheduleYawn() {
    clearTimeout(state.yawnTimer);
    state.yawnTimer = 0;
    if (state.nat !== 'gaber' && state.nat !== 'doeser') return;
    state.yawnTimer = setTimeout(() => {
      state.yawnTimer = 0;
      if ((state.nat === 'gaber' || state.nat === 'doeser') && !state.eating && el.app.dataset.play !== 'out') {
        pulse('gaab', 1600);
        if (!state.bubbleTimer) say(YAWN_LINES[Math.floor(Math.random() * YAWN_LINES.length)], 4000);
      }
      scheduleYawn();
    }, (4 + Math.random() * 2) * 60e3);
  }

  /** Dozing: it nods off and jerks awake every 40–90 s. */
  function scheduleNod() {
    clearTimeout(state.nodTimer);
    state.nodTimer = 0;
    if (state.nat !== 'doeser') return;
    state.nodTimer = setTimeout(() => {
      state.nodTimer = 0;
      if (state.nat === 'doeser' && !state.eating) pulse('nikker', 1200);
      scheduleNod();
    }, (40 + Math.random() * 50) * 1000);
  }

  /** "😴 Prøv Natte-Klippe": a yawn, dozing and sleep in 13 s. */
  function natDemo() {
    if (state.natDemo) clearTimeout(state.natDemo.timer);
    const steps = [['gaber', 4000], ['doeser', 4000], ['sover', 5000]];
    let i = 0;
    const next = () => {
      if (i >= steps.length) {
        state.natDemo = null;
        updateNat();
        return;
      }
      const [phase, ms] = steps[i];
      i += 1;
      state.natDemo = { phase, timer: after(ms, next) };
      updateNat();
      if (phase === 'gaber') {
        pulse('gaab', 1600);
        say(`Gaaab … sådan bliver jeg efter fyraften kl. ${fyraftenLabel(setting('widget_fyraften', '16:00'))} 🥱`, 3800);
      } else if (phase === 'doeser') {
        pulse('nikker', 1200);
      } else {
        say('💤 … og så sover jeg. Godnat!', 4500);
      }
    };
    next();
  }

  /** data-dans while you look for music (the bucket "musik"), to 120 BPM – not asleep, except in the demo. */
  function updateDance() {
    const s = state.status || {};
    const real = setting('widget_dans') && s.state === 'recording' && s.bucket === 'musik' && state.nat !== 'sover';
    const dancing = (real || state.dansDemoUntil > Date.now()) && !state.eating;
    if (dancing === state.dans) return;
    state.dans = dancing;
    if (!dancing) {
      delete el.app.dataset.dans;
      return;
    }
    el.app.dataset.dans = '';
    if (Date.now() - state.dansLineAt > 10 * 60e3) {
      state.dansLineAt = Date.now();
      say(DANCE_LINES[Math.floor(Math.random() * DANCE_LINES.length)], 4000);
    }
  }

  // ---------------------------------------------------------------- Halloween (SPEC §23.3)
  function currentSeason() {
    if (state.halloweenDemoUntil > Date.now()) return 'halloween';
    return setting('widget_saeson') ? seasonFor(clock()) : null;
  }

  /** data-saeson and – in Halloween week – data-kostume (this PC's costume this year). */
  function updateSeason() {
    const season = currentSeason();
    const before = state.season;
    state.season = season;
    if (season) {
      el.app.dataset.saeson = season;
    } else {
      delete el.app.dataset.saeson;
    }
    const s = state.saeson || {};
    const kostume = season !== 'halloween' ? null : KOSTUMER.includes(s.kostume) ? s.kostume
      : KOSTUMER.includes(s.kostume_aar) ? s.kostume_aar : null;
    if (kostume) {
      el.app.dataset.kostume = kostume;
    } else {
      delete el.app.dataset.kostume;
    }
    if (season === 'halloween' && state.ready && !(state.halloweenDemoUntil > Date.now()) && firstToday('halloween')) {
      say('Bøh! 🎃 Glædelig Halloween', 5000);
    }
    if (season === before) return;
    if (season === 'jul') loadJul();
    renderHeaderIcons();
    reportLook();
  }

  /** data-ballade: "hat" (the hat is stolen), "toiletpapir", or both after a second prank – till midnight
   *  (pet_saeson), or a prank just played (a demo's 10 s) until the server's word is here. */
  function balladeNow() {
    const now = Date.now();
    const local = state.balladeLocal && state.balladeLocal.until > now ? state.balladeLocal : null;
    const b = state.saeson && state.saeson.ballade;
    const server = b && !(b.til && b.til * 1000 <= now) ? b : null;
    return balladeTokens({ hat: Boolean((local && local.hat) || (server && server.hat)),
      toiletpapir: Boolean((local && local.toiletpapir) || (server && server.toiletpapir)) }) || null;
  }

  function renderBallade() {
    const kind = balladeNow();
    if (kind) {
      el.app.dataset.ballade = kind;
    } else {
      delete el.app.dataset.ballade;
    }
  }

  /** GET /api/pet/saeson and SSE `pet_saeson`: the costume, a prank played on Klippe, a hat it won. */
  function applySaeson(data) {
    if (!data || typeof data !== 'object') return;
    state.saeson = data;
    updateSeason();
    renderBallade();
    applyLook();
  }

  async function loadSaeson() {
    try {
      const data = await fresh('pet_saeson', '/api/pet/saeson');
      if (data) applySaeson(data);
    } catch { /* not there (yet) */ }
  }

  function userHere() {
    const mood = moodFor(state.status);
    return mood === 'working' || mood === 'waiting';
  }

  /** Trick or treat: the guest in its costume asks for a Booster; 25 s to give it one – else a prank
   *  (when you are here at all, or in the demo). A guest that had to wait in the queue (another guest,
   *  a meal, the render game) has less time left – the server takes the answer 60 s after it came;
   *  with under 8 s left it has gone on and only Klippe tells. */
  function startSlik(data, waited = 0) {
    const total = slikSeconds(data.til_s, waited);
    if (total < SLIK_MIN_S) {
      say(`🎃 ${shorten(data.navn, 20) || 'Klippe'} fra ${shorten(data.pc, 15) || 'kontoret'} var forbi efter slik, mens du havde travlt`,
        6000);
      nextOnStage();
      return;
    }
    el.panel.hidden = true;                        // nothing may cover the guest and its card
    closeTray();
    closePost();
    const { guest, bubble } = makeGuest(data);
    const run = { data, guest, bubble, timers: [], answered: false, countdown: 0, deadline: 0, total };
    state.visit = run;
    state.slik = run;
    walk(guest, -GUEST_PX - 20, 0, 1400);
    later(run, 1400, () => {
      bubble.hidden = false;
      jump();
    });
    if (!(data.demo || userHere()) || !el.slik) {
      later(run, 3200, () => {
        bubble.textContent = 'Her er ingen hjemme … 👻';
      });
      later(run, 5600, () => leaveSlik(run));
      return;
    }
    later(run, 1700, () => showSlikCard(run));
  }

  function showSlikCard(run) {
    const total = Math.max(5, run.total || 25);
    run.deadline = Date.now() + total * 1000 * state.tempo;
    el.slikTekst.textContent = slikLine(run.data);
    el.slikGiv.disabled = false;
    el.slik.hidden = false;
    requestAnimationFrame(() => fitGuest(run.guest));   // the card took room from the stage: stay on the floor
    tickSlik(run);
  }

  function tickSlik(run) {
    if (state.slik !== run || run.answered) return;
    const left = (run.deadline - Date.now()) / 1000 / state.tempo;
    if (el.slikTid) el.slikTid.textContent = slikCountdown(left);
    if (left <= 0) {
      if (run.data.demo || userHere()) {
        answerSlik(run, 'ballade');
      } else {
        run.answered = true;
        show(el.slik, false);
        run.bubble.textContent = 'Her er ingen hjemme … 👻';
        leaveSlik(run, 2200);
      }
      return;
    }
    run.countdown = setTimeout(() => tickSlik(run), 250);
  }

  /** "slik": a Booster for the guest. "ballade": no Booster in time – it steals the hat (or wraps
   *  Klippe in toilet paper). */
  async function answerSlik(run, svar) {
    if (!run || run.answered) return;
    run.answered = true;
    clearTimeout(run.countdown);
    show(el.slik, false);
    requestAnimationFrame(() => fitGuest(run.guest));
    let answer;
    try {
      answer = await send('POST', '/api/kontor/slik', { id: run.data.id, svar });
    } catch (err) {
      say(`Øv – ${err.message}`, 5000);
      leaveSlik(run, 300);
      return;
    }
    if (state.visit !== run) return;
    run.bubble.hidden = false;
    if (svar === 'slik') {
      run.bubble.textContent = 'Tak! 🍬';
      cheerGuest(run.guest);
      floatEmoji('🍬', 4);
      say('Glædelig Halloween! 🎃', 4000);
      leaveSlik(run, 2600);
      return;
    }
    const prank = (answer && answer.ballade) || { hat: null, toiletpapir: true };
    const demo = Boolean(answer && answer.demo);
    run.bubble.textContent = 'Ballade! 😈';
    if (prank.hat) run.guest.dataset.hat = prank.hat;          // it leaves wearing Klippe's hat
    const ms = demo ? 10e3 : 60e3;                              // a real prank: pet_saeson keeps it till midnight
    state.balladeLocal = { hat: Boolean(prank.hat), toiletpapir: Boolean(prank.toiletpapir || !prank.hat),
      until: Date.now() + ms };
    renderBallade();
    setTimeout(renderBallade, ms + 100);
    cheerGuest(run.guest);
    pulse('nej', 700);
    say(balladeLine(run.data.navn, prank, itemName(prank.hat)), 6000, true);
    leaveSlik(run, 3000);
  }

  function cheerGuest(guest) {
    const svg = guest.querySelector('svg');
    if (!svg) return;
    svg.classList.remove('jump', 'clap');
    void svg.getBoundingClientRect();
    svg.classList.add('jump', 'clap');
    guest.classList.add('party');
    setTimeout(() => {
      svg.classList.remove('jump', 'clap');
      guest.classList.remove('party');
    }, 1000);
  }

  function leaveSlik(run, delay = 0) {
    clearTimeout(run.countdown);
    if (state.slik === run) show(el.slik, false);
    run.timers.push(setTimeout(() => {
      if (state.visit !== run) return;
      run.bubble.hidden = true;
      walk(run.guest, 0, -GUEST_PX - 20, 1400);
      run.timers.push(setTimeout(() => {
        if (state.slik === run) state.slik = null;
        endVisit(run);
      }, 1500 * state.tempo));
    }, delay * state.tempo));
  }

  /** SSE `slik_svar`: what a colleague gave our trick-or-treating Klippe – a Booster (it drinks it) or
   *  its hat (pet_saeson puts it on). */
  function slikAnswered(d) {
    if (!d || typeof d !== 'object') return;
    say(slikSvarLine(d, itemName(d.hat)), 6000);
    // (not in the middle of a meal you are giving it – then only the words)
    const drinks = d.type === 'godbid' && hatchedFood() && !state.eating && !state.feeding && !REDUCED
      && el.app.dataset.play !== 'out';
    if (drinks) {
      const booster = state.menu.find((m) => m.id === 'booster') || { id: 'booster', name: 'Faxe Kondi Booster', kind: 'drik' };
      eat(booster).then(() => {
        jump();
        floatEmoji('🍬', 3);
      });
    } else {
      jump();
      floatEmoji(d.type === 'godbid' ? '🍬' : '😈', 3);
    }
  }

  // ---------------------------------------------------------------- Christmas (SPEC §23.4)
  /** December by the widget's clock and the server's word (a calendar loaded on 31 Dec is not open in January). */
  function julActive() {
    return Boolean(state.jul && state.jul.aktiv) && clock().getMonth() === 11;
  }

  /** The calendar shows as a demo ("Smugkig") outside December, after "Prøv julekalenderen". */
  function calendarDemo() {
    return state.julDemo && !julActive();
  }

  function julTabShown() {
    return state.julDemo || (setting('widget_saeson') && clock().getMonth() === 11);
  }

  /** GET /api/pet/jul, SSE `pet_jul` and an opened door: the 24 doors, the header's count. */
  function applyJul(data) {
    if (!data || typeof data !== 'object' || !Array.isArray(data.laager)) return;
    state.jul = data;
    renderHeaderIcons();
    if (!el.panel.hidden && state.panelTab === 'jul') renderPanel();
    greetJul();
  }

  /** Once a day in December, when a door is ready. */
  function greetJul() {
    const jul = state.jul;
    if (julActive() && jul.uaabnede > 0 && setting('widget_saeson') && state.ready && firstToday('jul-laage')) {
      say('🎄 Dagens låge i julekalenderen venter – tryk på 🎄 øverst', 6000);
    }
  }

  async function loadJul() {
    try {
      const data = await fresh('pet_jul', '/api/pet/jul');
      if (data) applyJul(data);
    } catch { /* not there (yet) */ }
  }

  function calendarNodes() {
    const demo = calendarDemo();
    const jul = state.jul || {};
    let summary;
    if (demo) {
      summary = 'Smugkig – i december åbner du én låge om dagen. Intet bliver gemt.';
    } else if (!jul.aktiv) {
      summary = 'Julekalenderen åbner den 1. december 🎄';
    } else {
      const ready = jul.uaabnede || 0;
      summary = `🎄 Julekalender ${jul.aar || ''} · ${ready ? `${ready} ${ready === 1 ? 'låge er klar' : 'låger er klar'}` : 'Ny låge i morgen'}`;
    }
    const grid = node('div', 'kalender');
    for (let nr = 1; nr <= 24; nr += 1) {
      const door = demo ? { nr, aaben: Boolean(state.julDemoOpen[nr]), kan: true, gave: state.julDemoOpen[nr] || null }
        : (Array.isArray(jul.laager) && jul.laager.find((d) => d && d.nr === nr)) || { nr, aaben: false, kan: false, gave: null };
      const view = doorView(door, jul.dag, demo);
      const button = node('button', `laage ${view.klasse}`);
      button.type = 'button';
      button.dataset.nr = String(nr);
      button.append(node('span', 'laage__nr', String(nr)));
      if (view.ikon) button.append(node('span', 'laage__gave', view.ikon));
      button.append(node('span', 'laage__tekst', view.tekst));
      button.disabled = view.klasse === 'is-laast';
      button.title = door.aaben && door.gave ? `${door.gave.name}${door.gave.tekst ? ` – ${door.gave.tekst}` : ''}`
        : view.klasse === 'is-klar' ? `Åbn låge ${nr}` : view.tekst;
      button.addEventListener('click', () => openDoor(nr, button));
      grid.append(button);
    }
    return [node('p', 'panel__summary', summary), grid];
  }

  /** A ready door swings open (POST /api/pet/jul, or the demo's), shows its gift a moment – then the
   *  panel closes so Klippe can be seen with it. An opened door says its gift again. */
  async function openDoor(nr, button) {
    if (state.julOpening) return;
    const demo = calendarDemo();
    const known = demo ? state.julDemoOpen[nr]
      : ((state.jul && state.jul.laager.find((d) => d.nr === nr && d.aaben)) || {}).gave;
    if (known) {
      el.panel.hidden = true;
      say(giftLine(known, nr), known.kind === 'vits' ? 9000 : 4500);
      return;
    }
    state.julOpening = true;
    button.classList.add('is-aabner');
    let answer;
    try {
      answer = await send('POST', demo ? '/api/pet/jul/demo' : '/api/pet/jul', { nr });
    } catch (err) {
      state.julOpening = false;
      button.classList.remove('is-aabner');
      el.panel.hidden = true;
      say(`Øv – ${err.message}`, 5000);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 900 * state.tempo));
    state.julOpening = false;
    const gave = answer && answer.gave;
    if (demo) {
      state.julDemoOpen[nr] = gave;
    } else if (answer && answer.jul) {
      applyJul(answer.jul);
    }
    if (!el.panel.hidden && state.panelTab === 'jul') renderPanel();
    setTimeout(() => {
      el.panel.hidden = true;
      revealGift(gave, nr, demo);
    }, 1100 * state.tempo);
  }

  function revealGift(gave, nr, demo) {
    if (!gave) return;
    if (demo && !state.julPeek) {
      state.julPeek = true;
      say('Smugkig – i december åbner du én låge om dagen 🎄', 4500);
    }
    if (nr === 24 || gave.id === 'julestjerne') {
      celebrate(giftLine(gave, nr), 'fireworks', 4);
      say('Glædelig jul! 🎄', 5000);
    } else if (gave.kind === 'ting') {
      celebrate(giftLine(gave, nr), 'confetti', 3);
    } else if (gave.kind === 'mad') {
      jump();
      floatEmoji(giftIcon(gave), 3);
      say(giftLine(gave, nr), 4500);
    } else if (gave.kind === 'sne') {
      floatEmoji('❄️', 4);
      say(giftLine(gave, nr), 5000);
    } else {
      jump();
      say(giftLine(gave, nr), 9000);                  // a Christmas joke
    }
  }

  /** SSE `sne`: snow on the monitor – and a snowstorm in the box meanwhile. */
  function snowStorm(seconds) {
    clearTimeout(state.snowTimer);
    el.app.dataset.snestorm = '';
    state.snowTimer = setTimeout(() => {
      delete el.app.dataset.snestorm;
      state.snowTimer = 0;
    }, Math.max(1, Math.min(180, Number(seconds) || 20)) * 1000);
  }

  // ---------------------------------------------------------------- the bath (SPEC §23.6)
  /** GET /api/rod and SSE `rod`: loose files in the project folder's root make Klippe dirty; the
   *  event that ends the mess (`bad`) gives it a foam bath. */
  function applyRod(rod, loaded = false) {
    if (!rod || typeof rod !== 'object') return;
    state.rod = rod;
    const enabled = setting('widget_bad');
    if (rod.bad && !loaded && enabled) {
      state.bathWaiting = rod;
      nextOnStage();
      return;                                    // the bath washes the dirt off
    }
    if (!state.bath) renderDirt();
    const line = enabled && !loaded ? dirtLine(rod) : null;
    if (line && firstToday(`rod:${String(rod.mappe || rod.projekt || '').toLowerCase()}`)) say(line, 8000);
  }

  function renderDirt() {
    const level = dirtLevel(state.rod, setting('widget_bad'));
    if (level) {
      el.app.dataset.beskidt = level;
    } else {
      delete el.app.dataset.beskidt;
    }
  }

  async function loadRod() {
    let rod;
    try {
      rod = await fresh('rod', '/api/rod');
    } catch {
      return;
    }
    if (!rod) return;                          // a newer `rod` event came meanwhile
    applyRod(rod, true);
    const line = setting('widget_bad') ? dirtLine(rod) : null;      // said once per project and day
    if (line && firstToday(`rod:${String(rod.mappe || rod.projekt || '').toLowerCase()}`)) say(line, 8000);
  }

  /** ~8 s: the tub slides in, Klippe scrubs in the foam, the dirt is gone – sparkles. */
  function startBath() {
    const run = { timers: [] };
    state.bath = run;
    el.app.dataset.bad = '';
    say('🛁 Endelig – skumbad!', 3500);
    run.timers.push(after(4000, renderDirt));
    run.timers.push(after(6800, () => {
      floatEmoji('✨', 4);
      say('Ahh … rent! ✨ Tak, fordi du ryddede op', 5000);
    }));
    run.timers.push(after(8200, () => {
      delete el.app.dataset.bad;
      if (state.bath === run) state.bath = null;
      renderDirt();
      nextOnStage();
    }));
  }

  // ---------------------------------------------------------------- lunch (SPEC §23.12)
  /** SSE `frokost`: Klippe rings the bell, runs out through the door and is at lunch till you are back. */
  function startLunch(ev) {
    if (!ev || typeof ev !== 'object') return;
    const line = lunchLine(ev);
    if (state.lunch || el.app.dataset.play === 'out' || state.eating || state.fest || state.visit || state.bath) {
      say(line, 5000);                         // busy: only the words
      return;
    }
    const run = { ev, timers: [], phase: null, ude: 0 };
    state.lunch = run;
    const step = (ms, fn) => run.timers.push(after(ms, () => {
      if (state.lunch === run) fn();
    }));
    lunchPhase(run, 'ringer');
    say(line, 4500, true);
    if (hatchedFood() && (state.hunger === 'sulten' || state.hunger === 'skrubsulten')) step(1500, () => say('Endelig! 🌯', 3000));
    step(2500, () => lunchPhase(run, 'loeber'));
    step(4000, () => {
      run.ude = Date.now();
      lunchPhase(run, 'ude');
      run.timers.push(setTimeout(comeHome, ev.demo ? 8000 * state.tempo : 45 * 60e3));
    });
  }

  function lunchPhase(run, phase) {
    run.phase = phase;
    el.app.dataset.frokost = phase;
    show(el.frokostSkilt, phase === 'ude');
    renderMoodLine();
  }

  /** Back from lunch – if Klippe is out. */
  function comeHome() {
    const run = state.lunch;
    if (!run || run.phase !== 'ude') return;
    run.timers.forEach(clearTimeout);
    run.timers = [];
    lunchPhase(run, 'tilbage');
    say('Mæt og glad! 😋', 3500);
    run.timers.push(after(1500, () => endLunch(run)));
  }

  function endLunch(run) {
    run.timers.forEach(clearTimeout);
    if (state.lunch !== run) return;
    state.lunch = null;
    delete el.app.dataset.frokost;
    show(el.frokostSkilt, false);
    renderMoodLine();
    nextOnStage();
  }

  /** You work again (≥ 3 min after Klippe left): it comes back. */
  function lunchCheck(mood) {
    const run = state.lunch;
    if (run && run.phase === 'ude' && mood === 'working' && Date.now() - run.ude >= 3 * 60e3) comeHome();
  }

  async function ringLunch() {
    try {
      await send('POST', '/api/kontor/frokost', {});
    } catch (err) {
      say(`Øv – ${err.message}`, 5000);
    }
  }

  // ---------------------------------------------------------------- Klippe-post (SPEC §23.12)
  /** The colleagues seen now – and the one whose post is being answered ("Svar"), even when it is not
   *  about (the demo's DEMO-PC); only while that answer is being written. */
  function postPeers() {
    const peers = state.peers.map((p) => ({ pc: p.pc, navn: p.navn }));
    const answering = state.postSvarTil;
    if (answering && !peers.some((p) => p.pc === answering)) peers.push({ pc: answering, navn: null });
    return peers;
  }

  function openPost(til = null) {
    if (!el.postPanel) return;
    el.panel.hidden = true;
    closeTray();
    state.postSvarTil = til || null;
    if (til) state.postTil = til;
    setPostError('');
    renderPost();
    el.postPanel.hidden = false;
    loadKontor();
  }

  /** The panel closes: an answer's recipient who is not about is not kept (never DEMO-PC by mistake). */
  function closePost() {
    show(el.postPanel, false);
    state.postSvarTil = null;
    if (state.postTil && !state.peers.some((p) => p.pc === state.postTil)) state.postTil = null;
  }

  function setPostError(text) {
    if (!el.postFejl) return;
    el.postFejl.textContent = text;
    el.postFejl.hidden = !text;
  }

  function renderPost() {
    if (!el.postPanel) return;
    const peers = postPeers();
    if (!peers.some((p) => p.pc === state.postTil)) state.postTil = peers.length ? peers[0].pc : null;
    if (el.postTil) {
      el.postTil.replaceChildren(...(peers.length ? peers.map((p) => {
        const label = p.navn ? `${shorten(p.navn, 20)} (${shorten(p.pc, 15)})` : shorten(p.pc, 15);
        const button = node('button', 'post__modtager', label);
        button.type = 'button';
        button.dataset.pc = p.pc;
        button.setAttribute('aria-pressed', String(p.pc === state.postTil));
        button.addEventListener('click', () => {
          state.postTil = p.pc;
          renderPost();
        });
        return button;
      }) : [node('p', 'post__ingen', 'Ingen kolleger er på lige nu')]));
    }
    if (el.postHurtig && !el.postHurtig.childElementCount) {
      el.postHurtig.replaceChildren(...POST_NOTES.map((text) => {
        const button = node('button', 'post__hurtig', text);
        button.type = 'button';
        button.addEventListener('click', () => {
          if (el.postTekst) el.postTekst.value = text;
          setPostError('');
        });
        return button;
      }));
    }
    if (el.postSend) el.postSend.disabled = !state.postTil;
  }

  async function sendPost() {
    const tekst = (el.postTekst ? el.postTekst.value : '').trim();
    if (!state.postTil) {
      setPostError('Vælg en kollega');
      return;
    }
    if (!tekst) {
      setPostError('Skriv en besked');
      return;
    }
    if (tekst.length > POST_MAX) {
      setPostError(`Beskeden er for lang (højst ${POST_MAX} tegn)`);
      return;
    }
    setPostError('');
    el.postSend.disabled = true;
    try {
      await send('POST', '/api/kontor/post', { til: state.postTil, tekst });
      if (el.postTekst) el.postTekst.value = '';
      closePost();
    } catch (err) {
      setPostError(err.message);
    } finally {
      el.postSend.disabled = !state.postTil;
    }
  }

  /** SSE `post_sendt`: Klippe walks out with the envelope and comes back. */
  function postSent(d) {
    const name = shorten(d && (d.navn_til || d.til), 20) || 'kollegaen';
    if (stageBusy() || state.visit || state.fest || state.lunch || el.app.dataset.post) {
      say(`✉️ Posten er sendt til ${name}!`, 4000);
      return;
    }
    say(`✉️ Jeg løber over til ${name} med posten!`, 4000);
    tempAttr('post', 6000, 'ud');
    after(6200, () => say('Posten er afleveret! 📬', 3500));
  }

  // ---------------------------------------------------------------- render bets (SPEC §23.12)
  /** SSE `vaeddemaal` (and GET /api/kontor/vaeddemaal with `loaded`): one bet's state. `previous`: the
   *  bets known before (a reload builds a new map from what the server still lists). */
  function applyBet(bet, loaded = false, previous = state.bets) {
    if (!bet || typeof bet !== 'object' || bet.pc == null || bet.vid == null) return;
    const key = betKey(bet);
    const old = previous.get(key);
    const nowS = Date.now() / 1000;
    const ended = bet.status === 'slut' || bet.status === 'aflyst';
    const next = { ...bet, endedAt: ended ? (old && old.endedAt) || nowS : null };
    if (old && old.mit_gaet && !next.mit_gaet) next.mit_gaet = old.mit_gaet;
    state.bets.set(key, next);
    if (ended && !state.betsCelebrated.has(key)) {
      state.betsCelebrated.add(key);
      const line = betResult(next);
      if (!loaded && line) {
        if (next.resultat && next.resultat.mig) {
          celebrate(line, 'fireworks', 4);
        } else {
          say(line, 7000);
        }
      }
    }
    if (!old && !loaded && next.status === 'aaben' && !next.egen && setting('widget_vaeddemaal')) {
      jump();
      say('🎲 Render-væddemål! Hvornår er den færdig?', 4000);
    }
    renderBetCard();
  }

  /** What the server lists now replaces what this page knew (a bet whose end was lost goes); what was
   *  ended or guessed here is kept for the bets still listed. */
  async function loadBets() {
    let data;
    try {
      data = await fresh('vaeddemaal', '/api/kontor/vaeddemaal');
    } catch {
      return;                                  // the office has not started
    }
    if (!data) return;                         // a newer bet event came meanwhile
    const previous = state.bets;
    state.bets = new Map();
    for (const bet of data.vaeddemaal || []) applyBet(bet, true, previous);
    renderBetCard();
  }

  /** The card: the newest open bet (quick choices, a time field, "Gæt"), what you guessed, the result. */
  function renderBetCard() {
    if (!el.vm) return;
    const nowS = Date.now() / 1000;
    for (const [key, b] of state.bets) {
      if ((b.endedAt && nowS - b.endedAt >= 120) || betStale(b, nowS)) state.bets.delete(key);
    }
    const bet = setting('widget_vaeddemaal') ? pickBet([...state.bets.values()], nowS) : null;
    el.vm.hidden = !bet;
    if (!bet) return;
    const key = betKey(bet);
    el.vm.dataset.status = bet.status;
    el.vm.dataset.bet = key;
    const result = betResult(bet);
    const guessing = bet.status === 'aaben' && (!bet.mit_gaet || state.betEdit === key);
    if (el.vmTitel) el.vmTitel.textContent = result || betTitle(bet);
    show(el.vmValg, guessing);
    show(el.vmTid && el.vmTid.parentElement !== el.vm ? el.vmTid.parentElement : el.vmTid, guessing);   // "Eller kl. [tid] Gæt"
    show(el.vmGaet, guessing);
    if (guessing && el.vmValg) {
      const choices = guessChoices(Date.now());
      if (!state.betChoice || state.betChoice.key !== key || state.betChoice.at <= nowS) {
        state.betChoice = { key, at: choices[1].at };
      }
      el.vmValg.replaceChildren(...choices.map((c) => {
        const button = node('button', 'vm__valg', c.label);
        button.type = 'button';
        button.dataset.at = String(c.at);
        button.title = `Om ca. ${c.min} min`;
        button.setAttribute('aria-pressed', String(c.at === state.betChoice.at));
        button.addEventListener('click', () => {
          state.betChoice = { key, at: c.at };
          if (el.vmTid) el.vmTid.value = c.label;
          renderBetCard();
        });
        return button;
      }));
      if (el.vmTid && document.activeElement !== el.vmTid) el.vmTid.value = clockLabel(state.betChoice.at);
    }
    const status = result ? '' : bet.mit_gaet || bet.status !== 'aaben' ? betStatus(bet, nowS) : '';
    if (el.vmStatus) {
      el.vmStatus.textContent = status;
      el.vmStatus.hidden = !status;
      el.vmStatus.title = bet.status === 'aaben' && bet.mit_gaet ? 'Klik for at skifte dit gæt' : '';
    }
  }

  async function placeGuess() {
    const bet = state.bets.get(el.vm.dataset.bet);
    if (!bet) return;
    const at = guessFromField(el.vmTid ? el.vmTid.value : '', Date.now()) || (state.betChoice && state.betChoice.at);
    if (!at) return;
    el.vmGaet.disabled = true;
    try {
      await send('POST', '/api/kontor/gaet', { pc: bet.pc, vid: bet.vid, tid: at });
      const now = state.bets.get(betKey(bet));
      if (now) now.mit_gaet = at;
      state.betEdit = null;
      jump();
      say(`🎲 Dit gæt: ${clockLabel(at)} – held og lykke!`, 4000);
    } catch (err) {
      say(`Øv – ${err.message}`, 5000);
    } finally {
      el.vmGaet.disabled = false;
      renderBetCard();
    }
  }

  // ---------------------------------------------------------------- easter eggs (SPEC §23.8)
  /** SSE `paaskeaeg`: one waits while Klippe is busy (≤ 20 s). */
  function easterEgg(data) {
    const ord = data && data.ord;
    if (!eggPlan(ord)) return;
    state.eggWaiting = { ord, at: Date.now() };
    nextOnStage();
  }

  function runEgg(ord) {
    const plan = eggPlan(ord, clock());
    if (!plan) return;
    if (plan.celebrate) {
      celebrate(plan.line, 'confetti', 2.5);
      return;
    }
    if (plan.cat) {
      catCameo();
      return;
    }
    if (plan.haand) {
      state.tmpHaand = { item: plan.haand, until: Date.now() + plan.ms };
      applyLook();
      setTimeout(applyLook, plan.ms + 50);
    }
    if (plan.attr === 'snestorm') {
      snowStorm(plan.ms / 1000);
    } else if (plan.attr) {
      tempAttr(plan.attr, plan.ms);
    }
    if (plan.energi) {
      el.app.dataset.energi = plan.energi;
      setTimeout(renderHunger, plan.ms);
    }
    if (plan.nat) {
      state.natEggUntil = Date.now() + plan.ms;
      pulse('gaab', 1600);
      updateNat();
      setTimeout(updateNat, plan.ms + 50);
    }
    jump();
    say(plan.line, Math.max(3000, Math.min(plan.ms, 6000)), true);      // with the pose, not after the queue
  }

  /** "festkat": the party cat comes in, spins once and goes. */
  function catCameo() {
    if (state.cameo || state.fest) return;
    const run = { timers: [], cat: null };
    state.cameo = run;
    catEnters(run, festkat(), 1700);
    say('Mjav! 🐱', 3000);
    run.timers.push(after(5200, () => catLeaves(run)));
    run.timers.push(after(6700, () => {
      stopCat(run);
      if (state.cameo === run) state.cameo = null;
    }));
  }

  // ---------------------------------------------------------------- the header, memories, the year (SPEC §23.9, §23.11)
  function renderHeaderIcons() {
    const now = clock();
    show(el.frokostKnap, setting('widget_frokost') && setting('widget_kontor') && lunchTime(now));
    show(el.postKnap, setting('widget_post') && setting('widget_kontor') && state.peers.length > 0);
    show(el.julKnap, setting('widget_saeson') && now.getMonth() === 11);
    if (el.julAntal) {
      const ready = (julActive() && state.jul.uaabnede) || 0;
      el.julAntal.textContent = String(ready);
      el.julAntal.hidden = !ready;
    }
    const year = yearInReview(now);
    const yearOn = setting('widget_aarsrapport') && year != null;
    show(el.aarKnap, yearOn);
    if (el.aarKnap && year != null) el.aarKnap.title = `Klippet ${year} – din årsoversigt`;
    if (yearOn && state.ready) {
      const data = memory();
      if (data.aarSagt !== year) {
        data.aarSagt = year;
        remember(data);
        say(`🎬 Dit Klippet ${year} er klar! Tryk på 🎬 øverst`, 7000);
      }
    }
  }

  async function openYear(year) {
    try {
      await send('POST', '/api/aar/aabn', { aar: year });
      say(`🎬 Klippet ${year} åbner i din browser`, 4500);
    } catch (err) {
      say(`Øv – ${err.message}`, 5000);
    }
  }

  /** Once a day: a memory from the projects (GET /api/minder). */
  async function showMemory() {
    if (!setting('search_minder') || clock().getHours() < 6) return;
    let data;
    try {
      data = await get('/api/minder');
    } catch {
      return;
    }
    const line = memoryLine(data && Array.isArray(data.minder) ? data.minder[0] : null);
    if (line && firstToday('minde')) say(line, 9000);
  }

  // ---------------------------------------------------------------- the render game (SPEC §23.10)
  /** The games of the overlay: Hop (the runner), Fejljagt and Over tidslinjen. */
  const GAME_INFO = {
    hop: { name: 'Hop', help: 'Hop over klippefejlene og saml filmruder – klik eller tryk Mellemrum',
      make: gameNew, step: gameStep },
    skyd: { name: 'Fejljagt', help: 'Klap klippefejlene ned, før de når tidslinjen – ← → flytter, Mellemrum klapper (eller brug musen)',
      make: skydNew, step: skydStep },
    kryds: { name: 'Over tidslinjen', help: 'Hop over sporene til Deliver uden at blive ramt af et klip – piletaster (eller klik, hvor Klippe skal hen)',
      make: krydsNew, step: krydsStep },
  };

  /** The game picked last (kept over a reload). */
  function pickedGame() {
    const kind = memory().spil;
    return GAME_IDS.includes(kind) ? kind : 'hop';
  }

  /** The overlay: a round starts on a click (or Space); a demo is one round. */
  function openGame(demo = false, kind = null) {
    if (!el.spil || !el.spilCanvas) return;
    if (!demo && !state.render) {
      say('Render-spillet kan kun spilles, mens Resolve renderer 🎬', 4500);
      return;
    }
    if (state.game) closeGame();
    el.panel.hidden = true;
    closeTray();
    closePost();
    const css = getComputedStyle(el.app);
    const colour = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
    state.game = { demo, kind: GAME_IDS.includes(kind) ? kind : pickedGame(), g: null, raf: 0, last: 0, running: false,
      ended: false, rounds: 0, closeTimer: 0, pops: [], submitted: false, unit: 1, ratio: 1, wUnits: 320,
      look: { body: colour('--body', '#2c3440'), edge: colour('--body-edge', '#46536a'),
        dark: colour('--stripe-dark', '#1b1b1d'), light: colour('--stripe-light', '#f3efe6') } };
    el.spil.hidden = false;
    if (demo) {
      el.spil.dataset.demo = '';
    } else {
      delete el.spil.dataset.demo;
    }
    if (el.spilStart) {
      el.spilStart.textContent = '▶ Start';
      el.spilStart.hidden = false;
    }
    sizeGame();
    gameRender(state.render ? renderProgress(state.render) : null);
    pickGame(state.game.kind);
  }

  /** Another game in the picker (between rounds): its help, its records and how it looks. */
  function pickGame(kind) {
    const G = state.game;
    if (!G || G.running || !GAME_IDS.includes(kind)) return;
    if (kind !== G.kind) {
      G.kind = kind;
      G.g = null;
      G.rounds = 0;
      if (el.spilStart) el.spilStart.textContent = '▶ Start';
      show(el.spilStart, !G.ended);
    }
    const data = memory();
    data.spil = kind;
    remember(data);
    el.spil.dataset.valgt = kind;
    for (const b of el.spilValg ? el.spilValg.querySelectorAll('[data-spil]') : []) {
      b.setAttribute('aria-pressed', String(b.dataset.spil === kind));
    }
    if (!G.ended) gameMessage(G.demo ? `Prøverunde! ${GAME_INFO[kind].help}` : GAME_INFO[kind].help);
    renderGameStats();
    loadGameState();
    drawGame();
  }

  function closeGame() {
    const G = state.game;
    if (!G) return;
    clearTimeout(G.closeTimer);
    cancelAnimationFrame(G.raf);
    if (G.running && G.g && !G.demo) submitScore(G, G.g.score);
    G.running = false;
    state.game = null;
    show(el.spil, false);
    nextOnStage();
  }

  /** Units to pixels: every game is at least GAME.w units wide and GAME.h high; a taller canvas gets
   *  more sky – the game stays at the bottom. */
  function sizeGame() {
    const G = state.game;
    const canvas = el.spilCanvas;
    const ratio = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || canvas.width || 300;
    const h = canvas.clientHeight || canvas.height || 150;
    canvas.width = Math.max(1, Math.round(w * ratio));
    canvas.height = Math.max(1, Math.round(h * ratio));
    G.ratio = ratio;
    G.unit = Math.min(h / GAME.h, w / GAME.w);
    G.wUnits = w / G.unit;
    G.hUnits = h / G.unit;
  }

  async function loadGameState() {
    const G = state.game;
    if (!G) return;
    try {
      const answer = await get(`/api/pet/spil?spil=${G.kind}`);
      if (state.game === G && (!answer.spil || answer.spil === G.kind)) {
        state.spil = answer;
        renderGameStats();
      }
    } catch { /* no records (yet) */ }
  }

  function gameMessage(text) {
    if (!el.spilBesked) return;
    el.spilBesked.textContent = text;
    el.spilBesked.hidden = !text;
  }

  function renderGameStats() {
    const G = state.game;
    const score = G && G.g ? G.g.score : 0;
    if (el.spilPoint) el.spilPoint.textContent = String(score);
    if (el.spilValg) el.spilValg.hidden = Boolean(G && (G.running || G.ended));
    const spil = state.spil && G && (state.spil.spil || 'hop') === G.kind ? state.spil : {};
    if (el.spilBedste) el.spilBedste.textContent = G && G.demo ? 'Prøverunde' : `Din bedste: ${spil.bedste || 0}`;
    if (el.spilRekord) {
      const top = Array.isArray(spil.kontor) && spil.kontor.length ? spil.kontor[0] : null;
      el.spilRekord.textContent = !top ? '' : top.egen ? `Kontorets rekord: ${top.score} – din! 🏆`
        : `Kontorets rekord: ${top.score} – ${shorten(top.navn, 20) || 'Klippe'} (${shorten(top.pc, 15)})`;
      el.spilRekord.hidden = !top;
    }
  }

  /** The render's bar in the game – and the round ends when the render does. */
  function gameRender(info) {
    const G = state.game;
    if (el.spilRender) {
      el.spilRender.style.width = `${G && G.demo ? 100 : (info && info.pct) || 0}%`;
      el.spilRender.title = info ? info.text : '';
    }
    if (!G || G.demo || G.ended || info) return;
    G.ended = true;
    const score = G.g ? G.g.score : 0;
    if (G.running) {
      G.running = false;
      cancelAnimationFrame(G.raf);
      drawGame();
    }
    gameMessage(score > 0 ? `Renderen er færdig! 🎬 Din score: ${score}` : 'Renderen er færdig! 🎬');
    show(el.spilStart, false);
    renderGameStats();
    if (score > 0) submitScore(G, score);
    G.closeTimer = setTimeout(() => {
      if (state.game === G) closeGame();
    }, 5000);
  }

  function startRound() {
    const G = state.game;
    if (!G || G.running || G.ended) return;
    if (G.demo && G.rounds >= 1) {
      closeGame();
      return;
    }
    sizeGame();
    G.g = GAME_INFO[G.kind].make((Date.now() % 2147483647) + 1, G.wUnits, G.hUnits);
    G.rounds += 1;
    G.running = true;
    G.submitted = false;
    G.last = 0;
    G.pops = [];
    show(el.spilStart, false);
    gameMessage('');
    renderGameStats();
    cancelAnimationFrame(G.raf);
    G.raf = requestAnimationFrame(gameFrame);
  }

  function gameFrame(ts) {
    const G = state.game;
    if (!G || !G.running) return;
    const dt = G.last ? (ts - G.last) / 1000 : 0;
    G.last = ts;
    const g = G.g;
    for (const event of GAME_INFO[G.kind].step(g, dt)) {
      if (REDUCED) continue;
      if (G.kind === 'hop' && event === 'frame') {
        G.pops.push({ x: GAME.kx + 6, y: GAME.ground - g.y - GAME.kh - 4, t: 0, text: '+10' });
      } else if (G.kind === 'skyd' && (event === 'frame' || event === 'wave')) {
        G.pops.push(event === 'frame' ? { x: g.x - 6, y: g.floor - SKYD.kh - 4, t: 0, text: '+25' }
          : { x: g.w / 2 - 22, y: 70, t: 0, text: `Bølge klaret! +${50 * g.wave}` });
      } else if (G.kind === 'kryds' && (event === 'frame' || event === 'leveret')) {
        G.pops.push(event === 'frame' ? { x: g.x - 6, y: krydsRowY(g.row) - 2, t: 0, text: '+25' }
          : { x: g.w / 2 - 26, y: krydsRowY(KRYDS.goal) + 12, t: 0, text: `Leveret! 🎬 +${50 + 20 * (g.level - 1)}` });
      }
    }
    for (const pop of G.pops) pop.t += Math.min(0.05, dt);
    G.pops = G.pops.filter((p) => p.t < 0.8);
    drawGame();
    renderGameStats();
    if (g.over) {
      roundOver(G);
      return;
    }
    G.raf = requestAnimationFrame(gameFrame);
  }

  const GAME_OVER = { hul: 'Du faldt i et jump cut! 🕳️', fejl: 'Av – en klippefejl! 💥',
    stoej: 'Støjen ramte – alle tre takes er brugt! 📺', nede: 'Klippefejlene nåede tidslinjen! 👾',
    klip: 'Et klip kørte dig over – alle tre takes er brugt! 🎞️' };

  function roundOver(G) {
    G.running = false;
    cancelAnimationFrame(G.raf);
    const score = G.g.score;
    const why = GAME_OVER[G.g.why] || 'Slut!';
    gameMessage(G.demo ? `${why} ${score} point · Prøverunden er slut – i Resolve kan du spille, mens den renderer`
      : `${why} ${score} point`);
    if (el.spilStart) {
      el.spilStart.textContent = G.demo ? 'Luk' : '▶ Prøv igen';
      el.spilStart.hidden = false;
    }
    renderGameStats();
    submitScore(G, score);
  }

  async function submitScore(G, score) {
    if (G.submitted || !(score > 0)) return;
    G.submitted = true;
    let answer;
    try {
      answer = await send('POST', '/api/pet/spil', { score: Math.round(score), demo: G.demo, spil: G.kind });
    } catch {
      return;                                  // a round that cannot be kept (the render ended long ago)
    }
    if (answer && answer.spil && state.game === G && answer.spil.spil === G.kind) state.spil = answer.spil;
    const name = G.kind === 'hop' ? 'render-spillet' : GAME_INFO[G.kind].name;
    if (state.game === G) {
      renderGameStats();
      if (answer && answer.kontor_rekord) {
        gameMessage(`🏆 Ny kontorrekord: ${score} point!`);
      } else if (answer && answer.ny_rekord) {
        gameMessage(`🎉 Ny rekord: ${score} point!`);
      }
    } else if (answer && (answer.kontor_rekord || answer.ny_rekord)) {
      say(answer.kontor_rekord ? `🏆 Ny kontorrekord i ${name}: ${score} point!` : `🎮 Ny rekord i ${name}: ${score} point!`, 6000);
    }
  }

  /** A click on the canvas, Space or ↑: start a round – or jump (Hop), clap (Fejljagt), hop up
   *  (Over tidslinjen). */
  function gameInput() {
    const G = state.game;
    if (!G) return;
    if (!G.running) {
      startRound();
      return;
    }
    if (G.kind === 'hop') gameJump(G.g);
    else if (G.kind === 'skyd') skydFire(G.g);
    else krydsHop(G.g, 'op');
  }

  /** A click or tap on the course: Fejljagt walks there and claps, Over tidslinjen hops towards it. */
  function gamePointer(event) {
    const G = state.game;
    if (!G || !G.running || G.kind === 'hop') {
      gameInput();
      return;
    }
    const spot = gameSpot(event);
    if (G.kind === 'skyd') {
      G.g.aim = spot.x;
      skydFire(G.g);
      return;
    }
    const dx = spot.x - G.g.x;
    const dy = spot.y - (krydsRowY(G.g.row) + KRYDS.lane / 2);
    if (Math.max(Math.abs(dx), Math.abs(dy)) < 6) return;
    krydsHop(G.g, Math.abs(dy) >= Math.abs(dx) ? (dy < 0 ? 'op' : 'ned') : dx < 0 ? 'venstre' : 'hoejre');
  }

  /** Where a pointer event is on the course, in units (y down from the top of the game). */
  function gameSpot(event) {
    const G = state.game;
    const rect = el.spilCanvas.getBoundingClientRect();
    const extra = G.kind === 'skyd' ? 0 : Math.max(0, (G.hUnits || GAME.h) - GAME.h);   // Fejljagt fills it all
    return { x: (event.clientX - rect.left) / G.unit, y: (event.clientY - rect.top) / G.unit - extra };
  }

  const KEY_DIRS = { ArrowUp: 'op', ArrowDown: 'ned', ArrowLeft: 'venstre', ArrowRight: 'hoejre', w: 'op', s: 'ned',
    a: 'venstre', d: 'hoejre' };

  function gameKey(event) {
    const G = state.game;
    const space = event.key === ' ' || event.code === 'Space';
    const dir = KEY_DIRS[event.key] || KEY_DIRS[String(event.key).toLowerCase()];
    if (G && G.running && G.kind === 'skyd' && (dir === 'venstre' || dir === 'hoejre')) {
      event.preventDefault();
      G.g.aim = null;
      G.g.dir = dir === 'venstre' ? -1 : 1;
      return true;
    }
    if (G && G.running && G.kind === 'skyd' && (space || dir === 'op')) {
      event.preventDefault();
      G.g.fire = true;
      skydFire(G.g);
      return true;
    }
    if (G && G.running && G.kind === 'kryds' && dir) {
      event.preventDefault();
      krydsHop(G.g, dir);
      return true;
    }
    if (space || event.key === 'ArrowUp') {
      event.preventDefault();
      gameInput();
      return true;
    }
    return false;
  }

  /** Fejljagt: letting go of an arrow stops Klippe, letting go of Space stops the clapping. */
  function gameKeyUp(event) {
    const G = state.game;
    if (!G || !G.running || G.kind !== 'skyd') return;
    const dir = KEY_DIRS[event.key] || KEY_DIRS[String(event.key).toLowerCase()];
    if ((dir === 'venstre' && G.g.dir < 0) || (dir === 'hoejre' && G.g.dir > 0)) G.g.dir = 0;
    if (event.key === ' ' || event.code === 'Space' || dir === 'op') G.g.fire = false;
  }

  /** The course in units: a sky (more of it over a tall canvas), then the game picked – drawn with
   *  Klippe's own colours. */
  function drawGame() {
    const G = state.game;
    const canvas = el.spilCanvas;
    if (!G || !canvas) return;
    const ctx = canvas.getContext('2d');
    const k = G.unit * G.ratio;
    const w = G.wUnits;
    const extra = Math.max(0, (G.hUnits || GAME.h) - GAME.h);  // more sky above a tall canvas
    ctx.setTransform(k, 0, 0, k, 0, 0);
    const sky = ctx.createLinearGradient(0, 0, 0, GAME.h + extra);
    sky.addColorStop(0, '#11141f');
    sky.addColorStop(1, '#2b2233');
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, GAME.h + extra);
    ctx.fillStyle = 'rgba(255, 255, 255, 0.35)';            // stars over a tall course
    for (let i = 0; i < Math.round(extra / 6); i += 1) {
      ctx.fillRect((i * 67.3) % w, (i * 29.7) % Math.max(1, extra), 0.9, 0.9);
    }
    ctx.setTransform(k, 0, 0, k, 0, G.kind === 'skyd' ? 0 : extra * k);   // Fejljagt fills the canvas
    if (G.kind === 'skyd') {
      drawSkyd(ctx, G, w, GAME.h + extra);
    } else if (G.kind === 'kryds') {
      drawKryds(ctx, G, w);
    } else {
      drawHop(ctx, G, w);
    }
    ctx.font = '700 9px "Segoe UI", sans-serif';
    ctx.fillStyle = '#ffcf4a';
    for (const pop of G.pops) {
      ctx.globalAlpha = Math.max(0, 1 - pop.t / 0.8);
      ctx.fillText(pop.text, pop.x, pop.y - pop.t * 30);
    }
    ctx.globalAlpha = 1;
  }

  function drawHop(ctx, G, w) {
    const g = G.g;
    const dist = g ? g.dist : 0;
    ctx.fillStyle = 'rgba(255, 255, 255, 0.05)';            // monitors far away
    for (let i = 0; i < 7; i += 1) {
      const span = w + 80;
      const x = ((((i * 113) - dist * 0.25) % span) + span) % span - 40;
      ctx.fillRect(x, 18 + ((i * 41) % 64), 30, 17);
    }
    if (g && g.falling) drawKlippe(ctx, G);
    drawTrack(ctx, w, dist, g);
    if (g) {
      for (const o of g.obstacles) drawObstacle(ctx, o);
      for (const f of g.frames) drawFrame(ctx, f);
    }
    if (!g || !g.falling) drawKlippe(ctx, G);
  }

  function drawTrack(ctx, w, dist, g) {
    const top = GAME.ground;
    ctx.fillStyle = '#121418';
    ctx.fillRect(0, top, w, GAME.h - top);
    const lanes = [[top + 3, '#4f7bd9', '#7f9cff'], [top + 15, '#3c9e6b', '#5fcf8f']];
    const blocks = [[0, 44], [48, 112], [116, 150], [154, 214]];
    for (const [y, fill, edge] of lanes) {
      for (let n = -1; n * 220 < w + 220; n += 1) {
        for (const [a, b] of blocks) {
          const x = a + n * 220 - (dist % 220);
          if (x > w || x + (b - a) < 0) continue;
          ctx.fillStyle = fill;
          ctx.fillRect(x, y, b - a, 9);
          ctx.fillStyle = edge;
          ctx.fillRect(x, y, b - a, 1.5);
        }
      }
    }
    if (!g) return;
    for (const o of g.obstacles) {
      if (o.kind !== 'hul') continue;
      ctx.fillStyle = '#05060a';
      ctx.fillRect(o.x, top - 1, o.w, GAME.h - top + 1);
      ctx.fillStyle = '#ef7166';
      ctx.fillRect(o.x, top - 1, 1.5, 6);
      ctx.fillRect(o.x + o.w - 1.5, top - 1, 1.5, 6);
    }
  }

  function drawObstacle(ctx, o) {
    if (o.kind === 'hul') return;
    drawFejl(ctx, o.kind, o.x, GAME.ground - o.b - o.h, o.w, o.h);
  }

  /** A klippefejl: a red "Media Offline" block, a black frame or a glitch block. */
  function drawFejl(ctx, kind, x, y, w, h) {
    if (kind === 'offline') {
      ctx.fillStyle = '#c93434';
      ctx.fillRect(x, y, w, h);
      ctx.fillStyle = '#ffd6d2';
      ctx.font = '700 5px "Segoe UI", sans-serif';
      if (h >= 18) {
        ctx.fillText('MEDIA', x + 2, y + h / 2 - 1);
        ctx.fillText('OFFLINE', x + 1, y + h / 2 + 5);
      } else {
        ctx.fillText('OFF', x + w / 2 - 4.5, y + h / 2 + 2);
      }
    } else if (kind === 'sort') {
      ctx.fillStyle = '#000';
      ctx.fillRect(x, y, w, h);
      ctx.strokeStyle = '#e8e4dc';
      ctx.lineWidth = 1;
      ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
    } else {
      const colours = ['#ff4fd8', '#33e1ff', '#ffe14a', '#7cff6b'];
      const rows = Math.max(3, Math.round(h / 5));
      for (let i = 0; i < rows; i += 1) {
        const shift = ((i * 7 + Math.floor(x / 6)) % 5) - 2;
        ctx.fillStyle = colours[(i + Math.floor(x / 9)) % colours.length];
        ctx.fillRect(x + shift, y + (i * h) / rows, w, h / rows + 0.5);
      }
    }
  }

  function drawFrame(ctx, f) {
    if (f.taken) return;
    drawFilmFrame(ctx, f.x, GAME.ground - f.b - f.h, f.w, f.h);
  }

  /** A film frame to catch (top-left x, y). */
  function drawFilmFrame(ctx, x, y, w, h) {
    ctx.fillStyle = '#f0a45b';
    ctx.fillRect(x, y, w, h);
    ctx.fillStyle = '#3a2513';
    for (const dx of [1.5, 4.5, 7.5]) {
      ctx.fillRect(x + dx, y + 1, 1.2, 1.4);
      ctx.fillRect(x + dx, y + h - 2.4, 1.2, 1.4);
    }
    ctx.fillStyle = 'rgba(255, 255, 255, 0.55)';
    ctx.fillRect(x + 2, y + 3.2, w - 4, h - 6.4);
  }

  /** The takes left (Fejljagt, Over tidslinjen): small clapper boards in the top left corner. */
  function drawTakes(ctx, G, takes) {
    for (let i = 0; i < 3; i += 1) {
      ctx.globalAlpha = i < takes ? 1 : 0.22;
      ctx.fillStyle = G.look.body;
      ctx.fillRect(5 + i * 11, 5, 8, 6);
      ctx.fillStyle = G.look.light;
      ctx.fillRect(5 + i * 11, 3, 8, 1.6);
    }
    ctx.globalAlpha = 1;
  }

  /** Fejljagt: the flock of klippefejl, their noise, the claps, the timeline and Klippe. */
  function drawSkyd(ctx, G, w, h) {
    const g = G.g;
    const floor = h - SKYD.ground;
    ctx.fillStyle = '#121418';                                // the timeline at the bottom
    ctx.fillRect(0, floor, w, SKYD.ground);
    ctx.fillStyle = '#4f7bd9';
    ctx.fillRect(0, floor + 2, w, 6);
    ctx.fillStyle = '#7f9cff';
    ctx.fillRect(0, floor + 2, w, 1.2);
    const x = g ? g.x : w / 2;
    if (g) {
      for (const f of g.fejl) {
        if (f.hp <= 0) continue;
        ctx.globalAlpha = f.kind === 'offline' && f.hp === 1 ? 0.6 : 1;   // a cracked Media Offline
        drawFejl(ctx, f.kind, f.x, f.y, f.w, f.h);
      }
      ctx.globalAlpha = 1;
      ctx.fillStyle = '#e8e4dc';                               // noise: little static snow
      for (const n of g.noise) {
        for (let i = 0; i < 3; i += 1) ctx.fillRect(n.x + ((i * 2 + Math.floor(n.y)) % 3), n.y + i * 2, 1.2, 1.4);
      }
      ctx.fillStyle = '#ffcf4a';                               // the claps
      for (const s of g.shots) ctx.fillRect(s.x, s.y, s.w, s.h);
      for (const f of g.frames) drawFilmFrame(ctx, f.x, f.y, f.w, f.h);
      drawTakes(ctx, G, g.takes);
    }
    const blink = g && !g.over && g.safe > 0 && Math.floor(g.safe * 10) % 2;   // just hit: it blinks
    if (!blink) {
      drawSlate(ctx, G.look, x - SKYD.kw / 2, floor, SKYD.kw, SKYD.kh - 4,
        { open: g && g.cool > SKYD.reload / 2 ? -0.45 : 0, crashed: g && g.over, mouthO: false, step: 0 });
    }
  }

  /** Over tidslinjen: Deliver at the top, the video tracks, the markers, the audio tracks and the
   *  Media Pool at the bottom – the clips sliding along – and Klippe. */
  function drawKryds(ctx, G, w) {
    const g = G.g;
    const L = KRYDS.lane;
    ctx.font = '700 6px "Segoe UI", sans-serif';
    for (let row = 0; row <= KRYDS.goal; row += 1) {
      const y = krydsRowY(row);
      const track = KRYDS_TRACKS[row];
      ctx.fillStyle = row === KRYDS.goal ? '#3a2f1c' : row === 0 ? '#1f2530' : row === KRYDS.markers ? '#24202c'
        : row % 2 ? '#15171c' : '#121418';
      ctx.fillRect(0, y, w, L);
      ctx.fillStyle = 'rgba(255, 255, 255, 0.35)';
      if (track) {
        ctx.fillText(track[0], 2, y + L / 2 + 2);
      } else if (row === KRYDS.goal) {
        ctx.fillStyle = '#ffcf4a';
        ctx.fillText('🎬 DELIVER', w / 2 - 18, y + L / 2 + 2);
      } else if (row === 0) {
        ctx.fillText('MEDIA POOL', w / 2 - 17, y + L / 2 + 2);
      } else {
        for (let x = 6; x < w; x += 22) {                      // the marker strip
          ctx.fillStyle = ['#ef7166', '#ffcf4a', '#5fcf8f', '#7f9cff'][Math.floor(x / 22) % 4];
          ctx.fillRect(x, y + L / 2 - 2, 4, 4);
        }
      }
    }
    if (g) {
      for (const lane of g.lanes) {
        const y = krydsRowY(lane.row) + 2;
        const [fill, edge] = lane.kind === 'lyd' ? ['#3c9e6b', '#5fcf8f'] : ['#4f7bd9', '#7f9cff'];
        for (const c of lane.clips) {
          if (c.x > w || c.x + c.w < 0) continue;
          ctx.fillStyle = fill;
          ctx.fillRect(c.x, y, c.w, L - 4);
          ctx.fillStyle = edge;
          ctx.fillRect(c.x, y, c.w, 1.5);
          if (lane.kind === 'lyd') {                             // a little waveform
            ctx.fillStyle = 'rgba(255, 255, 255, 0.35)';
            for (let x = c.x + 2; x < c.x + c.w - 2; x += 3) {
              const amp = 1 + Math.abs(Math.sin(x * 0.7)) * 3;
              ctx.fillRect(x, y + (L - 4) / 2 - amp / 2, 1.4, amp);
            }
          }
        }
      }
      if (g.frame) drawFilmFrame(ctx, g.frame.x, krydsRowY(g.frame.row) + 3, g.frame.w, g.frame.h);
      drawTakes(ctx, G, g.takes);
    }
    const row = g ? g.row : 0;
    let x = g ? g.x : w / 2;
    let bottom = krydsRowY(row) + L - 1;
    let lift = 0;
    if (g && g.from && g.hopT > 0) {                            // in the air between two rows
      const p = 1 - g.hopT / KRYDS.hop;
      x = g.from.x + (g.x - g.from.x) * p;
      bottom = krydsRowY(g.from.row) + L - 1 + (krydsRowY(g.row) - krydsRowY(g.from.row)) * p;
      lift = Math.sin(p * Math.PI) * 4;
    }
    const blink = g && !g.over && g.safe > 0 && Math.floor(g.safe * 10) % 2;   // just hit: it blinks
    if (!blink) {
      drawSlate(ctx, G.look, x - 6, bottom - lift, 12, 11,
        { open: lift > 0 ? -0.45 : 0, crashed: g && g.over, mouthO: lift > 0, step: 0 });
    }
  }

  /** Klippe in Hop, a little clapper board on two feet: its clapper opens while it is in the air. */
  function drawKlippe(ctx, G) {
    const g = G.g;
    drawSlate(ctx, G.look, GAME.kx, GAME.ground - (g ? g.y : 0), GAME.kw, GAME.kh - 8, {
      open: g && !g.ground ? -0.45 : 0, crashed: g && g.over && g.why === 'fejl', mouthO: Boolean(g && !g.ground),
      step: G.running && g && g.ground ? Math.floor(g.t * 10) % 2 : 0 });
  }

  /** Klippe as a clapper board `w` wide with a slate `h` high (22/24 of `w` for its face to fit) on two
   *  feet standing at `bottom`. */
  function drawSlate(ctx, look, x, bottom, w, h, { open, crashed, mouthO, step }) {
    const s = w / 24;                                          // drawn for a slate 24 wide (Hop's)
    const top = bottom - 4 * s - h;
    ctx.fillStyle = '#222a34';                                 // feet
    ctx.fillRect(x + 4 * s, bottom - 4 * s - (step ? 1 : 0), 6 * s, 4 * s);
    ctx.fillRect(x + w - 10 * s, bottom - 4 * s - (step ? 0 : 1), 6 * s, 4 * s);
    ctx.fillStyle = look.body;                                 // the slate
    ctx.strokeStyle = look.edge;
    ctx.lineWidth = 1.2;
    roundRect(ctx, x, top, w, h, 3 * s);
    ctx.fill();
    ctx.stroke();
    ctx.save();                                                // the clapper
    ctx.translate(x, top + 1);
    ctx.rotate(open);
    for (let i = 0; i < 6; i += 1) {
      ctx.fillStyle = i % 2 ? look.light : look.dark;
      ctx.fillRect((i * w) / 6, -5 * s, w / 6 + 0.3, 4 * s);
    }
    ctx.restore();
    ctx.fillStyle = '#fbfaf7';
    for (const ex of [x + 7 * s, x + w - 7 * s]) {
      if (crashed) {
        ctx.strokeStyle = '#fbfaf7';
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(ex - 2 * s, top + 7 * s);
        ctx.lineTo(ex + 2 * s, top + 11 * s);
        ctx.moveTo(ex + 2 * s, top + 7 * s);
        ctx.lineTo(ex - 2 * s, top + 11 * s);
        ctx.stroke();
      } else {
        ctx.beginPath();
        ctx.ellipse(ex, top + 9 * s, 2.6 * s, 3.1 * s, 0, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    if (!crashed) {
      ctx.fillStyle = '#1d1b19';
      for (const ex of [x + 7.6 * s, x + w - 6.4 * s]) {
        ctx.beginPath();
        ctx.arc(ex, top + 9.5 * s, 1.3 * s, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.strokeStyle = '#f3efe6';                               // the mouth: a smile (an O in the air)
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    if (mouthO) {
      ctx.arc(x + w / 2, top + 17 * s, 1.6 * s, 0, Math.PI * 2);
    } else {
      ctx.arc(x + w / 2, top + 15 * s, 3 * s, 0.2 * Math.PI, 0.8 * Math.PI);
    }
    ctx.stroke();
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  // ---------------------------------------------------------------- their buttons
  show(el.frokostSkilt, false);                // only while Klippe is at lunch
  on(el.frokostKnap, 'click', ringLunch);
  on(el.frokostSkilt, 'click', comeHome);
  on(el.postKnap, 'click', () => (el.postPanel && el.postPanel.hidden ? openPost() : closePost()));
  on(el.postLuk, 'click', closePost);
  on(el.postSend, 'click', sendPost);
  on(el.postTekst, 'keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendPost();
    }
  });
  on(el.julKnap, 'click', () => {
    closeTray();
    closePost();
    openPanel('jul');
  });
  on(el.aarKnap, 'click', () => openYear(yearInReview(clock()) || clock().getFullYear()));
  on(el.slikGiv, 'click', () => answerSlik(state.slik, 'slik'));
  on(el.vmGaet, 'click', placeGuess);
  on(el.vmTid, 'change', () => {
    const at = guessFromField(el.vmTid.value, Date.now());
    if (at && el.vm.dataset.bet) state.betChoice = { key: el.vm.dataset.bet, at };
    for (const b of (el.vmValg ? el.vmValg.querySelectorAll('.vm__valg') : [])) {
      b.setAttribute('aria-pressed', String(Number(b.dataset.at) === at));
    }
  });
  on(el.vmStatus, 'click', () => {
    const bet = state.bets.get(el.vm.dataset.bet);
    if (bet && bet.status === 'aaben' && bet.mit_gaet) {
      state.betEdit = betKey(bet);
      renderBetCard();
    }
  });
  on(el.spilKnap, 'click', () => openGame(false));
  on(el.spilLuk, 'click', closeGame);
  on(el.spilStart, 'click', (event) => {
    event.stopPropagation();
    if (state.game && state.game.demo && state.game.rounds >= 1 && !state.game.running) {
      closeGame();
    } else {
      startRound();
    }
  });
  on(el.spilCanvas, 'pointerdown', (event) => {
    event.preventDefault();
    gamePointer(event);
  });
  on(el.spilCanvas, 'pointermove', (event) => {
    const G = state.game;
    if (G && G.running && G.kind === 'skyd' && event.pointerType === 'mouse') G.g.aim = gameSpot(event).x;
  });
  for (const b of el.spilValg ? el.spilValg.querySelectorAll('[data-spil]') : []) {
    b.addEventListener('click', () => pickGame(b.dataset.spil));
  }
  document.addEventListener('visibilitychange', () => {
    if (state.game) state.game.last = 0;      // no jump forward after a pause
  });

  window.__klippe = { celebrate, fx, state, applyStatus, applyStage, applyJob, applyPlay, lookReport,
    renderMessages, applySettings, celebrateProgress, openPanel, applyFood, feed, openTray, answerCall,
    applyBuild, applyRobot, aim, naughtyRobot, applyRender, visit, deliver, festkat, floor,
    // fun in the office (SPEC §23)
    clock, funTick, updateNat, natDemo, applySaeson, applyJul, applyRod, startLunch, comeHome, applyBet, renderBetCard,
    easterEgg, snowStorm, openPost, openGame, closeGame, gameInput, startRound, pickGame, loadRod, loadBets, loadSaeson,
    loadJul, eat };   // for tests

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
        const equipped = JSON.parse(event.data).equipped;
        state.looks += 1;
        state.equipped = { ...(state.equipped || {}), ...equipped };
        applyWardrobe(equipped);
      } catch { /* ignore */ }
    });
    source.addEventListener('pet_progress', (event) => {
      try {
        celebrateProgress(JSON.parse(event.data));
      } catch { /* ignore */ }
    });
    source.addEventListener('pet_mad', (event) => {
      try {
        if (!state.feeding) applyFood(JSON.parse(event.data));
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
    source.addEventListener('bygger', (event) => {
      try {
        applyBuild(JSON.parse(event.data));
      } catch { /* ignore */ }
    });
    source.addEventListener('robot', (event) => {
      try {
        applyRobot(JSON.parse(event.data));
      } catch { /* ignore */ }
    });
    source.addEventListener('settings', (event) => {
      try {
        applySettings(JSON.parse(event.data));
      } catch { /* ignore */ }
    });
    source.addEventListener('render', (event) => {
      try {
        applyRender(JSON.parse(event.data));
      } catch { /* ignore */ }
    });
    source.addEventListener('levering', (event) => {
      try {
        deliver(JSON.parse(event.data));
      } catch { /* ignore */ }
    });
    source.addEventListener('besoeg', (event) => {
      try {
        visit(JSON.parse(event.data));
      } catch { /* ignore */ }
    });
    // The fun in the office (SPEC §23.13).
    const handlers = { pet_saeson: applySaeson, pet_jul: applyJul, rod: (d) => applyRod(d),
      sne: (d) => snowStorm(d && d.sekunder), frokost: startLunch, post_sendt: postSent,
      post_svar: (d) => openPost(d && typeof d.til === 'string' ? d.til : null), slik_svar: slikAnswered,
      vaeddemaal: (d) => applyBet(d), paaskeaeg: easterEgg };
    for (const [type, handle] of Object.entries(handlers)) {
      source.addEventListener(type, (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        state.sseSeq[type] = (state.sseSeq[type] || 0) + 1;      // newer than any GET on its way (fresh())
        handle(data);
      });
    }
    // Events are not replayed: when the stream is (back) up, what it may have missed is asked for –
    // a build that ended, a call that stopped ringing or a render that finished in a gap – and, on
    // load too, which colleagues' Klippes are about (visits are never replayed); so is the season's
    // state, the project folder's mess and the bets that are on.
    source.onopen = () => {
      loadBuild();
      loadMessages();
      loadRender();
      loadKontor();
      loadSaeson();
      loadBets();
      if (state.ready) loadRod();
      if (clock().getMonth() === 11 || state.julDemo) loadJul();
    };
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
    setInterval(() => state.build && loadBuild(), 30e3);         // a lost "done" never leaves it building
    setInterval(() => state.render && loadRender(), 30e3);       // … nor rendering
    await loadMessages();
    await loadRender();                    // before the stream: a finished render is celebrated once
    loadBuild();
    loadPet();
    loadFood();
    setInterval(loadFood, 60e3);           // it gets hungry slowly: once a minute is plenty
    window.addEventListener('resize', () => reportLook());
    loadPlay();
    connectEvents();
    setTimeout(() => say(`Hej! Jeg er ${state.name} 👋`), 600);
    setTimeout(() => {
      const hungry = state.hunger === 'sulten' || state.hunger === 'skrubsulten';
      if (hatchedFood() && hungry && firstToday('sult-hej')) say(`${foodSay(state.hunger)} Tryk på 🍔 Mad`, 7000);
    }, 3000);
    // The fun in the office (SPEC §23): after the hello it may speak (fyraften, Halloween, a mess).
    setTimeout(() => {
      state.ready = true;
      funTick();
      loadRod();
    }, 1500);
    setTimeout(showMemory, 6600);
    setInterval(funTick, 15e3);
    setInterval(loadKontor, 60e3);           // colleagues come and go: the ✉️ follows them
  }

  init();
})();

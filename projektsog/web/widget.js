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

  const SLOT_KEYS = ['farve', 'striber', 'hat', 'briller', 'mund', 'haand', 'aura'];
  const OUTFITS = ['none', 'color', 'fusion', 'audio', 'deliver'];

  /** How a colleague's Klippe looks (SSE `besoeg`): only known stages and outfits, only the known
   *  wardrobe slots with plain item names – they only ever become data-* attributes that the CSS
   *  matches. A guest from a delivery party wears the Deliver cap. */
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
    return { stage, outfit, pynt };
  }

  /** What the guest says: "Hej fra STUDIO-PC! Jeg fik 🏆 Durumkongen". */
  function visitLine(b) {
    const v = b || {};
    const from = `Hej fra ${shorten(v.pc, 15) || 'kontoret'}!`;
    if (v.type === 'fest') return `${from} Vi har leveret! 🎉`;
    const t = v.trofae;
    if (!t || !t.name) return `${from} Jeg har fået et nyt trofæ 🏆`;
    return t.kind === 'fund' ? `${from} Jeg fandt 🎁 ${shorten(t.name, 32)}` : `${from} Jeg fik 🏆 ${shorten(t.name, 32)}`;
  }

  /** What Klippe answers its guest. */
  function visitReply(b) {
    const name = shorten(b && b.navn, 20) || 'du';
    return b && b.type === 'fest' ? `Hej ${name}! 👋 Tillykke med leveringen!` : `Hej ${name}! 👋 Flot klaret!`;
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
    visitReply, deliveryLine, catFrames, nextSpin, keyGreen, renameRefs, STAGES, FOCUS_STARS, BREAK_NUDGES,
    ROBOT_POSES, ROBOT_COUNT, BODY_SCALE, DIRECTOR_LINES, SLOT_KEYS, OUTFITS, FESTKAT_FRAMES };
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
    tempo: 1,                                // tests speed up the visits and the party (their timelines × tempo)
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
    const rendering = renderLine(state.render, state.name);
    const building = buildLine(state.build, state.name);
    const job = jobLine(transferInfo(state.job), state.job);
    const mood = moodFor(state.status);
    const food = hatchedFood();
    const hungry = foodLine(food, mood, state.name, state.craving);
    const rush = food && food.energi && mood === 'working' ? '⚡ ' : '';   // turbo editing
    el.mood.textContent = rendering || building || job || hungry || `${rush}${moodLine(state.status, state.name)}`;
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

  /** "🎉 Prøv leveringsfesten" / "👋 Prøv et besøg" in the 🏆 panel: the panel closes, so it is seen. */
  async function demoNow(path) {
    el.panel.hidden = true;
    try {
      await send('POST', path, {});
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
    state.peers = Array.isArray(data.peers) ? data.peers.filter((p) => p && typeof p === 'object') : [];
    const names = state.peers.map((p) => `${shorten(p.navn, 20) || 'Klippe'} (${shorten(p.pc, 15)})`);
    el.demoVisit.title = names.length ? `Kollegernes Klipper lige nu: ${names.join(', ')}`
      : 'En kollegas Klippe kigger forbi – som når den har fået et trofæ';
  }

  /** SSE `besoeg`: a colleague's Klippe comes by (the newest waits – one at most). */
  function visit(data) {
    if (!data || typeof data !== 'object') return;
    state.visitWaiting = data;
    nextOnStage();
  }

  /** The next party or guest once the stage is free – never while Klippe eats or plays out of the
   *  box, a guest not while a card is transferred either; a party goes first. */
  function nextOnStage() {
    clearTimeout(state.stageTimer);
    state.stageTimer = 0;
    if (state.visit || state.fest || (!state.visitWaiting && !state.festWaiting)) return;
    const busy = Boolean(state.eating) || el.app.dataset.play === 'out';
    if (state.festWaiting && !busy) {
      const data = state.festWaiting;
      state.festWaiting = null;
      startParty(data);
    } else if (state.visitWaiting && !busy && !el.app.dataset.transfer) {
      const data = state.visitWaiting;
      state.visitWaiting = null;
      startVisit(data);
    } else {
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
  // What a guest does not bring along: Klippe's own jobs, its phone, its food and its hole.
  const GUEST_LEAVE_OUT = '.crew, .luge, .render, .pakke, .telefon, .carry, .hole, .zzz, .drop, .drop__tape, .megafon,'
    + ' .drom, .knurr, .savl, .mad';

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

  /** The guest walks in from the left, says its line, both jump and clap (twice), and after ~6 s it
   *  walks out again. Everything from the packet goes into textContent or data-* only. */
  function startVisit(data) {
    const look = guestLook(data);
    const guest = node('div', 'gaest');
    Object.assign(guest.dataset, { stage: look.stage, outfit: look.outfit, mood: 'chill', ...look.pynt });
    const bubble = node('p', 'gaest__boble', visitLine(data));
    bubble.setAttribute('role', 'status');
    bubble.hidden = true;
    guest.append(bubble, guestDrawing());
    guest.style.left = '-6px';
    guest.style.top = `${Math.round(floor().y - GUEST_PX * 0.9)}px`;
    document.body.append(guest);
    const run = { data, guest, timers: [] };
    state.visit = run;
    walk(guest, -GUEST_PX - 20, 0, 1400);
    later(run, 1400, () => {
      bubble.hidden = false;
    });
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
   *  GIF once it is decoded (≤ 2 s more), else the drawn cat. */
  async function catEnters(run, frames) {
    const cat = await Promise.race([frames, new Promise((resolve) => setTimeout(resolve, 2000, null))]);
    if (state.fest !== run || run.cat) return;
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
      playCat(run);
    } else if (!REDUCED) {
      run.cat.timer = setTimeout(() => spinDrawnCat(run), 1800 * state.tempo);
    }
  }

  /** The GIF: it stands and hops (the stand frames over and over), and every 2.5–6 s it spins once
   *  (the spin frames, no hop meanwhile). Reduced motion: one standing frame. */
  function playCat(run) {
    const c = run.cat;
    const { frames, plan } = c.cat;
    const ctx = c.figure.getContext('2d');
    let index = plan.stand[0];
    let spinning = false;
    let spinAt = performance.now() + nextSpin() * state.tempo;
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
      if (equipped && equipped[slot]) el.app.dataset[slot] = equipped[slot];
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
  for (const tab of el.panel.querySelectorAll('[data-panel-tab]')) {
    tab.addEventListener('click', () => {
      state.panelTab = tab.dataset.panelTab;
      renderPanel();
    });
  }
  window.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    el.panel.hidden = true;
    closeTray();
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
    renderMessages, applySettings, celebrateProgress, openPanel, applyFood, feed, openTray, answerCall,
    applyBuild, applyRobot, aim, naughtyRobot, applyRender, visit, deliver, festkat, floor };   // for tests

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
    // Events are not replayed: when the stream is (back) up, what it may have missed is asked for –
    // a build that ended, a call that stopped ringing or a render that finished in a gap – and, on
    // load too, which colleagues' Klippes are about (visits are never replayed).
    source.onopen = () => {
      loadBuild();
      loadMessages();
      loadRender();
      loadKontor();
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
  }

  init();
})();

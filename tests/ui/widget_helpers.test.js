// Unit tests for the pure helpers in projektsog/web/widget.js (node --test; run by tests/test_ui_js.py).
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const w = require(path.join(__dirname, '..', '..', 'projektsog', 'web', 'widget.js'));

test('Klippe grows with all time ever logged', () => {
  assert.deepEqual([0, 4.9, 5, 30, 120, 400].map((h) => w.stageFor(h).key),
    ['egg', 'egg', 'baby', 'junior', 'pro', 'legend']);
  assert.equal(w.stageFor(0).level, 1);
  assert.equal(w.stageFor(32).level, 9);
  assert.deepEqual([w.stageFor(20).next, w.stageFor(20).toNext], ['Junior', 5]);
  assert.equal(w.stageFor(1000).next, null);
});

test('the mood follows the time tracking', () => {
  assert.equal(w.moodFor({ state: 'recording', enabled: true }), 'working');
  assert.equal(w.moodFor({ state: 'away', enabled: true }), 'waiting');
  assert.equal(w.moodFor({ state: 'idle', enabled: true }), 'sleepy');
  assert.equal(w.moodFor({ state: 'paused', enabled: true }), 'chill');
  assert.equal(w.moodFor({ state: 'no-resolve', enabled: true }), 'sleeping');
  assert.equal(w.moodFor({ state: 'recording', enabled: false }), 'sleeping');
  assert.equal(w.moodFor(null), 'sleeping');
});

test('the outfit follows the page', () => {
  const at = (bucket, state = 'recording') => w.outfitFor({ state, bucket });
  assert.deepEqual(['color', 'fusion', 'fairlight', 'musik', 'deliver', 'edit'].map((b) => at(b)),
    ['color', 'fusion', 'audio', 'audio', 'deliver', 'none']);
  assert.equal(at('color', 'away'), 'color');
  assert.equal(at('color', 'idle'), 'none');
});

test('milestones: whole hours, focus marks and the streak of days', () => {
  assert.deepEqual(w.crossedHours(3500, 3700), [1]);
  assert.deepEqual(w.crossedHours(3700, 11000), [2, 3]);
  assert.deepEqual(w.crossedHours(0, 3599), []);
  assert.deepEqual(w.crossedMarks(w.FOCUS_STARS, 24, 26), [25]);
  assert.deepEqual(w.crossedMarks(w.FOCUS_STARS, 26, 95), [50, 90]);
  const days = { '2026-10-02': 4000, '2026-10-01': 9000, '2026-09-30': 3600, '2026-09-29': 100 };
  assert.equal(w.dayStreak(days, '2026-10-02'), 3);
  // Today is still young (under an hour): the streak up to yesterday still stands.
  assert.equal(w.dayStreak({ ...days, '2026-10-02': 600 }, '2026-10-02'), 2);
  assert.equal(w.dayStreak({}, '2026-10-02'), 0);
});

test('what Klippe says', () => {
  const working = { state: 'recording', enabled: true, project: 'Rikke Lindholm - Testimonial', timeline: 'Testimonial v3',
    bucket: 'edit', bucket_label: 'Edit' };
  assert.equal(w.moodLine(working), 'Klipper løs på Testimonial v3 · Edit');
  assert.equal(w.moodLine({ ...working, bucket: 'color' }), 'Gør Testimonial v3 smuk i Color 😎');
  assert.equal(w.moodLine({ ...working, bucket: 'ai' }), 'Laver AI-video og -billeder til Testimonial v3 🤖');
  assert.equal(w.moodLine({ ...working, state: 'away' }, 'Bobby'), 'Bobby venter på dig – tiden tæller stadig ⏳');
  assert.equal(w.moodLine({ state: 'off', enabled: false }), 'Tidsregistreringen er slået fra 💤');
  assert.equal(w.cheer('hour', 1, () => 0), '1 time i dag! 🎉');
  assert.equal(w.cheer('hour', 3, () => 0), '3 timer i dag! 🎉');
  assert.equal(w.cheer('card', { mode: 'move', camera: 'FX9' }, () => 0), 'Kortet er tømt – alt ligger sikkert på disken! 🎉');
  assert.equal(w.hm(167 * 60), '2:47');
  assert.equal(w.words(167 * 60), '2 t 47 min');
});

test("Klippe's job: carrying, checking and emptying the card", () => {
  const job = { id: 'j1', state: 'copying', mode: 'copy', camera: 'FX9', project_name: 'Rikke Lindholm',
    files_done: 40, files_total: 297, bytes_total: 1000, copied: 500, verified: 300, eta_s: 380 };
  assert.deepEqual(w.transferInfo(job), { busy: true, phase: 'copy', pct: 40, title: 'Overfører FX9-kort → Rikke Lindholm',
    sub: '40 af 297 filer · ca. 6 min tilbage' });
  assert.equal(w.jobLine(w.transferInfo(job), job), 'Bærer FX9-filer over i Rikke Lindholm 📦');
  const checking = { ...job, state: 'verifying' };
  assert.equal(w.transferInfo(checking).phase, 'verify');
  assert.equal(w.jobLine(w.transferInfo(checking), checking), 'Tjekker hver fil med lup 🔍 – 40 %');
  const emptying = { ...job, mode: 'move', state: 'deleting', deleted: 120 };
  assert.deepEqual([w.transferInfo(emptying).phase, w.transferInfo(emptying).sub],
    ['delete', '120 af 297 filer slettet fra kortet – alt er kontrolleret']);
  assert.equal(w.transferInfo({ ...job, eta_s: 30 }).sub, '40 af 297 filer · under 1 min tilbage');
  const now = Date.now();
  const done = { ...job, mode: 'move', state: 'done', files_done: 297, finished: now / 1000 - 60 };
  assert.deepEqual([w.transferInfo(done, now).title, w.transferInfo(done, now).pct, w.transferInfo(done, now).busy],
    ['FX9-kortet er flyttet ✓', 100, false]);
  assert.equal(w.jobLine(w.transferInfo(done, now), done), null);
  assert.equal(w.transferInfo({ ...done, finished: now / 1000 - 3600 }, now), null);   // long ago: gone
  const failed = { ...job, state: 'failed', error: 'Kortet blev taget ud', finished: now / 1000 };
  assert.deepEqual([w.transferInfo(failed, now).failed, w.transferInfo(failed, now).sub], [true, 'Kortet blev taget ud']);
  assert.equal(w.transferInfo(null), null);
});

test('messages: what needs you first, quiet ones last, expired gone', () => {
  const now = 1000;
  const list = [
    { tag: 'a', prioritet: 'stille', udloeber_ved: 2000 },
    { tag: 'b', udloeber_ved: 2000 },
    { tag: 'c', udloeber_ved: 999 },
    { tag: 'd', prioritet: 'normal' },
  ];
  assert.deepEqual(w.messageOrder(list, now).map((m) => m.tag), ['b', 'd', 'a']);
  assert.deepEqual(w.messageOrder(null, now), []);
  assert.equal(w.quietLine(1), '📬 1 besked · vis');
  assert.equal(w.quietLine(3), '📬 3 beskeder · vis');
});

test('a call: who rings, missed or ringing, and only until it is answered', () => {
  const call = { tag: 'koe:venter', titel: '🎬 Mette vil bruge Resolve', opkald: true, besvaret: false, ringer: true };
  assert.equal(w.isCall(call), true);
  assert.equal(w.isCall({ ...call, besvaret: true }), false);              // answered: a plain card
  assert.equal(w.isCall({ tag: 'x', titel: 'Claude · færdig' }), false);   // no buttons: never a call
  assert.equal(w.isCall(null), false);
  assert.equal(w.callerName({ ...call, session: 'Jonas' }), 'Jonas');      // the session first
  assert.equal(w.callerName(call), 'Mette');                               // else the name in the title
  assert.equal(w.callerName({ titel: 'Lise Marie vil bruge Resolve' }), 'Lise Marie');
  assert.equal(w.callerName({ titel: '🔐 Noget skal have lov', session: '  ' }), 'Claude');
  assert.equal(w.callLine(call), '📞 Mette ringer');
  assert.equal(w.callLine({ ...call, ringer: false }), '📞 Ubesvaret opkald fra Mette');
});

test('the robot crew: the sheet, the muzzle, what Klippe says', () => {
  assert.deepEqual(w.ROBOT_POSES, ['robot-a', 'robot-b', 'robot-baer', 'robot-klip', 'robot-hop', 'robot-fraek', 'robot-panik']);
  assert.equal(w.isRobotSheet(w.ROBOT_POSES), true);
  assert.equal(w.isRobotSheet(['normal', 'happy']), false);
  assert.equal(w.isRobotSheet(['robot-a', 'aim']), false);                 // all of them, or Klippe's sheet
  assert.equal(w.isRobotSheet([]), false);
  assert.deepEqual(w.ROBOT_COUNT, { egg: 4, baby: 5, junior: 7, pro: 9, legend: 12 });
  // The end of the barrel in the mirrored aim pose: (100 − 92·S, 180 − 54.75·S).
  assert.deepEqual(w.muzzle('legend'), { x: 8, y: 125.25 });
  assert.deepEqual(w.muzzle('legend', 'hoejre'), { x: 192, y: 125.25 });     // aiming right: as drawn
  const baby = w.muzzle('baby');
  assert.ok(Math.abs(baby.x - 31.92) < 1e-9 && Math.abs(baby.y - 139.485) < 1e-9);
  assert.equal(w.muzzle('egg'), null);
  assert.equal(w.directorLine(() => 0), 'Action! 🎬');
  assert.equal(w.directorLine(() => 0.4, 5), 'Flot, robot nr. 3! 🤖');
  for (let i = 0; i < 20; i += 1) assert.ok(w.directorLine(Math.random, 12).length > 3);
  const build = { aktiv: true, navn: 'Mette', projekt: 'Rikke Lindholm', opgave: '', demo: false, ude: false };
  assert.equal(w.buildLine({ ...build, projekt: '' }), '🤖 Mette bygger i Resolve – Klippe dirigerer robotterne');
  assert.equal(w.buildLine(build, 'Bobby'), '🤖 Mette bygger i Rikke Lindholm – Bobby dirigerer robotterne');
  assert.equal(w.buildLine({ ...build, opgave: 'Portræt v2' }), '🤖 Mette bygger „Portræt v2“ – Klippe dirigerer robotterne');
  assert.ok(w.buildLine({ ...build, opgave: 'x'.repeat(200) }).includes('…'));
  assert.equal(w.buildLine({ ...build, ude: true }), '🤖 Robotterne klipper ude på skærmen – rør musen, så går de ind');
  assert.equal(w.buildLine({ ...build, demo: true, navn: 'Demo', projekt: 'Robotterne øver sig' }),
    '🤖 Robotterne øver sig – Klippe dirigerer');
  assert.equal(w.buildLine({ ...build, aktiv: false }), null);
  assert.equal(w.buildLine(null), null);
  assert.equal(w.doneLine(build), '✅ Mette er færdig – klar til at klippe!');
  assert.equal(w.doneLine({ ...build, demo: true }), 'Robotterne er færdige med at øve! 🎉');
});

test('hatched by hand: at least a baby, the level stays the hours', () => {
  const egg = w.stageFor(2);
  const baby = w.stageFor(2, true);
  assert.equal(egg.key, 'egg');
  assert.deepEqual([baby.key, baby.level, baby.next, baby.toNext], ['baby', egg.level, 'Junior', 23]);
  assert.equal(w.stageFor(30, true).key, 'junior');
});

test('trophies: progress, the news and the wardrobe of a sprite sheet', () => {
  assert.equal(w.trophyProgress({ goal: 20, current: 12, unit: 'dage' }), '12 / 20 dage');
  assert.equal(w.trophyProgress({ goal: 10, current: 3.46, unit: 't' }), '3,4 / 10 t');
  assert.equal(w.trophyProgress({ goal: 1, current: 0.37, unit: 'TB' }), '0,3 / 1 TB');
  assert.equal(w.trophyProgress({ goal: 1, current: 0, unit: '' }), '');
  assert.equal(w.trophyProgress({ goal: 5, current: 5, unit: 'dage', unlocked: 1 }), '');
  assert.equal(w.trophyProgress({ goal: 1, current: 0, secret: true }), '');
  assert.equal(w.progressLine({ kind: 'trofae', name: 'Mål!', reward: { name: 'Festhat' }, rarity: 'almindelig' }),
    '🏆 Mål! Ny ting: Festhat');
  assert.equal(w.progressLine({ kind: 'trofae', name: 'Trofast', reward: null }), '🏆 Trofast!');
  assert.equal(w.progressLine({ kind: 'fund', name: 'AWP', rarity: 'legendarisk' }), '🌟 LEGENDARISK! 🎁 Klippe fandt noget: AWP!');
  assert.deepEqual(w.parsePynt('hat:baret,haand:awp,x:<script>,:y,briller'), { hat: 'baret', haand: 'awp' });
});

test('hunger: how full, what Klippe says and the drip', () => {
  assert.deepEqual([100, 70, 69, 35, 34, 12, 11, 0].map(w.hungerLevel),
    ['maet', 'maet', 'fin', 'fin', 'sulten', 'sulten', 'skrubsulten', 'skrubsulten']);
  assert.equal(w.hungerLevel(null), null);
  assert.equal(w.hungerLevel('x'), null);
  const hungry = { maet: 20, energi: null };
  assert.equal(w.foodLine(hungry, 'chill', 'Klippe', 'durum'), 'Klippe er sulten – drømmer om en durum 🌯');
  assert.equal(w.foodLine(hungry, 'working', 'Klippe', 'durum'), null);              // the work line stays
  assert.equal(w.foodLine(hungry, 'sleeping', 'Klippe', null), 'Klippe sover og drømmer om mad 🍔 💤');
  assert.equal(w.foodLine({ maet: 5 }, 'working', 'Klippe', null), 'Klippe er skrubsulten! Giv den noget at spise 🍔');
  const rush = { maet: 60, energi: { item: 'booster', name: 'Faxe Kondi Booster' } };
  assert.equal(w.foodLine(rush, 'chill', 'Klippe'), '⚡ Klippe er helt oppe at køre på Faxe Kondi Booster');
  assert.equal(w.foodLine(rush, 'working', 'Klippe'), null);
  assert.equal(w.foodLine({ maet: 60, energi: { item: 'drop', name: 'Booster-drop' } }, 'chill', 'Klippe'),
    '💉 Klippe ligger i Booster-drop – fuld fart ⚡');
  assert.equal(w.foodLine({ maet: 60 }, 'chill'), null);
  assert.equal(w.foodLine(null, 'chill'), null);
  assert.equal(w.foodHint({ kind: 'mad', points: 60, energy_min: 0 }), '+60');
  assert.equal(w.foodHint({ kind: 'drop', points: 15, energy_min: 45 }), '⚡ 45 min');
  assert.equal(w.foodSay('durum', () => 0), 'Mmm, durum med det hele! 🌯');
  assert.equal(w.foodSay('maet', () => 0.99), 'Ikke en bid mere … 😵');
  assert.equal(w.foodSay('ukendt'), 'Mmm! 😋');
  assert.equal(w.dropLeft({ fra: 0, til: 100 }, 25), 0.75);                           // the drip's own bag
  assert.equal(w.dropLeft({ fra: 0, til: 100 }, 100), 0.05);                          // never quite empty
  assert.equal(w.dropLeft(null), 1);
});

test('renders: the progress line under the pet, the mood line and how a render ends', () => {
  const r = { aktiv: true, pct: 47, eta_s: 180, navn: 'Portræt_v3.mp4', tidslinje: 'Portræt v3', projekt: 'Rikke Lindholm',
    af_claude: null, faerdig: null };
  assert.deepEqual(w.renderProgress(r), { what: 'Renderer Portræt_v3.mp4', tal: '47 % · ca. 3 min', pct: 47,
    text: 'Renderer Portræt_v3.mp4 · 47 % · ca. 3 min' });
  assert.equal(w.renderProgress({ ...r, eta_s: 30 }).tal, '47 % · under 1 min');
  assert.equal(w.renderProgress({ ...r, eta_s: 3900 }).tal, '47 % · ca. 1 t 5 min');
  assert.equal(w.renderProgress({ ...r, eta_s: null }).tal, '47 %');
  assert.equal(w.renderProgress({ ...r, pct: null, eta_s: null }).text, 'Renderer Portræt_v3.mp4 · går i gang …');
  assert.equal(w.renderProgress({ ...r, navn: null }).what, 'Renderer Portræt v3');      // the timeline, else the job
  assert.equal(w.renderProgress({ ...r, navn: null, tidslinje: null }).what, 'Renderer tidslinjen');
  assert.equal(w.renderProgress({ ...r, pct: 140.2 }).pct, 100);
  assert.equal(w.renderProgress({ ...r, aktiv: false }), null);
  assert.equal(w.renderProgress(null), null);
  assert.equal(w.renderLine(r), '🎬 Robotterne renderer Portræt v3 – Klippe holder øje');
  assert.equal(w.renderLine({ ...r, af_claude: 'Mette' }, 'Bobby'), '🎬 Mette renderer Portræt v3 – robotterne fodrer maskinen');
  assert.equal(w.renderLine({ ...r, aktiv: false }), null);
  const done = { udfald: 'done', fil: 'Portræt_v3.mp4', sti: 'D:\Rikke\Final\Portræt_v3.mp4', mappe: 'D:\Rikke\Final',
    levering: true, fejl: null, seq: 3 };
  assert.equal(w.renderDoneLine(done), 'Renderen er færdig! 🎬');
  assert.equal(w.renderDoneLine({ ...done, udfald: 'failed', fejl: 'Disken er fuld' }), 'Renderen fejlede 😟 – Disken er fuld');
  assert.equal(w.renderDoneLine({ ...done, udfald: 'failed' }), 'Renderen fejlede 😟');
  assert.equal(w.renderDoneLine({ ...done, udfald: 'cancelled' }), 'Renderen blev stoppet ✋');
  assert.equal(w.renderDoneLine({ ...done, udfald: 'gone' }), null);                   // just gone: nothing to say
  assert.equal(w.renderDoneLine(null), null);
  assert.equal(w.renderKey(done), '3|done|D:\Rikke\Final\Portræt_v3.mp4');
  assert.equal(w.renderKey({ ...done, sti: null }), '3|done|Portræt_v3.mp4');
  assert.notEqual(w.renderKey(done), w.renderKey({ ...done, seq: 4 }));
  assert.equal(w.renderKey({ ...done, seq: null }), null);
  assert.equal(w.renderKey(null), null);
});

test("office Klippes: how a colleague's Klippe looks and what they say", () => {
  const visit = { type: 'trofae', pc: 'STUDIO-PC', navn: 'Bobby', stage: 'pro', outfit: 'color',
    pynt: { hat: 'festhat', farve: 'guld', haand: 'awp', ukendt: 'x', briller: '<b>', aura: 'Hjerter' },
    trofae: { kind: 'trofae', id: 'durumkongen', name: 'Durumkongen', rarity: 'sjælden' } };
  // only known stages, outfits and slots, only plain item names – they only ever become data-*
  assert.deepEqual(w.guestLook(visit), { stage: 'pro', outfit: 'color', pynt: { farve: 'guld', hat: 'festhat', haand: 'awp' },
    kostume: null, post: false });
  assert.deepEqual(w.guestLook({ stage: 'boss', outfit: 'pyjamas', pynt: 'hat:x' }),
    { stage: 'baby', outfit: 'none', pynt: {}, kostume: null, post: false });
  assert.deepEqual(w.guestLook({ pynt: JSON.parse('{"__proto__": {"hat": "x"}, "constructor": "y"}') }).pynt, {});
  assert.deepEqual(w.guestLook(null), { stage: 'baby', outfit: 'none', pynt: {}, kostume: null, post: false });
  assert.equal(w.guestLook({ ...visit, type: 'fest' }).outfit, 'deliver');              // a party guest wears the cap
  assert.equal(w.visitLine(visit), 'Hej fra STUDIO-PC! Jeg fik 🏆 Durumkongen');
  assert.equal(w.visitLine({ ...visit, trofae: { kind: 'fund', id: 'awp', name: 'AWP' } }), 'Hej fra STUDIO-PC! Jeg fandt 🎁 AWP');
  assert.equal(w.visitLine({ ...visit, trofae: null }), 'Hej fra STUDIO-PC! Jeg har fået et nyt trofæ 🏆');
  assert.equal(w.visitLine({ ...visit, type: 'fest', trofae: null }), 'Hej fra STUDIO-PC! Vi har leveret! 🎉');
  assert.equal(w.visitReply(visit), 'Hej Bobby! 👋 Flot klaret!');
  assert.equal(w.visitReply({ ...visit, type: 'fest', navn: '' }), 'Hej du! 👋 Tillykke med leveringen!');
  const ids = { stripes: 'stripes-gaest', glow: 'glow-gaest' };
  assert.equal(w.renameRefs('url(#stripes)', ids), 'url(#stripes-gaest)');
  assert.equal(w.renameRefs('url(#shades)', ids), 'url(#shades)');                     // ours: shared, fixed colours
  assert.equal(w.renameRefs('url(#toString)', ids), 'url(#toString)');
});

test('the delivery party: the line, the cat and its green screen', () => {
  assert.equal(w.deliveryLine({ kilde: 'render', fil: 'Portræt_v3.mp4', projekt: 'Rikke', sti: null, demo: false }),
    'Leveret: Portræt_v3.mp4 🎉');
  assert.equal(w.deliveryLine({ fil: null, projekt: 'Rikke Lindholm' }), 'Leveret: Rikke Lindholm 🎉');
  assert.equal(w.deliveryLine(null), 'Leveret: filen 🎉');
  // GIPHY's cat stands on frames 0–23 and spins on 24–69; any other GIF stands on its first and spins on all
  assert.deepEqual(w.catFrames(w.FESTKAT_FRAMES), { stand: [0, 23], spin: [24, 69] });
  assert.deepEqual(w.catFrames(4), { stand: [0, 0], spin: [0, 3] });
  assert.deepEqual(w.catFrames(1), { stand: [0, 0], spin: [0, 0] });
  assert.equal(w.nextSpin(() => 0), 2500);
  assert.equal(w.nextSpin(() => 1), 6000);
  // 2×2 pixels: green screen and a soft edge (40 greener), the cat and a darker green
  const px = new Uint8ClampedArray([0, 255, 0, 255, 120, 160, 110, 255, 70, 64, 58, 255, 10, 200, 30, 255]);
  assert.equal(w.keyGreen(px, 2), 1);                                                    // the cat stands on row 1
  assert.deepEqual([...px], [0, 255, 0, 0, 120, 120, 110, 128, 70, 64, 58, 255, 10, 200, 30, 0]);
  const kept = new Uint8ClampedArray([100, 120, 90, 255]);                               // only 20 greener: kept
  assert.equal(w.keyGreen(kept, 1), 0);
  assert.deepEqual([...kept], [100, 120, 90, 255]);
  assert.equal(w.keyGreen(new Uint8ClampedArray([0, 255, 0, 255, 0, 250, 0, 255]), 1), -1);   // all green: no cat
});

// ---------------------------------------------------------------- fun in the office (SPEC §23)
const at = (y, mo, d, h = 12, mi = 0, s = 0) => new Date(y, mo - 1, d, h, mi, s);

test('Natte-Klippe: yawns from fyraften, dozes after 20 min, sleeps after 45 until 06:00', () => {
  assert.equal(w.clockMinutes('16:00'), 960);
  assert.equal(w.clockMinutes(' 9:05 '), 545);
  for (const bad of ['', null, undefined, '16', '24:00', '16:60', '16.00', 'x']) assert.equal(w.clockMinutes(bad), null);
  const phase = (h, m, f = '16:00') => w.natFase(at(2026, 10, 12, h, m), f);
  assert.deepEqual([phase(15, 59), phase(16, 0), phase(16, 19), phase(16, 20), phase(16, 44), phase(16, 45)],
    [null, 'gaber', 'gaber', 'doeser', 'doeser', 'sover']);
  assert.deepEqual([phase(23, 59), phase(0, 30), phase(5, 59), phase(6, 0), phase(11, 0)],
    ['sover', 'sover', 'sover', null, null]);
  assert.equal(phase(16, 30, ''), null);                                    // switched off
  assert.equal(phase(17, 10, '17:30'), null);
  assert.equal(phase(17, 40, '17:30'), 'gaber');
  // a late fyraften goes past midnight: 23:50 → dozing at 00:10
  assert.deepEqual([phase(23, 55, '23:50'), phase(0, 10, '23:50'), phase(0, 40, '23:50')], ['gaber', 'doeser', 'sover']);
  assert.deepEqual([w.fyraftenLabel('16:00'), w.fyraftenLabel('16:30'), w.fyraftenLabel('')], ['16', '16.30', '16']);
  assert.equal(w.natLine('sover', 'working', '16:00', 'Klippe'), '💤 Klippe sover – fyraften var kl. 16. Gå hjem! 🏠');
  assert.equal(w.natLine('sover', 'sleeping', '16:00'), '💤 Godnat …');
  assert.equal(w.natLine('gaber', 'working', '16:00'), '🥱 Det er fyraften – snart godnat');
  assert.equal(w.natLine('doeser', 'chill', '16:00'), null);
  assert.equal(w.natLine(null, 'working', '16:00'), null);
});

test('seasons: Halloween week (the ISO week of 31 Oct) and December; the lunch bell; the year in review', () => {
  // 2026: 31 Oct is a Saturday – Monday 26 Oct to Sunday 1 Nov ("uge 44")
  assert.deepEqual([at(2026, 10, 25), at(2026, 10, 26), at(2026, 10, 31, 23, 59), at(2026, 11, 1), at(2026, 11, 2)]
    .map(w.halloweenWeek), [false, true, true, true, false]);
  // 2027: 31 Oct is a Sunday – 25 to 31 Oct
  assert.deepEqual([at(2027, 10, 24), at(2027, 10, 25), at(2027, 10, 31), at(2027, 11, 1)].map(w.halloweenWeek),
    [false, true, true, false]);
  // 2028: 31 Oct is a Tuesday – 30 Oct to 5 Nov
  assert.deepEqual([at(2028, 10, 29), at(2028, 10, 30), at(2028, 11, 5), at(2028, 11, 6)].map(w.halloweenWeek),
    [false, true, true, false]);
  assert.deepEqual([at(2026, 10, 28), at(2026, 12, 1), at(2026, 12, 31), at(2027, 1, 1), at(2026, 6, 1)].map(w.seasonFor),
    ['halloween', 'jul', 'jul', null, null]);
  const lunch = (d, h, m) => w.lunchTime(at(2026, 10, d, h, m));
  assert.deepEqual([lunch(12, 10, 44), lunch(12, 10, 45), lunch(12, 13, 29), lunch(12, 13, 30), lunch(17, 12, 0)],
    [false, true, true, false, false]);                                       // 17 Oct 2026 is a Saturday
  assert.deepEqual([at(2026, 12, 5), at(2027, 1, 20), at(2026, 11, 30)].map(w.yearInReview), [2026, 2026, null]);
});

test('render bets: quick choices, the time field, which bet shows and what it says', () => {
  const now = at(2026, 10, 12, 14, 32, 30).getTime();
  const choices = w.guessChoices(now);
  assert.deepEqual(choices.map((c) => c.label), ['14:40', '14:45', '14:55', '15:15']);
  assert.deepEqual(choices.map((c) => c.min), [5, 10, 20, 40]);
  for (const c of choices) {
    assert.ok(c.at * 1000 > now + 60e3);
    assert.equal(new Date(c.at * 1000).getMinutes() % 5, 0);
  }
  assert.equal(w.guessChoices(at(2026, 10, 12, 14, 34, 50).getTime())[0].label, '14:40');   // never within a minute
  assert.equal(w.guessFromField('14:35', now), Math.round(at(2026, 10, 12, 14, 35).getTime() / 1000));
  assert.equal(w.guessFromField('09:00', now), Math.round(at(2026, 10, 13, 9, 0).getTime() / 1000));   // tomorrow
  assert.equal(w.guessFromField('', now), null);
  const nowS = now / 1000;
  const bet = { pc: 'GRAFIK-PC', vid: 3, navn: 'Bobby', maskine: 'GRAFIK-PC', fil: 'Portræt_v3.mp4', pct: 12,
    start: nowS - 60, lukker: nowS + 130, status: 'aaben', egen: false, mit_gaet: null, antal_gaet: 2, resultat: null };
  assert.equal(w.betKey(bet), `GRAFIK-PC:3:${nowS - 60}`);          // a restarted PC counts from 0 again
  assert.notEqual(w.betKey({ ...bet, start: nowS + 900 }), w.betKey(bet));
  assert.equal(w.betTitle(bet), '🎲 GRAFIK-PC renderer Portræt_v3.mp4 (12 %) – hvornår er den færdig?');
  assert.equal(w.betTitle({ ...bet, egen: true }), '🎲 Du renderer Portræt_v3.mp4 – kollegerne gætter med!');
  const guessed = { ...bet, mit_gaet: Math.round(at(2026, 10, 12, 14, 50).getTime() / 1000), antal_gaet: 3 };
  assert.equal(w.betStatus(guessed, nowS), '🎲 Dit gæt: 14:50 · 3 har gættet · lukker om 2 min');
  assert.equal(w.betStatus({ ...bet, lukker: nowS + 20 }, nowS), '2 har gættet · lukker om under 1 min');
  assert.equal(w.betStatus({ ...bet, status: 'lukket' }, nowS), '🎲 Gættene er lukket · 2 har gættet · venter på renderen …');
  assert.equal(w.betResult(bet), null);
  const end = Math.round(at(2026, 10, 12, 14, 51).getTime() / 1000);
  const won = { ...guessed, status: 'slut',
    resultat: { vinder_pc: 'KLIPPER-PC', vinder_navn: 'Bob', tid: guessed.mit_gaet, slut: end, mig: false } };
  assert.equal(w.betResult(won), '🏆 Bob (KLIPPER-PC) vandt! Gættede 14:50 – færdig 14:51');
  assert.equal(w.betResult({ ...won, resultat: { ...won.resultat, mig: true } }),
    'Du vandt render-væddemålet! 🔮 Gættede 14:50 – færdig 14:51');
  assert.equal(w.betResult({ ...won, resultat: { vinder_pc: null, vinder_navn: null, tid: null, slut: end, mig: false } }),
    '🎲 Ingen gættede – renderen blev færdig 14:51');
  assert.equal(w.betResult({ ...bet, status: 'aflyst' }), 'Renderen stoppede – væddemålet er aflyst');
  // open before closed before ended (the ended only for 2 min)
  const closed = { ...bet, vid: 4, status: 'lukket', start: nowS };
  const ended = { ...won, vid: 5, endedAt: nowS - 30 };
  assert.equal(w.pickBet([ended, closed, bet], nowS).vid, 3);
  assert.equal(w.pickBet([ended, closed], nowS).vid, 4);
  assert.equal(w.pickBet([ended], nowS).vid, 5);
  assert.equal(w.pickBet([{ ...ended, endedAt: nowS - 121 }], nowS), null);
  assert.equal(w.pickBet([bet, { ...bet, vid: 9, start: nowS }], nowS).vid, 9);    // the newest open one
  assert.equal(w.pickBet(null, nowS), null);
  // A colleague's bet whose end never came (its PC slept, the last packet was lost) goes 3 h after it closed …
  const forgotten = { ...closed, lukker: nowS - w.BET_STALE_S };
  assert.equal(w.betStale(forgotten, nowS), true);
  assert.equal(w.betStale({ ...forgotten, lukker: nowS - w.BET_STALE_S + 60 }, nowS), false);
  assert.equal(w.pickBet([forgotten], nowS), null);
  assert.equal(w.pickBet([forgotten, ended], nowS).vid, 5);
  // … our own never does, nor one that ended (that one goes 2 min after its end)
  assert.equal(w.betStale({ ...forgotten, egen: true }, nowS), false);
  assert.equal(w.betStale({ ...forgotten, status: 'slut' }, nowS), false);
});

test('lunch, post and trick or treat: what is said', () => {
  assert.equal(w.lunchLine({ pc: 'KLIPPER-PC', navn: 'Bobby', selv: false }), '🍽️ FROKOST! (Bobby ringede fra KLIPPER-PC)');
  assert.equal(w.lunchLine({ pc: 'STUDIO-PC', navn: 'Klippe', selv: true }), '🔔 Du ringede til frokost!');
  assert.equal(w.POST_NOTES.length, 6);
  assert.ok(w.POST_NOTES.every((n) => n.length <= w.POST_MAX));
  assert.equal(w.slikLine({ navn: 'Bobby', pc: 'KLIPPER-PC' }), '🎃 Bobby fra KLIPPER-PC vil have slik!');
  assert.deepEqual([w.slikCountdown(20), w.slikCountdown(0.2), w.slikCountdown(-3)],
    ['… ellers bliver der ballade om 20 s', '… ellers bliver der ballade om 1 s', '… ellers bliver der ballade om 0 s']);
  assert.equal(w.balladeLine('Bobby', { hat: 'festhat', toiletpapir: false }, 'Festhat'),
    'Ballade! 😈 Bobby stak af med din festhat!');
  assert.equal(w.balladeLine('Bobby', { hat: null, toiletpapir: true }), 'Ballade! 😈 Bobby pakkede mig ind i toiletpapir! 🧻');
  assert.equal(w.slikSvarLine({ type: 'godbid', pc: 'GRAFIK-PC', navn: 'Lise' }), '🍬 Lise (GRAFIK-PC) gav mig en Booster!');
  assert.equal(w.slikSvarLine({ type: 'ballade', pc: 'GRAFIK-PC', navn: 'Lise', hat: 'nissehue' }, 'Nissehue'),
    '😈 Hehe – jeg fik Lises nissehue!');
  assert.equal(w.slikSvarLine({ type: 'ballade', pc: 'GRAFIK-PC', navn: 'Lise', hat: null }),
    '😈 Hehe – jeg pakkede Lise ind i toiletpapir! 🧻');
  // Danish: Lars' (a name ending in s, x or z), Lises; dit gevir, din hat
  assert.deepEqual(['Lars', 'Max', 'Inez', 'Lise', 'Klippe'].map(w.possessive), ["Lars'", "Max'", "Inez'", 'Lises', 'Klippes']);
  assert.equal(w.slikSvarLine({ type: 'ballade', pc: 'GRAFIK-PC', navn: 'Lars', hat: 'heksehat' }, 'Heksehat'),
    "😈 Hehe – jeg fik Lars' heksehat!");
  assert.deepEqual([w.yourItem('gevir'), w.yourItem('festhat'), w.yourItem('nissehue')], ['dit', 'din', 'din']);
  assert.equal(w.balladeLine('Bobby', { hat: 'gevir', toiletpapir: false }, 'Rensdyrgevir'),
    'Ballade! 😈 Bobby stak af med dit rensdyrgevir!');
  // A second prank keeps the hat away under the toilet paper
  assert.equal(w.balladeTokens({ hat: 'festhat', toiletpapir: true }), 'hat toiletpapir');
  assert.equal(w.balladeTokens({ hat: 'festhat', toiletpapir: false }), 'hat');
  assert.equal(w.balladeTokens({ hat: null, toiletpapir: true }), 'toiletpapir');
  assert.equal(w.balladeTokens(null), '');
  // A trick-or-treater that waited in the queue has less time: the server takes the answer 60 s after it came
  assert.equal(w.slikSeconds(25, 0), 25);
  assert.equal(w.slikSeconds(25, 40e3), 15);
  assert.equal(w.slikSeconds(undefined, 10e3), 25);
  assert.ok(w.slikSeconds(25, 50e3) < w.SLIK_MIN_S);                          // it has gone on
});

test("visitors: a trick-or-treater in costume, a postman with an envelope, a colleague's year", () => {
  const guest = { pc: 'KLIPPER-PC', navn: 'Bobby', stage: 'junior', outfit: 'none', pynt: { hals: 'halstoerklaede' } };
  assert.deepEqual(w.guestLook({ ...guest, type: 'slik', kostume: 'vampyr' }),
    { stage: 'junior', outfit: 'none', pynt: { hals: 'halstoerklaede' }, kostume: 'vampyr', post: false });
  assert.equal(w.guestLook({ ...guest, type: 'slik', kostume: '"><b>' }).kostume, null);
  assert.equal(w.guestLook({ ...guest, type: 'trofae', kostume: 'vampyr' }).kostume, null);   // only trick-or-treaters dress up
  assert.equal(w.guestLook({ ...guest, type: 'post' }).post, true);
  assert.ok(w.SLOT_KEYS.includes('hals'));
  assert.equal(w.visitLine({ ...guest, type: 'slik' }), 'Slik eller ballade! 🎃');
  assert.equal(w.visitLine({ ...guest, type: 'post' }), 'Hej fra KLIPPER-PC! ✉️ Post til dig!');
  const year = { aar: 2026, timer: 311.6, side: 'color', projekter: 23, durum: 31, kort: 38, leveringer: 19, trofaeer: 21 };
  assert.equal(w.visitLine({ ...guest, type: 'aar', aar: year }),
    'Hej fra KLIPPER-PC! 📊 Mit Klippet 2026: 312 timer · mest i Color · 31 durum!');
  assert.equal(w.yearShareLine({ aar: 2026, timer: 1, side: 'x', durum: 0 }), '📊 Mit Klippet 2026: 1 time!');
  assert.equal(w.yearShareLine(null), '📊 Mit Klippet !');
  assert.equal(w.visitReply({ ...guest, type: 'post' }), 'Tak for posten, Bobby! 📬');
  assert.equal(w.visitReply({ ...guest, type: 'aar' }), 'Flot år, Bobby! 🎉');
});

test('the Christmas calendar: doors, gifts and what Klippe says', () => {
  assert.equal(w.giftIcon({ id: 'gevir', kind: 'ting', item: 'gevir' }), '🦌');
  assert.equal(w.giftIcon({ id: 'vits2', kind: 'vits' }), '😂');
  assert.equal(w.giftIcon(null), '🎁');
  const gift = { id: 'gevir', kind: 'ting', name: 'Rensdyrgevir', item: 'gevir' };
  assert.deepEqual(w.doorView({ nr: 5, aaben: true, kan: false, gave: gift }, 7), { klasse: 'is-aaben', ikon: '🦌', tekst: 'Rensdyrgevir' });
  assert.deepEqual(w.doorView({ nr: 7, aaben: false, kan: true, gave: null }, 7), { klasse: 'is-klar', ikon: '', tekst: 'Åbn' });
  assert.equal(w.doorView({ nr: 8, aaben: false, kan: false }, 7).tekst, 'I morgen');
  assert.equal(w.doorView({ nr: 12, aaben: false, kan: false }, 7).tekst, 'Om 5 dage');
  assert.equal(w.doorView({ nr: 12, aaben: false, kan: false }, null).tekst, '12. dec.');
  assert.equal(w.doorView({ nr: 12, aaben: false, kan: false }, null, true).klasse, 'is-klar');        // the demo: all open
  assert.equal(w.giftLine(gift, 5), '🎁 Låge 5: Rensdyrgevir!');
  assert.equal(w.giftLine({ id: 'aebleskiver', kind: 'mad', name: 'Æbleskiver', points: 25 }, 3), 'Æbleskiver! 😋');
  assert.equal(w.giftLine({ id: 'sne1', kind: 'sne', name: 'Første sne', tekst: 'Første sne! ❄️' }, 1), 'Første sne! ❄️');
  assert.equal(w.giftLine({ id: 'vits1', kind: 'vits', name: 'Julevits', tekst: 'Ho ho ho!' }, 9), 'Ho ho ho!');
});

test('easter eggs, the dirt and a memory', () => {
  assert.equal(w.eggPlan('fredag', at(2026, 10, 16)).line, 'Det ER fredag! 🎉');          // a Friday
  assert.equal(w.eggPlan('fredag', at(2026, 10, 14)).line, 'Fredag? Snart! 🎉');
  assert.deepEqual(w.eggPlan('kaffe'), { ord: 'kaffe', line: 'Værsgo! ☕', haand: 'kaffe', ms: 8000 });
  assert.equal(w.eggPlan('deadline').attr, 'panik');
  assert.equal(w.eggPlan('klippe').attr, 'vink');
  assert.equal(w.eggPlan('durum').attr, 'savler');
  assert.equal(w.eggPlan('hjaelp').attr, 'helt');
  assert.equal(w.eggPlan('sne').attr, 'snestorm');
  assert.equal(w.eggPlan('fyraften').nat, true);
  assert.equal(w.eggPlan('festkat').cat, true);
  assert.equal(w.eggPlan('booster').energi, 'booster');
  for (const bad of ['toString', '__proto__', 'nej', '', null]) assert.equal(w.eggPlan(bad), null);
  assert.equal(Object.keys(w.EGGS).length, 10);
  const rod = { aktiv: true, projekt: 'Rikke Lindholm', mappe: 'C:\\Rikke', antal: 7, niveau: 'beskidt', filer: [], mere: 0, bad: false };
  assert.equal(w.dirtLevel(rod), 'beskidt');
  assert.equal(w.dirtLevel(rod, false), null);
  assert.equal(w.dirtLevel({ ...rod, antal: 0, niveau: 'ren' }), null);
  assert.equal(w.dirtLevel({ ...rod, niveau: '<b>' }), null);
  assert.equal(w.dirtLine(rod), 'Uh, der ligger 7 løse filer i roden af Rikke Lindholm – jeg bliver beskidt 🫣 Se forslagene i Projektsøg');
  assert.equal(w.dirtLine({ ...rod, antal: 1, niveau: 'lidt' }),
    'Uh, der ligger 1 løs fil i roden af Rikke Lindholm – jeg bliver lidt beskidt 🫣 Se forslaget i Projektsøg');
  assert.equal(w.dirtLine({ ...rod, antal: 14, niveau: 'meget' }).includes('helt beskidt'), true);
  assert.equal(w.dirtLine(null), null);
  const memory = { name: 'Rikke Lindholm', minde: { aar_siden: 3, dato: '2023-10-12', antal: 112, slags: 'klip' } };
  assert.equal(w.memoryLine(memory), '📸 I dag for 3 år siden: 112 klip i Rikke Lindholm');
  assert.equal(w.memoryLine({ ...memory, minde: { ...memory.minde, aar_siden: 1, slags: 'billeder' } }),
    '📸 I dag for et år siden: 112 billeder i Rikke Lindholm');
  assert.equal(w.memoryLine({ name: 'X' }), null);
  assert.equal(w.memoryLine(null), null);
});

test('the render game: jumps, klippefejl, film frames, jump cuts and the score', () => {
  const r1 = w.seeded(7);
  const r2 = w.seeded(7);
  assert.deepEqual([r1(), r1(), r1()], [r2(), r2(), r2()]);              // the same seed, the same course
  const g = w.gameNew(5, 320);
  assert.equal(g.ground, true);
  assert.equal(w.gameJump(g), true);
  assert.equal(w.gameJump(g), true);                                     // one double jump …
  assert.equal(w.gameJump(g), false);                                    // … no more
  let steps = 0;
  while (!g.ground && steps < 200) {
    w.gameStep(g, 1 / 60);
    steps += 1;
  }
  assert.ok(g.ground && g.y === 0 && g.jumps === 0, 'it lands');
  assert.ok(g.dist > 0 && g.score === Math.floor(g.dist / 10));
  // a red "Media Offline" block right in front of Klippe: it crashes
  const c = w.gameNew(1, 320);
  c.next = 1e9;
  c.obstacles.push({ kind: 'offline', x: w.GAME.kx + 30, w: 20, h: 24, b: 0 });
  let events = [];
  for (let i = 0; i < 60 && !c.over; i += 1) events = events.concat(w.gameStep(c, 1 / 60));
  assert.ok(c.over && c.why === 'fejl' && events.includes('crash'));
  assert.deepEqual(w.gameStep(c, 1 / 60), []);                           // nothing moves after the end
  // … jumping clears it
  const j = w.gameNew(1, 320);
  j.next = 1e9;
  j.obstacles.push({ kind: 'sort', x: w.GAME.kx + 40, w: 14, h: 20, b: 0 });
  w.gameJump(j);
  for (let i = 0; i < 90; i += 1) w.gameStep(j, 1 / 60);
  assert.equal(j.over, false);
  // a film frame on the floor ahead is caught: +10
  const f = w.gameNew(1, 320);
  f.next = 1e9;
  f.frames.push({ x: w.GAME.kx + 20, b: 8, w: 10, h: 9, taken: false });
  events = [];
  for (let i = 0; i < 30; i += 1) events = events.concat(w.gameStep(f, 1 / 60));
  assert.ok(events.includes('frame') && f.taken === 1);
  assert.equal(f.score, Math.floor(f.dist / 10) + 10);
  // a jump cut (a hole in the timeline) under its feet: it falls in
  const h = w.gameNew(1, 320);
  h.next = 1e9;
  h.obstacles.push({ kind: 'hul', x: w.GAME.kx + 10, w: 40, h: 0, b: 0 });
  events = [];
  for (let i = 0; i < 120 && !h.over; i += 1) events = events.concat(w.gameStep(h, 1 / 60));
  assert.ok(events.includes('fall') && h.over && h.why === 'hul');
  // spawning: always to the right, the faster the further apart; every kind comes
  const s = w.gameNew(3, 320);
  const kinds = new Set();
  for (let i = 0; i < 4000; i += 1) {
    w.gameStep(s, 1 / 30);
    if (s.over) Object.assign(s, { over: false, falling: false, y: 0, vy: 0, ground: true, obstacles: [] });
    for (const o of s.obstacles) kinds.add(o.kind);
  }
  assert.deepEqual([...kinds].sort(), [...w.GAME_KINDS].sort());
  assert.ok(s.speed <= w.GAME.speedMax);
  assert.equal(w.gameHits({ x: 0, w: 10, b: 0, h: 10 }, { x: 9, w: 5, b: 9, h: 5 }), true);
  assert.equal(w.gameHits({ x: 0, w: 10, b: 0, h: 10 }, { x: 10, w: 5, b: 0, h: 5 }), false);
});

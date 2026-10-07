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

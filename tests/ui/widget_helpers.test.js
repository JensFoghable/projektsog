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

// Unit tests for the pure helpers in projektsog/web/aar.js – Klippet <år> (node --test; run by tests/test_ui_js.py).
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const a = require(path.join(__dirname, '..', '..', 'projektsog', 'web', 'aar.js'));

// The shape of aar.year_review (SPEC §23.11), as the mock backend serves it.
const REVIEW = {
  aar: 2026, fra: '2026-01-01', til: '2026-12-31', siden: '2026-09-30', dage: 52, timer: 311.5, projekter: 23,
  top: [{ navn: 'Klar Tand - Silkeborg', timer: 40.5 }, { navn: 'Rikke Lindholm - Testimonial', timer: 61.2 },
    { navn: 'Forår 2026', timer: 22 }],
  sider: [{ side: 'color', navn: 'Color', timer: 60.1 }, { side: 'edit', navn: 'Edit', timer: 180.2 },
    { side: 'musik', navn: 'Musik og lyd', timer: 12.5 }, { side: 'ai', navn: 'AI', timer: 0 }],
  bedste_dag: { dato: '2026-10-08', timer: 8.1 }, ugedag: 'tirsdag', tidligst: '07:42', fokus_90: 41, pauser: 17,
  kort: { antal: 38, tb: 4.2, filer: 5120, kamera: 'FX9' }, leveringer: 19, renders: 64,
  mad: { i_alt: 88, spist: { durum: 31, booster: 20, pommes: 0 }, yndlings: { id: 'durum', navn: 'Durum', antal: 31 } },
  klippe: { navn: 'Klippe', stage: 'junior', level: 25, trofaeer: 21, fund: 1 },
  kontor: { frokost_ringet: 9, post_sendt: 14, post_faaet: 0, slik_givet: 3, vaeddemaal_vundet: 0, spil_bedste: 1480, sne: 1 },
};

test('numbers read the Danish way: 1.234 timer, 7,5 timer, 4,2 TB', () => {
  assert.equal(a.num(1234.4), '1.234');
  assert.equal(a.hours(311.5), '312');
  assert.equal(a.hours(7.46), '7,5');
  assert.equal(a.hours(10), '10');
  assert.equal(a.hours(null), '0');
  assert.equal(a.hoursUnit(1), 'time');
  assert.equal(a.hoursUnit(1.04), 'time');
  assert.equal(a.hoursUnit(2), 'timer');
  assert.equal(a.tbText(4.21), '4,2 TB');
  assert.equal(a.tbText(0.38), '380 GB');
  assert.equal(a.plural(1, 'dag', 'dage'), '1 dag');
  assert.equal(a.plural(1200, 'fil', 'filer'), '1.200 filer');
});

test('days and the year: "Siden 30. september", "tirsdagen", "Klippes"', () => {
  assert.equal(a.longDay('2026-10-08'), '8. oktober');
  assert.equal(a.longDay('2026-02-30'), '');
  assert.equal(a.longDay(null), '');
  const now = new Date(2026, 11, 3);
  assert.equal(a.sinceText({ aar: 2026, siden: '2026-09-30' }, now), 'Siden 30. september');
  assert.equal(a.sinceText({ aar: 2026, siden: '2026-01-01' }, now), 'I år');
  assert.equal(a.sinceText({ aar: 2026, siden: null }, new Date(2027, 0, 10)), 'Hele 2026');
  assert.equal(a.weekdayDefinite('Tirsdag'), 'tirsdagen');
  assert.equal(a.weekdayDefinite(''), '');
  assert.equal(a.genitive('Klippe'), 'Klippes');
  assert.equal(a.genitive('Klaus'), "Klaus'");
  assert.deepEqual(a.pageInfo('fairlight'), { name: 'Fairlight', emoji: '🎚️' });
  assert.deepEqual(a.pageInfo('nyside', 'Ny side'), { name: 'Ny side', emoji: '🎞️' });
});

test('the year comes from ?aar= (this year when missing or out of range)', () => {
  const now = new Date(2027, 0, 15);
  assert.equal(a.yearFrom('?aar=2026', now), 2026);
  assert.equal(a.yearFrom('?aar=2027', now), 2027);
  assert.equal(a.yearFrom('', now), 2027);
  assert.equal(a.yearFrom('?aar=2025', now), 2027);   // before Klippe
  assert.equal(a.yearFrom('?aar=2031', now), 2027);
  assert.equal(a.yearFrom('?aar=abc', now), 2027);
});

test('only plain wardrobe names reach Klippe\'s frame', () => {
  assert.deepEqual(a.cleanLook({ hat: 'festhat', haand: 'awp', farve: 'midnat' }), { hat: 'festhat', haand: 'awp', farve: 'midnat' });
  assert.deepEqual(a.cleanLook({ hat: '"><script>', 'bad slot': 'x', mund: 7, aura: 'stjernestoev' }), { aura: 'stjernestoev' });
  assert.deepEqual(a.cleanLook(null), {});
});

test('numbers count up eased and land on the target', () => {
  assert.equal(a.countAt(100, 0), 0);
  assert.equal(a.countAt(100, 1), 100);
  assert.equal(a.countAt(100, 2), 100);
  assert.ok(a.countAt(100, 0.5) > 50);              // ease out: fast first
  assert.equal(a.formatCount(311.5, 'hours'), '312');
  assert.equal(a.formatCount(1480, 'int'), '1.480');
});

test('a full year: every slide in order, the winners first', () => {
  const slides = a.buildSlides(REVIEW);
  assert.deepEqual(slides.map((s) => s.key),
    ['intro', 'timer', 'top', 'top5', 'sider', 'musik', 'rytme', 'kort', 'leveret', 'mad', 'trofaeer', 'kontor', 'outro']);
  const by = Object.fromEntries(slides.map((s) => [s.key, s]));
  assert.equal(by.intro.kicker, 'Klippet 2026');
  assert.match(by.intro.sub, /^Siden 30\. september – fortalt af Klippe$/);
  assert.ok(by.intro.klippe && by.outro.klippe && by.outro.actions);
  assert.equal(by.outro.sub, 'Klippe glæder sig til 2027');
  assert.deepEqual(by.timer.big, { value: 311.5, format: 'hours', unit: 'timer' });
  assert.equal(by.timer.foot, 'fordelt på 52 dage · 23 projekter');
  assert.equal(by.top.title, 'Rikke Lindholm - Testimonial');       // sorted by hours, not by order
  assert.equal(by.top.foot, '61 timer – 20 % af året');
  assert.deepEqual(by.top5.bars.map((b) => [b.rank, b.label, b.text]),
    [[1, 'Rikke Lindholm - Testimonial', '61 t'], [2, 'Klar Tand - Silkeborg', '41 t'], [3, 'Forår 2026', '22 t']]);
  assert.equal(by.top5.kicker, 'Top 3');
  assert.equal(by.top5.bars[0].share, 1);
  assert.equal(by.sider.lead, 'Mest af alt i Edit ✂️');
  assert.deepEqual(by.sider.bars.map((b) => b.label), ['✂️ Edit', '🎨 Color']);   // music and AI have their own slide
  assert.equal(by.sider.deco, '✂️');
  assert.deepEqual(by.musik.tiles.map((t) => t.emoji), ['🎧']);                     // no AI hours: no AI tile
  assert.equal(by.rytme.title, '8. oktober');
  assert.deepEqual(by.rytme.facts, ['Du klipper helst om tirsdagen', 'Tidligste start: kl. 07:42', '41 × 90 minutter i flow',
    '17 gode pauser']);
  assert.equal(by.kort.foot, '4,2 TB kontrolleret · 5.120 filer · mest fra FX9');
  assert.equal(by.leveret.big.unit, 'gange 🎉');
  assert.equal(by.leveret.foot, '– og 64 renders blev færdige undervejs');
  assert.equal(by.mad.kicker, 'Klippes mave');
  assert.equal(by.mad.foot, 'Yndlingsretten: Durum 🌯 × 31');
  assert.deepEqual(by.mad.menu, [{ emoji: '🌯', count: 31 }, { emoji: '⚡', count: 20 }]);
  assert.equal(by.mad.deco, '🌯');
  assert.equal(by.trofaeer.big.unit, 'nye trofæer 🏆');
  assert.equal(by.trofaeer.foot, '1 sjældent fund 🎁 · Klippe er nu Junior · Lv 25');
  assert.deepEqual(by.kontor.tiles.map((t) => [t.emoji, t.value, t.label]), [
    ['🔔', 9, 'gange ringede du til frokost'], ['✉️', 14, 'breve sendt med Klippe-post'],
    ['🍬', 3, 'Boostere givet til Halloween'], ['🎮', 1480, 'point – din rekord i render-spillet'],
    ['❄️', 1, 'snevejr på skærm 2']]);
  for (const slide of slides) assert.ok(slide.theme && slide.kicker, slide.key);
  assert.equal(by.intro.deco, null);
  assert.equal(by.timer.deco, '⏱️');
});

test('a quiet year leaves out what did not happen', () => {
  const slides = a.buildSlides({ aar: 2026, siden: '2026-09-30', timer: 2.5, dage: 1, projekter: 1,
    top: [{ navn: 'Eneste', timer: 2.5 }], sider: [{ side: 'edit', navn: 'Edit', timer: 2.5 }],
    kort: { antal: 0 }, leveringer: 0, renders: 2, mad: { i_alt: 0 }, klippe: { navn: 'Bobby', stage: 'egg', trofaeer: 0, fund: 0 },
    kontor: {} });
  assert.deepEqual(slides.map((s) => s.key), ['intro', 'timer', 'top', 'sider', 'leveret', 'outro']);
  const by = Object.fromEntries(slides.map((s) => [s.key, s]));
  assert.deepEqual(by.timer.big, { value: 2.5, format: 'hours', unit: 'timer' });
  assert.equal(by.timer.foot, 'fordelt på 1 dag · 1 projekt');
  assert.equal(by.leveret.kicker, 'Renders');
  assert.equal(by.intro.sub, 'Siden 30. september – fortalt af Bobby');
  assert.equal(by.outro.sub, 'Bobby glæder sig til 2027');
});

test('no time at all: a friendly empty story', () => {
  const slides = a.buildSlides({ aar: 2026, timer: 0, klippe: { navn: 'Klippe' } });
  assert.deepEqual(slides.map((s) => s.key), ['intro', 'tom', 'outro']);
  assert.equal(slides[1].title, 'Ingen tid endnu');
  assert.deepEqual(a.buildSlides(null).map((s) => s.key), ['intro', 'tom', 'outro']);
});

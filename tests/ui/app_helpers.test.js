// Unit tests for the pure helpers in projektsog/web/app.js (node --test; run by tests/test_ui_js.py).
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const h = require(path.join(__dirname, '..', '..', 'projektsog', 'web', 'app.js'));

const NOW = new Date(2026, 8, 30, 14, 0, 0).getTime(); // 30 Sep 2026 14:00 local time
const at = (...parts) => new Date(...parts).getTime() / 1000;
const ago = (ms) => (NOW - ms) / 1000;

test('numbers and sizes use Danish formatting and Explorer units', () => {
  assert.equal(h.formatInt(120000), '120.000');
  assert.equal(h.plural(1, 'fil', 'filer'), '1 fil');
  assert.equal(h.plural(304000, 'fil', 'filer'), '304.000 filer');
  assert.equal(h.formatBytes(null), '–');
  assert.equal(h.formatBytes(512), '512 B');
  assert.equal(h.formatBytes(1536), '1,5 kB');
  assert.equal(h.formatBytes(12.44 * 1024 ** 3), '12,4 GB');
  assert.equal(h.formatBytes(123 * 1024 ** 3), '123 GB');
  assert.equal(h.formatBytes(16 * 1024 ** 4), '16 TB');
});

test('relative times read like "ændret for 2 dage siden"', () => {
  assert.equal(h.relativeTime(ago(10e3), NOW), 'lige nu');
  assert.equal(h.relativeTime(ago(5 * 60e3), NOW), 'for 5 minutter siden');
  assert.equal(h.relativeTime(ago(3 * 3600e3), NOW), 'for 3 timer siden');
  assert.equal(h.relativeTime(at(2026, 8, 29, 9, 0), NOW), 'i går');
  assert.equal(h.relativeTime(at(2026, 8, 28, 9, 0), NOW), 'for 2 dage siden');
  assert.equal(h.relativeTime(at(2026, 8, 14, 9, 0), NOW), 'for 2 uger siden');
  assert.equal(h.relativeTime(at(2026, 8, 1, 9, 0), NOW), '1. sep.');
  assert.equal(h.relativeTime(at(2025, 8, 12, 9, 0), NOW), '12. sep. 2025');
  assert.equal(h.relativeTime(ago(-60e3), NOW), 'lige nu'); // clock skew
  assert.equal(h.modifiedText(ago(2 * 86400e3), NOW), 'ændret for 2 dage siden');
  assert.equal(h.modifiedText(null, NOW), '–');
});

test('highlight ranges are code points, merged and clamped', () => {
  assert.deepEqual(h.highlightParts('Rikke Lindholm', [[6, 14]]),
    [{ text: 'Rikke ', hl: false }, { text: 'Lindholm', hl: true }]);
  assert.deepEqual(h.highlightParts('🎬 Bøgely Jul', [[2, 8]]),
    [{ text: '🎬 ', hl: false }, { text: 'Bøgely', hl: true }, { text: ' Jul', hl: false }]);
  assert.deepEqual(h.highlightParts('abcdef', [[4, 9], [0, 2], [1, 3]]),
    [{ text: 'abc', hl: true }, { text: 'd', hl: false }, { text: 'ef', hl: true }]);
  assert.deepEqual(h.highlightParts('Rikke Lindholm v2.mp4', [[0, 5], [6, 14]]),
    [{ text: 'Rikke Lindholm', hl: true }, { text: ' v2.mp4', hl: false }]);
  assert.deepEqual(h.highlightParts('Klar Tand - Silkeborg', [[0, 4], [5, 9], [12, 21]]),
    [{ text: 'Klar Tand', hl: true }, { text: ' - ', hl: false }, { text: 'Silkeborg', hl: true }]);
  assert.deepEqual(h.highlightParts('Pixelbro', []), [{ text: 'Pixelbro', hl: false }]);
  assert.deepEqual(h.highlightParts('Pixelbro', [[-3, 2], [5, 5]]), [{ text: 'Pi', hl: true }, { text: 'xelbro', hl: false }]);
});

test('item location: enclosing project and the folders in between', () => {
  const clip = { kind: 'file', rel_path: 'Rikke Lindholm\\Klip\\FX9\\FX9_7912.MXF',
    project: { name: 'Rikke Lindholm', rel_path: 'Rikke Lindholm' } };
  assert.deepEqual(h.itemLocation(clip), { project: clip.project, crumbs: ['Klip', 'FX9'] });
  const grouped = { kind: 'project', rel_path: 'Klar Tand 2026\\Klar Tand - Silkeborg',
    project: { name: 'Klar Tand - Silkeborg', rel_path: 'Klar Tand 2026\\Klar Tand - Silkeborg' } };
  assert.deepEqual(h.itemLocation(grouped), { project: null, crumbs: ['Klar Tand 2026'] });
  assert.deepEqual(h.itemLocation({ kind: 'toplevel', rel_path: 'Sound Effects', project: null }),
    { project: null, crumbs: [] });
});

test('location badges: shares, the system disk and other disks', () => {
  assert.deepEqual(h.sourceBadge({ kind: 'share', host: 'GRAFIK-PC', name: 'Kunder 2026 (Grafik)' }),
    { icon: 'net', text: 'GRAFIK-PC · Kunder 2026 (Grafik)' });
  assert.deepEqual(h.sourceBadge({ kind: 'local', host: 'STUDIO-PC', name: 'Kunder 2026 (STUDIO)', drive: 'C:', disk_name: 'Windows' }),
    { icon: 'pc', text: 'STUDIO-PC · Kunder 2026 (STUDIO)' });
  assert.deepEqual(h.sourceBadge({ kind: 'local', host: 'STUDIO-PC', name: '2024 Disk Sølv', drive: 'H:', disk_name: '2024 Disk Sølv' }),
    { icon: 'disk', text: 'Disk: 2024 Disk Sølv (H:)' });
  assert.deepEqual(h.sourceBadge({ kind: 'local', host: 'STUDIO-PC', name: '(Z) Kunder 2026 (STUDIO)', drive: 'z:', disk_name: 'Lokal disk 2' }),
    { icon: 'disk', text: 'Disk: Lokal disk 2 (Z:) · (Z) Kunder 2026 (STUDIO)' });
  assert.deepEqual(h.sourceBadge({}), { icon: 'folder', text: 'Ukendt placering' });
  assert.deepEqual(h.sourceBadge({ kind: 'share', name: 'Rejsefilm' }), { icon: 'net', text: 'Rejsefilm' });
});

test('location badges follow is_system (§15.4), not the drive letter', () => {
  const local = { kind: 'local', host: 'KLIPPER-PC', name: 'Kunder 2026', disk_name: 'Data' };
  // Windows on D:, a data disk at C: – only is_system tells them apart.
  assert.deepEqual(h.sourceBadge({ ...local, drive: 'D:', is_system: true }), { icon: 'pc', text: 'KLIPPER-PC · Kunder 2026' });
  assert.deepEqual(h.sourceBadge({ ...local, drive: 'C:', is_system: false }), { icon: 'disk', text: 'Disk: Data (C:) · Kunder 2026' });
  // An unlabeled system volume is called "Systemdisk" – still the PC badge.
  assert.deepEqual(h.sourceBadge({ ...local, drive: 'C:', disk_name: 'Systemdisk', is_system: true }),
    { icon: 'pc', text: 'KLIPPER-PC · Kunder 2026' });
  // A disk without a known drive letter keeps its disk badge.
  assert.deepEqual(h.sourceBadge({ ...local, drive: null, is_system: false }), { icon: 'disk', text: 'Disk: Data · Kunder 2026' });
  // Without is_system (older backend): C: is the system volume, other letters are disks.
  assert.deepEqual(h.sourceBadge({ ...local, drive: 'C:' }), { icon: 'pc', text: 'KLIPPER-PC · Kunder 2026' });
  assert.deepEqual(h.sourceBadge({ ...local, drive: null }), { icon: 'pc', text: 'KLIPPER-PC · Kunder 2026' });
});

test('row online state', () => {
  assert.equal(h.itemOnline({ source: { online: false } }), false);
  assert.equal(h.itemOnline({ source: { online: true } }), true);
  assert.equal(h.itemOnline({ source: null }), true);
  assert.equal(h.itemOnline(undefined), true);
});

test('removing a computer asks how many shared folders it forgets (§15.8)', () => {
  const sources = [
    { kind: 'share', host: 'GRAFIK-PC', manual: false }, { kind: 'share', host: 'grafik-pc', manual: false },
    { kind: 'share', host: 'GRAFIK-PC', manual: true }, { kind: 'local', host: 'GRAFIK-PC' },
    { kind: 'share', host: 'KLIPPER-PC', manual: false },
  ];
  assert.equal(h.hostShareCount(sources, 'GRAFIK-PC'), 2);
  assert.equal(h.removeHostQuestion('GRAFIK-PC', 2), 'Fjern GRAFIK-PC og glem dens 2 delte mapper?');
  assert.equal(h.removeHostQuestion('KLIPPER-PC', h.hostShareCount(sources, 'KLIPPER-PC')),
    'Fjern KLIPPER-PC og glem dens 1 delte mappe?');
  assert.equal(h.removeHostQuestion('NYPC', h.hostShareCount(sources, 'NYPC')), 'Fjern NYPC fra listen?');
});

test('removing a computer reports what the server forgot (§15.12 forgotten)', () => {
  assert.equal(h.removedHostText('GRAFIK-PC', 2), 'GRAFIK-PC er fjernet, og dens 2 delte mapper er glemt');
  assert.equal(h.removedHostText('NAS', 1), 'NAS er fjernet, og dens 1 delte mappe er glemt');
  assert.equal(h.removedHostText('NAS', 0), 'NAS er fjernet');
  // An answer without the count claims nothing about shared folders.
  assert.equal(h.removedHostText('NAS', undefined), 'NAS er fjernet');
  assert.equal(h.removedHostText('NAS', null), 'NAS er fjernet');
});

test('new-disk card reports what [Medtag] really did', () => {
  const on = { included: true, online: true };
  const off = { included: true, online: false };
  assert.equal(h.includeOutcome([on], 1), 'Medtaget i søgningen – disken bliver scannet nu.');
  assert.equal(h.includeOutcome([on], 3), '1 af 3 mapper er medtaget i søgningen – disken bliver scannet nu.');
  assert.equal(h.includeOutcome([off, off], 2), 'Medtaget i søgningen – disken scannes, når den er tilsluttet igen.');
});

test('offline hints match the server wording', () => {
  const disk = { kind: 'local', name: '2024 Disk Sølv', disk_name: '2024 Disk Sølv', last_seen: at(2026, 8, 12, 10, 0) };
  assert.equal(h.offlineHint(disk), 'Tilslut disken ‘2024 Disk Sølv’');
  assert.equal(h.offlineHint({ kind: 'share', host: 'MEDIESERVER', name: '2025Arkiv' }),
    'Computeren MEDIESERVER svarer ikke – er den tændt?');
  assert.equal(h.offlineSince(disk, NOW), 'Offline – sidst set 12. sep.');
  assert.equal(h.offlineSince({ kind: 'local' }, NOW), 'Offline');
  assert.equal(h.offlineHint({}), 'Placeringen er ikke tilgængelig');
  assert.equal(h.offlineHint(null), 'Placeringen er ikke tilgængelig');
});

test('offline hints: a folder gone from a disk or computer that is there (§15.12 volume_present)', () => {
  const system = { kind: 'local', name: 'Kunder 2026 (STUDIO)', disk_name: 'Systemdisk', is_system: true,
    online: false, volume_present: true };
  assert.equal(h.offlineHint(system), 'Mappen findes ikke længere'); // never "Tilslut disken ‘Systemdisk’"
  const disk = { kind: 'local', name: 'Forår 2026 RØD', disk_name: 'Forår 2026 RØD', drive: 'D:', is_system: false,
    online: false, volume_present: true };
  assert.equal(h.offlineHint(disk), 'Mappen findes ikke længere');
  assert.equal(h.offlineHint({ ...disk, volume_present: false }), 'Tilslut disken ‘Forår 2026 RØD’');
  const share = { kind: 'share', host: 'GRAFIK-PC', name: 'Forår 2026 (HDD)', online: false };
  assert.equal(h.offlineHint({ ...share, volume_present: true }), 'Mappen findes ikke længere');
  assert.equal(h.offlineHint({ ...share, volume_present: false }), 'Computeren GRAFIK-PC svarer ikke – er den tændt?');
  // An older backend without the field: the disk/host hint as before.
  assert.equal(h.offlineHint({ ...system, volume_present: undefined }), 'Tilslut disken ‘Systemdisk’');
});

test('status pill: online counts, scanning, first indexing, problems', () => {
  const base = { sources_included_online: 11, sources_offline: 1, sources_ready: 11, initial_scan_done: true, scanning: [] };
  const deep = { source_id: 9, name: '2025Arkiv', kind: 'deep', entries: 120000, started: (NOW - 60e3) / 1000 };
  assert.deepEqual(h.statusPill(null), { tone: 'muted', text: 'Forbinder …' });
  assert.equal(h.statusPill(base, { offline: true }).text, 'Ingen forbindelse – prøver igen …');
  assert.deepEqual(h.statusPill(base, { now: NOW }), { tone: 'ok', text: '11 placeringer online · 1 offline' });
  assert.equal(h.statusPill({ ...base, sources_included_online: 1, sources_offline: 0 }, { now: NOW }).text, '1 placering online');
  assert.deepEqual(h.statusPill({ ...base, scanning: [deep] }, { now: NOW }),
    { tone: 'scan', text: 'Scanner 2025Arkiv – 120.000 filer' });
  const fresh = { ...deep, started: (NOW - 500) / 1000 };
  assert.equal(h.statusPill({ ...base, scanning: [fresh] }, { now: NOW }).text, '11 placeringer online · 1 offline');
  const shallow = { ...deep, kind: 'shallow' };
  assert.equal(h.statusPill({ ...base, scanning: [shallow] }, { now: NOW }).tone, 'ok');
  assert.equal(h.statusPill({ ...base, sources_ready: 10, initial_scan_done: false, scanning: [deep] }, { now: NOW }).text,
    '10 af 11 placeringer klar · Scanner 2025Arkiv – 120.000 filer');
  const second = { ...deep, source_id: 3, name: 'Forår 2026 RØD', started: deep.started + 5 };
  assert.equal(h.statusPill({ ...base, scanning: [second, deep] }, { now: NOW }).text,
    'Scanner 2025Arkiv – 120.000 filer (+1)');
  assert.equal(h.statusPill({ ...base, worker: { running: false } }).text, 'Indeksering stoppet – se loggen');
});

test('zero-result reasons come with one-click fixes', () => {
  const status = { initial_scan_done: true, scanning: [{ source_id: 9, name: '2025Arkiv', kind: 'deep', entries: 120000 }] };
  const reasons = h.zeroResultReasons({ hidden: { kind: 12, offline: 3, source: 0 } }, status, { kind: 'file', excludedOnline: 2 });
  assert.deepEqual(reasons.map((r) => [r.text, r.action]), [
    ['12 resultater skjules af filteret ‘Filer’', 'all-kinds'],
    ['3 på offline placeringer', 'show-offline'],
    ['2025Arkiv indekseres stadig (120.000 filer indtil nu)', undefined],
    ['2 tilsluttede placeringer er ikke medtaget', 'show-sources'],
  ]);
  const single = h.zeroResultReasons({ hidden: { kind: 1, offline: 0, source: 4 } }, null, { kind: 'project', excludedOnline: 1 });
  assert.deepEqual(single.map((r) => r.text), ['1 resultat skjules af filteret ‘Projekter’',
    '4 resultater på andre placeringer', '1 tilsluttet placering er ikke medtaget']);
  assert.deepEqual(h.zeroResultReasons({}, { initial_scan_done: true, scanning: [] }, {}), []);
});

test('matches hidden by several filters at once are offered "Ryd alle filtre" (§15.7 any)', () => {
  const idle = { initial_scan_done: true, scanning: [] };
  const both = h.zeroResultReasons({ hidden: { kind: 0, offline: 3, source: 0, any: 4 } }, idle, { kind: 'file' });
  assert.deepEqual(both.map((r) => [r.text, r.action, r.label]), [
    ['3 på offline placeringer', 'show-offline', 'Vis'],
    ['4 resultater skjules af filtrene', 'clear-filters', 'Ryd alle filtre'],
  ]);
  // Only hidden by two filters: without the reset reason nothing would help.
  assert.deepEqual(h.zeroResultReasons({ hidden: { kind: 0, offline: 0, source: 0, any: 1 } }, idle, {}).map((r) => r.text),
    ['1 resultat skjules af filtrene']);
  // Each match fails a single filter (or an older backend without `any`): no reset reason.
  assert.equal(h.zeroResultReasons({ hidden: { kind: 2, offline: 0, source: 1, any: 3 } }, idle, {}).length, 2);
  assert.equal(h.zeroResultReasons({ hidden: { kind: 2, offline: 0, source: 0 } }, idle, {}).length, 1);
});

test('Resolve offline warnings: per disk, per host, and from the summary only', () => {
  const rs = { connected: true, offline_clips: 18, offline_disks: ['2024 Disk Sølv', 'MEDIESERVER'], folders: [
    { online: true, count: 160, source: { kind: 'local', disk_name: 'Windows' } },
    { online: false, count: 6, source: { kind: 'local', disk_name: '2024 Disk Sølv' } },
    { online: false, count: 12, source: { kind: 'share', host: 'MEDIESERVER' } },
  ] };
  assert.deepEqual(h.resolveOfflineWarnings(rs), [
    '12 klip ligger på MEDIESERVER, som ikke svarer',
    '6 klip ligger på disken ‘2024 Disk Sølv’, som ikke er tilsluttet',
  ]);
  assert.deepEqual(h.resolveOfflineWarnings({ connected: true, offline_clips: 5, offline_disks: ['ARKIV'], folders: [] }),
    ['5 klip ligger på disken ‘ARKIV’, som ikke er tilsluttet']);
  assert.deepEqual(h.resolveOfflineWarnings({ connected: true, offline_clips: 7, offline_disks: ['A', 'B'], folders: [] }),
    ['7 klip ligger på diskene ‘A’ og ‘B’, som ikke er tilsluttet']);
  assert.deepEqual(h.resolveOfflineWarnings({ connected: false, offline_clips: 3 }), []);
});

test('Resolve offline warnings name a gone folder, never its connected disk (§15.12)', () => {
  const studio = { id: 7, kind: 'local', name: 'Kunder 2026 (STUDIO)', disk_name: 'Systemdisk', is_system: true,
    online: false, volume_present: true };
  const red = { id: 2, kind: 'local', name: 'Forår 2026 RØD', disk_name: 'Forår 2026 RØD', is_system: false,
    online: false, volume_present: true };
  const solv = { id: 3, kind: 'local', name: '2024 Disk Sølv', disk_name: '2024 Disk Sølv', online: false,
    volume_present: false };
  const grafik = { id: 10, kind: 'share', host: 'GRAFIK-PC', name: 'Forår 2026 (HDD)', online: false, volume_present: true };
  const rs = { connected: true, offline_clips: 34, offline_disks: ['Systemdisk', 'Forår 2026 RØD', '2024 Disk Sølv'],
    folders: [
      { online: false, count: 12, source: studio }, { online: false, count: 3, source: studio }, // two projects in it
      { online: false, count: 9, source: red }, { online: false, count: 6, source: solv },
      { online: false, count: 4, source: grafik },
    ] };
  assert.deepEqual(h.resolveOfflineWarnings(rs), [
    '15 klip ligger i mappen ‘Kunder 2026 (STUDIO)’, som ikke findes længere',
    '9 klip ligger i mappen ‘Forår 2026 RØD’, som ikke findes længere',
    '6 klip ligger på disken ‘2024 Disk Sølv’, som ikke er tilsluttet',
    '4 klip ligger i mappen ‘Forår 2026 (HDD)’, som ikke findes længere',
  ]);
  // Clips beyond the listed folders: a disk that is there is never "ikke tilsluttet".
  assert.equal(h.resolveOfflineWarnings({ ...rs, offline_clips: 39 }).at(-1),
    '5 klip ligger på placeringer, der ikke er tilgængelige');
  assert.equal(h.resolveOfflineWarnings({ ...rs, offline_clips: 39, offline_disks: ['Systemdisk', 'ARKIV'] }).at(-1),
    '5 klip ligger på disken ‘ARKIV’, som ikke er tilsluttet');
});

test('Resolve passthrough list and the one-time question', () => {
  assert.deepEqual(h.withResolvePassthrough(['Premiere.exe'], true), ['Premiere.exe', 'Resolve.exe', 'Fusion.exe']);
  assert.deepEqual(h.withResolvePassthrough(['resolve.exe', 'Fusion.exe', 'x.exe'], false), ['x.exe']);
  assert.deepEqual(h.withResolvePassthrough(['Resolve.exe', 'Fusion.exe'], true), ['Resolve.exe', 'Fusion.exe']);
  assert.equal(h.hasResolvePassthrough(['RESOLVE.EXE']), true);
  assert.equal(h.hasResolvePassthrough([]), false);
  const settings = { resolve_hotkey_asked: false, hotkey: 'shift+space' };
  assert.equal(h.shouldAskResolveHotkey({ from_app: 'Resolve.exe' }, settings), true);
  assert.equal(h.shouldAskResolveHotkey({ from_app: 'fusion.exe' }, settings), true);
  assert.equal(h.shouldAskResolveHotkey({ from_app: 'explorer.exe' }, settings), false);
  assert.equal(h.shouldAskResolveHotkey({ from_app: null }, settings), false);
  assert.equal(h.shouldAskResolveHotkey({ from_app: 'Resolve.exe' }, { ...settings, resolve_hotkey_asked: true }), false);
  assert.equal(h.shouldAskResolveHotkey({ from_app: 'Resolve.exe' }, settings, 'ctrl+space'), false);
  assert.equal(h.shouldAskResolveHotkey({ from_app: 'Resolve.exe' }, null), false);
});

test('query lifetime helpers and launch parameters', () => {
  assert.equal(h.typedSince('lindholm', 'lindholm'), null);
  assert.equal(h.typedSince('lindholm', 'lindholmtø'), 'tø');
  assert.equal(h.typedSince('lindholm', 'x'), 'x');
  assert.equal(h.typedSince('', 'ab'), 'ab');
  assert.deepEqual(h.parseLaunchParams('?q=lindholm&panel=settings&tab=Generelt'),
    { query: 'lindholm', panel: 'settings', tab: 'generelt' });
  assert.deepEqual(h.parseLaunchParams('?tab=nope'), { query: '', panel: '', tab: null });
  assert.deepEqual(h.parseLaunchParams(''), { query: '', panel: '', tab: null });
});

test('open actions, visuals and paths', () => {
  assert.equal(h.openAction({ kind: 'project' }, false), 'folder');
  assert.equal(h.openAction({ kind: 'dir' }, true), 'reveal');
  assert.equal(h.openAction({ kind: 'file' }, false), 'reveal');
  assert.equal(h.openAction({ kind: 'file' }, true), 'file');
  const visual = (item) => h.itemVisual(item).tone;
  assert.equal(visual({ kind: 'project' }), 'project');
  assert.equal(visual({ kind: 'group' }), 'group');
  assert.equal(visual({ kind: 'toplevel' }), 'folder');
  assert.equal(visual({ kind: 'file', ext: 'MXF' }), 'video');
  assert.equal(visual({ kind: 'file', ext: 'wav' }), 'audio');
  assert.equal(visual({ kind: 'file', ext: 'exr', is_seq: true }), 'sequence');
  assert.equal(visual({ kind: 'file', ext: 'png' }), 'image');
  assert.equal(visual({ kind: 'file', ext: 'drp' }), 'resolve');
  assert.equal(visual({ kind: 'file', ext: 'prproj' }), 'projfile');
  assert.equal(visual({ kind: 'file', ext: 'pdf' }), 'doc');
  assert.deepEqual(h.itemVisual({ kind: 'file', ext: 'xyz' }), { icon: 'file', tone: 'other' });
  assert.equal(h.joinWinPath('C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm', 'Klip'), 'C:\\Kunder 2026 (STUDIO)\\Rikke Lindholm\\Klip');
  assert.equal(h.joinWinPath('F:\\', 'Klip'), 'F:\\Klip');
  assert.equal(h.leafName('\\\\GRAFIK-PC\\Forår 2026 (HDD)\\'), 'Forår 2026 (HDD)');
});

test('footer hint follows the hotkey status', () => {
  const hotkey = { spec: 'shift+space', label: 'Shift+Mellemrum', enabled: true, active: true, mode: 'll' };
  assert.equal(h.footerHint(hotkey), 'Shift+Mellemrum åbner Projektsøg overalt · Esc skjuler');
  assert.equal(h.footerHint(hotkey, { hasQuery: true }), 'Shift+Mellemrum åbner Projektsøg overalt · Esc rydder søgningen');
  assert.equal(h.footerHint(hotkey, { settingsOpen: true }), 'Shift+Mellemrum åbner Projektsøg overalt · Esc lukker indstillinger');
  assert.equal(h.footerHint({ ...hotkey, enabled: false }), 'Genvejstasten er slået fra · Esc skjuler');
  assert.equal(h.footerHint({ ...hotkey, active: false }), 'Shift+Mellemrum virker ikke lige nu – se Indstillinger · Esc skjuler');
  assert.equal(h.footerHint(null), 'Esc skjuler');
});

test('settings: sources grouped by computer, scan state and mode reason', () => {
  const groups = h.groupSources([
    { id: 1, host: 'GRAFIK-PC', display_name: 'Kunder 2026 (Grafik)', included: true },
    { id: 2, host: 'GRAFIK-PC', display_name: 'Økonomi', included: false },
    { id: 3, host: 'GRAFIK-PC', display_name: 'Forår 2026 (HDD)', included: true },
    { id: 4, host: 'STUDIO-PC', display_name: 'Kunder 2026 (STUDIO)', included: true },
  ], [{ name: 'GRAFIK-PC', online: true }, { name: 'KLIPPER-PC', online: false }, { name: 'STUDIO-PC', self: true }]);
  assert.deepEqual(groups.map((g) => [g.host, g.sources.map((s) => s.id)]),
    [['STUDIO-PC', [4]], ['GRAFIK-PC', [3, 1, 2]], ['KLIPPER-PC', []]]);

  const source = { online: true, included: true, last_scan_end: ago(5 * 60e3), last_scan_ok: true };
  assert.deepEqual(h.sourceScanState(source, null, NOW), { main: 'Scannet for 5 minutter siden', sub: '' });
  assert.equal(h.sourceScanState(source, { units_done: 12, units_total: 53, entries: 120000 }, NOW).main, 'Scanner … 22 %');
  assert.equal(h.sourceScanState({ ...source, queued: true }, null, NOW).main, 'I kø til scanning');
  assert.equal(h.sourceScanState({ ...source, included: false }, null, NOW).main, 'Ikke medtaget');
  assert.equal(h.sourceScanState({ ...source, last_scan_end: null }, null, NOW).main, 'Ikke scannet endnu');
  assert.deepEqual(h.sourceScanState({ ...source, last_scan_ok: 0, last_error: 'Ingen adgang' }, null, NOW),
    { main: 'Fejl ved sidste scanning', sub: 'Ingen adgang', tone: 'error' });
  assert.equal(h.sourceScanState({ ...source, online: false, last_seen: at(2026, 8, 12, 9, 0) }, null, NOW).main,
    'Offline – sidst set 12. sep.');
  // §15.12: its disk is there, the folder is not – next to its [Glem] button.
  const gone = { ...source, online: false, volume_present: true, last_seen: at(2026, 8, 12, 9, 0) };
  assert.deepEqual(h.sourceScanState(gone, null, NOW), { main: 'Mappen findes ikke længere', sub: 'Sidst set 12. sep.', tone: 'off' });
  assert.equal(h.sourceScanState({ ...gone, volume_present: false }, null, NOW).main, 'Offline – sidst set 12. sep.');
  assert.equal(h.sourceScanState({ ...gone, online: true }, null, NOW).main, 'Scannet for 5 minutter siden');
  assert.equal(h.modeReason({ mode: 'auto', included: true, auto_reason: '3 projektmapper fundet' }), 'Medtaget: 3 projektmapper fundet');
  assert.equal(h.modeReason({ mode: 'auto', included: false, auto_reason: 'Ingen adgang' }), 'Ikke medtaget: Ingen adgang');
  assert.equal(h.modeReason({ mode: 'exclude', included: false }), 'Du har valgt aldrig at medtage den');
});

test('time tracking: durations, invoice hours and rounding up', () => {
  assert.equal(h.formatDuration(0), '0:00');
  assert.equal(h.formatDuration(1000), '0:17');           // 16 min 40 s, like the CSV's t:mm
  assert.equal(h.formatDuration(167 * 60), '2:47');
  assert.equal(h.durationWords(167 * 60), '2 t 47 min');
  assert.equal(h.durationWords(120 * 60), '2 t');
  assert.equal(h.durationWords(45 * 60), '45 min');
  assert.equal(h.formatHours(2.75 * 3600), '2,75');
  assert.equal(h.roundUpSeconds(1000, 15), 1800);          // 16:40 -> 0:30
  assert.equal(h.roundUpSeconds(1800, 15), 1800);          // exact steps stay
  assert.equal(h.roundUpSeconds(1000, 0), 1000);
  assert.equal(h.roundUpSeconds(0, 15), 0);
});

test('time tracking: periods run Monday to Sunday and whole months', () => {
  // NOW is Wednesday 30 Sep 2026
  assert.deepEqual(h.periodRange('today', NOW), { from: '2026-09-30', to: '2026-09-30' });
  assert.deepEqual(h.periodRange('yesterday', NOW), { from: '2026-09-29', to: '2026-09-29' });
  assert.deepEqual(h.periodRange('week', NOW), { from: '2026-09-28', to: '2026-10-04' });
  assert.deepEqual(h.periodRange('last-week', NOW), { from: '2026-09-21', to: '2026-09-27' });
  assert.deepEqual(h.periodRange('month', NOW), { from: '2026-09-01', to: '2026-09-30' });
  assert.deepEqual(h.periodRange('last-month', NOW), { from: '2026-08-01', to: '2026-08-31' });
  const sunday = new Date(2026, 9, 4, 12).getTime();
  assert.deepEqual(h.periodRange('week', sunday), { from: '2026-09-28', to: '2026-10-04' });
  const january = new Date(2027, 0, 10).getTime();
  assert.deepEqual(h.periodRange('last-month', january), { from: '2026-12-01', to: '2026-12-31' });
  assert.equal(h.periodOf('2026-09-28', '2026-10-04', NOW), 'week');
  assert.equal(h.periodOf('2026-09-02', '2026-09-03', NOW), 'custom');
  assert.equal(h.parseIsoDate('2026-02-30'), null);
  assert.equal(h.formatReportDay('2026-10-01'), 'tors. 1. okt.');
});

test('time tracking: the live line says what counts and why not', () => {
  const since = new Date(2026, 8, 30, 10, 42).getTime() / 1000;
  assert.deepEqual(h.timeStatusText({ state: 'recording', project: 'Rikke Lindholm - Testimonial', bucket: 'color',
    bucket_label: 'Color', since }), { tone: 'rec', main: 'Registrerer: Rikke Lindholm - Testimonial', sub: 'Color siden 10.42' });
  assert.equal(h.timeStatusText({ state: 'recording', project: 'X', bucket: 'musik', since: null }).sub, 'Musik/lyd i browseren');
  assert.equal(h.timeStatusText({ state: 'recording', project: 'X', bucket: 'color', bucket_label: 'Color',
    timeline: 'Teaser v2', since }).sub, 'Tidslinje „Teaser v2“ · Color siden 10.42');
  assert.match(h.timeStatusText({ state: 'idle' }, 15).sub, /i over 15 min/);
  const away = h.timeStatusText({ state: 'away', project: 'Rikke Lindholm - Testimonial', away_since: since,
    away_until: since + 600 });
  assert.deepEqual(away, { tone: 'away', main: 'Uden for Resolve – tæller stadig: Rikke Lindholm - Testimonial',
    sub: 'Kommer du tilbage inden kl. 10.52, tæller pausen med. Ellers stopper tiden, fra da du forlod Resolve.' });
  assert.equal(h.timeStatusText({ state: 'paused' }).main, 'Pause – DaVinci Resolve er ikke i forgrunden');
  assert.equal(h.timeStatusText({ state: 'no-resolve' }).main, 'Venter på DaVinci Resolve');
  assert.equal(h.timeStatusText({ state: 'off', enabled: false }).tone, 'off');
});

test('time tracking: the report table rounds like the CSV export', () => {
  const report = {
    buckets: { edit: 'Edit', cut: 'Cut', color: 'Color', musik: 'Musik/lyd' },
    projects: [{
      project: 'Rikke Lindholm - Testimonial', database: 'Kunder 2026', folder: 'Rikke Lindholm',
      buckets: { edit: 3600, color: 1000 }, total_s: 4600,
      days: { '2026-09-29': 3600, '2026-09-30': 1000 },
      day_buckets: { '2026-09-29': { edit: 3600 }, '2026-09-30': { color: 1000 } },
    }, {
      project: 'Klar Tand - Skive', database: 'Kunder 2026', folder: null,
      buckets: { musik: 600 }, total_s: 600, days: { '2026-09-30': 600 }, day_buckets: { '2026-09-30': { musik: 600 } },
    }],
  };
  const table = h.timeTable(report, { roundMinutes: 15 });
  assert.deepEqual(table.columns.map((c) => c.label), ['Edit', 'Color', 'Musik/lyd']);   // no empty Cut column
  assert.deepEqual(table.rows.map((r) => [r.kind, r.total, r.rounded]),
    [['project', 4600, 5400], ['project', 600, 900]]);
  assert.deepEqual(table.sums, { buckets: { edit: 3600, color: 1000, musik: 600 }, total: 5200, rounded: 6300 });
  const perDay = h.timeTable(report, { roundMinutes: 15, perDay: true });
  assert.deepEqual(perDay.rows.map((r) => [r.kind, r.label || r.project, r.rounded]), [
    ['project', 'Rikke Lindholm - Testimonial', 5400], ['day', 'tirs. 29. sep.', 3600], ['day', 'ons. 30. sep.', 1800],
    ['project', 'Klar Tand - Skive', 900], ['day', 'ons. 30. sep.', 900]]);
  assert.deepEqual(perDay.rows[2].buckets, { color: 1000 });
  assert.equal(h.timeTable(null).rows.length, 0);
  const timelines = { ...report, projects: [{ ...report.projects[0], timelines: [
    { name: 'Testimonial v3', total_s: 3600, buckets: { edit: 3600 } },
    { name: '', total_s: 1000, buckets: { color: 1000 } }] }] };
  const perTimeline = h.timeTable(timelines, { roundMinutes: 15, perTimeline: true });
  assert.deepEqual(perTimeline.rows.map((r) => [r.kind, r.label || r.project, r.rounded]), [
    ['project', 'Rikke Lindholm - Testimonial', 5400], ['timeline', 'Testimonial v3', 3600],
    ['timeline', 'Ukendt tidslinje', 1800]]);
});

test('time tracking: download names come from Content-Disposition', () => {
  assert.equal(h.downloadName("attachment; filename=\"Projekts?g tid.csv\"; filename*=UTF-8''Projekts%C3%B8g%20tid.csv"),
    'Projektsøg tid.csv');
  assert.equal(h.downloadName('attachment; filename="tid.csv"'), 'tid.csv');
  assert.equal(h.downloadName(null), '');
});

const FX9 = {
  id: '7E3A91C4@E:', drive: 'E:', camera: 'FX9', model: 'PXW-FX9V', clips: 99, files: 297, stills: 0,
  bytes: 77.7e9, first: at(2026, 8, 29, 21, 41), last: at(2026, 8, 29, 23, 11), found: { clips: 0, projects: [] },
};

test('import: a camera card is described in one line', () => {
  assert.equal(h.cardTitle(FX9), 'FX9-kort i E:');
  assert.equal(h.cardFacts(FX9, NOW), '99 klip · 72,4 GB · optaget 29. sep. 21.41–23.11');
  const overnight = { ...FX9, stills: 12, last: at(2026, 8, 30, 1, 5) };
  assert.equal(h.cardFacts(overnight, NOW), '99 klip · 12 fotos · 72,4 GB · optaget 29. sep. 21.41–30. sep. 01.05');
  assert.deepEqual(h.cardStatus(FX9), { tone: 'new', text: 'Ikke overført før' });
  const project = { name: 'Rikke Lindholm', path: 'C:\\K\\Rikke Lindholm', folder: 'C:\\K\\Rikke Lindholm\\Klip\\FX9' };
  const found = (clips, files) => ({ clips, files, total: 297, complete: files === 297, projects: [project] });
  assert.deepEqual(h.cardStatus({ ...FX9, found: found(40, 120) }),
    { tone: 'part', text: '40 af 99 klip ligger allerede i Rikke Lindholm' });
  assert.deepEqual(h.cardStatus({ ...FX9, found: found(99, 290) }),
    { tone: 'part', text: 'Alle klip er overført til Rikke Lindholm, men 7 filer mangler' });
  assert.deepEqual(h.cardStatus({ ...FX9, found: found(99, 297) }),
    { tone: 'done', text: 'Alle 99 klip er overført til Rikke Lindholm' });
  const empty = { ...FX9, camera: 'Sony', model: null, files: 0, clips: 0, bytes: 0, first: null, last: null,
    volume_size: 128e9, blank: true };
  assert.deepEqual(h.cardStatus(empty), { tone: 'done', text: 'Kortet er tomt – der er ingen klip at overføre' });
  assert.equal(h.cardTitle(empty), 'Sony-kort i E:');
  assert.equal(h.cardFacts(empty, NOW), 'Ingen filer · 119 GB-kort');
  assert.equal(h.cardTitle({ ...empty, camera: null }), 'Kort i E:');      // nothing on it at all
  assert.equal(h.cardFacts({ ...empty, volume_size: 0 }, NOW), 'Ingen filer');
});

test('import: progress, time left and the outcome', () => {
  const job = { state: 'copying', current: 'FX9_9070.MXF', files_done: 12, files_total: 297, bytes_total: 1000,
    copied: 500, verified: 300, speed: 420 * 1024 ** 2, eta_s: 190, target: 'F:\\K\\Mette\\Klip\\FX9', error: null };
  assert.deepEqual(h.importProgress(job), { tone: 'busy', pct: 40, main: 'Kopierer FX9_9070.MXF – 12 af 297 filer',
    sub: '420 MB/s · ca. 3 min tilbage' });
  assert.equal(h.importProgress({ ...job, state: 'verifying' }).main, 'Kontrollerer FX9_9070.MXF – 12 af 297 filer');
  assert.equal(h.formatEta(30), 'under 1 min');
  assert.equal(h.formatEta(3900), 'ca. 1 t 5 min');
  const done = h.importProgress({ ...job, state: 'done', files_done: 297, copied: 1000, verified: 1000, bytes_total: 77.7e9 });
  assert.deepEqual([done.tone, done.pct, done.main], ['ok', 100, '297 filer (72,4 GB) er kopieret og kontrolleret']);
  assert.equal(done.sub, 'I F:\\K\\Mette\\Klip\\FX9 – kortet kan tages ud.');
  assert.equal(h.importProgress({ ...job, state: 'failed', error: 'Kortet blev taget ud' }).main, 'Kortet blev taget ud');
  assert.equal(h.importProgress({ ...job, state: 'cancelled' }).main, 'Overførslen blev stoppet');
  assert.equal(h.importProgress(null), null);
});

test('import: moving ("Klip") deletes from the card only at the end, and says what happened', () => {
  const job = { mode: 'move', state: 'deleting', current: 'FX9_9070.MXF', files_done: 297, files_total: 297,
    bytes_total: 1000, copied: 1000, verified: 1000, deleted: 120, kept: 0, speed: 0, eta_s: null,
    target: 'F:\\K\\Mette\\Klip\\FX9', error: null };
  assert.deepEqual(h.importProgress(job), { tone: 'busy', pct: 99,
    main: 'Sletter fra kortet: FX9_9070.MXF – 120 af 297 filer', sub: 'Alle filer er kopieret og kontrolleret' });
  const done = h.importProgress({ ...job, state: 'done', deleted: 297, bytes_total: 77.7e9 });
  assert.equal(done.main, '297 filer (72,4 GB) er flyttet – kopieret, kontrolleret og slettet fra kortet');
  assert.equal(h.importProgress({ ...job, state: 'done', deleted: 295, kept: 2 }).sub,
    'I F:\\K\\Mette\\Klip\\FX9. 2 filer var i brug og ligger stadig på kortet.');
  assert.match(h.importProgress({ ...job, state: 'failed', deleted: 0, error: 'Kopien af X er ikke identisk med kortet' }).sub,
    /Intet er slettet fra kortet\.$/);
  assert.match(h.importProgress({ ...job, state: 'cancelled', deleted: 120 }).sub,
    /^120 filer er slettet fra kortet \(de er kontrolleret\) – resten ligger stadig på kortet\./);
});

test('import: the plan says where the clips go and what is new', () => {
  const plan = { target: 'F:\\K\\Mette\\Klip\\FX9', target_exists: false, camera: 'FX9', files: 297, new_files: 290,
    new_bytes: 70 * 1024 ** 3, already: 7, conflicts: 0, other_media: 0, separate: false, free: 7200 * 1024 ** 3, fits: true };
  assert.deepEqual(h.planText(plan), { target: 'F:\\K\\Mette\\Klip\\FX9', creates: true, lines: [
    '290 nye filer (70 GB) · 7 findes allerede og springes over', '7 TB fri på disken'] });
  assert.deepEqual(h.planText({ ...plan, new_files: 0, already: 297, fits: false, free: 70 * 1024 ** 3 }).lines, [
    'Alle 297 filer ligger der allerede.', 'Der er kun 70 GB fri på disken – vælg en anden']);
  assert.equal(h.planText({ ...plan, other_media: 12 }).lines[1], 'FX9-mappen har allerede 12 andre klip.');
  assert.match(h.planText({ ...plan, conflicts: 2, separate: true }).lines[1], /^2 klip med samme navn/);
  assert.equal(h.planText(null), null);
});

test('Klippe plays: the line under "Vis legen nu"', () => {
  assert.equal(h.petPlayText({ state: 'waiting' }), 'Slip musen … så kommer Klippe ud 🎈');
  assert.equal(h.petPlayText({ state: 'out' }), 'Klippe leger – rør musen, så flyver den hjem 🏠');
  assert.equal(h.petPlayText({ state: 'ready', message: 'Skærmen er låst' }), 'Skærmen er låst');
  assert.equal(h.petPlayText({ state: 'ready', message: '' }), 'Klippe kommer ud, når du har sluppet musen et par sekunder.');
  assert.equal(h.petPlayText(null), 'Klippe kommer ud, når du har sluppet musen et par sekunder.');
});

test('updates: the Opdatering box and its button', () => {
  const latest = { sha: 'b'.repeat(40), date: '2026-09-29T10:00:00Z', title: 'Opdateringsknap' };
  const base = { mode: 'zip', installed: { sha: 'b'.repeat(40), date: latest.date }, latest, available: false,
    blocked: null, busy: null, checked: ago(5 * 60e3), error: null };
  assert.deepEqual(h.updateView(base, NOW), { label: 'Du har den nyeste version',
    hint: 'Version fra 29. sep. · tjekket for 5 minutter siden', warn: false, button: 'Søg efter opdatering',
    primary: false, disabled: false });
  const ready = h.updateView({ ...base, installed: null, available: true }, NOW);
  assert.deepEqual([ready.label, ready.hint, ready.button, ready.primary], ['Ny version klar',
    'Fra 29. sep.: Opdateringsknap', 'Opdater nu', true]);
  const blocked = h.updateView({ ...base, available: true, blocked: 'Der er ændrede filer i mappen' }, NOW);
  assert.deepEqual([blocked.label, blocked.hint, blocked.warn, blocked.button],
    ['Ny version fra 29. sep.', 'Der er ændrede filer i mappen', true, 'Søg efter opdatering']);
  const busy = h.updateView({ ...base, busy: 'restarting' }, NOW);
  assert.deepEqual([busy.label, busy.disabled], ['Projektsøg genstarter med den nye version …', true]);
  assert.equal(h.updateView({ ...base, busy: 'downloading' }, NOW).label, 'Henter den nye version …');
  const offline = h.updateView({ ...base, latest: null, installed: null, error: 'Ingen forbindelse til GitHub' }, NOW);
  assert.deepEqual([offline.label, offline.hint, offline.warn],
    ['Kunne ikke søge efter en ny version', 'Ingen forbindelse til GitHub', true]);
  assert.equal(h.updateView(null, NOW).button, 'Søg efter opdatering');
});

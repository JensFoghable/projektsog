/* Projektsøg – search window (SPEC.md §11–§12).
 *
 * Plain script: no build step, no framework, no external resources. The first half holds pure
 * helpers without DOM access (tests/ui/*.test.js load them under Node); the second half drives
 * the page. document.title is never changed – the app window is found by its exact title. */
(() => {
  'use strict';

  // ===========================================================================================
  // Pure helpers
  // ===========================================================================================

  const LOCALE = 'da-DK';
  const NUMBER = new Intl.NumberFormat(LOCALE);
  const DECIMAL = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 1 });
  const DAY_MONTH = new Intl.DateTimeFormat(LOCALE, { day: 'numeric', month: 'short' });
  const DAY_MONTH_YEAR = new Intl.DateTimeFormat(LOCALE, { day: 'numeric', month: 'short', year: 'numeric' });
  const RELATIVE = new Intl.RelativeTimeFormat(LOCALE, { numeric: 'always' });
  const COLLATOR = new Intl.Collator(LOCALE, { sensitivity: 'base', numeric: true });
  const MINUTE = 60e3;
  const HOUR = 60 * MINUTE;
  const DAY = 24 * HOUR;
  /* Fallback for a SourceRef without `is_system` (SPEC §15.4): Windows is on C: on every PC here. */
  const SYSTEM_DRIVE = 'C:';
  const RESOLVE_APPS = ['Resolve.exe', 'Fusion.exe'];
  const KINDS = ['all', 'project', 'dir', 'file'];
  const KIND_LABELS = { all: 'Alle', project: 'Projekter', dir: 'Mapper', file: 'Filer' };
  const ACTION_LABELS = { folder: 'Åbn mappe', reveal: 'Vis i Stifinder', file: 'Åbn fil' };
  const SETTINGS_TABS = ['placeringer', 'generelt', 'resolve'];
  const SCAN_VISIBLE_AFTER_MS = 1500;

  const FILE_TYPES = extensionMap({
    video: 'mxf mov mp4 m4v mts m2ts braw r3d crm ari avi mkv insv lrv wmv webm mpg mpeg vob 3gp dv flv',
    audio: 'wav bwf mp3 aif aiff aac m4a flac ogg oga opus wma caf',
    image: 'png jpg jpeg tif tiff exr dpx psd psb ai arw cr2 cr3 dng nef raf orf rw2 heic heif webp gif bmp tga svg',
    resolve: 'drp drt dra drb',
    projfile: 'prproj aep aepx aet fcpxml fcpbundle edl otio blend c4d uproject mogrt comp setting veg',
    doc: 'pdf doc docx txt rtf md odt xls xlsx csv ppt pptx pages key numbers srt vtt ass xml json',
  });
  const TYPE_ICONS = {
    video: 'video', audio: 'audio', image: 'image', sequence: 'sequence', resolve: 'resolve',
    projfile: 'projfile', doc: 'doc', other: 'file',
  };

  function extensionMap(groups) {
    const map = new Map();
    for (const [type, list] of Object.entries(groups)) {
      for (const ext of list.split(' ')) map.set(ext, type);
    }
    return map;
  }

  /** Icon and tile tone: projects, groups and folders by kind; files by type. */
  function itemVisual(item) {
    if (item.kind === 'project') return { icon: 'project', tone: 'project' };
    if (item.kind === 'group') return { icon: 'group', tone: 'group' };
    if (item.kind !== 'file') return { icon: 'folder', tone: 'folder' };
    const type = item.is_seq ? 'sequence' : FILE_TYPES.get(String(item.ext || '').toLowerCase()) || 'other';
    return { icon: TYPE_ICONS[type], tone: type };
  }

  function isFolder(item) {
    return item.kind !== 'file';
  }

  /** Enter opens folders and reveals files; Ctrl+Enter reveals folders and opens files. */
  function openAction(item, alternate) {
    if (isFolder(item)) return alternate ? 'reveal' : 'folder';
    return alternate ? 'file' : 'reveal';
  }

  function formatInt(value) {
    return NUMBER.format(Math.round(Number(value) || 0));
  }

  function plural(count, one, many) {
    return `${formatInt(count)} ${Math.round(Number(count) || 0) === 1 ? one : many}`;
  }

  /** Sizes like Explorer (1024-based) with Danish decimals: "12,4 GB", "123 GB". */
  function formatBytes(bytes) {
    if (bytes == null || !Number.isFinite(bytes)) return '–';
    if (bytes < 1024) return `${formatInt(bytes)} B`;
    const units = ['kB', 'MB', 'GB', 'TB', 'PB'];
    let value = bytes / 1024;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value < 100 ? DECIMAL.format(value) : formatInt(value)} ${units[unit]}`;
  }

  function startOfDay(ms) {
    const date = new Date(ms);
    date.setHours(0, 0, 0, 0);
    return date.getTime();
  }

  /** "12. sep." this year, "12. sep. 2025" otherwise. `seconds` is a Unix timestamp. */
  function formatDay(seconds, now = Date.now()) {
    const date = new Date(seconds * 1000);
    const sameYear = date.getFullYear() === new Date(now).getFullYear();
    return (sameYear ? DAY_MONTH : DAY_MONTH_YEAR).format(date);
  }

  /** "lige nu", "for 5 minutter siden", "i går", "for 2 dage siden", "for 3 uger siden", "12. sep.". */
  function relativeTime(seconds, now = Date.now()) {
    const ms = seconds * 1000;
    const diff = now - ms;
    if (diff < 45e3) return 'lige nu';
    if (diff < HOUR) return RELATIVE.format(-Math.max(1, Math.round(diff / MINUTE)), 'minute');
    const days = Math.round((startOfDay(now) - startOfDay(ms)) / DAY);
    if (days === 0 || diff < 6 * HOUR) return RELATIVE.format(-Math.round(diff / HOUR), 'hour');
    if (days === 1) return 'i går';
    if (days < 7) return RELATIVE.format(-days, 'day');
    if (days < 28) return RELATIVE.format(-Math.round(days / 7), 'week');
    return formatDay(seconds, now);
  }

  function modifiedText(seconds, now = Date.now()) {
    return seconds ? `ændret ${relativeTime(seconds, now)}` : '–';
  }

  /**
   * Split `name` into plain and highlighted parts; ranges are [start, end) in code points.
   * Matches separated only by spaces become one mark ("Rikke Lindholm", not "Rikke" + "Lindholm").
   */
  function highlightParts(name, ranges) {
    const text = String(name ?? '');
    if (!Array.isArray(ranges) || !ranges.length) return [{ text, hl: false }];
    const chars = Array.from(text);
    const spans = [];
    for (const [start, end] of [...ranges].sort((a, b) => a[0] - b[0])) {
      const from = Math.max(0, Math.min(chars.length, Math.trunc(start)));
      const to = Math.max(from, Math.min(chars.length, Math.trunc(end)));
      const last = spans[spans.length - 1];
      if (last && (from <= last[1] || /^\s+$/.test(chars.slice(last[1], from).join('')))) last[1] = Math.max(last[1], to);
      else if (to > from) spans.push([from, to]);
    }
    const parts = [];
    let pos = 0;
    for (const [from, to] of spans) {
      if (from > pos) parts.push({ text: chars.slice(pos, from).join(''), hl: false });
      parts.push({ text: chars.slice(from, to).join(''), hl: true });
      pos = to;
    }
    if (pos < chars.length) parts.push({ text: chars.slice(pos).join(''), hl: false });
    return parts;
  }

  function splitRel(rel) {
    return String(rel || '').split('\\').filter(Boolean);
  }

  /** The project an item lies inside (never the item itself) and the folders in between. */
  function itemLocation(item) {
    const parents = splitRel(item.rel_path).slice(0, -1);
    const project = item.project;
    if (project && project.rel_path && item.kind !== 'project') {
      const projectParts = splitRel(project.rel_path);
      const inside = projectParts.length <= parents.length
        && projectParts.every((part, i) => part.toLowerCase() === parents[i].toLowerCase());
      if (inside) return { project, crumbs: parents.slice(projectParts.length) };
    }
    return { project: null, crumbs: parents };
  }

  /**
   * Location badge: "HOST · share", "Disk: <disk> (H:) · <name>" for local sources on another
   * volume than Windows' own, else "HOST · <folder>". `is_system` (§15.4) decides; without it
   * a drive other than C: counts as another volume.
   */
  function sourceBadge(source) {
    if (!source || !(source.name || source.host)) return { icon: 'folder', text: 'Ukendt placering' };
    const name = source.name || '';
    const hostAndName = [source.host, name].filter(Boolean).join(' · ');
    if (source.kind === 'share') return { icon: 'net', text: hostAndName };
    const drive = source.drive ? String(source.drive).toUpperCase() : null;
    const system = typeof source.is_system === 'boolean' ? source.is_system : !drive || drive === SYSTEM_DRIVE;
    if (!system) {
      const disk = source.disk_name || source.volume_label || name;
      const label = drive ? `Disk: ${disk} (${drive})` : `Disk: ${disk}`;
      return { icon: 'disk', text: !name || disk.toLowerCase() === name.toLowerCase() ? label : `${label} · ${name}` };
    }
    return { icon: 'pc', text: hostAndName };
  }

  /** Whether a row's location answered when the row was fetched (no source → a plain path). */
  function itemOnline(item) {
    return !item || !item.source || item.source.online !== false;
  }

  /** Offline although its disk is mounted (or its computer answers): the folder itself is gone
   *  – moved, renamed or deleted (§15.12 `volume_present`). */
  function folderGone(source) {
    return Boolean(source && source.volume_present === true && source.online !== true);
  }

  /** What to do about an offline location – the same wording the server uses (§11, §15.12). */
  function offlineHint(source) {
    if (folderGone(source)) return 'Mappen findes ikke længere';
    if (source && source.kind === 'share' && source.host) return `Computeren ${source.host} svarer ikke – er den tændt?`;
    const disk = source && (source.disk_name || source.volume_label || source.name);
    return disk ? `Tilslut disken ‘${disk}’` : 'Placeringen er ikke tilgængelig';
  }

  function offlineSince(source, now = Date.now()) {
    return source && source.last_seen ? `Offline – sidst set ${formatDay(source.last_seen, now)}` : 'Offline';
  }

  /** Scans worth showing: deep ones (all during first indexing) that have run for a moment. */
  function visibleScans(status, now = Date.now()) {
    if (!status || !Array.isArray(status.scanning)) return [];
    const firstIndexing = status.initial_scan_done === false;
    return status.scanning
      .filter((scan) => (scan.kind !== 'shallow' || firstIndexing)
        && (!scan.started || now - scan.started * 1000 >= SCAN_VISIBLE_AFTER_MS))
      .sort((a, b) => (a.started || 0) - (b.started || 0));
  }

  function statusPill(status, { offline = false, now = Date.now() } = {}) {
    if (offline) return { tone: 'warn', text: 'Ingen forbindelse – prøver igen …' };
    if (!status) return { tone: 'muted', text: 'Forbinder …' };
    if (status.worker && status.worker.running === false) return { tone: 'warn', text: 'Indeksering stoppet – se loggen' };
    const scans = visibleScans(status, now);
    let scanning = '';
    if (scans.length) {
      const more = scans.length > 1 ? ` (+${scans.length - 1})` : '';
      scanning = `Scanner ${scans[0].name} – ${plural(scans[0].entries || 0, 'fil', 'filer')}${more}`;
    }
    const online = status.sources_included_online ?? status.sources_online ?? 0;
    if (status.initial_scan_done === false && online > 0) {
      const ready = `${formatInt(status.sources_ready || 0)} af ${plural(online, 'placering', 'placeringer')} klar`;
      return { tone: 'scan', text: scanning ? `${ready} · ${scanning}` : ready };
    }
    if (scanning) return { tone: 'scan', text: scanning };
    const offlineCount = status.sources_offline || 0;
    const text = `${plural(online, 'placering', 'placeringer')} online${offlineCount ? ` · ${formatInt(offlineCount)} offline` : ''}`;
    return { tone: online ? 'ok' : 'muted', text };
  }

  /** Why a search found nothing, with one-click fixes (§12 zero results). */
  function zeroResultReasons(response, status, { kind = 'all', excludedOnline = 0 } = {}) {
    const reasons = [];
    const hidden = (response && response.hidden) || {};
    if (hidden.kind > 0) {
      reasons.push({ key: 'kind', icon: 'filter', action: 'all-kinds', label: 'Vis alle',
        text: `${plural(hidden.kind, 'resultat', 'resultater')} skjules af filteret ‘${KIND_LABELS[kind] || kind}’` });
    }
    if (hidden.offline > 0) {
      reasons.push({ key: 'offline', icon: 'disk', action: 'show-offline', label: 'Vis',
        text: `${formatInt(hidden.offline)} på offline placeringer` });
    }
    if (hidden.source > 0) {
      reasons.push({ key: 'source', icon: 'folder', action: 'all-sources', label: 'Vis alle placeringer',
        text: `${plural(hidden.source, 'resultat', 'resultater')} på andre placeringer` });
    }
    // The counts above are what switching off just that filter shows (§15.7); matches that
    // several filters hide at once are only in `any` – then only clearing them all helps.
    const single = (hidden.kind || 0) + (hidden.offline || 0) + (hidden.source || 0);
    if (hidden.any > single) {
      reasons.push({ key: 'any', icon: 'filter', action: 'clear-filters', label: 'Ryd alle filtre',
        text: `${plural(hidden.any, 'resultat', 'resultater')} skjules af filtrene` });
    }
    const firstIndexing = Boolean(status && status.initial_scan_done === false);
    for (const scan of (status && status.scanning) || []) {
      if (scan.kind !== 'shallow' || firstIndexing) {
        reasons.push({ key: `scan:${scan.source_id}`, icon: 'refresh',
          text: `${scan.name} indekseres stadig (${plural(scan.entries || 0, 'fil', 'filer')} indtil nu)` });
      }
    }
    if (excludedOnline > 0) {
      reasons.push({ key: 'excluded', icon: 'pc', action: 'show-sources', label: 'Vis',
        text: `${formatInt(excludedOnline)} ${excludedOnline === 1 ? 'tilsluttet placering' : 'tilsluttede placeringer'} er ikke medtaget` });
    }
    return reasons;
  }

  /**
   * "6 klip ligger på disken ‘2024 Disk Sølv’, som ikke er tilsluttet" (share: "… på HOST, som
   * ikke svarer"; a folder gone from a mounted disk or an answering computer (§15.12): "… i mappen
   * ‘Kunder 2026 (STUDIO)’, som ikke findes længere").
   */
  function resolveOfflineWarnings(rs) {
    if (!rs || !rs.connected) return [];
    const groups = new Map();
    const present = new Set(); // disks/computers that are there – only a folder on them is gone
    let covered = 0;
    for (const folder of rs.folders || []) {
      if (folder.online !== false || !folder.count) continue;
      const source = folder.source || {};
      const share = source.kind === 'share';
      const where = share ? source.host : source.disk_name || source.volume_label || source.name;
      const gone = source.volume_present === true; // the folder is offline (above), its disk is not
      if (gone) present.add(where);
      const type = gone ? 'gone' : share ? 'host' : 'disk';
      const label = gone ? source.name || where : where;
      const key = `${type}:${gone ? source.id ?? label : label}`;
      const group = groups.get(key) || { type, label, count: 0 };
      group.count += folder.count;
      covered += folder.count;
      groups.set(key, group);
    }
    const lines = [...groups.values()]
      .sort((a, b) => b.count - a.count)
      .map((g) => {
        if (g.type === 'gone') return `${formatInt(g.count)} klip ligger i mappen ‘${g.label}’, som ikke findes længere`;
        return g.type === 'host'
          ? `${formatInt(g.count)} klip ligger på ${g.label}, som ikke svarer`
          : `${formatInt(g.count)} klip ligger på disken ‘${g.label}’, som ikke er tilsluttet`;
      });
    const rest = (rs.offline_clips || 0) - covered;
    if (rest > 0) {
      const known = new Set([...groups.values()].filter((g) => g.type !== 'gone').map((g) => g.label));
      const disks = (rs.offline_disks || []).filter((disk) => !known.has(disk) && !present.has(disk));
      lines.push(disks.length
        ? `${formatInt(rest)} klip ligger på ${disks.length === 1 ? 'disken' : 'diskene'} ${disks.map((d) => `‘${d}’`).join(' og ')}, som ikke er tilsluttet`
        : `${formatInt(rest)} klip ligger på placeringer, der ikke er tilgængelige`);
    }
    return lines;
  }

  function hasResolvePassthrough(apps) {
    return (apps || []).some((app) => String(app).toLowerCase() === 'resolve.exe');
  }

  /** hotkey_passthrough_apps with Resolve.exe/Fusion.exe added (keep = true) or removed. */
  function withResolvePassthrough(apps, keep) {
    const resolve = RESOLVE_APPS.map((app) => app.toLowerCase());
    const others = (apps || []).filter((app) => !resolve.includes(String(app).toLowerCase()));
    return keep ? [...others, ...RESOLVE_APPS] : others;
  }

  /** Ask once, when the window was summoned from Resolve/Fusion with the conflicting Shift+Space. */
  function shouldAskResolveHotkey(focus, settings, hotkeySpec) {
    if (!focus || !settings || settings.resolve_hotkey_asked !== false) return false;
    const app = String(focus.from_app || '').toLowerCase();
    if (app !== 'resolve.exe' && app !== 'fusion.exe') return false;
    return String(hotkeySpec || settings.hotkey || '').toLowerCase().replace(/\s+/g, '') === 'shift+space';
  }

  /** Characters typed since the window was hidden (the hotkey helper may replay keys early). */
  function typedSince(before, current) {
    if (current === before) return null;
    return current.startsWith(before) ? current.slice(before.length) : current;
  }

  function parseLaunchParams(search) {
    const params = new URLSearchParams(search || '');
    const tab = (params.get('tab') || '').toLowerCase();
    return {
      query: params.get('q') || '',
      panel: (params.get('panel') || '').toLowerCase(),
      tab: SETTINGS_TABS.includes(tab) ? tab : null,
    };
  }

  function joinWinPath(base, name) {
    return /[\\/]$/.test(base) ? `${base}${name}` : `${base}\\${name}`;
  }

  function leafName(path) {
    return String(path || '').split(/[\\/]/).filter(Boolean).pop() || String(path || '');
  }

  function footerHint(hotkey, { settingsOpen = false, hasQuery = false } = {}) {
    const esc = settingsOpen ? 'Esc lukker indstillinger' : hasQuery ? 'Esc rydder søgningen' : 'Esc skjuler';
    if (!hotkey) return esc;
    if (hotkey.enabled === false) return `Genvejstasten er slået fra · ${esc}`;
    const label = hotkey.label || 'Genvejstasten';
    if (hotkey.active === false) return `${label} virker ikke lige nu – se Indstillinger · ${esc}`;
    return `${label} åbner Projektsøg overalt · ${esc}`;
  }

  /** Sources grouped by computer: this PC first, then by name; included sources first in a group. */
  function groupSources(sources, hosts) {
    const byHost = new Map();
    for (const host of hosts || []) {
      byHost.set(String(host.name).toUpperCase(), { host: host.name, info: host, sources: [] });
    }
    for (const source of sources || []) {
      const key = String(source.host || '').toUpperCase();
      if (!byHost.has(key)) byHost.set(key, { host: source.host || '–', info: null, sources: [] });
      byHost.get(key).sources.push(source);
    }
    const groups = [...byHost.values()];
    for (const group of groups) {
      group.sources.sort((a, b) => Number(Boolean(b.included)) - Number(Boolean(a.included))
        || COLLATOR.compare(a.display_name || '', b.display_name || ''));
    }
    const isSelf = (group) => Number(Boolean(group.info && group.info.self));
    return groups.sort((a, b) => isSelf(b) - isSelf(a) || COLLATOR.compare(a.host, b.host));
  }

  function sourceScanState(source, scan, now = Date.now()) {
    if (scan) {
      const pct = scan.units_total ? Math.min(99, Math.floor((100 * (scan.units_done || 0)) / scan.units_total)) : null;
      return { main: pct == null ? 'Scanner …' : `Scanner … ${pct} %`,
        sub: `${plural(scan.entries || 0, 'fil', 'filer')} indtil nu`, tone: 'scan' };
    }
    const scanned = source.last_scan_end ? `Scannet ${relativeTime(source.last_scan_end, now)}` : 'Ikke scannet endnu';
    if (folderGone(source)) {
      // Its disk/computer is there (§15.12): nothing to plug in – [Glem] next to it is the fix.
      return { main: 'Mappen findes ikke længere', sub: source.last_seen ? `Sidst set ${formatDay(source.last_seen, now)}` : '', tone: 'off' };
    }
    if (!source.online) return { main: offlineSince(source, now), sub: source.last_scan_end ? scanned : '', tone: 'off' };
    if (!source.included) return { main: 'Ikke medtaget', sub: '' };
    if (source.queued) return { main: 'I kø til scanning', sub: source.last_scan_end ? scanned : '' };
    if ((source.last_scan_ok === false || source.last_scan_ok === 0) && source.last_error) {
      return { main: 'Fejl ved sidste scanning', sub: source.last_error, tone: 'error' };
    }
    return { main: scanned, sub: '' };
  }

  function modeReason(source) {
    if (source.mode === 'include') return 'Du har valgt altid at medtage den';
    if (source.mode === 'exclude') return 'Du har valgt aldrig at medtage den';
    const reason = source.auto_reason ? `: ${source.auto_reason}` : '';
    return source.included ? `Medtaget${reason}` : `Ikke medtaget${reason}`;
  }

  /** Share sources that removing `host` forgets at most (§15.8: manually added roots stay) – for
   *  the question; what was really forgotten comes back from the server (§15.12). */
  function hostShareCount(sources, host) {
    const name = String(host || '').toUpperCase();
    return (sources || []).filter((s) => s.kind === 'share' && !s.manual
      && String(s.host || '').toUpperCase() === name).length;
  }

  /** The confirmation asked before a computer is removed (§15.8). */
  function removeHostQuestion(host, shares) {
    return shares > 0
      ? `Fjern ${host} og glem dens ${plural(shares, 'delte mappe', 'delte mapper')}?`
      : `Fjern ${host} fra listen?`;
  }

  /** What removing a computer did – `forgotten` as DELETE /api/hosts reports it (§15.12). */
  function removedHostText(host, forgotten) {
    const count = Number.isFinite(forgotten) ? forgotten : 0;
    return count > 0
      ? `${host} er fjernet, og dens ${plural(count, 'delte mappe', 'delte mapper')} er glemt`
      : `${host} er fjernet`;
  }

  /** What [Medtag] on a new-disk card achieved, from the Source objects the server returned. */
  function includeOutcome(included, total) {
    const count = included.length;
    const what = count < total
      ? `${formatInt(count)} af ${plural(total, 'mappe', 'mapper')} er medtaget i søgningen`
      : 'Medtaget i søgningen';
    return included.some((source) => source && source.online !== false)
      ? `${what} – disken bliver scannet nu.`
      : `${what} – disken scannes, når den er tilsluttet igen.`;
  }

  const helpers = {
    itemVisual, isFolder, openAction, formatInt, plural, formatBytes, formatDay, relativeTime,
    modifiedText, highlightParts, itemLocation, sourceBadge, itemOnline, offlineHint, offlineSince,
    visibleScans, statusPill, zeroResultReasons, resolveOfflineWarnings, hasResolvePassthrough,
    withResolvePassthrough, shouldAskResolveHotkey, typedSince, parseLaunchParams, joinWinPath,
    leafName, footerHint, groupSources, sourceScanState, modeReason, hostShareCount,
    removeHostQuestion, removedHostText, includeOutcome,
  };
  if (typeof module === 'object' && module.exports) module.exports = helpers;
  if (typeof document === 'undefined') return;

  // ===========================================================================================
  // Page
  // ===========================================================================================

  const DEBOUNCE_MS = 80;
  const QUERY_LIFETIME_MS = 30e3;
  const INTERACTION_QUIET_MS = 1500;
  const REFRESH_MIN_INTERVAL_MS = 1000;
  const RESULT_LIMIT = 200;
  const RECENT_LIMIT = 30;
  const FIRST_CHUNK_ROWS = 30;
  const LATER_CHUNK_ROWS = 60;
  const LOADBAR_DELAY_MS = 150;
  const OFFLINE_GRACE_MS = 2500;
  const SOURCES_THROTTLE_MS = { open: 400, listed: 1000, closed: 5000 };
  const SSE_BACKOFF_MS = [1000, 2000, 4000, 8000, 15000, 30000];
  const INCLUDED_DISK_CARD_MS = 20e3;
  const LOCATION_KEY_MS = 400; // a location 'change' this soon after a key press came from the keyboard
  const LOCATION_HANDOVER_KEYS = new Set(['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageUp', 'PageDown',
    'Home', 'End', 'Enter']);
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const OPENED = { folder: 'Mappen er åbnet i Stifinder', reveal: 'Vist i Stifinder', file: 'Filen er åbnet' };

  const $ = (id) => document.getElementById(id);
  const el = {
    input: $('q'), clear: $('clear'), pill: $('pill'), pillText: document.querySelector('#pill .pill__text'),
    settingsButton: $('settings-button'), loadbar: $('loadbar'),
    seg: document.querySelector('.seg'), onlineOnly: $('online-only'), count: $('result-count'), location: $('location'),
    resolve: $('resolve'), cards: $('cards'), results: $('results'), empty: $('empty'),
    settings: $('settings'), settingsClose: $('settings-close'), tabs: [...document.querySelectorAll('.tabs__tab')],
    sourcesSummary: $('sources-summary'), scanAll: $('scan-all'), sourceGroups: $('source-groups'),
    hostForm: $('host-form'), hostInput: $('host-input'), hostError: $('host-error'),
    rootList: $('root-list'), rootForm: $('root-form'), rootInput: $('root-input'), rootError: $('root-error'),
    hotkeyForm: $('hotkey-form'), hotkeyInput: $('hotkey-input'), hotkeyError: $('hotkey-error'),
    hotkeyState: $('hotkey-state'), passthrough: $('passthrough-switch'), passthroughLabel: $('lbl-passthrough'),
    about: $('about'),
    resolveConnection: $('resolve-connection'), actions: $('actions'), hotkeyHint: $('hotkey-hint'),
    toast: $('toast'), announcer: $('announcer'),
  };

  const state = {
    status: null, hotkey: null, settings: null, resolve: null,
    sources: [], hosts: [], sourcesLoaded: false, sourcesDirty: true, sourcesLoading: false,
    sourcesReload: false, sourcesTimer: 0, sourcesDue: 0, sourcesLastLoad: 0,
    query: '', kind: 'all', onlineOnly: null, sourceId: null,
    view: { mode: 'loading' }, groups: [], items: [], itemByKey: new Map(), groupOf: new Map(),
    selectedKey: null, userSelected: false, notice: null, pendingEnter: null, pendingOpen: null,
    searchTimer: 0, inflight: null, inflightReason: null, requestSeq: 0, loadbarTimer: 0,
    lastInteraction: 0, lastRefresh: 0, refreshPending: false, refreshTimer: 0,
    hiddenAt: null, valueAtHide: '', projectAtHide: null,
    cards: [], askDismissed: false,
    settingsOpen: false, settingsTab: 'placeringer', hotkeyDirty: false, confirmForget: null, confirmTimer: 0,
    confirmHost: null, hostRefusal: null, removingHost: null, revealExcluded: false,
    resolveExpanded: false, resolveAnimate: false, resolveBusy: false,
    apiReachable: true, eventsDownSince: null, eventSource: null, sseAttempt: 0, sseTimer: 0, sseNeedsResync: false,
    opening: false, menu: null, toastTimer: 0, announceTimer: 0, locationSignature: '', emptySignature: '',
    locationKeyAt: 0, locationPointer: false, firstPaint: null, renderToken: 0,
  };

  // ------------------------------------------------------------------------------ DOM helpers

  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (value == null || value === false) continue;
      if (name === 'class') node.className = value;
      else if (name === 'dataset') Object.assign(node.dataset, value);
      else if (name.startsWith('on')) node.addEventListener(name.slice(2), value);
      else node.setAttribute(name, value === true ? '' : String(value));
    }
    for (const child of children.flat()) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  function svgIcon(name, extraClass) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', extraClass ? `icon ${extraClass}` : 'icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function setIcon(svg, name) {
    svg.firstChild.setAttribute('href', `#i-${name}`);
  }

  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }

  function showFieldError(errorNode, message, input) {
    errorNode.textContent = message;
    errorNode.hidden = false;
    if (input) input.classList.add('is-invalid');
  }

  function hideFieldError(errorNode, input) {
    errorNode.hidden = true;
    if (input) input.classList.remove('is-invalid');
  }

  // ------------------------------------------------------------------------------ API

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
    }
  }

  async function request(method, path, { params, body, signal } = {}) {
    let url = path;
    if (params) {
      const query = new URLSearchParams();
      for (const [key, value] of Object.entries(params)) {
        if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
      }
      const text = query.toString();
      if (text) url += `?${text}`;
    }
    const init = { method, signal, cache: 'no-store', headers: {} };
    if (method !== 'GET') init.headers['X-Projektsog'] = '1';
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(url, init);
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      setApiReachable(false);
      throw new ApiError('Projektsøg svarer ikke', 0);
    }
    setApiReachable(true);
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
    }
    if (!response.ok) throw new ApiError((data && data.error) || `Uventet svar fra Projektsøg (${response.status})`, response.status);
    return data;
  }

  const api = {
    get: (path, params, signal) => request('GET', path, { params, signal }),
    post: (path, body = {}) => request('POST', path, { body }),
    delete: (path, body = {}) => request('DELETE', path, { body }),
  };

  function setApiReachable(ok) {
    if (state.apiReachable === ok) return;
    state.apiReachable = ok;
    renderPill();
  }

  function setEventsDown(down) {
    if (down && state.eventsDownSince == null) {
      state.eventsDownSince = Date.now();
      setTimeout(renderPill, OFFLINE_GRACE_MS + 50);
    } else if (!down) {
      state.eventsDownSince = null;
    }
    renderPill();
  }

  function isDisconnected() {
    return !state.apiReachable
      || (state.eventsDownSince != null && Date.now() - state.eventsDownSince >= OFFLINE_GRACE_MS);
  }

  // ------------------------------------------------------------------------------ results

  function keyOf(item) {
    return item.id != null ? String(item.id) : `p:${item.path}`;
  }

  function rowId(key) {
    return `opt-${encodeURIComponent(key)}`;
  }

  function rowElement(key) {
    return key == null ? null : document.getElementById(rowId(key));
  }

  function selectedItem() {
    return state.itemByKey.get(state.selectedKey) || null;
  }

  function resolvePrimary() {
    const rs = state.resolve;
    return rs && rs.enabled && rs.connected && rs.primary ? rs.primary : null;
  }

  function resolveProject() {
    const rs = state.resolve;
    return rs && rs.enabled && rs.connected ? rs.project || null : null;
  }

  function primaryClipCount(primary) {
    const rs = state.resolve;
    if (!rs || primary.match === 'name') return 0;
    // A project that is a whole source (§15.3) has no index id: compare ids only when present.
    const folder = (rs.folders || []).find((f) => (f.item && f.item.id != null && f.item.id === primary.id)
      || (f.project && f.project.path === primary.path));
    return folder ? folder.count || 0 : 0;
  }

  function effectiveOnlineOnly() {
    return state.onlineOnly ?? !(state.settings ? state.settings.show_offline : true);
  }

  function setQuery(text) {
    el.input.value = text;
    state.query = text;
    el.clear.hidden = !text;
  }

  function scheduleQuery() {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => runView('query'), DEBOUNCE_MS);
  }

  /** Fetch recent projects (empty query) or search results. reason: query | refresh | resolve. */
  async function runView(reason) {
    clearTimeout(state.searchTimer);
    state.searchTimer = 0;
    if (state.inflight) state.inflight.abort();
    const controller = new AbortController();
    const seq = ++state.requestSeq;
    state.inflight = controller;
    state.inflightReason = reason;
    if (reason === 'query') {
      clearTimeout(state.loadbarTimer);
      state.loadbarTimer = setTimeout(() => { el.loadbar.hidden = false; }, LOADBAR_DELAY_MS);
    }
    const query = state.query.trim();
    let view;
    try {
      if (!query) {
        const data = await api.get('/api/recent', { limit: RECENT_LIMIT }, controller.signal);
        view = { mode: 'recent', results: (data && data.results) || [] };
      } else {
        const params = { q: query, kind: state.kind, limit: RESULT_LIMIT };
        if (state.onlineOnly !== null) params.online = state.onlineOnly ? 1 : 0;
        if (state.sourceId != null) params.source = state.sourceId;
        const data = await api.get('/api/search', params, controller.signal);
        view = { mode: 'search', response: { query, total: 0, results: [], ...(data || {}) } };
      }
    } catch (err) {
      if ((err && err.name === 'AbortError') || seq !== state.requestSeq) return;
      state.pendingEnter = null;
      view = { mode: 'error', error: err.message };
    }
    if (state.firstPaint) {
      // First paint waits for the status request (sent in parallel), so the Resolve bar and
      // the list appear together instead of the list jumping down a moment later.
      await state.firstPaint;
      state.firstPaint = null;
    }
    if (seq !== state.requestSeq) return; // a newer request owns the view
    state.inflight = null;
    state.inflightReason = null;
    clearTimeout(state.loadbarTimer);
    el.loadbar.hidden = true;
    applyView(view, reason);
    const pending = state.pendingOpen;
    if (pending && seq >= pending.seq) {
      // A row that looked offline while its location is online again, now re-fetched (XMC-1).
      state.pendingOpen = null;
      const item = pending.query === state.query ? state.itemByKey.get(pending.key) : null;
      if (item) openItem(item, pending.key, pending.alternate, { recheck: false });
    }
  }

  function buildGroups(view) {
    if (view.mode === 'recent') {
      const groups = [];
      const primary = resolvePrimary();
      if (primary) groups.push({ key: 'resolve', label: 'Fra DaVinci Resolve', items: [primary] });
      // The primary may lack an index id (built from a ProjectRef), so compare paths as well.
      const samePlace = (item) => primary && (keyOf(item) === keyOf(primary)
        || (item.path && primary.path && item.path.toLowerCase() === primary.path.toLowerCase()));
      const recent = view.results.filter((item) => !samePlace(item));
      if (recent.length) groups.push({ key: 'recent', label: 'Seneste projekter', items: recent });
      return groups;
    }
    if (view.mode === 'search' && view.response.results.length) {
      return [{ key: 'results', label: null, items: view.response.results }];
    }
    return [];
  }

  /** First row – except that a Resolve suggestion ("Muligt match") is never pre-selected. */
  function defaultSelection(groups) {
    const [first, second] = groups;
    if (!first) return null;
    if (first.key === 'resolve' && first.items[0].match === 'name') return second ? keyOf(second.items[0]) : null;
    return keyOf(first.items[0]);
  }

  function applyView(view, reason) {
    state.view = view;
    state.groups = buildGroups(view);
    state.items = state.groups.flatMap((group) => group.items);
    state.itemByKey = new Map();
    state.groupOf = new Map();
    for (const group of state.groups) {
      for (const item of group.items) {
        state.itemByKey.set(keyOf(item), item);
        state.groupOf.set(keyOf(item), group.key);
      }
    }
    const previous = state.selectedKey;
    const present = previous != null && state.itemByKey.has(previous);
    const keep = present && (reason === 'refresh' || state.userSelected);
    if (!keep) {
      state.selectedKey = defaultSelection(state.groups);
      state.userSelected = false;
    }
    if (state.notice && (reason === 'query' || noticeOutdated(state.notice))) state.notice = null;
    renderView({ preserveScroll: reason !== 'query' });
    if (reason === 'query' && view.mode === 'search') announceResults();
    if (state.pendingEnter && reason === 'query') {
      const { alternate } = state.pendingEnter;
      state.pendingEnter = null;
      openSelected(alternate);
    }
  }

  /** A row notice outlives a re-render only while its row is there and as online as it was. */
  function noticeOutdated(notice) {
    const item = state.itemByKey.get(notice.key);
    return !item || itemOnline(item) !== notice.online;
  }

  function renderView({ preserveScroll }) {
    const token = ++state.renderToken;
    if (!state.items.length) {
      el.results.replaceChildren();
      el.results.hidden = true;
      renderEmpty();
    } else {
      el.empty.hidden = true;
      el.empty.replaceChildren();
      state.emptySignature = '';
      el.results.hidden = false;
      const scrollTop = el.results.scrollTop;
      const fragment = document.createDocumentFragment();
      const pending = [];
      for (const group of state.groups) {
        const labelId = group.label ? `section-${group.key}` : null;
        const node = h('div', { class: 'group', role: 'group', 'aria-labelledby': labelId });
        if (group.label) node.append(h('div', { class: 'section', id: labelId }, group.label));
        fragment.append(node);
        for (const item of group.items) pending.push([node, item, group.key]);
      }
      const note = truncationNote();
      if (note) fragment.append(h('div', { class: 'results__note' }, note));
      // A new list shows its first screenful at once and the rest on the next frames, so a
      // keystroke never waits for 200 rows. Re-renders in place (refresh) stay synchronous
      // to keep the scroll position exact; so do lists whose selection lies further down.
      const selectedIndex = state.items.findIndex((item) => keyOf(item) === state.selectedKey);
      const chunked = !preserveScroll && selectedIndex < FIRST_CHUNK_ROWS;
      appendRows(chunked ? pending.splice(0, FIRST_CHUNK_ROWS) : pending.splice(0));
      el.results.replaceChildren(fragment);
      el.results.scrollTop = preserveScroll ? scrollTop : 0;
      ensureSelectedVisible();
      if (pending.length) appendRemainingRows(pending, token);
    }
    syncActiveDescendant();
    renderCount();
    renderFooter();
  }

  function appendRows(batch) {
    for (const [node, item, groupKey] of batch) node.append(renderRow(item, groupKey));
  }

  function appendRemainingRows(pending, token) {
    requestAnimationFrame(() => {
      if (token !== state.renderToken) return; // a newer render replaced this list
      appendRows(pending.splice(0, LATER_CHUNK_ROWS));
      if (pending.length) appendRemainingRows(pending, token);
      else syncActiveDescendant();
    });
  }

  function renderRow(item, groupKey) {
    const key = keyOf(item);
    const online = itemOnline(item);
    const file = item.kind === 'file';
    const visual = itemVisual(item);
    const row = h('div', {
      class: `row${online ? '' : ' row--offline'}${file ? ' row--file' : ''}`,
      role: 'option', id: rowId(key), 'aria-selected': String(key === state.selectedKey), dataset: { key },
    });
    row.append(h('div', { class: `tile tile--${visual.tone}`, 'aria-hidden': 'true' }, svgIcon(visual.icon)));

    const name = h('span', { class: 'row__name', title: item.name });
    for (const part of highlightParts(item.name, item.hl)) name.append(part.hl ? h('mark', null, part.text) : part.text);
    const title = h('div', { class: 'row__title' }, name);
    for (const tag of rowTags(item, groupKey)) {
      title.append(h('span', { class: tag.tone ? `tag tag--${tag.tone}` : 'tag' }, tag.text));
    }
    const body = h('div', { class: 'row__body' }, title, renderLocation(item));
    const notice = rowNotice(item, key, online);
    if (notice) body.append(notice);
    else if (!file && Array.isArray(item.subfolders) && item.subfolders.length) body.append(renderChips(item));
    row.append(body);

    const facts = [];
    if (item.size != null) facts.push(formatBytes(item.size));
    if (!file && item.file_count != null) facts.push(plural(item.file_count, 'fil', 'filer'));
    row.append(h('div', { class: 'row__meta' },
      h('div', { class: online ? 'row__when' : 'row__when row__when--off' },
        online ? modifiedText(item.mtime) : offlineSince(item.source)),
      h('div', { class: 'row__facts' }, facts.join(' · ') || '–')));
    return row;
  }

  function rowTags(item, groupKey) {
    const tags = [];
    if (item.kind === 'group') tags.push({ text: 'Gruppe' });
    if (item.kind === 'template') tags.push({ text: 'Skabelon' });
    if (item.is_seq && item.seq_count) tags.push({ text: plural(item.seq_count, 'billede', 'billeder') });
    if (groupKey === 'resolve') {
      if (item.match === 'name') {
        tags.push({ text: 'Muligt match', tone: 'warn' });
      } else {
        const clips = primaryClipCount(item);
        if (clips) tags.push({ text: `${formatInt(clips)} klip`, tone: 'accent' });
      }
    }
    return tags;
  }

  function renderLocation(item) {
    const badge = sourceBadge(item.source);
    const where = itemLocation(item);
    const line = h('div', { class: 'row__loc', title: item.path || '' },
      h('span', { class: 'badge' }, svgIcon(badge.icon), h('span', { class: 'badge__text' }, badge.text)));
    if (where.project) {
      line.append(h('span', { class: 'loc-sep', 'aria-hidden': 'true' }, '›'),
        h('span', { class: 'badge badge--project' }, svgIcon('project'),
          h('span', { class: 'badge__text' }, where.project.name)));
    }
    if (where.crumbs.length) {
      line.append(h('span', { class: 'loc-sep', 'aria-hidden': 'true' }, '›'),
        h('span', { class: 'crumbs' }, where.crumbs.join(' › ')));
    }
    return line;
  }

  function rowNotice(item, key, online) {
    const active = Boolean(state.notice && state.notice.key === key);
    if (!online) {
      const share = item.source && item.source.kind === 'share';
      const icon = folderGone(item.source) ? 'folder' : share ? 'net' : 'disk';
      return h('div', { class: active ? 'row__notice is-active' : 'row__notice' },
        svgIcon(active ? 'warn' : icon), h('span', null, offlineHint(item.source)));
    }
    if (active) return h('div', { class: 'row__notice is-active' }, svgIcon('warn'), h('span', null, state.notice.text));
    return null;
  }

  function renderChips(item) {
    const chips = h('div', { class: 'chips' });
    for (const name of item.subfolders.slice(0, 20)) {
      chips.append(h('button', {
        type: 'button', class: 'chip', tabindex: '-1', dataset: { chip: name },
        title: `Åbn ${joinWinPath(item.path, name)}`,
      }, name));
    }
    return chips;
  }

  function rerenderRow(key) {
    const old = rowElement(key);
    const item = state.itemByKey.get(key);
    if (old && item) old.replaceWith(renderRow(item, state.groupOf.get(key)));
  }

  function showNotice(key, text) {
    const previous = state.notice && state.notice.key;
    state.notice = { key, text, online: itemOnline(state.itemByKey.get(key)) };
    if (previous != null && previous !== key) rerenderRow(previous);
    rerenderRow(key);
    announce(text);
  }

  function clearNotice() {
    const key = state.notice && state.notice.key;
    state.notice = null;
    if (key != null) rerenderRow(key);
  }

  function truncationNote() {
    const response = state.view.mode === 'search' ? state.view.response : null;
    if (!response) return null;
    const shown = response.results.length;
    if (response.total > shown) {
      return `Viser de første ${formatInt(shown)} af ${formatInt(response.total)} – skriv mere for at indsnævre`;
    }
    if (response.truncated) return `Viser de første ${formatInt(shown)} – skriv mere for at indsnævre`;
    return null;
  }

  function renderCount() {
    const response = state.view.mode === 'search' ? state.view.response : null;
    setText(el.count, response && response.total ? plural(response.total, 'resultat', 'resultater') : '');
  }

  function select(key, { user = false } = {}) {
    if (user) state.userSelected = true;
    if (key === state.selectedKey) return;
    const before = rowElement(state.selectedKey);
    if (before) before.setAttribute('aria-selected', 'false');
    state.selectedKey = key;
    if (state.notice && state.notice.key !== key) clearNotice();
    const after = rowElement(key);
    if (after) after.setAttribute('aria-selected', 'true');
    syncActiveDescendant();
    ensureSelectedVisible();
    renderFooter();
  }

  function moveSelection(delta) {
    const count = state.items.length;
    if (!count) return;
    const index = state.items.findIndex((item) => keyOf(item) === state.selectedKey);
    const next = index < 0 ? (delta > 0 ? 0 : count - 1) : Math.max(0, Math.min(count - 1, index + delta));
    select(keyOf(state.items[next]), { user: true });
  }

  function pageSize() {
    return Math.max(1, Math.floor(el.results.clientHeight / 64) - 1);
  }

  function ensureSelectedVisible() {
    const row = rowElement(state.selectedKey);
    if (!row) return;
    if (state.items.length && keyOf(state.items[0]) === state.selectedKey) {
      el.results.scrollTop = 0;
      return;
    }
    row.scrollIntoView({ block: 'nearest' });
    const header = row.previousElementSibling; // first row of a group: show its heading too
    if (header && header.classList.contains('section')) header.scrollIntoView({ block: 'nearest' });
  }

  function syncActiveDescendant() {
    const row = rowElement(state.selectedKey);
    if (row) el.input.setAttribute('aria-activedescendant', row.id);
    else el.input.removeAttribute('aria-activedescendant');
  }

  function announce(text) {
    el.announcer.textContent = '';
    setTimeout(() => { el.announcer.textContent = text; }, 30);
  }

  function announceResults() {
    clearTimeout(state.announceTimer);
    state.announceTimer = setTimeout(() => {
      const view = state.view;
      if (view.mode !== 'search') return;
      const total = view.response.total || 0;
      announce(total ? plural(total, 'resultat', 'resultater') : `Ingen resultater for ${view.response.query}`);
    }, 700);
  }

  // ------------------------------------------------------------------------------ empty states

  function renderEmpty() {
    const view = state.view;
    let spec = null;
    if (view.mode === 'search') {
      const reasons = zeroResultReasons(view.response, state.status, {
        kind: state.kind, excludedOnline: excludedOnlineCount(),
      });
      spec = {
        icon: 'search', title: `Ingen resultater for ‘${view.response.query}’`, reasons,
        text: reasons.length ? null : 'Prøv et kortere ord eller en anden stavemåde.',
      };
    } else if (view.mode === 'error') {
      spec = { icon: 'warn', title: 'Kunne ikke hente resultater', text: view.error,
        button: { label: 'Prøv igen', run: () => runView('query') } };
    } else if (view.mode === 'recent' && state.status) {
      const indexing = state.status.initial_scan_done === false || visibleScans(state.status).length > 0;
      spec = indexing
        ? { icon: 'refresh', title: 'Projektsøg gennemgår dine placeringer',
          text: 'Du kan allerede søge – resultaterne dukker op, efterhånden som placeringerne bliver klar.' }
        : { icon: 'folder', title: 'Ingen projekter endnu',
          text: 'Projektmapper på dine diske og netværksdelinger vises her, når de er fundet.',
          button: { label: 'Vis placeringer', run: () => openSettings('placeringer') } };
    }
    if (!spec) {
      el.empty.hidden = true;
      el.empty.replaceChildren();
      state.emptySignature = '';
      return;
    }
    const reasons = spec.reasons || [];
    const signature = [spec.title, spec.text, ...reasons.map((r) => r.key)].join('\n');
    if (signature === state.emptySignature) {
      // Same reasons – only numbers changed (e.g. files scanned). Update text in place so a click
      // on a fix button is never lost to a re-render.
      reasons.forEach((reason, i) => {
        const text = el.empty.querySelectorAll('.reason__text')[i];
        if (text) setText(text, reason.text);
      });
      return;
    }
    state.emptySignature = signature;
    const list = reasons.length ? h('ul', { class: 'reasons' }, reasons.map((reason) => h('li', { class: 'reason' },
      svgIcon(reason.icon), h('span', { class: 'reason__text' }, reason.text),
      reason.action ? h('button', { type: 'button', class: 'link', onclick: () => runReason(reason.action) }, reason.label) : null)))
      : null;
    el.empty.replaceChildren(h('div', { class: 'empty__inner' },
      h('div', { class: 'empty__icon', 'aria-hidden': 'true' }, svgIcon(spec.icon)),
      h('h2', { class: 'empty__title' }, spec.title),
      spec.text ? h('p', { class: 'empty__text' }, spec.text) : null,
      list,
      spec.button ? h('button', { type: 'button', class: 'btn btn--secondary', onclick: spec.button.run }, spec.button.label) : null));
    el.empty.hidden = false;
  }

  function excludedOnlineCount() {
    if (state.sourcesLoaded) return state.sources.filter((s) => s.online && !s.included).length;
    return (state.status && state.status.sources_excluded) || 0;
  }

  function runReason(action) {
    if (action === 'all-kinds') setKind('all');
    else if (action === 'show-offline') setOnlineOnly(false);
    else if (action === 'all-sources') setSource(null);
    else if (action === 'clear-filters') clearFilters();
    else if (action === 'show-sources') {
      state.revealExcluded = true; // unfold "Ikke medtaget" where connected folders wait
      openSettings('placeringer');
    }
    focusSearch(true);
  }

  // ------------------------------------------------------------------------------ filters

  function setKind(kind) {
    if (!KINDS.includes(kind)) return;
    const changed = state.kind !== kind;
    state.kind = kind;
    renderFilters();
    if (changed && state.query.trim()) runView('query');
  }

  function setOnlineOnly(value) {
    state.onlineOnly = value;
    renderFilters();
    if (state.query.trim()) runView('query');
  }

  function setSource(id) {
    state.sourceId = id;
    renderFilters();
    if (state.query.trim()) runView('query');
  }

  /** Every filter off in one go (a single search). */
  function clearFilters() {
    state.kind = 'all';
    state.sourceId = null;
    if (effectiveOnlineOnly()) state.onlineOnly = false;
    renderFilters();
    if (state.query.trim()) runView('query');
  }

  function renderFilters() {
    for (const button of el.seg.querySelectorAll('[data-kind]')) {
      const checked = button.dataset.kind === state.kind;
      button.setAttribute('aria-checked', String(checked));
      button.tabIndex = checked ? 0 : -1;
    }
    el.onlineOnly.setAttribute('aria-checked', String(effectiveOnlineOnly()));
    renderLocationOptions();
  }

  function renderLocationOptions() {
    const dropdown = el.location;
    const groups = groupSources(state.sources.filter((s) => s.included || s.entry_count > 0), state.hosts)
      .filter((group) => group.sources.length)
      .map((group) => ({
        host: group.host,
        options: group.sources.map((s) => ({ value: String(s.id), label: s.online ? s.display_name : `${s.display_name} (offline)` })),
      }));
    const signature = JSON.stringify(groups);
    if (signature !== state.locationSignature && document.activeElement !== dropdown) {
      state.locationSignature = signature;
      dropdown.replaceChildren(h('option', { value: '' }, 'Alle placeringer'),
        ...groups.map((group) => h('optgroup', { label: group.host },
          group.options.map((option) => h('option', { value: option.value }, option.label)))));
    }
    const value = state.sourceId == null ? '' : String(state.sourceId);
    if ([...dropdown.options].some((option) => option.value === value)) {
      dropdown.value = value;
    } else if (state.sourcesLoaded && state.sourceId != null) {
      state.sourceId = null; // the chosen location is gone
      dropdown.value = '';
      if (state.query.trim()) scheduleQuery();
    }
  }

  // ------------------------------------------------------------------------------ opening & copying

  function openSelected(alternate) {
    if (state.searchTimer || (state.inflight && state.inflightReason === 'query')) {
      state.pendingEnter = { alternate }; // results for what was just typed are on their way
      return;
    }
    const item = selectedItem();
    if (item) openItem(item, state.selectedKey, alternate);
  }

  /**
   * Open a row. An offline row is refused with its hint, without /api/open (§12) – unless the
   * live source list says its location is online again: then fresh data is fetched first and
   * the row opened from that, so a disk back under another drive letter opens at its new path
   * (§15.1, XMC-1). `recheck: false` refuses at once (the data was just fetched).
   */
  function openItem(item, key, alternate, { recheck = true } = {}) {
    closeMenu();
    // Footer and menu actions hold the row of an older render: prefer the current one.
    const current = key != null ? state.itemByKey.get(key) : null;
    const target = current || item;
    if (!itemOnline(target)) {
      if (recheck && liveSourceOnline(target.source)) openWhenRefreshed(target, current ? key : null, alternate);
      else reportOpenProblem(key, offlineHint(target.source), target.path);
      return;
    }
    openPath(target.open_path || target.path, openAction(target, alternate), { key, name: target.name });
  }

  /** The source as the last GET /api/sources listed it (null before that, or when unknown). */
  function liveSource(id) {
    return state.sourcesLoaded && id != null ? state.sources.find((s) => s.id === id) || null : null;
  }

  function liveSourceOnline(source) {
    const live = source ? liveSource(source.id) : null;
    return Boolean(live && live.online);
  }

  /** Re-run the view and open the row from the fresh results (applyView → pendingOpen). */
  function openWhenRefreshed(item, key, alternate) {
    if (key == null || state.groupOf.get(key) === 'resolve') {
      openViaLiveSource(item, key, alternate); // the Resolve primary is not re-fetched by runView
      return;
    }
    // Consumed by the first view fetched from now on (runView); a query on its way counts too.
    const inflightQuery = Boolean(state.inflight && state.inflightReason === 'query');
    state.pendingOpen = { key, alternate, query: state.query,
      seq: state.requestSeq + (inflightQuery && !state.searchTimer ? 0 : 1) };
    if (!state.searchTimer && !inflightQuery) runView('refresh');
  }

  /**
   * A Resolve folder that looked offline, while the source list says its location is online:
   * the bridge re-maps a few seconds later, so fetch the current source list and open the folder
   * under the location's current path.
   */
  async function openViaLiveSource(item, key, alternate) {
    let live = null;
    try {
      applySources(await api.get('/api/sources'));
      live = liveSource(item.source && item.source.id);
    } catch {
      live = null;
    }
    if (!live || !live.online || !live.path || typeof item.rel_path !== 'string') {
      reportOpenProblem(key, offlineHint(item.source), item.path);
      return;
    }
    const path = item.rel_path ? joinWinPath(live.path, item.rel_path) : live.path;
    openPath(path, openAction(item, alternate), { key, name: item.name });
  }

  function reportOpenProblem(key, message, detail) {
    if (key != null && rowElement(key)) showNotice(key, message);
    else toast(message, 'warn', detail);
  }

  async function openPath(path, action, { key = null, name = '' } = {}) {
    if (state.opening || !path) return;
    state.opening = true;
    const slow = setTimeout(() => toast(`Åbner ‘${name || leafName(path)}’ …`, 'info'), 450);
    try {
      const result = await api.post('/api/open', { path, action });
      clearTimeout(slow);
      if (result && result.ok) {
        hideToast();
        if (key != null && state.notice && state.notice.key === key) clearNotice();
        if (state.settings && state.settings.hide_after_open === false) toast(OPENED[action], 'ok', path);
      } else {
        reportOpenProblem(key, (result && result.error) || 'Kunne ikke åbne', path);
      }
    } catch (err) {
      clearTimeout(slow);
      reportOpenProblem(key, err.message, path);
    } finally {
      state.opening = false;
    }
  }

  async function copyPath(item, unc) {
    const text = unc ? item.unc_path : item.path;
    if (!text) {
      toast('Ingen netværkssti – placeringen er ikke delt på netværket', 'warn');
      return;
    }
    const ok = await writeClipboard(text);
    toast(ok ? (unc ? 'Netværksstien er kopieret' : 'Stien er kopieret') : 'Kunne ikke kopiere stien',
      ok ? 'ok' : 'warn', ok ? text : null);
  }

  async function writeClipboard(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch {
      // Clipboard API refused (focus, permissions) – fall back to execCommand below.
    }
    const active = document.activeElement;
    const { selectionStart, selectionEnd } = el.input;
    const area = h('textarea', { class: 'sr-only', readonly: true, 'aria-hidden': 'true' });
    area.value = text;
    document.body.append(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    area.remove();
    if (active && typeof active.focus === 'function') active.focus({ preventScroll: true });
    if (active === el.input) el.input.setSelectionRange(selectionStart, selectionEnd);
    return ok;
  }

  // ------------------------------------------------------------------------------ toast & context menu

  function toast(message, tone = 'info', detail = null, ms = 2800) {
    clearTimeout(state.toastTimer);
    const iconName = tone === 'ok' ? 'check' : tone === 'warn' ? 'warn' : 'open';
    el.toast.dataset.tone = tone;
    el.toast.replaceChildren(svgIcon(iconName),
      h('div', { class: 'toast__body' }, h('div', null, message), detail ? h('span', { class: 'toast__detail' }, detail) : null));
    el.toast.hidden = false;
    el.toast.style.animation = 'none';
    void el.toast.offsetWidth; // restart the entry animation
    el.toast.style.animation = '';
    state.toastTimer = setTimeout(hideToast, ms);
  }

  function hideToast() {
    clearTimeout(state.toastTimer);
    el.toast.hidden = true;
  }

  function openMenu(x, y, item, key) {
    closeMenu();
    const primary = openAction(item, false);
    const alternate = openAction(item, true);
    const entries = [
      { icon: primary === 'folder' ? 'open' : 'folder', label: ACTION_LABELS[primary], keys: 'Enter', run: () => openItem(item, key, false) },
      { icon: alternate === 'file' ? 'file' : 'folder', label: ACTION_LABELS[alternate], keys: 'Ctrl+Enter', run: () => openItem(item, key, true) },
      null,
      { icon: 'copy', label: 'Kopiér sti', keys: 'Ctrl+C', run: () => copyPath(item, false) },
      { icon: 'net', label: 'Kopiér netværkssti', keys: 'Ctrl+Shift+C', run: () => copyPath(item, true), disabled: !item.unc_path },
    ];
    const menu = h('div', { class: 'menu', role: 'menu', 'aria-label': item.name });
    for (const entry of entries) {
      if (!entry) {
        menu.append(h('div', { class: 'menu__sep', role: 'separator' }));
        continue;
      }
      menu.append(h('button', {
        type: 'button', class: 'menu__item', role: 'menuitem', disabled: Boolean(entry.disabled),
        onclick: () => { closeMenu({ refocus: true }); entry.run(); },
      }, svgIcon(entry.icon), h('span', null, entry.label), h('span', { class: 'menu__keys' }, entry.keys)));
    }
    document.body.append(menu);
    const rect = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - rect.width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - rect.height - 8))}px`;
    state.menu = menu;
    menu.querySelector('.menu__item:not(:disabled)').focus();
  }

  function closeMenu({ refocus = false } = {}) {
    if (!state.menu) return;
    state.menu.remove();
    state.menu = null;
    if (refocus) focusSearch(true);
  }

  function handleMenuKey(event) {
    const items = [...state.menu.querySelectorAll('.menu__item:not(:disabled)')];
    const index = items.indexOf(document.activeElement);
    if (event.key === 'Escape' || event.key === 'Tab') {
      event.preventDefault();
      closeMenu({ refocus: true });
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      items[(index + step + items.length) % items.length].focus();
    }
  }

  // ------------------------------------------------------------------------------ status, footer

  function renderPill() {
    const model = statusPill(state.status, { offline: isDisconnected() });
    // The pill changes several times a second while scanning, so it is no live region;
    // only a problem appearing or clearing is announced.
    const before = el.pill.dataset.tone;
    if (before !== model.tone && (before === 'warn' || model.tone === 'warn') && state.status) announce(model.text);
    el.pill.dataset.tone = model.tone;
    setText(el.pillText, model.text);
    const s = state.status;
    el.pill.title = s
      ? `${sourcesSummary()} – klik for at se placeringerne`
      : 'Placeringer';
  }

  function sourcesSummary() {
    const s = state.status;
    if (!s) return '';
    const parts = [`${plural(s.sources_included_online ?? s.sources_online ?? 0, 'placering', 'placeringer')} online`];
    if (s.sources_offline) parts.push(`${formatInt(s.sources_offline)} offline`);
    if (s.sources_excluded) parts.push(`${formatInt(s.sources_excluded)} ikke medtaget`);
    if (s.entries != null) parts.push(`${formatInt(s.entries)} elementer i indekset`);
    return parts.join(' · ');
  }

  function renderFooter() {
    const actions = [];
    const item = state.settingsOpen ? null : selectedItem();
    if (state.settingsOpen) {
      actions.push({ keys: ['Esc'], label: 'Luk indstillinger', run: () => closeSettings() });
    } else if (item) {
      const key = state.selectedKey;
      actions.push({ keys: ['Enter'], label: ACTION_LABELS[openAction(item, false)], run: () => openItem(item, key, false) });
      actions.push({ keys: ['Ctrl', 'Enter'], label: ACTION_LABELS[openAction(item, true)], run: () => openItem(item, key, true) });
      actions.push({ keys: ['Ctrl', 'C'], label: 'Kopiér sti', run: () => copyPath(item, false), extra: 'action--copy' });
      if (item.unc_path) {
        actions.push({ keys: ['Ctrl', 'Shift', 'C'], label: 'Kopiér netværkssti', run: () => copyPath(item, true), extra: 'action--copy' });
      }
    } else if (state.items.length) {
      actions.push({ keys: ['↑', '↓'], label: 'Vælg' });
    }
    el.actions.replaceChildren(...actions.map((action) => h('button', {
      type: 'button', class: action.extra ? `action ${action.extra}` : 'action', tabindex: '-1',
      disabled: !action.run, onclick: action.run,
    }, h('span', { class: 'action__keys' }, action.keys.map((k) => h('kbd', null, k))), action.label)));
    setText(el.hotkeyHint, footerHint(state.hotkey, { settingsOpen: state.settingsOpen, hasQuery: Boolean(el.input.value) }));
  }

  // ------------------------------------------------------------------------------ DaVinci Resolve

  function setResolve(rs) {
    const before = state.resolve;
    if (JSON.stringify(before) === JSON.stringify(rs || null)) return; // nothing new: keep the bar still
    state.resolve = rs || null;
    renderResolve();
    renderSettingControls();
    if (state.view.mode !== 'recent') return;
    const id = (value) => (value && value.primary ? `${value.primary.id}:${value.primary.match}` : '');
    const changed = id(before) !== id(state.resolve) || resolveProjectOf(before) !== resolveProject();
    applyView(state.view, changed ? 'resolve' : 'refresh');
  }

  function resolveProjectOf(rs) {
    return rs && rs.enabled && rs.connected ? rs.project || null : null;
  }

  function hasResolveDetails(rs) {
    return Boolean((rs.folders || []).length || (rs.other_dirs || []).length || (rs.suggestions || []).length);
  }

  function renderResolve() {
    const rs = state.resolve;
    const node = el.resolve;
    if (!rs || !rs.enabled || !rs.running) {
      node.hidden = true;
      node.replaceChildren();
      return;
    }
    const focused = node.contains(document.activeElement) ? document.activeElement.dataset.resolve : null;
    const text = h('div', { class: 'resolve__text' }, h('span', { class: 'resolve__label' }, 'DaVinci Resolve:'));
    const actions = h('div', { class: 'resolve__actions' });
    const primary = rs.connected ? rs.primary : null;
    if (!rs.connected) {
      text.append(rs.error
        ? h('span', { class: 'resolve__error' }, rs.error)
        : h('span', { class: 'resolve__muted' }, 'forbinder …'));
    } else {
      text.append(h('span', { class: 'resolve__project', title: rs.database ? `${rs.project} · ${rs.database}` : rs.project || '' },
        rs.project || 'Intet projekt åbent'));
      if (primary) {
        text.append(svgIcon('arrow-right', 'resolve__arrow'),
          h('span', { class: 'resolve__target', title: primary.path || '' }, svgIcon('folder'),
            h('span', { class: 'resolve__folder' }, primary.name),
            primary.source ? h('span', { class: 'resolve__src' }, `(${primary.source.name})`) : null));
        if (primary.match === 'name') text.append(h('span', { class: 'tag tag--warn' }, 'Muligt match'));
        const offline = primary.source && primary.source.online === false;
        actions.append(h('button', {
          type: 'button', class: 'btn btn--primary btn--sm', dataset: { resolve: 'open' },
          title: offline ? offlineHint(primary.source) : primary.path,
        }, svgIcon('open'), 'Åbn mappe'));
      } else if (rs.project) {
        text.append(h('span', { class: 'resolve__muted' },
          rs.clip_count ? '– ingen projektmappe fundet' : '– ingen klip i projektet'));
      }
    }
    actions.append(h('button', {
      type: 'button', class: state.resolveBusy ? 'btn btn--ghost btn--sm is-busy' : 'btn btn--ghost btn--sm',
      dataset: { resolve: 'refresh' }, disabled: state.resolveBusy, title: 'Læs projektet i Resolve igen',
    }, svgIcon('refresh'), h('span', { class: 'resolve__refresh-label' }, 'Opdater')));
    const details = rs.connected && hasResolveDetails(rs);
    if (details) {
      actions.append(h('button', {
        type: 'button', class: 'btn btn--ghost btn--sm resolve__toggle', dataset: { resolve: 'toggle' },
        'aria-expanded': String(state.resolveExpanded), 'aria-controls': 'resolve-details',
        title: state.resolveExpanded ? 'Skjul detaljer' : 'Vis hvor klippene ligger',
      }, rs.clip_count ? `${formatInt(rs.clip_count)} klip` : 'Detaljer', svgIcon('chevron-down')));
    }
    const parts = [h('div', { class: 'resolve__main' },
      h('span', { class: 'resolve__logo', 'aria-hidden': 'true' }, svgIcon('resolve')), text, actions)];
    for (const warning of resolveOfflineWarnings(rs)) {
      parts.push(h('div', { class: 'resolve__warn' }, svgIcon('warn'), h('span', null, warning)));
    }
    if (details && state.resolveExpanded) parts.push(renderResolveDetails(rs));
    node.replaceChildren(...parts);
    node.hidden = false;
    if (focused) {
      const target = node.querySelector(`[data-resolve="${focused}"]`);
      if (target && !target.disabled) target.focus();
      else focusSearch(true);
    }
  }

  function renderResolveDetails(rs) {
    const box = h('div', { class: state.resolveAnimate ? 'resolve__details is-entering' : 'resolve__details', id: 'resolve-details' });
    state.resolveAnimate = false;
    const folders = rs.folders || [];
    const others = rs.other_dirs || [];
    const suggestions = rs.suggestions || [];
    if (folders.length) {
      box.append(h('div', { class: 'rd__section' }, 'Mapper med klip fra projektet'));
      folders.forEach((f, i) => box.append(resolveRow('folders', i, f.project ? f.project.name : leafName(f.item && f.item.path),
        f.source, f.online !== false, `${formatInt(f.count || 0)} klip`)));
    }
    if (others.length) {
      box.append(h('div', { class: 'rd__section' }, 'Andre mapper'));
      others.forEach((d, i) => box.append(resolveRow('other', i, d.path, null, d.online !== false, `${formatInt(d.count || 0)} klip`)));
    }
    if (suggestions.length) {
      box.append(h('div', { class: 'rd__section' }, 'Muligt match efter projektnavnet'));
      suggestions.forEach((s, i) => box.append(resolveRow('suggestions', i, s.project ? s.project.name : '–',
        s.source, s.online !== false, `${Math.round((s.score || 0) * 100)} % match`)));
    }
    const foot = [];
    if (rs.database) foot.push(`Database: ${rs.database}`);
    if (rs.updated) foot.push(`læst ${relativeTime(rs.updated)}`);
    if (foot.length) box.append(h('div', { class: 'rd__foot' }, foot.join(' · ')));
    return box;
  }

  function resolveRow(list, index, name, source, online, count) {
    return h('button', {
      type: 'button', class: online ? 'rd__row' : 'rd__row is-offline',
      dataset: { resolve: 'folder', list, index: String(index) },
      title: online ? 'Åbn mappen' : source ? offlineHint(source) : 'Placeringen er ikke tilgængelig',
    },
    svgIcon(list === 'other' ? 'folder' : 'project'),
    h('span', { class: 'rd__main' }, h('span', { class: 'rd__name' }, name),
      source ? h('span', { class: 'rd__where' }, sourceBadge(source).text) : null,
      online ? null : h('span', { class: 'tag tag--warn' }, 'Offline')),
    h('span', { class: 'rd__count' }, count));
  }

  function openResolvePrimary() {
    const primary = resolvePrimary();
    if (!primary) return;
    const key = keyOf(primary);
    openItem(primary, rowElement(key) ? key : null, false);
  }

  function openResolveEntry(list, index) {
    const rs = state.resolve;
    if (!rs) return;
    if (list === 'other') {
      const dir = (rs.other_dirs || [])[index];
      if (!dir) return;
      if (dir.online === false) toast('Placeringen er ikke tilgængelig lige nu', 'warn', dir.path);
      else openPath(dir.path, 'folder', { name: leafName(dir.path) });
      return;
    }
    const entry = (list === 'folders' ? rs.folders : rs.suggestions || [])[index];
    if (!entry) return;
    const path = entry.item ? entry.item.open_path || entry.item.path : entry.project && entry.project.path;
    const name = entry.project ? entry.project.name : leafName(path);
    if (entry.online === false && liveSourceOnline(entry.source)) {
      // The folder's location is online again, the bridge just has not re-mapped yet (XMC-1).
      const rel = entry.item ? entry.item.rel_path : entry.project && entry.project.rel_path;
      openViaLiveSource({ kind: 'dir', name, path, rel_path: rel, source: entry.source }, null, false);
    } else if (entry.online === false) {
      toast(offlineHint(entry.source), 'warn', path);
    } else if (path) {
      openPath(path, 'folder', { name });
    }
  }

  async function refreshResolve() {
    if (state.resolveBusy) return;
    state.resolveBusy = true;
    renderResolve();
    try {
      const rs = await api.post('/api/resolve/refresh');
      state.resolveBusy = false;
      setResolve(rs);
    } catch (err) {
      toast(err.message, 'warn');
    } finally {
      state.resolveBusy = false;
      renderResolve();
    }
  }

  // ------------------------------------------------------------------------------ cards

  function addCard(card) {
    if (state.cards.some((c) => c.id === card.id)) return;
    state.cards.push(card);
    renderCards();
  }

  function removeCard(id) {
    const count = state.cards.length;
    state.cards = state.cards.filter((card) => card.id !== id);
    if (state.cards.length !== count) renderCards();
  }

  function renderCards() {
    const active = document.activeElement;
    const focused = active && el.cards.contains(active) ? active.dataset.cardFocus : null;
    el.cards.replaceChildren(...state.cards.map(renderCard));
    if (focused) {
      const target = el.cards.querySelector(`[data-card-focus="${CSS.escape(focused)}"]`);
      if (target && !target.disabled) target.focus();
      else focusSearch(true);
    }
  }

  function renderCard(card) {
    const close = (label) => h('button', {
      type: 'button', class: 'btn btn--ghost btn--icon btn--sm card__close', title: label, 'aria-label': label,
      dataset: { cardFocus: `${card.id}:close` }, onclick: () => dismissCard(card),
    }, svgIcon('close'));
    if (card.type === 'resolve-question') {
      return h('div', { class: 'card', role: 'region', 'aria-label': 'DaVinci Resolve og genvejstasten' },
        h('div', { class: 'card__icon', 'aria-hidden': 'true' }, svgIcon('resolve')),
        h('div', null,
          h('p', { class: 'card__title' }, 'DaVinci Resolve bruger selv Shift+Mellemrum til effektsøgning.'),
          h('p', { class: 'card__text' }, 'Hvad skal genvejen gøre, når Resolve er aktiv?'),
          h('div', { class: 'card__actions' },
            h('button', { type: 'button', class: 'btn btn--primary btn--sm', dataset: { cardFocus: 'ask:open' },
              onclick: () => answerResolveQuestion(false) }, 'Åbn Projektsøg'),
            h('button', { type: 'button', class: 'btn btn--secondary btn--sm', dataset: { cardFocus: 'ask:keep' },
              onclick: () => answerResolveQuestion(true) }, 'Lad Resolve beholde den (tryk to gange hurtigt for Projektsøg)'))),
        close('Spørg igen senere'));
    }
    const disk = card.data;
    const drive = disk.drive ? ` (${disk.drive})` : '';
    // Only folders (sources) can be included: a disk without any offers nothing to click (XMC-3).
    const informational = !card.done && !(disk.source_ids || []).length;
    let body;
    if (card.done) {
      body = h('p', { class: 'card__text' }, card.outcome || 'Medtaget i søgningen – disken bliver scannet nu.');
    } else if (informational) {
      body = h('p', { class: 'card__text' }, 'Ingen mapper at medtage endnu – nye projektmapper på disken findes automatisk.');
    } else {
      body = h('div', { class: 'card__actions' },
        h('button', { type: 'button', class: 'btn btn--primary btn--sm', disabled: card.busy,
          dataset: { cardFocus: `${card.id}:include` }, onclick: () => includeDisk(card) }, card.busy ? 'Medtager …' : 'Medtag'),
        h('button', { type: 'button', class: 'btn btn--secondary btn--sm', dataset: { cardFocus: `${card.id}:dismiss` },
          onclick: () => dismissCard(card) }, 'Luk'));
    }
    return h('div', { class: 'card', role: 'region', 'aria-label': `Ny disk ${disk.disk_name}` },
      h('div', { class: 'card__icon', 'aria-hidden': 'true' }, svgIcon('disk')),
      h('div', null,
        h('p', { class: 'card__title' }, `Ny disk ‘${disk.disk_name}’${drive} tilsluttet – ${disk.reason}`),
        body,
        card.error ? h('p', { class: 'card__error', role: 'alert' }, card.error) : null),
      card.done || informational ? close('Luk') : null);
  }

  function dismissCard(card) {
    if (card.type === 'resolve-question') state.askDismissed = true;
    removeCard(card.id);
    focusSearch(true);
  }

  function onNewVolume(data) {
    if (!data) return;
    const ids = data.source_ids || [];
    const id = `disk:${ids.join(',')}:${data.drive || ''}:${data.disk_name || ''}`;
    addCard({ id, type: 'disk', data, busy: false, done: Boolean(data.included), outcome: null, error: null });
    if (data.included || !ids.length) setTimeout(() => removeCard(id), INCLUDED_DISK_CARD_MS);
  }

  /** [Medtag]: include every folder of the disk; the card reports what actually happened. */
  async function includeDisk(card) {
    const ids = card.data.source_ids || [];
    if (card.busy || !ids.length) return;
    card.busy = true;
    card.error = null;
    renderCards();
    const included = [];
    for (const id of ids) {
      try {
        const source = await api.post(`/api/sources/${id}/mode`, { mode: 'include' });
        replaceSource(source);
        if (source && source.included !== false) included.push(source);
      } catch (err) {
        card.error = card.error || err.message;
      }
    }
    card.busy = false;
    if (included.length) {
      card.done = true;
      card.outcome = includeOutcome(included, ids.length);
      setTimeout(() => removeCard(card.id), card.error ? INCLUDED_DISK_CARD_MS : 4000);
    } else if (!card.error) {
      card.error = 'Disken kunne ikke medtages – prøv under Indstillinger ▸ Placeringer';
    }
    renderCards();
  }

  function onFocusEvent(data) {
    api.get('/api/resolve').then(setResolve, () => {});
    // Beyond SPEC §3.1: the tray's "Indstillinger …" shows the window with {"panel": "settings"}
    // (Controller.show_window(panel=...)) – the app window itself cannot be navigated.
    if (data && data.panel === 'settings') openSettings();
    const spec = state.hotkey && state.hotkey.spec;
    if (!state.askDismissed && shouldAskResolveHotkey(data, state.settings, spec)) {
      addCard({ id: 'resolve-question', type: 'resolve-question' });
    }
  }

  async function answerResolveQuestion(keepForResolve) {
    const apps = withResolvePassthrough(state.settings ? state.settings.hotkey_passthrough_apps : [], keepForResolve);
    if (await saveSettings({ hotkey_passthrough_apps: apps, resolve_hotkey_asked: true })) {
      removeCard('resolve-question');
      toast(keepForResolve
        ? 'Resolve beholder Shift+Mellemrum – tryk to gange hurtigt for Projektsøg'
        : 'Shift+Mellemrum åbner Projektsøg – også i Resolve', 'ok');
      focusSearch(true);
    }
  }

  // ------------------------------------------------------------------------------ settings panel

  function openSettings(tab) {
    if (tab) state.settingsTab = tab;
    if (!state.settingsOpen) {
      state.settingsOpen = true;
      el.settings.hidden = false;
      el.settingsButton.setAttribute('aria-expanded', 'true');
      coverStage(true);
      closeMenu();
    }
    showTab(state.settingsTab, { focus: true });
    loadSources();
    loadSettings();
    renderFooter();
  }

  function closeSettings({ focus = true } = {}) {
    if (!state.settingsOpen) return;
    state.settingsOpen = false;
    state.confirmHost = null;
    state.hostRefusal = null;
    el.settings.hidden = true;
    el.settingsButton.setAttribute('aria-expanded', 'false');
    coverStage(false);
    hideFieldError(el.hostError, el.hostInput);
    hideFieldError(el.rootError, el.rootInput);
    hideFieldError(el.hotkeyError, el.hotkeyInput);
    if (focus) focusSearch(true);
    renderFooter();
  }

  function toggleSettings() {
    if (state.settingsOpen) closeSettings();
    else openSettings();
  }

  /** What the open settings cover (filters, Resolve bar, cards, list) is inert: Tab and Shift+Tab
   *  skip it, so no hidden button, select or row can act (UI2-2, R3-UI-2). */
  function coverStage(covered) {
    for (const node of el.settings.parentElement.children) {
      if (node !== el.settings) node.inert = covered;
    }
  }

  function showTab(tab, { focus = false } = {}) {
    state.settingsTab = SETTINGS_TABS.includes(tab) ? tab : 'placeringer';
    for (const button of el.tabs) {
      const active = button.dataset.tab === state.settingsTab;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
      $(button.getAttribute('aria-controls')).hidden = !active;
      if (active && focus) button.focus();
    }
    if (state.settingsTab === 'placeringer') renderSourcesPanel();
    renderSettingControls();
  }

  const hostGroups = new Map();
  const sourceRows = new Map();

  /**
   * A focused control that the re-render removes or hides (a confirmed 'Fjern', 'Bekræft' on a
   * forgotten folder) leaves the focus on <body>, not on a focusable panel: the browser keeps the
   * Tab starting point where the control was, so Tab goes on to the next one and the list stays
   * put (R3-UI-1). onKeyDown keeps <body> away from the hidden result list (UI2-2).
   */
  function renderSourcesPanel() {
    const active = document.activeElement;
    setText(el.sourcesSummary, sourcesSummary());
    renderRoots();
    if (state.sourcesLoaded) renderHostGroups(active);
  }

  function renderHostGroups(active) { // `active`: a row moving between lists must keep its focus
    const scans = new Map(((state.status && state.status.scanning) || []).map((scan) => [scan.source_id, scan]));
    const configured = new Set(((state.settings && state.settings.hosts) || []).map((name) => name.toUpperCase()));
    const groupNodes = [];
    const seenSources = new Set();
    const seenHosts = new Set();
    for (const group of groupSources(state.sources, state.hosts)) {
      const hostKey = String(group.host).toUpperCase();
      seenHosts.add(hostKey);
      let view = hostGroups.get(hostKey);
      if (!view) {
        view = createHostGroup(group.host);
        hostGroups.set(hostKey, view);
      }
      updateHostGroup(view, group, configured.has(hostKey));
      const rowNode = (source) => {
        seenSources.add(source.id);
        let row = sourceRows.get(source.id);
        if (!row) {
          row = createSourceRow(source.id);
          sourceRows.set(source.id, row);
        }
        updateSourceRow(row, source, scans.get(source.id));
        return row.node;
      };
      // Folders that are not searched sit folded away under "Ikke medtaget (N)", one click from
      // their mode select (known issue 2) – the list shows what is actually searched.
      const excluded = group.sources.filter((source) => !source.included);
      placeChildren(view.list, group.sources.filter((source) => source.included).map(rowNode));
      placeChildren(view.moreList, excluded.map(rowNode));
      view.more.hidden = !excluded.length;
      setText(view.moreLabel, `Ikke medtaget (${formatInt(excluded.length)})`);
      if (state.revealExcluded && excluded.some((source) => source.online)) view.more.open = true;
      view.empty.hidden = group.sources.length > 0;
      groupNodes.push(view.node);
    }
    state.revealExcluded = false;
    for (const [id, row] of sourceRows) {
      if (!seenSources.has(id)) {
        row.node.remove();
        sourceRows.delete(id);
      }
    }
    for (const [key, view] of hostGroups) {
      if (!seenHosts.has(key)) {
        view.node.remove();
        hostGroups.delete(key);
      }
    }
    placeChildren(el.sourceGroups, groupNodes);
    if (active && active !== document.activeElement && active.isConnected && el.settings.contains(active)) {
      const folded = active.closest('details.sg__more');
      if (folded) folded.open = true; // 'Medtag aldrig' chosen: follow the row into the group
      active.focus({ preventScroll: true });
    }
  }

  /** Order `parent`'s children like `nodes`, moving only misplaced ones (keeps focus and open selects). */
  function placeChildren(parent, nodes) {
    nodes.forEach((node, i) => {
      if (parent.children[i] !== node) parent.insertBefore(node, parent.children[i] || null);
    });
    while (parent.children.length > nodes.length) parent.lastElementChild.remove();
  }

  function createHostGroup(host) {
    const icon = svgIcon('net');
    const selfTag = h('span', { class: 'tag' }, 'Denne computer');
    const stateText = h('span', { class: 'sg__state' });
    const remove = h('button', { type: 'button', class: 'btn btn--ghost btn--sm', dataset: { removeHost: host },
      title: `Fjern ${host} fra listen` }, 'Fjern');
    const confirmText = h('span', { class: 'sg__confirm-text' });
    const cancel = h('button', { type: 'button', class: 'btn btn--ghost btn--sm', dataset: { cancelRemoveHost: host } }, 'Annuller');
    const accept = h('button', { type: 'button', class: 'btn btn--danger btn--sm', dataset: { confirmRemoveHost: host } }, 'Fjern');
    const confirm = h('div', { class: 'sg__confirm', role: 'group', 'aria-label': `Fjern ${host}` },
      svgIcon('warn'), confirmText, h('span', { class: 'sg__spacer' }), cancel, accept);
    const list = h('div', { class: 'sg__list' });
    const moreLabel = h('span', { class: 'sg__more-label' });
    const moreList = h('div', { class: 'sg__list' });
    const more = h('details', { class: 'sg__more' },
      h('summary', { class: 'sg__more-head' }, svgIcon('chevron-down', 'sg__more-chevron'), moreLabel,
        h('span', { class: 'sg__more-hint' }, 'Vælg ‘Medtag altid’ for at søge i en af dem')),
      moreList);
    const empty = h('div', { class: 'sg__empty' });
    const node = h('section', { class: 'sg', 'aria-label': host },
      h('div', { class: 'sg__head' }, icon, h('span', { class: 'sg__name' }, host), selfTag, stateText,
        h('span', { class: 'sg__spacer' }), remove),
      confirm, list, more, empty);
    return { node, icon, selfTag, stateText, remove, confirm, confirmText, cancel, accept, list, more, moreLabel,
      moreList, empty };
  }

  function updateHostGroup(view, group, removable) {
    const info = group.info;
    const self = Boolean(info && info.self);
    const online = self || !info || info.online !== false;
    setIcon(view.icon, self ? 'pc' : 'net');
    view.selfTag.hidden = !self;
    let text;
    if (!online) text = `Svarer ikke${info.last_seen ? ` – sidst set ${formatDay(info.last_seen)}` : ''}`;
    else if (self) text = plural(group.sources.length, 'placering', 'placeringer');
    else text = `Online · ${plural(info ? info.shares : group.sources.length, 'delt mappe', 'delte mapper')}`;
    setText(view.stateText, text);
    view.stateText.classList.toggle('is-off', !online);
    const canRemove = !self && removable;
    view.remove.hidden = !canRemove;
    const confirming = canRemove && state.confirmHost === String(group.host).toUpperCase();
    // The server refused (§15.12, e.g. an added folder lies on it): its reason replaces the question.
    const refusal = confirming && state.hostRefusal ? state.hostRefusal : null;
    view.confirm.hidden = !confirming;
    view.remove.disabled = confirming;
    view.accept.hidden = Boolean(refusal);
    setText(view.cancel, refusal ? 'OK' : 'Annuller');
    if (confirming) setText(view.confirmText, refusal || removeHostQuestion(group.host, hostShareCount(state.sources, group.host)));
    setText(view.empty, online ? 'Ingen delte mapper fundet.' : 'Computeren svarer ikke – dens delte mapper vises, når den er tændt.');
  }

  function createSourceRow(id) {
    const modeSelect = h('select', { 'aria-label': 'Medtag i søgningen', dataset: { sourceMode: String(id) } },
      h('option', { value: 'auto' }, 'Automatisk'),
      h('option', { value: 'include' }, 'Medtag altid'),
      h('option', { value: 'exclude' }, 'Medtag aldrig'));
    const row = {
      dot: h('span', { class: 'src__dot' }),
      name: h('span'),
      manualTag: h('span', { class: 'tag' }, 'Tilføjet manuelt'),
      path: h('div', { class: 'src__path' }),
      statsMain: h('div'),
      statsSub: h('div', { class: 'src__sub' }),
      scanMain: h('div'),
      scanSub: h('div', { class: 'src__sub' }),
      modeSelect,
      selectWrap: h('label', { class: 'select select--sm' }, modeSelect, svgIcon('chevron-down', 'select__chevron')),
      manual: h('span', { class: 'src__manual' }, 'Medtages altid'),
      reason: h('div', { class: 'src__reason' }),
      action: h('button', { type: 'button', class: 'btn btn--secondary btn--sm', dataset: { sourceAction: String(id) } }),
    };
    row.node = h('div', { class: 'src', dataset: { id: String(id) } },
      row.dot,
      h('div', { class: 'src__main' }, h('div', { class: 'src__name' }, row.name, row.manualTag), row.path),
      h('div', { class: 'src__stats' }, row.statsMain, row.statsSub),
      h('div', { class: 'src__scan' }, row.scanMain, row.scanSub),
      h('div', { class: 'src__mode' }, row.selectWrap, row.manual, row.reason),
      h('div', { class: 'src__actions' }, row.action));
    return row;
  }

  function updateSourceRow(row, source, scan) {
    const scanning = Boolean(scan) || Boolean(source.scanning);
    const dot = !source.online ? 'is-off' : !source.included ? 'is-excluded' : scanning ? 'is-scan' : '';
    row.node.classList.toggle('is-excluded', !source.included);
    row.dot.className = dot ? `src__dot ${dot}` : 'src__dot';
    row.dot.title = { 'is-off': 'Offline', 'is-excluded': 'Ikke medtaget', 'is-scan': 'Scanner' }[dot] || 'Online';
    setText(row.name, source.display_name);
    row.manualTag.hidden = !source.manual;
    let path = source.path || '';
    if (source.kind === 'local' && source.disk_name) path += ` · Disk: ${source.disk_name}`;
    setText(row.path, path);
    row.path.title = source.unc_path && source.unc_path !== source.path ? `${source.path}\n${source.unc_path}` : source.path || '';
    const indexed = source.included || source.entry_count > 0;
    setText(row.statsMain, indexed ? plural(source.file_count, 'fil', 'filer') : '–');
    setText(row.statsSub, indexed ? `${formatBytes(source.total_size)} · ${plural(source.project_count, 'projekt', 'projekter')}` : '');
    const scanState = sourceScanState(source, scan);
    setText(row.scanMain, scanState.main);
    row.scanMain.className = scanState.tone ? `is-${scanState.tone}` : '';
    setText(row.scanSub, scanState.sub);
    row.scanSub.title = scanState.tone === 'error' ? scanState.sub : '';
    row.selectWrap.hidden = Boolean(source.manual);
    row.manual.hidden = !source.manual;
    if (document.activeElement !== row.modeSelect) row.modeSelect.value = source.mode;
    const reason = source.manual ? '' : modeReason(source);
    setText(row.reason, reason);
    row.reason.title = reason;
    const action = row.action;
    if (!source.online) {
      const confirming = state.confirmForget === source.id;
      action.hidden = false;
      action.disabled = false;
      action.dataset.op = 'forget';
      action.className = confirming ? 'btn btn--danger btn--sm' : 'btn btn--secondary btn--sm';
      setText(action, confirming ? 'Bekræft' : 'Glem');
      action.title = confirming ? 'Klik igen for at glemme placeringen og dens indeks' : 'Glem placeringen og dens indeks';
    } else if (source.included) {
      action.hidden = false;
      action.disabled = scanning || Boolean(source.queued);
      action.dataset.op = 'scan';
      action.className = 'btn btn--secondary btn--sm';
      setText(action, scanning ? 'Scanner …' : source.queued ? 'I kø' : 'Scan nu');
      action.title = 'Scan placeringen nu';
    } else {
      action.hidden = true;
    }
  }

  function renderRoots() {
    const roots = (state.settings && state.settings.extra_roots) || [];
    const signature = roots.join('\n');
    if (el.rootList.dataset.signature === signature) return;
    el.rootList.dataset.signature = signature;
    el.rootList.replaceChildren(...roots.map((root) => h('li', null, h('span', { title: root }, root),
      h('button', { type: 'button', class: 'btn btn--ghost btn--sm', dataset: { removeRoot: root } }, 'Fjern'))));
  }

  function replaceSource(source) {
    if (!source || source.id == null) return;
    const index = state.sources.findIndex((s) => s.id === source.id);
    if (index >= 0) state.sources[index] = source;
    else state.sources.push(source);
    if (state.settingsOpen) renderSourcesPanel();
    renderLocationOptions();
  }

  async function setSourceMode(id, mode) {
    try {
      replaceSource(await api.post(`/api/sources/${id}/mode`, { mode }));
    } catch (err) {
      toast(err.message, 'warn');
      renderSourcesPanel();
    }
  }

  async function sourceAction(id, op) {
    const source = state.sources.find((s) => s.id === id);
    if (op === 'scan') {
      try {
        await api.post(`/api/sources/${id}/scan`, { full: false });
        if (source) replaceSource({ ...source, queued: true });
      } catch (err) {
        toast(err.message, 'warn');
      }
      return;
    }
    if (op !== 'forget') return;
    clearTimeout(state.confirmTimer);
    if (state.confirmForget !== id) {
      state.confirmForget = id;
      state.confirmTimer = setTimeout(() => { state.confirmForget = null; renderSourcesPanel(); }, 4000);
      renderSourcesPanel();
      return;
    }
    state.confirmForget = null;
    try {
      await api.post(`/api/sources/${id}/forget`);
      state.sources = state.sources.filter((s) => s.id !== id);
      renderSourcesPanel();
      renderLocationOptions();
      toast(`‘${source ? source.display_name : 'Placeringen'}’ er glemt`, 'ok');
    } catch (err) {
      toast(err.message, 'warn');
      renderSourcesPanel();
    }
  }

  async function submitHost(event) {
    event.preventDefault();
    const name = el.hostInput.value.trim();
    if (!name) return;
    try {
      await api.post('/api/hosts', { name });
      el.hostInput.value = '';
      hideFieldError(el.hostError, el.hostInput);
      toast(`${name.toUpperCase()} er tilføjet – delte mapper findes om lidt`, 'ok');
      loadSources();
    } catch (err) {
      showFieldError(el.hostError, err.message, el.hostInput);
    }
  }

  /** "Fjern" on a computer asks first: it also forgets the computer's shared folders (§15.8). */
  function askRemoveHost(name) {
    state.confirmHost = String(name).toUpperCase();
    state.hostRefusal = null;
    renderSourcesPanel();
    const view = hostGroups.get(state.confirmHost);
    if (view && !view.confirm.hidden) view.accept.focus({ preventScroll: true });
  }

  function cancelRemoveHost() {
    const view = hostGroups.get(state.confirmHost);
    state.confirmHost = null;
    state.hostRefusal = null;
    renderSourcesPanel();
    if (view && !view.remove.hidden) view.remove.focus({ preventScroll: true });
  }

  /**
   * The confirmed "Fjern": the question stays until the server has answered. Then the toast says
   * how many shared folders were really forgotten (§15.12 `forgotten`), or the server's reason
   * for refusing replaces the question (e.g. an added folder lies on the computer).
   */
  async function removeHost(name) {
    const host = String(name).toUpperCase();
    if (state.removingHost) return; // clicked again while the first request is on its way
    state.removingHost = host;
    try {
      const result = await api.delete('/api/hosts', { name });
      if (state.confirmHost === host) state.confirmHost = null;
      renderSourcesPanel(); // the question goes – Tab goes on from where it was (R3-UI-1)
      toast(removedHostText(name, result && result.forgotten), 'ok');
      loadSources();
    } catch (err) {
      showHostRefusal(host, err.message);
    } finally {
      state.removingHost = null;
    }
  }

  function showHostRefusal(host, message) {
    const view = hostGroups.get(host);
    if (!state.settingsOpen || !view || view.remove.hidden) {
      toast(message, 'warn');
      return;
    }
    state.confirmHost = host;
    state.hostRefusal = message;
    renderSourcesPanel();
    announce(message);
    const active = document.activeElement; // the hidden [Fjern] – or <body> once it lost the focus
    if (!active || active === document.body || active === view.accept) {
      view.cancel.focus({ preventScroll: true });
    }
  }

  async function submitRoot(event) {
    event.preventDefault();
    const path = el.rootInput.value.trim();
    if (!path) return;
    try {
      const result = await api.post('/api/roots', { path });
      el.rootInput.value = '';
      hideFieldError(el.rootError, el.rootInput);
      if (result && result.source) replaceSource(result.source);
      toast('Mappen er tilføjet og bliver scannet', 'ok', path);
      loadSettings();
      loadSources();
    } catch (err) {
      showFieldError(el.rootError, err.message, el.rootInput);
    }
  }

  async function removeRoot(path) {
    try {
      await api.delete('/api/roots', { path });
      toast('Mappen er fjernet', 'ok', path);
      loadSettings();
      loadSources();
    } catch (err) {
      toast(err.message, 'warn');
    }
  }

  function renderSettingControls() {
    const s = state.settings;
    if (!s) return;
    for (const control of el.settings.querySelectorAll('[data-setting]')) {
      control.setAttribute('aria-checked', String(Boolean(s[control.dataset.setting])));
    }
    el.passthrough.setAttribute('aria-checked', String(hasResolvePassthrough(s.hotkey_passthrough_apps)));
    for (const choice of el.settings.querySelectorAll('[data-follow]')) {
      const checked = choice.dataset.follow === s.resolve_follow;
      choice.setAttribute('aria-checked', String(checked));
      choice.tabIndex = checked ? 0 : -1;
    }
    if (document.activeElement !== el.hotkeyInput && !state.hotkeyDirty) el.hotkeyInput.value = s.hotkey || '';
    const hk = state.hotkey;
    const label = (hk && hk.label) || 'Shift+Mellemrum';
    setText(el.passthroughLabel, `Lad DaVinci Resolve beholde ${label} (tryk to gange hurtigt for Projektsøg)`);
    let hotkeyText = 'Slået fra';
    if (s.hotkey_enabled) {
      hotkeyText = hk && hk.active === false
        ? 'Genvejstasten kunne ikke aktiveres – prøv en anden kombination'
        : `Aktiv: ${(hk && hk.label) || s.hotkey}`;
    }
    setText(el.hotkeyState, hotkeyText);
    el.hotkeyState.classList.toggle('is-warn', Boolean(s.hotkey_enabled && hk && hk.active === false));
    const status = state.status;
    setText(el.about, status ? `Projektsøg ${status.version || ''} · ${status.hostname || ''}`.trim() : '');
    setText(el.resolveConnection, resolveConnectionText(state.resolve, s));
  }

  function resolveConnectionText(rs, settings) {
    if (!settings.resolve_enabled) return 'Slået fra';
    if (!rs || !rs.running) return 'DaVinci Resolve kører ikke lige nu';
    if (rs.error) return rs.error;
    if (!rs.connected) return 'Forbinder til DaVinci Resolve …';
    const db = rs.database ? ` (${rs.database})` : '';
    return `Forbundet · ${rs.project || 'intet projekt åbent'}${db}`;
  }

  async function saveSettings(changes, { errorNode = null, input = null } = {}) {
    try {
      const data = await api.post('/api/settings', changes);
      if (data && data.settings) applySettings(data.settings);
      if (errorNode) hideFieldError(errorNode, input);
      return true;
    } catch (err) {
      if (errorNode) showFieldError(errorNode, err.message, input);
      else toast(err.message, 'warn');
      renderSettingControls();
      return false;
    }
  }

  function applySettings(settings) {
    if (!settings) return;
    const previous = state.settings;
    state.settings = settings;
    renderFilters();
    renderSettingControls();
    if (state.settingsOpen) renderSourcesPanel();
    if (settings.resolve_hotkey_asked) removeCard('resolve-question');
    const offlineChanged = previous && previous.show_offline !== settings.show_offline;
    if (offlineChanged && (state.onlineOnly === null || !state.query.trim())) runView('refresh');
  }

  async function submitHotkey(event) {
    event.preventDefault();
    const spec = el.hotkeyInput.value.trim().toLowerCase().replace(/\s+/g, '');
    if (!spec) {
      showFieldError(el.hotkeyError, 'Skriv en tastekombination, fx shift+space', el.hotkeyInput);
      return;
    }
    if (await saveSettings({ hotkey: spec }, { errorNode: el.hotkeyError, input: el.hotkeyInput })) {
      state.hotkeyDirty = false;
      renderSettingControls();
      toast('Genvejstasten er gemt', 'ok', spec);
    }
  }

  // ------------------------------------------------------------------------------ data loading & events

  async function loadStatus() {
    try {
      applyStatus(await api.get('/api/status'), true);
    } catch {
      // The pill shows the connection problem; SSE reconnects and resyncs.
    }
  }

  function applyStatus(data, full) {
    if (!data) return;
    const { resolve, hotkey, ...rest } = data;
    state.status = full ? rest : { ...(state.status || {}), ...rest };
    if (full && resolve !== undefined) setResolve(resolve);
    if (full && hotkey !== undefined) setHotkey(hotkey);
    statusChanged();
  }

  function statusChanged() {
    renderPill();
    if (state.settingsOpen && state.settingsTab === 'placeringer') renderSourcesPanel();
    if (!state.items.length && state.view.mode !== 'loading') renderEmpty();
  }

  function onScanProgress(data) {
    if (!state.status) return;
    const scans = [...(state.status.scanning || [])];
    const index = scans.findIndex((scan) => scan.source_id === data.source_id);
    if (index >= 0) scans[index] = { ...scans[index], ...data };
    else scans.push({ ...data });
    state.status = { ...state.status, scanning: scans };
    statusChanged();
  }

  function setHotkey(hotkey) {
    state.hotkey = hotkey || null;
    renderFooter();
    renderSettingControls();
  }

  async function loadSettings() {
    try {
      const data = await api.get('/api/settings');
      applySettings(data && data.settings);
    } catch {
      // Keep the last known settings.
    }
  }

  async function loadSources() {
    if (state.sourcesLoading) {
      state.sourcesReload = true;
      return;
    }
    state.sourcesLoading = true;
    state.sourcesDirty = false;
    state.sourcesLastLoad = Date.now();
    try {
      applySources(await api.get('/api/sources'));
    } catch {
      state.sourcesDirty = true;
    } finally {
      state.sourcesLoading = false;
      if (state.sourcesReload) {
        state.sourcesReload = false;
        loadSources();
      }
    }
  }

  function applySources(data) {
    state.sources = (data && data.sources) || [];
    state.hosts = (data && data.hosts) || [];
    state.sourcesLoaded = true;
    renderLocationOptions();
    if (state.settingsOpen) renderSourcesPanel();
    if (!state.items.length && state.view.mode === 'search') renderEmpty();
    if (rowsDisagreeWithSources()) {
      state.refreshPending = true; // e.g. a disk came back: re-run the view (XMC-1)
      scheduleRefresh();
    }
  }

  /** Rows (not the Resolve primary) whose location is now more or less online than they show,
   *  or mounted at another drive letter. */
  function rowsDisagreeWithSources() {
    return state.items.some((item) => {
      if (!item.source || state.groupOf.get(keyOf(item)) === 'resolve') return false;
      const live = liveSource(item.source.id);
      return Boolean(live) && (Boolean(live.online) !== itemOnline(item)
        || (live.drive || null) !== (item.source.drive || null));
    });
  }

  /**
   * `sources` event: re-GET soon while the settings are open or a listed row's location changed
   * (its online state decides whether Enter may open it), lazily (for the dropdown) otherwise.
   */
  function onSourcesChanged(data) {
    state.sourcesDirty = true;
    if (document.visibilityState === 'hidden') return; // loaded when the window is shown again
    const changed = new Set((data && data.changed) || []);
    const listed = state.items.some((item) => item.source && changed.has(item.source.id));
    const interval = state.settingsOpen ? SOURCES_THROTTLE_MS.open
      : listed ? SOURCES_THROTTLE_MS.listed : SOURCES_THROTTLE_MS.closed;
    const due = Math.max(Date.now(), state.sourcesLastLoad + interval);
    if (state.sourcesTimer && state.sourcesDue <= due) return;
    clearTimeout(state.sourcesTimer);
    state.sourcesDue = due;
    state.sourcesTimer = setTimeout(() => {
      state.sourcesTimer = 0;
      if (state.sourcesDirty) loadSources();
    }, due - Date.now());
  }

  /** `index_updated` – also sent when a location goes online/offline or moves (§15.1). */
  function onIndexUpdated(data) {
    const other = data && data.source_id !== state.sourceId;
    if (state.view.mode === 'search' && state.sourceId != null && other) return; // filtered away
    state.refreshPending = true;
    scheduleRefresh();
  }

  /** Re-run the view for index_updated: ≤ 1/s, and never within 1.5 s of a key press or list hover. */
  function scheduleRefresh() {
    if (state.refreshTimer) return;
    const now = Date.now();
    const wait = Math.max(0, state.lastInteraction + INTERACTION_QUIET_MS - now,
      state.lastRefresh + REFRESH_MIN_INTERVAL_MS - now);
    state.refreshTimer = setTimeout(() => {
      state.refreshTimer = 0;
      if (!state.refreshPending || document.visibilityState === 'hidden') return;
      if (Date.now() - state.lastInteraction < INTERACTION_QUIET_MS || state.inflight || state.searchTimer) {
        scheduleRefresh();
        return;
      }
      state.refreshPending = false;
      state.lastRefresh = Date.now();
      runView('refresh');
    }, wait);
  }

  const EVENT_HANDLERS = {
    status: (data) => applyStatus(data, false),
    scan_progress: onScanProgress,
    sources: onSourcesChanged,
    index_updated: onIndexUpdated,
    new_volume: onNewVolume,
    resolve: setResolve,
    focus: onFocusEvent,
    settings: applySettings,
    hotkey: setHotkey,
  };

  function connectEvents() {
    clearTimeout(state.sseTimer);
    let source;
    try {
      source = new EventSource('/api/events');
    } catch {
      scheduleReconnect();
      return;
    }
    state.eventSource = source;
    source.onopen = () => {
      state.sseAttempt = 0;
      setEventsDown(false);
      if (state.sseNeedsResync) {
        state.sseNeedsResync = false;
        resync();
      }
    };
    source.onerror = () => {
      if (state.eventSource !== source) return;
      source.close();
      state.eventSource = null;
      state.sseNeedsResync = true;
      setEventsDown(true);
      scheduleReconnect();
    };
    for (const [type, handler] of Object.entries(EVENT_HANDLERS)) {
      source.addEventListener(type, (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        handler(data);
      });
    }
  }

  function scheduleReconnect() {
    const base = SSE_BACKOFF_MS[Math.min(state.sseAttempt, SSE_BACKOFF_MS.length - 1)];
    state.sseAttempt += 1;
    state.sseTimer = setTimeout(connectEvents, base + Math.random() * base * 0.2);
  }

  /** After a reconnect: fetch everything that events may have carried in the meantime. */
  function resync() {
    loadStatus();
    loadSettings();
    loadSources();
    runView('refresh');
  }

  // ------------------------------------------------------------------------------ focus & query lifetime

  function focusSearch(force = false) {
    const active = document.activeElement;
    if (!force && state.settingsOpen && active && el.settings.contains(active) && active.matches('input, select')) return;
    if (active !== el.input) el.input.focus({ preventScroll: true });
  }

  function onPageHidden() {
    if (state.hiddenAt != null) return;
    state.hiddenAt = Date.now();
    state.valueAtHide = el.input.value;
    state.projectAtHide = resolveProject();
    closeMenu();
    hideToast();
  }

  /** Window shown again: focus at once; after ≥ 30 s (or another Resolve project) start afresh. */
  function onPageShown() {
    focusSearch(true);
    if (state.hiddenAt == null) return;
    const hiddenFor = Date.now() - state.hiddenAt;
    state.hiddenAt = null;
    if (state.sourcesDirty) loadSources(); // `sources` events while hidden only marked them stale
    const typed = typedSince(state.valueAtHide, el.input.value);
    if (hiddenFor >= QUERY_LIFETIME_MS || resolveProject() !== state.projectAtHide) {
      resetSession(typed || '');
      return;
    }
    if (typed !== null) {
      setQuery(typed); // typing after the hotkey replaces the kept (selected) query
      runView('query');
      return;
    }
    if (el.input.value) el.input.select();
    state.refreshPending = false;
    runView('refresh');
  }

  function resetSession(query) {
    setQuery(query);
    state.kind = 'all';
    state.onlineOnly = null;
    state.sourceId = null;
    state.userSelected = false;
    state.notice = null;
    state.refreshPending = false;
    closeSettings({ focus: false });
    closeMenu();
    renderFilters();
    renderFooter();
    runView('query');
  }

  // ------------------------------------------------------------------------------ keyboard

  function onKeyDown(event) {
    if (event.isComposing) return;
    state.lastInteraction = Date.now();
    if (state.menu) {
      handleMenuKey(event);
      return;
    }
    const { key, target } = event;
    const ctrl = event.ctrlKey || event.metaKey;
    if (key === 'Escape') {
      event.preventDefault();
      onEscape(event.repeat);
      return;
    }
    if (ctrl && !event.altKey) {
      if (key === ',') {
        event.preventDefault();
        toggleSettings();
        return;
      }
      if (!event.shiftKey && key >= '1' && key <= '4' && key.length === 1) {
        event.preventDefault();
        closeSettings({ focus: false });
        setKind(KINDS[Number(key) - 1]);
        focusSearch(true);
        return;
      }
      if (key === 'f' || key === 'F' || key === 'l' || key === 'L') {
        event.preventDefault();
        closeSettings({ focus: false });
        focusSearch(true);
        el.input.select();
        return;
      }
    }
    const inInput = target === el.input;
    const printable = key.length === 1 && key !== ' ' && !ctrl && !event.altKey;
    // The location filter is a select, but letters typed there are meant for the search too.
    const field = isTextField(target) && target !== el.location;
    if (printable && !inInput && !field && !el.settings.contains(target)) {
      focusSearch(true); // typing always goes to the search field, whatever button has focus
      return;
    }
    // The location list opened with the mouse and closed without a new choice keeps the focus;
    // keys that would change it (or open it again) still act on the results (UI2-1). Alt+↓, F4
    // and Space open it; reached with Tab it keeps all its keys.
    const handOver = target === el.location && state.locationPointer && !event.altKey && LOCATION_HANDOVER_KEYS.has(key);
    if (handOver) {
      event.preventDefault();
      focusSearch(true);
    }
    // While the settings cover the result list, no key acts on it: not from <body> (UI2-2), and
    // not from the search field either, where window focus and a re-show put the caret (§12,
    // R3-UI-2). Typing there still closes the settings ('input'), and Esc closes them first.
    const free = !state.settingsOpen && (inInput || handOver || target === document.body
      || el.results.contains(target) || el.actions.contains(target));
    if (!free) return; // buttons, selects and fields elsewhere keep their own keys
    switch (key) {
      case 'ArrowDown':
      case 'ArrowUp':
        event.preventDefault();
        moveSelection(key === 'ArrowDown' ? 1 : -1);
        return;
      case 'PageDown':
      case 'PageUp':
        event.preventDefault();
        moveSelection(key === 'PageDown' ? pageSize() : -pageSize());
        return;
      case 'Home':
      case 'End':
        if (ctrl && state.items.length) {
          event.preventDefault();
          select(keyOf(state.items[key === 'Home' ? 0 : state.items.length - 1]), { user: true });
        }
        return;
      case 'Enter':
        event.preventDefault();
        if (!event.repeat) openSelected(ctrl);
        return;
      default:
        break;
    }
    if (ctrl && !event.altKey && (key === 'c' || key === 'C')) {
      if (!event.shiftKey && inInput && el.input.selectionStart !== el.input.selectionEnd) return; // copy text
      const item = selectedItem();
      if (!item) return;
      event.preventDefault();
      copyPath(item, event.shiftKey);
    }
  }

  function isTextField(node) {
    if (node instanceof HTMLInputElement) return !['button', 'checkbox', 'radio', 'submit', 'reset'].includes(node.type);
    return node instanceof HTMLTextAreaElement || node instanceof HTMLSelectElement || Boolean(node && node.isContentEditable);
  }

  /** A file dropped on an app window would navigate away from the UI; text drops stay allowed. */
  function blockFileDrop(event) {
    if (!event.dataTransfer || !Array.from(event.dataTransfer.types).includes('Files')) return;
    event.preventDefault();
    if (event.type === 'dragover') event.dataTransfer.dropEffect = 'none';
  }

  function onEscape(repeat) {
    if (state.settingsOpen) {
      closeSettings();
      return;
    }
    if (el.input.value) {
      setQuery('');
      renderFooter();
      runView('query');
      focusSearch(true);
      return;
    }
    if (!repeat) api.post('/api/window/hide', { restore_previous: true }).catch(() => {});
  }

  function onRovingKeys(event, items, activate) {
    const index = items.indexOf(document.activeElement);
    if (index < 0) return;
    let next = null;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % items.length;
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + items.length) % items.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    items[next].focus();
    activate(items[next]);
  }

  // ------------------------------------------------------------------------------ wiring

  function wireEvents() {
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') onPageHidden();
      else onPageShown();
    });
    window.addEventListener('focus', () => {
      if (document.visibilityState === 'visible' && state.hiddenAt != null) onPageShown();
      else focusSearch(false);
    });
    window.addEventListener('blur', () => closeMenu());
    window.addEventListener('resize', () => closeMenu());
    document.addEventListener('mousedown', (event) => {
      if (state.menu && !state.menu.contains(event.target)) closeMenu();
    }, true);
    document.addEventListener('contextmenu', (event) => {
      if (!event.target.closest('input, textarea')) event.preventDefault();
    });
    document.addEventListener('dragover', blockFileDrop);
    document.addEventListener('drop', blockFileDrop);

    el.input.addEventListener('input', () => {
      state.query = el.input.value;
      el.clear.hidden = !el.input.value;
      closeSettings({ focus: false });
      renderFooter();
      scheduleQuery();
    });
    el.clear.addEventListener('mousedown', (event) => event.preventDefault());
    el.clear.addEventListener('click', () => {
      setQuery('');
      renderFooter();
      runView('query');
      focusSearch(true);
    });

    el.pill.addEventListener('click', () => openSettings('placeringer'));
    el.settingsButton.addEventListener('click', toggleSettings);
    el.settingsClose.addEventListener('click', () => closeSettings());

    // Filters, bar and card buttons used with the mouse leave the caret in the search field, so
    // ↑/↓, Enter and typing keep acting on the results (UI-1); Tab still reaches them. A click
    // (detail > 0; keyboard activation has 0) also pulls focus back from wherever it was.
    const keepCaret = (event) => {
      if (event.button === 0 && event.target.closest('button')) event.preventDefault();
    };
    for (const node of [el.seg, el.onlineOnly, el.resolve, el.cards, el.empty]) node.addEventListener('mousedown', keepCaret);
    const byPointer = (event) => {
      if (event.detail > 0) focusSearch(true);
    };
    el.seg.addEventListener('click', (event) => {
      const button = event.target.closest('[data-kind]');
      if (!button) return;
      setKind(button.dataset.kind);
      byPointer(event);
    });
    el.seg.addEventListener('keydown', (event) => onRovingKeys(event, [...el.seg.querySelectorAll('[data-kind]')],
      (button) => setKind(button.dataset.kind)));
    el.onlineOnly.addEventListener('click', (event) => {
      setOnlineOnly(!effectiveOnlineOnly());
      byPointer(event);
    });
    el.location.addEventListener('keydown', () => { state.locationKeyAt = Date.now(); });
    // Last used with the mouse (until it loses focus): see onKeyDown.
    el.location.addEventListener('pointerdown', () => { state.locationPointer = true; });
    el.location.addEventListener('blur', () => { state.locationPointer = false; });
    el.location.addEventListener('change', () => {
      setSource(el.location.value ? Number(el.location.value) : null);
      // Picked from the list: back to the search. Arrow keys on the closed list change it too
      // (a 'change' per key) – then the keyboard user keeps the list.
      if (Date.now() - state.locationKeyAt > LOCATION_KEY_MS) focusSearch(true);
    });

    const results = el.results;
    results.addEventListener('mousedown', (event) => {
      if (event.target.closest('.row')) event.preventDefault(); // keep the caret in the search field
    });
    const touched = () => { state.lastInteraction = Date.now(); };
    results.addEventListener('mousemove', touched);
    results.addEventListener('wheel', touched, { passive: true });
    results.addEventListener('click', (event) => {
      const row = event.target.closest('.row');
      if (!row) return;
      const key = row.dataset.key;
      select(key, { user: true });
      byPointer(event); // focus may still sit on a filter or bar button from the keyboard
      const chip = event.target.closest('.chip');
      const item = state.itemByKey.get(key);
      // detail > 1: second click of a habitual double-click – open the folder only once
      if (chip && item && event.detail < 2) {
        openPath(joinWinPath(item.path, chip.dataset.chip), 'folder', { key, name: chip.dataset.chip });
      }
    });
    results.addEventListener('dblclick', (event) => {
      const row = event.target.closest('.row');
      if (!row || event.target.closest('.chip')) return;
      const item = state.itemByKey.get(row.dataset.key);
      if (item) openItem(item, row.dataset.key, event.ctrlKey);
    });
    results.addEventListener('contextmenu', (event) => {
      const row = event.target.closest('.row');
      const item = row && state.itemByKey.get(row.dataset.key);
      if (!item) return;
      event.preventDefault();
      select(row.dataset.key, { user: true });
      openMenu(event.clientX, event.clientY, item, row.dataset.key);
    });
    el.actions.addEventListener('mousedown', (event) => event.preventDefault());

    el.resolve.addEventListener('click', (event) => {
      const button = event.target.closest('[data-resolve]');
      if (!button) return;
      byPointer(event);
      const op = button.dataset.resolve;
      if (op === 'open') openResolvePrimary();
      else if (op === 'refresh') refreshResolve();
      else if (op === 'toggle') {
        state.resolveExpanded = !state.resolveExpanded;
        state.resolveAnimate = state.resolveExpanded;
        renderResolve();
      } else if (op === 'folder') openResolveEntry(button.dataset.list, Number(button.dataset.index));
    });

    for (const tab of el.tabs) tab.addEventListener('click', () => showTab(tab.dataset.tab, { focus: true }));
    el.tabs[0].parentElement.addEventListener('keydown', (event) => onRovingKeys(event, el.tabs,
      (tab) => showTab(tab.dataset.tab)));

    el.settings.addEventListener('click', (event) => {
      const setting = event.target.closest('[data-setting]');
      if (setting) {
        const key = setting.dataset.setting;
        const next = !(state.settings && state.settings[key]);
        setting.setAttribute('aria-checked', String(next));
        saveSettings({ [key]: next });
        return;
      }
      const follow = event.target.closest('[data-follow]');
      if (follow) {
        saveSettings({ resolve_follow: follow.dataset.follow });
        return;
      }
      if (event.target.closest('#passthrough-switch')) {
        const keep = !hasResolvePassthrough(state.settings && state.settings.hotkey_passthrough_apps);
        el.passthrough.setAttribute('aria-checked', String(keep));
        saveSettings({
          hotkey_passthrough_apps: withResolvePassthrough(state.settings && state.settings.hotkey_passthrough_apps, keep),
          resolve_hotkey_asked: true,
        });
        return;
      }
      const sourceButton = event.target.closest('[data-source-action]');
      if (sourceButton) {
        sourceAction(Number(sourceButton.dataset.sourceAction), sourceButton.dataset.op);
        return;
      }
      const host = event.target.closest('[data-remove-host]');
      if (host) {
        askRemoveHost(host.dataset.removeHost);
        return;
      }
      const confirmHost = event.target.closest('[data-confirm-remove-host]');
      if (confirmHost) {
        removeHost(confirmHost.dataset.confirmRemoveHost);
        return;
      }
      if (event.target.closest('[data-cancel-remove-host]')) {
        cancelRemoveHost();
        return;
      }
      const root = event.target.closest('[data-remove-root]');
      if (root) removeRoot(root.dataset.removeRoot);
    });
    el.settings.querySelector('.choices').addEventListener('keydown', (event) => onRovingKeys(event,
      [...el.settings.querySelectorAll('[data-follow]')], (choice) => saveSettings({ resolve_follow: choice.dataset.follow })));
    el.sourceGroups.addEventListener('change', (event) => {
      const select = event.target.closest('select[data-source-mode]');
      if (select) setSourceMode(Number(select.dataset.sourceMode), select.value);
    });
    el.scanAll.addEventListener('click', async () => {
      try {
        await api.post('/api/scan', { full: false });
        toast('Alle placeringer bliver scannet', 'ok');
      } catch (err) {
        toast(err.message, 'warn');
      }
    });
    el.hostForm.addEventListener('submit', submitHost);
    el.rootForm.addEventListener('submit', submitRoot);
    el.hotkeyForm.addEventListener('submit', submitHotkey);
    el.hotkeyInput.addEventListener('input', () => {
      state.hotkeyDirty = true;
      hideFieldError(el.hotkeyError, el.hotkeyInput);
    });
    el.hostInput.addEventListener('input', () => hideFieldError(el.hostError, el.hostInput));
    el.rootInput.addEventListener('input', () => hideFieldError(el.rootError, el.rootInput));
  }

  function init() {
    wireEvents();
    const launch = parseLaunchParams(window.location.search);
    setQuery(launch.query);
    if (document.visibilityState === 'hidden') {
      state.hiddenAt = Date.now(); // pre-launched hidden: the first show starts a fresh session
      state.valueAtHide = el.input.value;
    }
    renderFilters();
    renderPill();
    renderFooter();
    focusSearch(true);
    state.firstPaint = loadStatus();
    runView('query');
    loadSettings();
    loadSources();
    connectEvents();
    if (launch.panel === 'settings') openSettings(launch.tab || 'placeringer');
  }

  init();
})();

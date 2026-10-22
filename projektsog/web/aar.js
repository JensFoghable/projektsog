/* Klippet <år> – the year in review (SPEC §23.11), opened in the default browser from Klippe.
 *
 * Plain script like app.js: the first half holds pure helpers (tests/ui/aar_helpers.test.js loads
 * them under Node), the second half drives the page – a story of full-height slides with big numbers
 * that count up, a colour per slide and segment bars that run on their own (held: paused). */
(() => {
  'use strict';

  // ===========================================================================================
  // Pure helpers
  // ===========================================================================================

  const LOCALE = 'da-DK';
  const NUMBER = new Intl.NumberFormat(LOCALE);
  const ONE_DECIMAL = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 1 });
  const DAY_MONTH = new Intl.DateTimeFormat(LOCALE, { day: 'numeric', month: 'long' });
  const FIRST_YEAR = 2026;                    // Klippe arrived on 30 September 2026

  const PAGES = {
    edit: ['Edit', '✂️'], cut: ['Cut', '🎬'], color: ['Color', '🎨'], fusion: ['Fusion', '✨'],
    fairlight: ['Fairlight', '🎚️'], deliver: ['Deliver', '📦'], media: ['Media', '🗂️'], photo: ['Photo', '📷'],
    musik: ['Musik og lyd', '🎧'], ai: ['AI-video og -billeder', '🤖'],
  };
  const STAGES = { egg: 'et æg', baby: 'Baby', junior: 'Junior', pro: 'Pro', legend: 'Legende' };
  const FOODS = {
    durum: ['Durum', '🌯'], bigmac: ['Big Mac', '🍔'], nuggets: ['Chicken McNuggets', '🐔'], pommes: ['Pommes frites', '🍟'],
    booster: ['Faxe Kondi Booster', '⚡'], mangoloco: ['Monster Mango Loco', '🥭'], drop: ['Booster-drop', '💉'],
  };
  // A big glyph in the corner of each slide (the pages and the food slide use their winner's).
  const DECO = { tom: '⏳', timer: '⏱️', top: '🎬', top5: '🏆', musik: '🎧', rytme: '📅', kort: '💾', leveret: '🎉',
    trofaeer: '🏅', kontor: '☕' };

  function num(value) {
    return NUMBER.format(Math.round(Number(value) || 0));
  }

  function plural(count, one, many) {
    return `${num(count)} ${Math.round(Number(count) || 0) === 1 ? one : many}`;
  }

  /** "311" from 10 hours up, "7,5" below (as the big number shows it). */
  function hours(value) {
    const h = Math.max(0, Number(value) || 0);
    return h >= 10 ? NUMBER.format(Math.round(h)) : ONE_DECIMAL.format(h);
  }

  /** "time" or "timer" after `hours(value)`. */
  function hoursUnit(value) {
    return hours(value) === '1' ? 'time' : 'timer';
  }

  /** "4,2 TB", "380 GB" – what the cards held. */
  function tbText(tb) {
    const value = Math.max(0, Number(tb) || 0);
    return value >= 1 ? `${ONE_DECIMAL.format(value)} TB` : `${NUMBER.format(Math.round(value * 1000))} GB`;
  }

  /** A local Date from "2026-10-08" (null when invalid). */
  function parseDay(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
    if (!m) return null;
    const day = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return day.getMonth() === Number(m[2]) - 1 ? day : null;
  }

  /** "8. oktober". */
  function longDay(iso) {
    const day = parseDay(iso);
    return day ? DAY_MONTH.format(day) : '';
  }

  /** "Siden 30. september" when the year started later for Klippe, else "Hele 2026" / "I år". */
  function sinceText(review, now = new Date()) {
    const r = review || {};
    const since = parseDay(r.siden);
    if (since && !(since.getMonth() === 0 && since.getDate() === 1)) return `Siden ${longDay(r.siden)}`;
    return Number(r.aar) < now.getFullYear() ? `Hele ${r.aar}` : 'I år';
  }

  /** "tirsdag" → "tirsdagen" (Du klipper helst om tirsdagen). */
  function weekdayDefinite(day) {
    const text = String(day || '').trim().toLowerCase();
    return text ? `${text}en` : '';
  }

  /** "Klippe" → "Klippes", "Klaus" → "Klaus'". */
  function genitive(name) {
    const text = String(name || 'Klippe');
    return /[sxz]$/i.test(text) ? `${text}'` : `${text}s`;
  }

  function pageInfo(side, name) {
    const known = PAGES[side];
    return { name: known ? known[0] : String(name || side || ''), emoji: known ? known[1] : '🎞️' };
  }

  /** `widget.html`'s look for Klippe: only plain slot and item names ever reach the page. */
  function cleanLook(equipped) {
    const out = {};
    if (equipped && typeof equipped === 'object') {
      for (const [slot, item] of Object.entries(equipped)) {
        if (/^[a-z]{1,16}$/.test(slot) && typeof item === 'string' && /^[a-z0-9-]{1,32}$/.test(item)) out[slot] = item;
      }
    }
    return out;
  }

  /** The year from `?aar=2026` (this year when missing or nonsense). */
  function yearFrom(search, now = new Date()) {
    const value = Number(new URLSearchParams(search || '').get('aar'));
    return Number.isInteger(value) && value >= FIRST_YEAR && value <= now.getFullYear() ? value : now.getFullYear();
  }

  /** Eased count-up: the value after `t` (0…1) of the way to `target`. */
  function countAt(target, t) {
    const u = Math.max(0, Math.min(1, t));
    return target * (1 - (1 - u) ** 3);
  }

  /** The number as the slide shows it: 'int' (1.234), 'hours' (311 / 7,5). */
  function formatCount(value, format) {
    return format === 'hours' ? hours(value) : num(value);
  }

  /** The story's slides for a review (`aar.year_review`): only the ones with something to tell. */
  function buildSlides(review, { name = null } = {}) {
    const r = review || {};
    const year = Number(r.aar) || new Date().getFullYear();
    const k = r.klippe || {};
    const pet = name || k.navn || 'Klippe';
    const slides = [{
      key: 'intro', theme: 'intro', kicker: `Klippet ${year}`, title: 'Dit år i Resolve',
      sub: `${sinceText(r)} – fortalt af ${pet}`, klippe: true, foot: 'Tryk for at starte',
    }];
    const outro = {
      key: 'outro', theme: 'outro', kicker: `Klippet ${year}`, title: 'Tak for et godt år! 🎬',
      sub: `${pet} glæder sig til ${year + 1}`, klippe: true, actions: true,
    };
    const total = Math.max(0, Number(r.timer) || 0);
    if (!total) {
      slides.push({ key: 'tom', theme: 'tid', kicker: 'Tiden', title: 'Ingen tid endnu',
        sub: `${pet} har ikke registreret noget i ${year} endnu – kom igen, når du har klippet lidt.` });
      slides.push(outro);
      return slides;
    }
    slides.push({
      key: 'timer', theme: 'tid', kicker: 'Tiden', lead: 'Du har klippet i',
      big: { value: total, format: 'hours', unit: hoursUnit(total) },
      foot: [r.dage > 0 && `fordelt på ${plural(r.dage, 'dag', 'dage')}`, r.projekter > 0 && plural(r.projekter, 'projekt', 'projekter')]
        .filter(Boolean).join(' · '),
    });
    const top = (Array.isArray(r.top) ? r.top : []).filter((p) => p && p.navn && p.timer > 0)
      .sort((a, b) => b.timer - a.timer);
    if (top.length) {
      const share = Math.round((100 * top[0].timer) / total);
      slides.push({
        key: 'top', theme: 'projekt', kicker: 'Årets projekt', lead: 'Du brugte mest tid på', title: top[0].navn,
        foot: `${hours(top[0].timer)} ${hoursUnit(top[0].timer)}${share >= 1 ? ` – ${share} % af året` : ''}`,
      });
    }
    if (top.length > 1) {
      slides.push({
        key: 'top5', theme: 'top', kicker: `Top ${Math.min(5, top.length)}`, lead: 'Dine største projekter',
        bars: top.slice(0, 5).map((p, i) => ({ rank: i + 1, label: p.navn, text: `${hours(p.timer)} t`, share: p.timer / top[0].timer })),
      });
    }
    const sider = (Array.isArray(r.sider) ? r.sider : []).filter((s) => s && s.timer > 0);
    const pages = sider.filter((s) => s.side !== 'musik' && s.side !== 'ai').sort((a, b) => b.timer - a.timer);
    if (pages.length) {
      const best = pageInfo(pages[0].side, pages[0].navn);
      slides.push({
        key: 'sider', theme: 'sider', kicker: 'Resolves sider', lead: `Mest af alt i ${best.name} ${best.emoji}`, deco: best.emoji,
        bars: pages.slice(0, 7).map((s) => {
          const page = pageInfo(s.side, s.navn);
          return { label: `${page.emoji} ${page.name}`, text: `${hours(s.timer)} t`, share: s.timer / pages[0].timer };
        }),
      });
    }
    const side = (key) => sider.filter((s) => s.side === key).reduce((sum, s) => sum + s.timer, 0);
    const music = side('musik');
    const ai = side('ai');
    if (music > 0 || ai > 0) {
      slides.push({
        key: 'musik', theme: 'musik', kicker: 'Musik og AI', lead: 'Uden for Resolve',
        tiles: [music > 0 && { emoji: '🎧', value: music, format: 'hours', label: `${hoursUnit(music)} på jagt efter den rigtige musik` },
          ai > 0 && { emoji: '🤖', value: ai, format: 'hours', label: `${hoursUnit(ai)} med AI-video og -billeder` }].filter(Boolean),
      });
    }
    const best = r.bedste_dag;
    if (best && best.dato && best.timer > 0) {
      slides.push({
        key: 'rytme', theme: 'rytme', kicker: 'Rytmen', lead: 'Din stærkeste dag', title: longDay(best.dato),
        sub: `${hours(best.timer)} ${hoursUnit(best.timer)} på én dag`,
        facts: [r.ugedag && `Du klipper helst om ${weekdayDefinite(r.ugedag)}`, r.tidligst && `Tidligste start: kl. ${r.tidligst}`,
          r.fokus_90 > 0 && `${num(r.fokus_90)} × 90 minutter i flow`, r.pauser > 0 && plural(r.pauser, 'god pause', 'gode pauser')]
          .filter(Boolean),
      });
    }
    const kort = r.kort || {};
    if (kort.antal > 0) {
      slides.push({
        key: 'kort', theme: 'kort', kicker: 'Kortene', lead: 'Du overførte', big: { value: kort.antal, format: 'int', unit: 'kort' },
        foot: [kort.tb > 0 && `${tbText(kort.tb)} kontrolleret`, kort.filer > 0 && plural(kort.filer, 'fil', 'filer'),
          kort.kamera && `mest fra ${kort.kamera}`].filter(Boolean).join(' · '),
      });
    }
    const deliveries = Math.max(0, Number(r.leveringer) || 0);
    const renders = Math.max(0, Number(r.renders) || 0);
    if (deliveries > 0) {
      slides.push({
        key: 'leveret', theme: 'leveret', kicker: 'Leveret', lead: 'Festkatten dansede', big: { value: deliveries, format: 'int',
          unit: deliveries === 1 ? 'gang 🎉' : 'gange 🎉' },
        foot: renders > 0 ? `– og ${plural(renders, 'render', 'renders')} blev færdige undervejs` : 'én fest for hver levering',
      });
    } else if (renders > 0) {
      slides.push({ key: 'leveret', theme: 'leveret', kicker: 'Renders', lead: 'Resolve renderede', big: { value: renders, format: 'int',
        unit: renders === 1 ? 'gang' : 'gange' }, foot: 'og robotterne fodrede maskinen' });
    }
    const mad = r.mad || {};
    if (mad.i_alt > 0) {
      const fav = mad.yndlings && mad.yndlings.antal > 0 ? mad.yndlings : null;
      const favInfo = fav ? FOODS[fav.id] || [fav.navn || fav.id, '😋'] : null;
      const eaten = Object.entries(mad.spist || {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]).slice(0, 5)
        .map(([id, n]) => ({ emoji: (FOODS[id] || [id, '😋'])[1], count: n }));
      slides.push({
        key: 'mad', theme: 'mad', kicker: `${genitive(pet)} mave`, lead: `${pet} spiste og drak`, deco: favInfo ? favInfo[1] : '🍔',
        big: { value: mad.i_alt, format: 'int', unit: 'ting' }, menu: eaten,
        foot: favInfo ? `Yndlingsretten: ${favInfo[0]} ${favInfo[1]} × ${num(fav.antal)}` : '',
      });
    }
    const trophies = Math.max(0, Number(k.trofaeer) || 0);
    const finds = Math.max(0, Number(k.fund) || 0);
    if (trophies > 0 || finds > 0) {
      slides.push({
        key: 'trofaeer', theme: 'trofaeer', kicker: 'Trofæer', lead: `${pet} vandt`,
        big: { value: trophies, format: 'int', unit: trophies === 1 ? 'nyt trofæ 🏆' : 'nye trofæer 🏆' },
        foot: [finds > 0 && plural(finds, 'sjældent fund 🎁', 'sjældne fund 🎁'),
          k.stage && `${pet} er nu ${STAGES[k.stage] || k.stage}${k.level ? ` · Lv ${k.level}` : ''}`].filter(Boolean).join(' · '),
      });
    }
    const o = r.kontor || {};
    const tile = (value, emoji, one, many, format = 'int') => (value > 0
      ? { emoji, value, format, label: Math.round(value) === 1 ? one : many } : null);
    const tiles = [
      tile(o.frokost_ringet, '🔔', 'gang ringede du til frokost', 'gange ringede du til frokost'),
      tile(o.post_sendt, '✉️', 'brev sendt med Klippe-post', 'breve sendt med Klippe-post'),
      tile(o.post_faaet, '📬', 'brev fra kollegerne', 'breve fra kollegerne'),
      tile(o.slik_givet, '🍬', 'Booster givet til Halloween', 'Boostere givet til Halloween'),
      tile(o.vaeddemaal_vundet, '🔮', 'render-væddemål vundet', 'render-væddemål vundet'),
      tile(o.spil_bedste, '🎮', 'point – din rekord i render-spillet', 'point – din rekord i render-spillet'),
      tile(o.sne, '❄️', 'snevejr på skærm 2', 'snevejr på skærm 2'),
    ].filter(Boolean);
    if (tiles.length) slides.push({ key: 'kontor', theme: 'kontor', kicker: 'Kontoret', lead: 'Sjov på kontoret', tiles });
    slides.push(outro);
    for (const slide of slides) slide.deco = slide.deco || DECO[slide.key] || null;
    return slides;
  }

  const helpers = { num, plural, hours, hoursUnit, tbText, parseDay, longDay, sinceText, weekdayDefinite, genitive,
    pageInfo, cleanLook, yearFrom, countAt, formatCount, buildSlides };
  if (typeof module === 'object' && module.exports) module.exports = helpers;
  if (typeof document === 'undefined') return;

  // ===========================================================================================
  // Page
  // ===========================================================================================

  const SLIDE_MS = 6000;
  const HOLD_MS = 220;                       // a press this long pauses the story instead of a tap
  const COUNT_MS = 1300;
  const REDUCED = Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  const $ = (id) => document.getElementById(id);
  const el = { story: $('story'), slides: $('slides'), bars: $('bars'), brand: $('brand'), pause: $('pause'),
    prev: $('prev'), next: $('next'), hint: $('hint'), backdrop: $('backdrop') };

  const state = { year: yearFrom(window.location.search), slides: [], index: 0, elapsed: 0, paused: REDUCED, held: false,
    last: 0, frame: 0, pressAt: 0, pressX: 0, pet: null, look: {}, klippe: null, review: null, shared: false };

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
      if (child == null || child === false || child === '') continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  async function getJson(path) {
    const response = await fetch(path, { cache: 'no-store' });
    let data = null;
    try {
      data = await response.json();
    } catch { /* no body */ }
    if (!response.ok) throw new Error((data && data.error) || `Projektsøg svarede ${response.status}`);
    return data;
  }

  async function postJson(path, body) {
    const response = await fetch(path, { method: 'POST', cache: 'no-store', body: JSON.stringify(body),
      headers: { 'Content-Type': 'application/json', 'X-Projektsog': '1' } });
    let data = null;
    try {
      data = await response.json();
    } catch { /* no body */ }
    if (!response.ok) throw new Error((data && data.error) || `Projektsøg svarede ${response.status}`);
    return data;
  }

  // ---------------------------------------------------------------- Klippe
  // The pet's own drawing and stylesheet (widget.html, widget.css) in a frame of its own – a srcdoc
  // frame, as Projektsøg's pages may not be framed – cheering in its wardrobe, swaying gently.

  async function loadKlippe() {
    try {
      const text = await (await fetch('/widget.html', { cache: 'no-store' })).text();
      const svg = new DOMParser().parseFromString(text, 'text/html').getElementById('pet');
      return svg ? svg.outerHTML : null;
    } catch {
      return null;
    }
  }

  function klippeFrame() {
    const box = h('div', { class: 'klippe', 'aria-hidden': 'true' });
    if (!state.klippe) {
      box.append(h('span', { class: 'klippe__stand-in' }, '🎬'));
      return box;
    }
    const stage = (state.review && state.review.klippe && state.review.klippe.stage) || 'baby';
    const data = { mood: 'chill', pose: 'cheer', stage: /^[a-z]+$/.test(stage) ? stage : 'baby', outfit: 'none', ...state.look };
    const attrs = Object.entries(data).map(([key, value]) => ` data-${key}="${value}"`).join('');
    const frame = h('iframe', { class: 'klippe__frame', title: 'Klippe', tabindex: '-1' });
    frame.srcdoc = '<!doctype html><html lang="da"><head><meta charset="utf-8"><meta name="color-scheme" content="dark">'
      + '<link rel="stylesheet" href="/widget.css"><style>html,body{margin:0;height:100%;overflow:hidden;'
      + 'background:transparent!important}.klippe{display:flex;align-items:flex-end;justify-content:center;height:100%}'
      + '.klippe .pet{width:100%;height:100%;max-width:none;max-height:none;cursor:default}'
      + '.klippe .pet__shadow{fill:rgba(0,0,0,.25)}</style></head>'
      + `<body><div class="klippe party"${attrs}>${state.klippe}</div></body></html>`;
    frame.addEventListener('load', () => {
      const slide = frame.closest('.slide');
      if (slide && slide.classList.contains('is-active')) cheer(slide);   // the intro: its frame loads last
    });
    box.append(frame);
    return box;
  }

  /** Klippe jumps and claps as its slide comes up. */
  function cheer(slide) {
    const frame = slide && slide.querySelector('.klippe__frame');
    const svg = frame && frame.contentDocument && frame.contentDocument.querySelector('svg');
    if (!svg || REDUCED) return;
    svg.classList.remove('jump', 'clap');
    void svg.getBoundingClientRect();
    svg.classList.add('jump', 'clap');
    setTimeout(() => svg.classList.remove('jump', 'clap'), 1000);
  }

  // ---------------------------------------------------------------- slides

  function bigNumber(big) {
    return h('div', { class: 'big' },
      h('span', { class: 'big__value', dataset: { count: String(big.value), format: big.format } }, formatCount(big.value, big.format)),
      big.unit ? h('span', { class: 'big__unit' }, big.unit) : null);
  }

  function renderSlide(model, index) {
    let n = 0;
    const part = (node) => {
      if (node) node.style.setProperty('--i', String((n += 1)));
      return node;
    };
    const slide = h('section', { class: 'slide', dataset: { theme: model.theme, key: model.key }, 'aria-hidden': 'true',
      'aria-label': `${index + 1} af ${state.slides.length}` },
    h('div', { class: 'slide__blob slide__blob--a', 'aria-hidden': 'true' }),
    h('div', { class: 'slide__blob slide__blob--b', 'aria-hidden': 'true' }),
    model.deco ? h('div', { class: 'slide__deco', 'aria-hidden': 'true' }, model.deco) : null);
    const body = h('div', { class: 'slide__body' });
    body.append(part(h('p', { class: 'kicker' }, model.kicker)));
    if (model.klippe) body.append(part(klippeFrame()));
    if (model.lead) body.append(part(h('p', { class: 'lead' }, model.lead)));
    if (model.big) body.append(part(bigNumber(model.big)));
    if (model.title) body.append(part(h('h2', { class: model.title.length > 28 ? 'title title--long' : 'title' }, model.title)));
    if (model.sub) body.append(part(h('p', { class: 'sub' }, model.sub)));
    if (model.bars) {
      body.append(part(h('ol', { class: 'bars-list' }, model.bars.map((bar, j) => h('li', { class: 'bar' },
        h('div', { class: 'bar__row' }, bar.rank ? h('span', { class: 'bar__rank' }, String(bar.rank)) : null,
          h('span', { class: 'bar__label', title: bar.label }, bar.label), h('span', { class: 'bar__text' }, bar.text)),
        h('div', { class: 'bar__track' }, h('div', { class: 'bar__fill',
          style: `--share: ${Math.max(0.02, Math.min(1, Number(bar.share) || 0))}; --j: ${j}` })))))));
    }
    if (model.tiles) {
      body.append(part(h('div', { class: `tiles tiles--${Math.min(model.tiles.length, 4)}` }, model.tiles.map((tile) => h('div', { class: 'tile' },
        h('span', { class: 'tile__emoji' }, tile.emoji),
        h('span', { class: 'tile__value', dataset: { count: String(tile.value), format: tile.format || 'int' } },
          formatCount(tile.value, tile.format)),
        h('span', { class: 'tile__label' }, tile.label))))));
    }
    if (model.menu && model.menu.length) {
      body.append(part(h('p', { class: 'menu' }, model.menu.map((dish) => h('span', { class: 'menu__dish' },
        h('span', { class: 'menu__emoji' }, dish.emoji), h('span', { class: 'menu__count' }, `× ${num(dish.count)}`))))));
    }
    if (model.facts && model.facts.length) {
      body.append(part(h('ul', { class: 'facts' }, model.facts.map((fact) => h('li', null, fact)))));
    }
    if (model.foot) body.append(part(h('p', { class: 'foot' }, model.foot)));
    if (model.actions) {
      body.append(part(h('div', { class: 'actions' },
        h('button', { type: 'button', class: 'btn btn--primary', id: 'share', onclick: share }, 'Del med kontoret'),
        h('button', { type: 'button', class: 'btn', id: 'again', onclick: () => go(0) }, 'Se igen'))));
      body.append(part(h('p', { class: 'share-note', id: 'share-note', role: 'status' })));
    }
    slide.append(body);
    return slide;
  }

  function render() {
    el.slides.replaceChildren(...state.slides.map((model, i) => renderSlide(model, i)));
    el.bars.replaceChildren(...state.slides.map(() => h('span', { class: 'seg' }, h('span', { class: 'seg__fill' }))));
    show(0, false);
  }

  /** A message instead of the story (loading, or what went wrong) – with a button to try again. */
  function renderNotice(title, text, retry = false) {
    state.slides = [];
    el.bars.replaceChildren();
    el.slides.replaceChildren(h('section', { class: 'slide is-active', dataset: { theme: 'intro' } },
      h('div', { class: 'slide__blob slide__blob--a', 'aria-hidden': 'true' }),
      h('div', { class: 'slide__body' }, h('p', { class: 'kicker' }, `Klippet ${state.year}`), h('h2', { class: 'title' }, title),
        text ? h('p', { class: 'sub' }, text) : null,
        retry ? h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn btn--primary', onclick: start }, 'Prøv igen')) : null)));
    el.story.dataset.theme = 'intro';
  }

  function show(index, animate = true) {
    const slides = [...el.slides.children];
    if (!slides.length) return;
    state.index = Math.max(0, Math.min(slides.length - 1, index));
    state.elapsed = 0;
    slides.forEach((slide, i) => {
      const active = i === state.index;
      slide.classList.toggle('is-active', active);
      slide.classList.toggle('is-past', i < state.index);
      slide.setAttribute('aria-hidden', String(!active));
      for (const button of slide.querySelectorAll('button')) button.tabIndex = active ? 0 : -1;
    });
    const current = slides[state.index];
    el.story.dataset.theme = current.dataset.theme;
    el.backdrop.dataset.theme = current.dataset.theme;
    [...el.bars.children].forEach((seg, i) => {
      seg.classList.toggle('is-done', i < state.index);
      seg.classList.toggle('is-now', i === state.index);
      seg.firstChild.style.transform = i < state.index ? 'scaleX(1)' : i > state.index ? 'scaleX(0)' : `scaleX(${REDUCED ? 1 : 0})`;
    });
    el.prev.disabled = state.index === 0;
    el.next.disabled = state.index === slides.length - 1;
    countUp(current, animate && !REDUCED);
    cheer(current);
    renderPause();
  }

  function go(index) {
    show(index);
  }

  function step(delta) {
    const count = el.slides.children.length;
    if (!count) return;
    const next = state.index + delta;
    if (next < 0 || next >= count) return;
    show(next);
  }

  /** The slide's numbers count up from 0 (shown at once with reduced motion). */
  function countUp(slide, animate) {
    const nodes = [...slide.querySelectorAll('[data-count]')];
    if (!animate) {
      for (const node of nodes) node.textContent = formatCount(Number(node.dataset.count), node.dataset.format);
      return;
    }
    const begin = performance.now();
    const token = (state.countToken = (state.countToken || 0) + 1);
    const tick = (now) => {
      if (token !== state.countToken) return;
      const t = (now - begin) / COUNT_MS;
      for (const node of nodes) {
        const target = Number(node.dataset.count) || 0;
        const value = countAt(target, t);
        // while counting, the hours show whole numbers when the target is big
        node.textContent = formatCount(t >= 1 ? target : value, node.dataset.format);
      }
      if (t < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  // ---------------------------------------------------------------- the clock: segments run, held = paused

  function loop(now) {
    const dt = state.last ? now - state.last : 0;
    state.last = now;
    const count = el.slides.children.length;
    const last = state.index >= count - 1;
    if (count && !state.paused && !state.held && !last && document.visibilityState === 'visible') {
      state.elapsed += dt;
      const seg = el.bars.children[state.index];
      if (seg) seg.firstChild.style.transform = `scaleX(${Math.min(1, state.elapsed / SLIDE_MS)})`;
      if (state.elapsed >= SLIDE_MS) step(1);
    }
    state.frame = requestAnimationFrame(loop);
  }

  function renderPause() {
    const paused = state.paused || REDUCED;
    el.pause.textContent = paused ? '▶' : '❚❚';
    el.pause.setAttribute('aria-label', paused ? 'Afspil' : 'Pause');
    el.pause.title = paused ? 'Afspil (P)' : 'Pause (P)';
    el.story.classList.toggle('is-paused', paused || state.held);
  }

  function togglePause() {
    if (REDUCED) return;
    state.paused = !state.paused;
    renderPause();
  }

  // ---------------------------------------------------------------- sharing

  async function share() {
    const button = $('share');
    const note = $('share-note');
    if (!button || state.shared) return;
    button.disabled = true;
    note.textContent = '';
    try {
      await postJson('/api/aar/del', { aar: state.year });
      state.shared = true;
      button.textContent = 'Delt med kontoret ✓';
      note.textContent = 'Kollegernes Klipper får besøg med dit år 📊';
    } catch (err) {
      button.disabled = false;
      note.textContent = err.message;
    }
  }

  // ---------------------------------------------------------------- input

  function onPointerDown(event) {
    if (event.button !== 0 || event.target.closest('button, a')) return;
    state.pressAt = performance.now();
    state.pressX = event.clientX;
    state.held = true;
    renderPause();
  }

  function onPointerUp(event) {
    if (!state.held) return;
    state.held = false;
    renderPause();
    if (performance.now() - state.pressAt > HOLD_MS) return;   // a hold only paused
    const box = el.story.getBoundingClientRect();
    step(event.clientX - box.left < box.width * 0.3 ? -1 : 1);
  }

  function onKey(event) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const onButton = event.target instanceof HTMLButtonElement;
    if (event.key === 'ArrowRight' || event.key === 'PageDown' || (event.key === ' ' && !onButton)) {
      event.preventDefault();
      step(1);
    } else if (event.key === 'ArrowLeft' || event.key === 'PageUp') {
      event.preventDefault();
      step(-1);
    } else if (event.key === 'Home') {
      event.preventDefault();
      go(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      go(el.slides.children.length - 1);
    } else if (event.key === 'p' || event.key === 'P') {
      togglePause();
    }
  }

  // ---------------------------------------------------------------- start

  async function start() {
    document.title = `Klippet ${state.year}`;
    el.brand.textContent = `Klippet ${state.year}`;
    renderNotice('Henter dit år …', null);
    let review;
    try {
      [review, state.klippe] = await Promise.all([getJson(`/api/aar?aar=${state.year}`), loadKlippe()]);
    } catch (err) {
      renderNotice('Klippet kunne ikke hentes', err.message, true);
      return;
    }
    try {
      const pet = await getJson('/api/pet');
      state.look = cleanLook(pet && pet.equipped);
    } catch {
      state.look = {};                        // Klippe in its everyday look
    }
    state.review = review;
    state.slides = buildSlides(review);
    render();
  }

  el.story.addEventListener('pointerdown', onPointerDown);
  window.addEventListener('pointerup', onPointerUp);
  window.addEventListener('pointercancel', () => {
    state.held = false;
    renderPause();
  });
  window.addEventListener('keydown', onKey);
  el.prev.addEventListener('click', () => step(-1));
  el.next.addEventListener('click', () => step(1));
  el.pause.addEventListener('click', togglePause);
  document.addEventListener('visibilitychange', () => {
    state.last = 0;                           // no jump after the tab was hidden
  });
  window.__aar = { state, go, step, buildSlides };   // for tests
  state.frame = requestAnimationFrame(loop);
  start();
})();

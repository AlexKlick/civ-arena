'use strict';

/* Comparison views for the match room: the per-seat research diff, the base source
   catalog tech tree, eight per-seat timeline small multiples and the per-turn
   decision strip. Everything is read from the retained /api/run payload plus one
   /api/tech-tree request per match. No value is interpolated: an unrecorded value
   is a gap here and says so. */

(() => {
  const api = window.civArena;
  // The pure half of these views lives in compare-core.js so the same code can
  // be evaluated outside a browser; without it these panels do not draw.
  const Core = window.civArenaCompareCore;
  if (!api || !Core) return;
  const state = api.state;
  const H = api.helpers;
  const $ = id => document.getElementById(id);

  const ROW_H = 42, NODE_W = 104, NODE_H = 30, SUB_W = 118, ERA_GAP = 26, HEADER = 34;
  const PAD_X = 16, PAD_Y = 12, HALF_W = 49, HALF_GAP = 4;
  const PLOT = {w: 520, h: 132, x0: 44, x1: 508, y0: 10, y1: 100, ticks: 126, marks: 112};
  const FALLBACK = 'Comparison data not recorded for this match.';
  const TREE_FALLBACK = 'Tech tree unavailable: the base catalog asset could not be served. ' +
    'Research sets above are still shown.';
  const DETAIL_HINT = 'Choose a technology in the tree to read its era, cost, prerequisites and ' +
    'each seat’s recorded status at the selected turn.';
  const PROBE_HINT = 'Hover or focus this chart and use Left/Right for per-turn values.';
  const TREE_LEGEND = 'prerequisite → tech, left to right; connective unverified · ';
  const TREE_LEGEND_FALLBACK = TREE_LEGEND + 'base source catalog, not the effective ruleset';

  let tree = {runId: null, status: 'idle', data: null};
  let layout = null, catalogIds = null, selectedTech = null, fitWidth = false;
  let charts = [];
  let lastResearch = '', lastTree = '', lastTimeline = '', lastStrip = '';

  // Catalog ids arrive upper-cased (BRONZE_WORKING); title case reads as a name
  // while the raw id stays in the tooltip, the detail list and the SVG title.
  const {finite, number, label, short, fmt, statusFor} = Core;
  function selectedTurn() {
    if (state.turn == null || state.turn === '') return null;
    const value = Number(state.turn);
    return Number.isFinite(value) ? value : null;
  }
  function seatTag(playerId) { return `P${playerId}`; }
  function seatName(playerId, agentId) {
    const seats = (state.data && state.data.research && state.data.research.seats) || [];
    const match = seats.find(seat => seat && String(seat.player_id) === String(playerId));
    const civ = String((match && match.civ_name) || '').replace(/^CIVILIZATION_/, '');
    return civ || String((match && match.agent_id) || agentId || '').trim() || 'seat not named';
  }
  function seatTitle(playerId, agentId) { return `${seatTag(playerId)} ${seatName(playerId, agentId)}`; }
  function seatClass(index) { return index === 0 ? 'seat0' : 'seat1'; }
  function fact(term, value) {
    return H.append(H.element('div'), H.element('dt', '', term), H.element('dd', '', value));
  }
  function chip(tech, extra) {
    const node = H.element('span', 'chip', extra ? `${label(tech)} · ${extra}` : label(tech));
    node.title = String(tech);
    return node;
  }
  const inCatalog = tech => !catalogIds || catalogIds.has(tech);
  function unretainedNote(fold) {
    if (!fold || fold.retained !== false) return null;
    return `Research history before turn ${fold.retainedFrom} was not retained for ` +
      seatTitle(fold.seat.player_id, fold.seat.agent_id);
  }

  /* ---------------------------------------------------------------- research */

  function currentFolds() {
    const research = state.data && state.data.research;
    const seats = research && Array.isArray(research.seats) ? research.seats : [];
    const cutoff = selectedTurn();
    return seats.filter(seat => seat && typeof seat === 'object')
      .slice().sort((a, b) => Number(a.player_id) - Number(b.player_id))
      .map(seat => Core.foldSeat(seat, cutoff));
  }
  function diffColumn(title, techs, tone, note) {
    const column = H.element('div', `diff-column ${tone || ''}`);
    const head = H.append(H.element('h3', 'diff-head'), H.element('span', 'diff-title', title),
      H.element('span', 'diff-count', note ? '—' : String(techs.length)));
    // A seat whose history was dropped states that, instead of showing the
    // chips of a set nobody retained.
    if (note) return H.append(column, head, H.element('p', 'unretained-note', note));
    const row = H.element('div', 'chip-row');
    const outside = techs.filter(tech => !inCatalog(tech));
    const known = techs.filter(inCatalog);
    if (!techs.length) H.append(row, H.element('span', 'chip empty', 'none recorded'));
    known.forEach(tech => H.append(row, chip(tech)));
    H.append(column, head, row);
    if (outside.length) {
      const group = H.append(H.element('div', 'chip-group'),
        H.element('span', 'chip-group-label', 'Outside base catalog'));
      const extra = H.element('div', 'chip-row');
      outside.forEach(tech => H.append(extra, chip(tech)));
      H.append(column, H.append(group, extra));
    }
    return column;
  }
  function renderResearch() {
    const research = state.data && state.data.research;
    const ok = Boolean(research) && Array.isArray(research.seats);
    const cutoff = selectedTurn();
    const signature = JSON.stringify([state.runId, ok ? research : null, cutoff,
      catalogIds ? catalogIds.size : -1]);
    if (signature === lastResearch) return;
    lastResearch = signature;
    const asOf = $('researchAsOf'), diff = $('researchDiff'), rows = $('researchRows');
    // The catalog's own provenance note is printed verbatim when the payload
    // carries one; the static sentence is only a fallback.
    const catalog = ok && research.catalog && typeof research.catalog === 'object'
      ? research.catalog : null;
    $('treeLegend').textContent = catalog && typeof catalog.note === 'string' && catalog.note
      ? TREE_LEGEND + catalog.note : TREE_LEGEND_FALLBACK;
    if (!ok) {
      $('researchNote').textContent = FALLBACK;
      $('researchTurn').textContent = '';
      asOf.replaceChildren(); diff.replaceChildren(); rows.replaceChildren();
      return;
    }
    $('researchNote').textContent = String(research.note || '');
    $('researchTurn').textContent = cutoff == null ? 'as of the latest recorded packet' : `as of turn ${cutoff}`;
    const folds = currentFolds();
    asOf.replaceChildren(...folds.map((fold, index) => {
      const line = H.element('p', 'as-of-line');
      H.append(line, H.element('span', `key-line ${seatClass(index)}`));
      const packet = fold.turn == null ? 'no retained request packet'
        : `packet turn ${fold.turn}${fold.seq == null ? '' : ` (seq ${fold.seq})`}`;
      const packets = number(fold.seat.packets);
      H.append(line, H.element('span', '', `${seatTitle(fold.seat.player_id, fold.seat.agent_id)} · ` +
        `${packet} · ${packets == null ? 'packet count not recorded' : `${packets} packets`}`));
      return line;
    }));
    if (research.incomplete) {
      H.append(asOf, H.element('p', 'as-of-line warn',
        'At least one retained packet could not be read; these sets are incomplete.'));
    }
    const {shared, only} = Core.diffOf(folds);
    diff.replaceChildren(diffColumn('Shared', shared, 'shared'),
      ...folds.map((fold, index) => diffColumn(
        `Only ${seatTitle(fold.seat.player_id, fold.seat.agent_id)}`, only[index],
        seatClass(index), unretainedNote(fold))));
    const detail = [];
    folds.forEach((fold, index) => {
      const who = seatTitle(fold.seat.player_id, fold.seat.agent_id);
      const researching = fold.researching
        ? `${label(fold.researching)}${inCatalog(fold.researching) ? '' : ' (outside base catalog)'}`
        : 'not recorded in this packet';
      const row = fact(`Researching — ${who}`, researching);
      row.className = `research-row ${seatClass(index)}`;
      detail.push(row);
      const latest = fold.latest;
      const view = Core.optionsView(latest);
      const optionRow = H.append(H.element('div', `research-row ${seatClass(index)}`),
        H.element('dt', '', `Could pick next (recorded options, when observed) — ${who}`));
      const value = H.element('dd');
      if (view.note !== null) {
        value.textContent = view.note;
      } else {
        const chipRow = H.element('div', 'chip-row');
        view.chips.forEach(option => H.append(chipRow,
          chip(option.tech_id, option.cost == null ? 'cost not recorded' : `${option.cost}`)));
        H.append(value, chipRow);
        if (latest && cutoff != null && number(latest.turn) !== cutoff) {
          H.append(value, H.element('p', 'tiny', `Recorded in the packet for turn ${latest.turn}.`));
        }
      }
      detail.push(H.append(optionRow, value));
    });
    rows.replaceChildren(...detail);
  }

  /* --------------------------------------------------------------- tech tree */

  function validTree(payload) {
    if (!payload || typeof payload !== 'object') return false;
    if (!Array.isArray(payload.eras) || !payload.eras.length) return false;
    if (!Array.isArray(payload.nodes) || !payload.nodes.length) return false;
    if (!Array.isArray(payload.edges)) return false;
    return payload.nodes.every(node => node && typeof node === 'object' &&
      typeof node.id === 'string' && node.id && typeof node.era === 'string');
  }
  function ensureTree() {
    const runId = state.runId || '';
    if (!runId || tree.runId === runId) return;
    tree = {runId, status: 'loading', data: null};
    lastTree = '';
    renderTree();
    fetch('/api/tech-tree', {cache: 'no-store'}).then(response => {
      if (!response.ok) throw new Error(`tech tree unavailable (${response.status})`);
      return response.json();
    }).then(payload => {
      if (tree.runId !== runId) return;
      if (!validTree(payload)) throw new Error('tech tree shape rejected');
      tree = {runId, status: 'ok', data: payload};
      afterTree();
    }).catch(() => {
      if (tree.runId !== runId) return;
      tree = {runId, status: 'error', data: null};
      afterTree();
    });
  }
  function afterTree() {
    lastTree = ''; lastResearch = '';
    renderTree(); renderResearch(); paintTree();
  }
  function buildLayout(data) {
    const eras = data.eras.map(String);
    const nodes = data.nodes.map(entry => ({
      id: String(entry.id), era: String(entry.era),
      row: number(entry.row) == null ? 0 : number(entry.row),
      cost: number(entry.cost),
    }));
    nodes.forEach(node => { if (!eras.includes(node.era)) eras.push(node.era); });
    const byId = new Map(nodes.map(node => [node.id, node]));
    const prereqs = new Map(nodes.map(node => [node.id, []]));
    const edges = [];
    data.edges.forEach(edge => {
      if (!Array.isArray(edge) || edge.length < 2) return;
      const subject = String(edge[0]), prereq = String(edge[1]);
      if (!byId.has(subject) || !byId.has(prereq) || subject === prereq) return;
      prereqs.get(subject).push(prereq);
      edges.push([subject, prereq]);
    });
    prereqs.forEach(list => list.sort());
    const depth = Core.treeDepths(nodes, edges);
    const widest = new Map(eras.map(era => [era, 0]));
    nodes.forEach(node => widest.set(node.era, Math.max(widest.get(node.era) || 0, depth.get(node.id))));
    const eraX = new Map();
    let cursor = PAD_X;
    eras.forEach(era => {
      eraX.set(era, cursor);
      cursor += (widest.get(era) + 1) * SUB_W + ERA_GAP;
    });
    const rows = nodes.map(node => node.row);
    const minRow = Math.min(...rows), maxRow = Math.max(...rows);
    const cells = new Map();
    let bumps = 0;
    nodes.slice().sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)).forEach(node => {
      const key = `${node.era}|${depth.get(node.id)}|${node.row}`;
      const bump = cells.get(key) || 0;
      cells.set(key, bump + 1);
      bumps = Math.max(bumps, bump);
      node.depth = depth.get(node.id);
      node.x = eraX.get(node.era) + node.depth * SUB_W;
      node.y = HEADER + PAD_Y + (node.row - minRow + bump) * ROW_H;
    });
    return {
      eras, eraX, nodes, byId, prereqs, edges,
      width: Math.max(cursor - ERA_GAP + PAD_X, 320),
      height: HEADER + PAD_Y * 2 + (maxRow - minRow + 1 + bumps) * ROW_H,
    };
  }
  function renderTree() {
    const canvas = $('techTree'), status = $('treeState'), toggle = $('treeFit');
    if (tree.status !== 'ok') {
      canvas.replaceChildren();
      canvas.setAttribute('hidden', '');
      layout = null; catalogIds = null; selectedTech = null;
      status.hidden = false;
      status.textContent = tree.status === 'error' ? TREE_FALLBACK
        : tree.status === 'loading' ? 'Requesting the recorded base catalog tree…'
          : 'Select a match to request the base catalog tree.';
      toggle.disabled = true;
      renderDetail();
      return;
    }
    const data = tree.data;
    const signature = JSON.stringify([String(data.catalog_digest || ''), data.nodes.length,
      data.edges.length]);
    if (signature === lastTree) return;
    lastTree = signature;
    layout = buildLayout(data);
    catalogIds = new Set(layout.nodes.map(node => node.id));
    selectedTech = null;
    canvas.replaceChildren();
    canvas.setAttribute('viewBox', `0 0 ${layout.width} ${layout.height}`);
    canvas.removeAttribute('hidden');
    status.hidden = false;
    status.textContent = `${layout.nodes.length} technologies, ${layout.edges.length} recorded ` +
      `prerequisite links, catalog digest ${String(data.catalog_digest || 'not recorded').slice(0, 12)}.`;
    toggle.disabled = false;
    const headers = H.svg('g', {class: 'tree-eras'});
    layout.eraCounts = [];
    layout.eras.forEach((era, index) => {
      const x = layout.eraX.get(era);
      if (index > 0) {
        H.append(headers, H.svg('line', {class: 'era-rule', x1: x - ERA_GAP / 2, x2: x - ERA_GAP / 2,
          y1: 6, y2: layout.height - 6}));
      }
      H.append(headers, H.svg('text', {class: 'era-name', x, y: 15}, label(era)));
      const counts = H.svg('text', {class: 'era-counts', x, y: 28}, '');
      layout.eraCounts.push({era, node: counts,
        total: layout.nodes.filter(node => node.era === era).length});
      H.append(headers, counts);
    });
    const wires = H.svg('g', {class: 'tree-edges'});
    layout.edges.forEach(([subject, prereq]) => {
      const to = layout.byId.get(subject), from = layout.byId.get(prereq);
      const x1 = from.x + NODE_W, y1 = from.y + NODE_H / 2, x2 = to.x, y2 = to.y + NODE_H / 2;
      const bend = Math.max(16, (x2 - x1) / 2);
      H.append(wires, H.svg('path', {class: 'tech-edge',
        d: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`}));
    });
    const marks = H.svg('g', {class: 'tree-nodes'});
    layout.nodes.forEach(node => {
      const group = H.svg('g', {class: 'tech-node', role: 'button', tabindex: 0, 'data-id': node.id});
      const name = label(node.id);
      const text = name.length > 14 ? `${name.slice(0, 13)}…` : name;
      node.halves = [0, 1].map(seat => H.svg('rect', {class: `tech-half ${seatClass(seat)}`,
        x: node.x + 1 + seat * (HALF_W + HALF_GAP), y: node.y + 1,
        width: HALF_W, height: NODE_H - 2, rx: 5}));
      H.append(group, H.svg('title', {}, `${node.id} · ${node.era} · cost ` +
        `${node.cost == null ? 'not recorded' : node.cost}`),
      H.svg('rect', {class: 'node-face', x: node.x, y: node.y, width: NODE_W, height: NODE_H, rx: 6}),
      node.halves[0], node.halves[1],
      H.svg('text', {class: 'node-name', x: node.x + 8, y: node.y + 19}, text));
      if (text.length <= 10 && node.cost != null) {
        H.append(group, H.svg('text', {class: 'node-cost', x: node.x + NODE_W - 8, y: node.y + 19},
          String(node.cost)));
      }
      const activate = () => { selectTech(node.id); };
      group.addEventListener('click', activate);
      group.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault(); activate();
      });
      node.group = group;
      H.append(marks, group);
    });
    H.append(canvas, headers, wires, marks);
    applyFit();
  }
  function applyFit() {
    const canvas = $('techTree');
    canvas.classList.toggle('fit', fitWidth);
    $('treeFit').setAttribute('aria-pressed', String(fitWidth));
    canvas.setAttribute('preserveAspectRatio', 'xMinYMin meet');
    if (!layout) return;
    canvas.setAttribute('height', String(layout.height));
    if (fitWidth) canvas.removeAttribute('width');
    else canvas.setAttribute('width', String(layout.width));
  }
  function paintTree() {
    if (!layout) return;
    const folds = currentFolds();
    layout.nodes.forEach(node => {
      const parts = [];
      node.halves.forEach((half, index) => {
        const fold = folds[index] || null;
        const marks = Core.halfState(fold, node.id);
        half.classList.toggle('done', marks.done);
        half.classList.toggle('researching', marks.researching);
        half.classList.toggle('unretained', marks.unretained);
        half.classList.toggle('absent', !fold);
        if (fold) {
          parts.push(`${seatTag(fold.seat.player_id)} ${statusFor(fold, node.id)}`);
        }
      });
      node.group.setAttribute('aria-label',
        `${node.id}, ${label(node.era)}, ${parts.join(', ') || 'no seat packets retained'}`);
      node.group.classList.toggle('selected', selectedTech === node.id);
    });
    layout.eraCounts.forEach(entry => {
      entry.node.textContent = folds.length
        ? folds.map(fold => `${seatTag(fold.seat.player_id)} ` + (fold.retained === false ? '?'
          : `${layout.nodes.filter(node => node.era === entry.era &&
            fold.researched.has(node.id)).length}`) + `/${entry.total}`).join(' · ')
        : `${entry.total} recorded`;
    });
  }
  function selectTech(id) {
    selectedTech = selectedTech === id ? null : id;
    paintTree();
    renderDetail();
  }
  function renderDetail() {
    const list = $('researchDetail');
    if (!layout || !selectedTech || !layout.byId.has(selectedTech)) {
      list.replaceChildren(fact('Selected technology', DETAIL_HINT));
      return;
    }
    const node = layout.byId.get(selectedTech);
    const prereqs = layout.prereqs.get(node.id) || [];
    const folds = currentFolds();
    const rows = [
      fact('Technology', `${label(node.id)} (${node.id})`),
      fact('Era', label(node.era)),
      fact('Recorded cost', node.cost == null ? 'not recorded' : String(node.cost)),
      fact('Prerequisites', prereqs.length ? prereqs.map(label).join(', ') : 'none recorded'),
    ];
    folds.forEach(fold => rows.push(fact(seatTitle(fold.seat.player_id, fold.seat.agent_id),
      statusFor(fold, node.id))));
    list.replaceChildren(...rows);
  }

  /* --------------------------------------------------------------- timelines */

  function buildChart(series, turns, seats, violations) {
    const key = String(series.key);
    const figure = H.element('figure', 'timeline-chart');
    H.append(figure, H.element('figcaption', '', Core.seriesTitle(series)));
    const canvas = H.svg('svg', {class: 'timeline-svg', viewBox: `0 0 ${PLOT.w} ${PLOT.h}`,
      tabindex: 0, role: 'group', 'data-series': key,
      'aria-label': `${series.label || key} for each seat, by turn`});
    const seatValues = seats.map((seat, index) => ({
      tag: seatTag(seat.player_id), index,
      values: Core.seatSeries(seat.rows, turns, key),
    }));
    const all = seatValues.flatMap(entry => entry.values).filter(finite);
    const high = Core.niceMax(Math.max(0, ...all));
    const low = Math.min(0, ...all, 0);
    const span = high - low || 1;
    // The x axis is the turn number itself, so a turn nobody recorded stays an
    // empty stretch instead of being closed up by its neighbours.
    const first = turns.length ? Math.min(...turns) : 0;
    const width = turns.length ? Math.max(...turns) - first : 0;
    const xAt = turn => width <= 0 ? (PLOT.x0 + PLOT.x1) / 2
      : PLOT.x0 + (PLOT.x1 - PLOT.x0) * (turn - first) / width;
    const yAt = value => PLOT.y1 - (value - low) / span * (PLOT.y1 - PLOT.y0);
    [low, low + span / 2, high].forEach(value => {
      H.append(canvas, H.svg('line', {class: 'hairline', x1: PLOT.x0, x2: PLOT.x1,
        y1: yAt(value), y2: yAt(value)}));
      H.append(canvas, H.svg('text', {class: 'axis-label', x: 40, y: yAt(value) + 3,
        'text-anchor': 'end'}, fmt(value)));
    });
    Core.axisTicks(turns).forEach(turn => {
      H.append(canvas, H.svg('text', {class: 'axis-label', x: xAt(turn), y: PLOT.ticks,
        'text-anchor': 'middle'}, String(turn)));
    });
    const hair = H.svg('line', {class: 'turn-hairline', x1: PLOT.x0, x2: PLOT.x0,
      y1: PLOT.y0, y2: PLOT.y1, hidden: 'hidden'});
    const cross = H.svg('line', {class: 'crosshair', x1: PLOT.x0, x2: PLOT.x0,
      y1: PLOT.y0, y2: PLOT.y1, hidden: 'hidden'});
    H.append(canvas, hair, cross);
    const ends = [];
    seatValues.forEach(entry => {
      // One run per stretch of consecutive recorded turns; a run of one point is
      // drawn as a dot, since a single reading is not a line.
      const runs = Core.seriesSegments(entry.values, turns);
      const path = runs.map(run => run.map((point, index) =>
        `${index ? 'L' : 'M'} ${xAt(point.turn).toFixed(1)} ${yAt(point.value).toFixed(1)}`)
        .join(' ')).join(' ');
      runs.filter(run => run.length === 1).forEach(([point]) => {
        H.append(canvas, H.svg('circle', {class: `series-point ${seatClass(entry.index)}`,
          cx: xAt(point.turn), cy: yAt(point.value), r: 2.5}));
      });
      if (path) {
        H.append(canvas, H.svg('path', {class: `series ${seatClass(entry.index)}`, d: path}));
      }
      const last = runs.length ? runs[runs.length - 1][runs[runs.length - 1].length - 1] : null;
      if (last) {
        H.append(canvas, H.svg('circle', {class: `series-end ${seatClass(entry.index)}`,
          cx: xAt(last.turn), cy: yAt(last.value), r: 4}));
        ends.push({entry, x: xAt(last.turn), y: yAt(last.value), value: last.value});
      }
    });
    ends.sort((a, b) => a.y - b.y).forEach((end, index) => {
      let y = end.y - 8;
      if (y < PLOT.y0 + 8) y = end.y + 15;
      const clash = index > 0 && Math.abs(y - ends[index - 1].labelY) < 12;
      if (clash) y = ends[index - 1].labelY + 13;
      end.labelY = y;
      const x = Math.min(end.x, PLOT.x1) - 6;
      H.append(canvas, H.svg('text', {class: 'end-label', x, y, 'text-anchor': 'end',
        'data-seat': end.entry.tag}, `${fmt(end.value)} ${end.entry.tag}`));
      if (Math.abs(y - end.y) > 10) {
        H.append(canvas, H.svg('line', {class: `leader ${seatClass(end.entry.index)}`,
          x1: x + 2, x2: end.x, y1: y - 3, y2: end.y}));
      }
    });
    violations.forEach(entry => {
      if (!finite(entry.turn) || !turns.length) return;
      if (entry.turn < first || entry.turn > first + width) return;
      const mark = H.svg('text', {class: 'violation-mark', x: xAt(entry.turn) + entry.offset,
        y: PLOT.marks, 'text-anchor': 'middle'}, '▲');
      H.append(mark, H.svg('title', {}, entry.title));
      H.append(canvas, mark);
    });
    const hit = H.svg('rect', {class: 'plot-hit', x: PLOT.x0 - 8, y: PLOT.y0 - 4,
      width: PLOT.x1 - PLOT.x0 + 16, height: PLOT.y1 - PLOT.y0 + 12});
    H.append(canvas, hit);
    const probe = H.element('p', 'tiny probe', PROBE_HINT);
    probe.setAttribute('aria-live', 'polite');
    H.append(figure, canvas, probe);
    const chart = {key, canvas, probe, hair, cross, turns, seatValues, xAt, focus: null,
      short: short(key)};
    // Hover reads the nearest recorded turn, never an interpolated position.
    const locate = event => {
      const box = canvas.getBoundingClientRect();
      if (!box.width || !turns.length) return null;
      const x = (event.clientX - box.left) * (PLOT.w / box.width);
      let best = 0;
      turns.forEach((turn, index) => {
        if (Math.abs(xAt(turn) - x) < Math.abs(xAt(turns[best]) - x)) best = index;
      });
      return best;
    };
    canvas.addEventListener('pointermove', event => {
      const index = locate(event);
      if (index == null) return;
      chart.focus = index;
      showProbe(chart, index);
    });
    canvas.addEventListener('pointerleave', () => { chart.focus = null; showProbe(chart, null); });
    canvas.addEventListener('click', event => {
      const index = locate(event);
      if (index == null) return;
      H.selectTurn(turns[index]);
    });
    canvas.addEventListener('focus', () => showProbe(chart, chart.focus));
    canvas.addEventListener('keydown', event => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        const base = chart.focus == null ? Math.max(0, turns.indexOf(selectedTurn())) : chart.focus;
        chart.focus = Math.min(turns.length - 1, Math.max(0, base + (event.key === 'ArrowRight' ? 1 : -1)));
        showProbe(chart, chart.focus);
      } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        const index = chart.focus == null ? turns.indexOf(selectedTurn()) : chart.focus;
        if (index >= 0) H.selectTurn(turns[index]);
      }
    });
    return {figure, chart};
  }
  function showProbe(chart, index) {
    if (index == null || index < 0 || index >= chart.turns.length) {
      chart.cross.setAttribute('hidden', 'hidden');
      chart.probe.textContent = PROBE_HINT;
      return;
    }
    const x = chart.xAt(chart.turns[index]);
    chart.cross.setAttribute('x1', String(x));
    chart.cross.setAttribute('x2', String(x));
    chart.cross.removeAttribute('hidden');
    chart.probe.textContent = `T${chart.turns[index]} · ` + chart.seatValues
      .map(entry => `${entry.tag} ${chart.short} ${fmt(entry.values[index])}`).join(' · ');
  }
  function buildTimelines(timeline) {
    charts = [];
    const grid = $('timelineGrid'), legend = $('timelineLegend');
    const head = $('timelineHead'), body = $('timelineBody');
    if (!timeline) {
      $('timelineNote').textContent = FALLBACK;
      grid.replaceChildren(); legend.replaceChildren();
      head.replaceChildren(); body.replaceChildren();
      return;
    }
    $('timelineNote').textContent = String(timeline.note || '');
    const turns = timeline.turns.map(number).filter(finite);
    const seats = timeline.seats.filter(seat => seat && typeof seat === 'object')
      .slice().sort((a, b) => Number(a.player_id) - Number(b.player_id))
      .slice(0, 2).map(seat => ({
        player_id: seat.player_id, agent_id: seat.agent_id,
        rows: new Map((Array.isArray(seat.rows) ? seat.rows : [])
          .filter(row => row && typeof row === 'object').map(row => [number(row.turn), row])),
      }));
    const seen = new Map();
    const violations = (Array.isArray(timeline.violations) ? timeline.violations : [])
      .filter(entry => entry && typeof entry === 'object').map(entry => {
        const turn = number(entry.turn);
        const rank = seen.get(turn) || 0;
        seen.set(turn, rank + 1);
        return {turn, offset: rank * 7, title: `turn ${turn} · ${seatTag(entry.player_id)} · ` +
          `${String(entry.kind || 'not recorded')} · ${String(entry.detail || 'no detail recorded')}`};
      });
    legend.replaceChildren(...seats.map((seat, index) => H.append(
      H.element('span', 'legend-key'), H.element('span', `key-line ${seatClass(index)}`),
      H.element('span', '', seatTitle(seat.player_id, seat.agent_id)))));
    H.append(legend, H.append(H.element('span', 'legend-key'),
      H.element('span', 'key-mark', '▲'), H.element('span', '', 'watchdog violation')),
    H.element('span', 'legend-key', 'gap = not recorded'));
    const series = timeline.series.filter(entry => entry && typeof entry === 'object' && entry.key);
    const built = series.map(entry => buildChart(entry, turns, seats, violations));
    charts = built.map(item => item.chart);
    grid.replaceChildren(...built.map(item => item.figure));
    const columns = [];
    series.forEach((entry, seriesIndex) => seats.forEach((seat, seatIndex) => columns.push({
      label: `${seatTag(seat.player_id)} ${Core.seriesTitle(entry)}`,
      values: charts[seriesIndex].seatValues[seatIndex].values,
    })));
    const headRow = H.append(H.element('tr'), H.element('th', '', 'Turn'));
    columns.forEach(column => H.append(headRow, H.element('th', '', column.label)));
    head.replaceChildren(headRow);
    body.replaceChildren(...turns.map((turn, index) => {
      const row = H.append(H.element('tr'), H.element('th', '', String(turn)));
      columns.forEach(column => H.append(row,
        H.element('td', '', finite(column.values[index]) ? fmt(column.values[index]) : '—')));
      return row;
    }));
  }
  function paintTimelines() {
    const turn = selectedTurn();
    charts.forEach(chart => {
      const index = turn == null ? -1 : chart.turns.indexOf(turn);
      if (index < 0) { chart.hair.setAttribute('hidden', 'hidden'); return; }
      const x = chart.xAt(chart.turns[index]);
      chart.hair.setAttribute('x1', String(x));
      chart.hair.setAttribute('x2', String(x));
      chart.hair.removeAttribute('hidden');
    });
  }
  function renderTimelines() {
    const timeline = state.data && state.data.timeline;
    const ok = Boolean(timeline) && Array.isArray(timeline.seats) && Array.isArray(timeline.turns) &&
      Array.isArray(timeline.series);
    const signature = JSON.stringify([state.runId, ok ? timeline : null]);
    if (signature !== lastTimeline) {
      lastTimeline = signature;
      buildTimelines(ok ? timeline : null);
    }
    paintTimelines();
  }

  /* ---------------------------------------------------------- decision strip */

  function pill(kind, glyph, lines) {
    const button = H.element('button', 'strip-pill');
    button.type = 'button';
    button.setAttribute('data-kind', kind);
    const mark = H.element('span', 'pill-glyph', glyph);
    mark.setAttribute('aria-hidden', 'true');
    H.append(button, H.append(H.element('span', 'pill-head'), mark,
      H.element('span', 'pill-kind', kind)));
    lines.forEach(line => {
      H.append(button, H.element('span', `pill-line${line.bad ? ' status-bad' : ''}`, line.text));
    });
    button.addEventListener('click', () => {
      const target = $('strategyChoices');
      if (target) target.scrollIntoView({block: 'start'});
    });
    return button;
  }
  function renderStrip() {
    const turn = selectedTurn();
    const all = Array.isArray(state.data && state.data.turns) ? state.data.turns : [];
    const rows = all.filter(row => row && typeof row === 'object' && turn != null &&
      Number(row.turn) === turn).slice().sort((a, b) => Number(a.player_id) - Number(b.player_id));
    const signature = JSON.stringify([state.runId, turn,
      rows.map(row => [row.player_id, row.strategy_delta, row.economy, row.growth])]);
    if (signature === lastStrip) return;
    lastStrip = signature;
    const strip = $('decisionStrip');
    if (!rows.length) {
      strip.replaceChildren(H.element('p', 'subtle', 'No recorded seat decisions for this turn.'));
      return;
    }
    strip.replaceChildren(...rows.map(row => {
      const seat = H.element('div', 'strip-seat');
      H.append(seat, H.element('span', 'strip-seat-name',
        seatTitle(row.player_id, row.agent_id || (row.agent || ''))));
      H.append(seat,
        pill('Directive', '◈', [{text: Core.directiveText(row.strategy_delta), bad: false}]),
        pill('Economy', '◍', Core.economyLines(row.economy)),
        pill('Growth', '❖', [{text: Core.growthText(row.growth), bad: false}]));
      return seat;
    }));
  }

  /* -------------------------------------------------------------------- wire */

  function renderCompare() {
    ensureTree();
    renderTree();
    renderResearch();
    paintTree();
    // The selected node's per-seat status is turn-dependent, so it is rebuilt
    // whenever the tree is repainted for a new turn.
    renderDetail();
    renderTimelines();
    renderStrip();
  }
  $('treeFit').addEventListener('click', () => { fitWidth = !fitWidth; applyFit(); });
  renderDetail();
  api.renderers.push(renderCompare);
  renderCompare();
})();

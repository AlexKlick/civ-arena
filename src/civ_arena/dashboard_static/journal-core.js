'use strict';

/* Pure logic behind the match room's turn journal: which recorded calls a
   filter keeps, which consecutive calls fold into one run, what a collapsed
   seat-turn pill says, which turns the visible window covers, and where every
   band, pill and node sits.

   Every function here is a function of its arguments only. It never touches the
   page, the network or the shared renderer helpers, so the browser and the test
   harness evaluate exactly the same code. Nothing is invented: a seat turn that
   was never recorded stays null instead of becoming an empty turn, a fold keeps
   every call it swallowed, and the left-to-right order is recorded order only,
   never causality.

   The whole module lives inside one function scope: the match room loads it
   beside the comparison core, and two classic scripts cannot both declare a
   top-level `Core`. */

(() => {
  const LABEL_W = 64;
  const HEADER = 30;
  const LANE_H = 96;
  const PILL_W = 92;
  const PILL_H = 40;
  const NODE_W = 132;
  const NODE_H = 54;
  const PITCH = 142;
  const PAD = 10;
  const JUMP_W = 118;
  const RADIUS = 6;
  const HEIGHT = HEADER + 2 * LANE_H + 12;

  const KIND_GLYPH = {Observation: '◉', Action: '▶', Note: '✎'};
  const STATUS_GLYPH = {accepted: '✓', rejected: '✕', pending: '◌'};
  const KIND_ORDER = ['Observation', 'Action', 'Note'];
  const REJECTED = ['rejected', 'failed', 'error', 'aborted'];
  // Null-prototype tables: an unknown filter name must read as unknown, and an
  // ordinary object would answer 'constructor' or 'toString' with an inherited
  // value and quietly empty the lane.
  const FILTER_KIND = Object.assign(Object.create(null),
    {actions: 'Action', observations: 'Observation', notes: 'Note'});
  const FILTER_NOUN = Object.assign(Object.create(null),
    {all: 'call', actions: 'action', observations: 'observation', notes: 'note'});

  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const asSet = value => value instanceof Set ? value : new Set(Array.isArray(value) ? value : []);

  // A status the log never called accepted and never called a failure is
  // pending: an unrecorded status is not a silent success.
  function statusClass(status, isAccepted) {
    if (typeof isAccepted === 'function' && isAccepted(status)) return 'accepted';
    return REJECTED.includes(status) ? 'rejected' : 'pending';
  }

  // An unknown filter shows everything rather than silently emptying the lane.
  function filterCalls(calls, filter, kindOf) {
    const list = Array.isArray(calls) ? calls : [];
    const want = FILTER_KIND[filter];
    if (!want || typeof kindOf !== 'function') return list.slice();
    return list.filter(call => kindOf(call && call.tool) === want);
  }

  // A run is keyed by its first call, so the key survives a re-render and a
  // poll that appends later calls to the same seat turn.
  function runKey(turn, playerId, firstSeq) {
    return `${turn}:${playerId}:${firstSeq}`;
  }

  /* Consecutive calls with the same tool AND the same status fold into one run
     node; two of them are enough. Folding never drops a call — the run carries
     all of them, and an opened run yields each call again with its position, so
     the count in the foot stays the count of recorded calls. */
  function foldRuns(calls, turn, playerId, expandedRuns) {
    const list = Array.isArray(calls) ? calls : [];
    const opened = asSet(expandedRuns);
    const items = [];
    let index = 0;
    while (index < list.length) {
      let end = index + 1;
      while (end < list.length && list[end] && list[index] &&
             list[end].tool === list[index].tool &&
             list[end].status === list[index].status) end += 1;
      const group = list.slice(index, end);
      const head = group[0] || {};
      const key = runKey(turn, playerId, head.seq);
      if (group.length < 2) {
        items.push({type: 'call', call: head, index});
      } else if (opened.has(key)) {
        group.forEach((call, offset) => items.push({type: 'call', call, index: index + offset,
          run: {key, size: group.length, position: offset + 1}}));
      } else {
        items.push({type: 'run', key, tool: head.tool, status: head.status, calls: group, index});
      }
      index = end;
    }
    return items;
  }

  /* What a collapsed seat turn says without opening it. The glyphs repeat the
     kind counts the hue carries, so the pill still reads in grayscale. */
  function pillSummary(calls, filter, kindOf, isAccepted) {
    const list = filterCalls(calls, filter, kindOf);
    const kinds = {Observation: 0, Action: 0, Note: 0};
    let rejected = 0;
    let pending = 0;
    list.forEach(call => {
      const kind = typeof kindOf === 'function' ? kindOf(call && call.tool) : null;
      if (kinds[kind] != null) kinds[kind] += 1;
      const shape = statusClass(call && call.status, isAccepted);
      if (shape === 'rejected') rejected += 1;
      else if (shape === 'pending') pending += 1;
    });
    const count = list.length;
    const noun = FILTER_NOUN[filter] || FILTER_NOUN.all;
    const parts = KIND_ORDER.filter(kind => kinds[kind] > 0)
      .map(kind => `${KIND_GLYPH[kind]}${kinds[kind]}`);
    if (rejected > 0) parts.push(`${STATUS_GLYPH.rejected}${rejected}`);
    return {count, kinds, rejected, pending,
            text: `${count} ${noun}${count === 1 ? '' : 's'}`,
            glyphs: parts.join(' '),
            title: `${count} recorded call${count === 1 ? '' : 's'} · ` +
                   `accepted ${count - rejected - pending} · ` +
                   `rejected ${rejected} · pending ${pending}`};
  }

  /* The turns on screen, plus how many recorded turns sit outside on each side.
     A centre that is not itself a recorded turn snaps to the nearest recorded
     one instead of opening an empty window. */
  function windowOf(turns, center, radius = RADIUS) {
    const list = (Array.isArray(turns) ? turns : []).filter(finite);
    if (!list.length) return {turns: [], earlier: 0, later: 0, center: null};
    let middle = list.indexOf(center);
    if (middle < 0) {
      const target = finite(center) ? center : list[list.length - 1];
      middle = 0;
      let best = Math.abs(list[0] - target);
      list.forEach((turn, index) => {
        const distance = Math.abs(turn - target);
        if (distance < best) {
          best = distance;
          middle = index;
        }
      });
    }
    const low = Math.max(0, middle - radius);
    const high = Math.min(list.length - 1, middle + radius);
    return {turns: list.slice(low, high + 1), earlier: low,
            later: list.length - 1 - high, center: list[middle]};
  }

  /* Geometry for the whole strip. Columns abut in window order after the lane
     label gutter; an expanded column is as wide as its busiest lane. A lane
     with no recorded seat turn is 'absent', which is not the same as a lane
     whose calls the filter removed ('nodes' with no items). */
  function layout(model) {
    const source = model && typeof model === 'object' ? model : {};
    const turns = Array.isArray(source.turns) ? source.turns : [];
    const expanded = asSet(source.expanded);
    const lanes = Array.isArray(source.lanes) ? source.lanes : [];
    const selected = source.selected == null ? null : source.selected;
    const columns = [];
    let x = LABEL_W;
    turns.forEach(turn => {
      const open = expanded.has(turn);
      const entries = lanes.map(lane => (lane && lane.byTurn instanceof Map
        ? (lane.byTurn.get(turn) == null ? null : lane.byTurn.get(turn)) : null));
      const busiest = entries.reduce((most, entry) => Math.max(most,
        entry && Array.isArray(entry.items) ? entry.items.length : 0), 0);
      const width = open
        ? PAD + Math.max(1, busiest) * PITCH - (PITCH - NODE_W) + PAD
        : PAD + PILL_W + PAD;
      const column = {turn, x, width, expanded: open, selected: selected === turn, lanes: []};
      entries.forEach((entry, seat) => {
        const y = HEADER + seat * LANE_H;
        if (entry === null) {
          column.lanes.push({seat, y, kind: 'absent', items: []});
          return;
        }
        if (open) {
          const items = (Array.isArray(entry.items) ? entry.items : []).map((item, index) =>
            Object.assign({}, item, {x: x + PAD + index * PITCH, y: y + (LANE_H - NODE_H) / 2,
                                     w: NODE_W, h: NODE_H}));
          column.lanes.push({seat, y, kind: 'nodes', items});
          return;
        }
        column.lanes.push({seat, y, kind: 'pill', items: [{
          type: 'pill', status: entry.status == null ? null : entry.status,
          summary: entry.summary == null ? null : entry.summary,
          x: x + PAD, y: y + (LANE_H - PILL_H) / 2, w: PILL_W, h: PILL_H}]});
      });
      columns.push(column);
      x += width;
    });
    const last = columns[columns.length - 1];
    return {width: last ? last.x + last.width + PAD : LABEL_W + PAD, height: HEIGHT, columns};
  }

  const Core = {LABEL_W, HEADER, LANE_H, PILL_W, PILL_H, NODE_W, NODE_H, PITCH, PAD, JUMP_W,
                RADIUS, HEIGHT, KIND_GLYPH, STATUS_GLYPH, statusClass, filterCalls, runKey,
                foldRuns, pillSummary, windowOf, layout};

  if (typeof window !== 'undefined') window.civArenaJournalCore = Core;
  else module.exports = Core;
})();

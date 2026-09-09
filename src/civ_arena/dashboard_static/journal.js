'use strict';

/* The match room's turn journal: a horizontal swimlane per seat over the engine
   turns around the selected one. Time runs left to right, one column per turn.
   The selected turn (and any turn the reader shift-opened) is expanded into one
   node per recorded call in recorded order; every other turn in the window is a
   pill that summarises the seat turn without claiming anything about it.

   Arrows join consecutive nodes in one lane. They carry recorded order only —
   never causality, never a dependency. This file is DOM wiring: every number it
   draws with comes from journal-core.js and every colour comes from a class. */

(() => {
  const $ = id => document.getElementById(id);
  let signature = '';
  let lastSelected = null;
  let lastCenter = null;
  let columns = [];
  let gutter = null;
  let folds = [];

  /* Where the selected call sits inside a run, read off the CURRENT fold rather
     than remembered from the click. A poll that appends a third consecutive call
     turns a run of two into a run of three, and a hint that still said "call 1
     of 2" would be a false statement about the record. */
  function hintFrom(H, selectedCall) {
    if (selectedCall == null) return null;
    for (const entry of folds) {
      for (const item of entry.items) {
        if (item.type === 'run') {
          const position = item.calls
            .findIndex(call => H.callKey(entry.row, call) === selectedCall);
          if (position >= 0) {
            return {key: selectedCall, size: item.calls.length, position: position + 1,
                    tool: item.tool};
          }
        } else if (item.run && H.callKey(entry.row, item.call) === selectedCall) {
          return {key: selectedCall, size: item.run.size, position: item.run.position,
                  tool: item.call.tool};
        }
      }
    }
    return null;
  }

  function syncHint() {
    const api = window.civArena;
    if (!api) return;
    api.state.runHint = hintFrom(api.helpers, api.state.selectedCall);
  }

  /* The lane gutter holds the seat identity, so it stays put while the strip
     scrolls under it: a swimlane whose labels have scrolled away is unreadable.
     The strip is drawn at one user unit per CSS pixel, so the scroll offset is
     the translation. */
  function pinGutter() {
    const scroll = $('graphScroll');
    if (gutter && scroll) gutter.setAttribute('transform', `translate(${scroll.scrollLeft} 0)`);
  }

  function mark() {
    const api = window.civArena;
    if (!api) return;
    api.helpers.markSelection();
    const graph = $('actionGraph');
    if (!graph) return;
    // markSelection() matches a node's own call key; a folded run also answers
    // for every call it swallowed, so the reader still sees where they are.
    graph.querySelectorAll('.graph-node.run').forEach(node => {
      const keys = (node.getAttribute('data-keys') || '').split(' ');
      node.classList.toggle('selected', keys.indexOf(api.state.selectedCall) >= 0);
    });
  }

  function render() {
    const api = window.civArena;
    const Core = window.civArenaJournalCore;
    const graph = $('actionGraph');
    if (!api || !Core || !graph) return;
    const state = api.state;
    const H = api.helpers;
    const scroll = $('graphScroll');
    const rows = state.data && Array.isArray(state.data.turns) ? state.data.turns : [];
    const turnsAll = [...new Set(rows.map(row => Number(row.turn)))]
      .filter(Number.isFinite).sort((a, b) => a - b);
    const selected = state.turn == null || state.turn === '' ? null : Number(state.turn);
    const center = state.journalCenter == null ? selected : state.journalCenter;
    const win = Core.windowOf(turnsAll, center);
    const expanded = new Set();
    state.expandedTurns.forEach(turn => expanded.add(Number(turn)));
    if (selected != null) expanded.add(selected);

    const byKey = new Map();
    rows.forEach(row => byKey.set(`${Number(row.turn)}:${row.player_id}`, row));

    /* Every opened turn is folded here, whether or not it is still on screen.
       renderGraph() clears callsByKey each poll, so a turn the reader opened and
       then scrolled out of the window would otherwise lose the record behind its
       own selected call. The fold is computed once and the lanes reuse it. */
    const opened = new Map();
    folds = [];
    rows.forEach(row => {
      const turn = Number(row.turn);
      if (!expanded.has(turn)) return;
      (row.calls || []).forEach(call =>
        state.callsByKey.set(H.callKey(row, call), {turn: row, call}));
      const items = Core.foldRuns(Core.filterCalls(row.calls, state.filter, H.kindOf),
                                  turn, row.player_id, state.expandedRuns);
      opened.set(`${turn}:${row.player_id}`, {status: row.status, summary: null, items});
      folds.push({row, items});
    });

    const lanes = H.agentList().map((agent, seat) => {
      const byTurn = new Map();
      win.turns.forEach(turn => {
        const key = `${turn}:${agent.player_id}`;
        const row = byKey.get(key);
        if (!row) {
          byTurn.set(turn, null);
          return;
        }
        byTurn.set(turn, opened.get(key) || {status: row.status, items: [],
          summary: Core.pillSummary(row.calls, state.filter, H.kindOf, H.isAccepted)});
      });
      return {seat, player_id: agent.player_id, byTurn};
    });

    // The window bounds are part of what is drawn: an appended turn changes the
    // jump-pill counts and the range their handlers clamp to, even when no call
    // inside the window moved. Calls enter the signature by identity, not by
    // payload, so a large recorded result is not re-serialised every poll.
    const next = JSON.stringify([state.runId, win.turns, win.earlier, win.later, win.center,
      turnsAll.length, [...expanded], [...state.expandedRuns], state.filter, selected,
      win.turns.map(turn => lanes.map(lane => {
        const row = byKey.get(`${turn}:${lane.player_id}`);
        return row ? (row.calls || []).map(call => [call.seq, call.tool, call.status]) : null;
      }))]);
    if (next === signature) {
      mark();
      syncHint();
      H.renderDetail();
      return;
    }
    signature = next;

    const box = Core.layout({turns: win.turns, expanded, selected, lanes});
    columns = box.columns;
    const held = scroll ? scroll.scrollLeft : 0;
    graph.setAttribute('viewBox', `0 0 ${box.width} ${box.height}`);
    graph.setAttribute('width', box.width);
    graph.setAttribute('height', box.height);
    graph.replaceChildren();

    const defs = H.svg('defs');
    ['gold', 'teal'].forEach((name, seat) => {
      const marker = H.svg('marker', {id: `arrow-${name}`, class: `seat${seat}`,
        viewBox: '0 0 10 10', refX: 5, refY: 5, markerWidth: 5, markerHeight: 5,
        orient: 'auto-start-reverse'});
      H.append(marker, H.svg('path', {d: 'M 0 0 L 10 5 L 0 10 z'}));
      H.append(defs, marker);
    });
    H.append(graph, defs);

    function drawNode(column, lane, item) {
      const isRun = item.type === 'run';
      const call = (isRun ? item.calls[0] : item.call) || {};
      const size = isRun ? item.calls.length : item.run ? item.run.size : 1;
      const playerId = lanes[lane.seat].player_id;
      const key = H.callKey({turn: column.turn, player_id: playerId}, call);
      const kind = H.kindOf(call.tool);
      const shape = Core.statusClass(call.status, H.isAccepted);
      const status = H.humanize(call.status || 'pending');
      const suffix = isRun ? ` ×${size}` : '';
      const label = H.humanize(call.tool).slice(0, 16 - suffix.length) + suffix;
      const seqs = isRun ? item.calls.map(one => one.seq).join(', ') : String(call.seq);
      const classes = `graph-node ${lane.seat === 0 ? 'seat-gold' : 'seat-teal'}` +
        (isRun ? ' run' : '');
      const group = H.svg('g', {class: classes, role: 'button', tabindex: 0, 'data-key': key,
        'data-lane': String(lane.seat), 'data-x': item.x,
        'aria-label': `Seat ${lane.seat + 1}, turn ${column.turn}, ${label}, ${kind}, ` +
          `${status}, recorded call ${seqs}`});
      if (isRun) {
        group.setAttribute('data-run', item.key);
        group.setAttribute('data-keys', item.calls
          .map(one => H.callKey({turn: column.turn, player_id: playerId}, one)).join(' '));
      }
      H.append(group,
        H.svg('title', {}, `${call.tool}${suffix} · ${call.status || 'pending'} · ` +
          (isRun ? `${size} consecutive calls, recorded ${seqs}` : H.localTime(call.ts))),
        H.svg('rect', {class: 'node-bg', x: item.x, y: item.y, width: item.w, height: item.h,
          rx: 8}),
        H.svg('rect', {class: `kind-bar kind-${kind.toLowerCase()}`, x: item.x, y: item.y,
          width: 4, height: item.h, rx: 2}),
        H.svg('text', {class: 'node-index', x: item.x + 13, y: item.y + 18},
          String(item.index + 1).padStart(2, '0')),
        H.svg('text', {class: 'kind-glyph', x: item.x + 34, y: item.y + 18},
          Core.KIND_GLYPH[kind] || ''),
        H.svg('text', {class: `status-glyph ${shape}`, x: item.x + item.w - 11,
          y: item.y + 18, 'text-anchor': 'end'}, Core.STATUS_GLYPH[shape]),
        H.svg('text', {class: 'node-label', x: item.x + 13, y: item.y + 35}, label),
        H.svg('text', {class: 'node-meta', x: item.x + 13, y: item.y + 48},
          `${kind} · ${status}`));
      const activate = () => {
        state.selectedCall = key;
        if (isRun) {
          state.expandedRuns.add(item.key);
          render();
          return;
        }
        mark();
        syncHint();
        H.renderDetail();
      };
      group.addEventListener('click', activate);
      group.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          activate();
        }
      });
      H.append(graph, group);
    }

    function drawPill(column, lane, item) {
      const summary = item.summary ||
        {count: 0, text: '0 calls', glyphs: '', title: 'No calls recorded', rejected: 0};
      const turn = column.turn;
      const group = H.svg('g', {class: `turn-pill${column.expanded ? ' expanded' : ''}`,
        role: 'button', tabindex: 0, 'data-turn': turn, 'data-lane': String(lane.seat),
        'data-x': item.x,
        'aria-label': `Seat ${lane.seat + 1}, turn ${turn}, ${summary.text}. ` +
          'Select this turn, or hold Shift to open it beside the selected turn.'});
      const bad = summary.rejected > 0 ? ` ${Core.STATUS_GLYPH.rejected}${summary.rejected}` : '';
      const kinds = bad ? summary.glyphs.slice(0, summary.glyphs.length - bad.length)
        : summary.glyphs;
      const glyphs = H.svg('text', {class: 'pill-glyphs', x: item.x + 9, y: item.y + 31}, kinds);
      if (bad) H.append(glyphs, H.svg('tspan', {class: 'status-bad'}, bad));
      H.append(group,
        H.svg('title', {}, summary.title),
        H.svg('rect', {x: item.x, y: item.y, width: item.w, height: item.h, rx: 7}),
        H.svg('text', {class: 'pill-count', x: item.x + 9, y: item.y + 17}, summary.text),
        glyphs);
      const activate = event => {
        if (event && event.shiftKey) {
          if (state.expandedTurns.has(turn)) state.expandedTurns.delete(turn);
          else state.expandedTurns.add(turn);
          render();
          return;
        }
        H.selectTurn(turn);
      };
      group.addEventListener('click', activate);
      group.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          activate(event);
        }
      });
      H.append(graph, group);
    }

    function drawLane(column, lane) {
      const middle = lane.y + Core.LANE_H / 2;
      if (lane.kind === 'absent') {
        const group = H.svg('g', {class: 'turn-pill absent', 'aria-hidden': 'true'});
        H.append(group, H.svg('text', {x: column.x + Core.PAD, y: middle + 4},
          'no seat turn recorded'));
        H.append(graph, group);
        return;
      }
      if (lane.kind === 'pill') {
        drawPill(column, lane, lane.items[0]);
        return;
      }
      if (!lane.items.length) {
        H.append(graph, H.svg('text',
          {class: 'lane-empty', x: column.x + Core.PAD, y: middle + 4},
          'No matching calls recorded'));
        return;
      }
      lane.items.forEach((item, index) => {
        if (index + 1 < lane.items.length) {
          const mid = item.y + Core.NODE_H / 2;
          H.append(graph, H.svg('path', {class: `arrow seat${lane.seat}`,
            d: `M ${item.x + Core.NODE_W} ${mid} L ${item.x + Core.PITCH} ${mid}`,
            'marker-end': `url(#arrow-${lane.seat === 0 ? 'gold' : 'teal'})`}));
        }
        drawNode(column, lane, item);
      });
    }

    columns.forEach(column => {
      if (column.expanded) {
        const band = H.svg('rect', {class: 'turn-band', x: column.x, y: Core.HEADER,
          width: column.width, height: box.height - Core.HEADER, 'data-turn': column.turn});
        if (column.selected) band.setAttribute('data-selected', '1');
        H.append(graph, band);
      }
      H.append(graph, H.svg('line', {class: 'turn-rule', x1: column.x, y1: 0,
        x2: column.x, y2: box.height}));
    });

    columns.forEach(column => {
      const collapsible = column.expanded && !column.selected;
      const label = H.svg('text', {class: `turn-label${column.selected ? ' selected' : ''}` +
        (collapsible ? ' collapsible' : ''), x: column.x + 6, y: 19}, `T${column.turn}`);
      if (collapsible) {
        const collapse = () => {
          state.expandedTurns.delete(column.turn);
          render();
        };
        label.setAttribute('role', 'button');
        label.setAttribute('tabindex', '0');
        label.setAttribute('aria-label', `Close turn ${column.turn}`);
        label.addEventListener('click', collapse);
        label.addEventListener('keydown', event => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            collapse();
          }
        });
      }
      H.append(graph, label);
      column.lanes.forEach(lane => drawLane(column, lane));
    });

    function drawJump(kind, x, text) {
      const group = H.svg('g', {class: `jump-pill ${kind}`, role: 'button', tabindex: 0,
        'data-lane': 'head', 'data-x': x, 'aria-label': text});
      H.append(group,
        H.svg('rect', {x, y: 4, width: Core.JUMP_W, height: 22, rx: 6}),
        H.svg('text', {x: x + Core.JUMP_W / 2, y: 19, 'text-anchor': 'middle'}, text));
      const activate = () => {
        const step = kind === 'earlier' ? -Core.RADIUS : Core.RADIUS;
        const base = win.center == null ? 0 : win.center;
        state.journalCenter = Math.min(turnsAll[turnsAll.length - 1],
          Math.max(turnsAll[0], base + step));
        render();
      };
      group.addEventListener('click', activate);
      group.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          activate();
        }
      });
      H.append(graph, group);
    }

    if (columns.length && win.earlier > 0) {
      drawJump('earlier', columns[0].x,
        `‹ ${win.earlier} earlier turn${win.earlier === 1 ? '' : 's'}`);
    }
    if (columns.length && win.later > 0) {
      const last = columns[columns.length - 1];
      drawJump('later', last.x + last.width - Core.JUMP_W,
        `${win.later} later turn${win.later === 1 ? '' : 's'} ›`);
    }

    gutter = H.svg('g', {class: 'lane-gutter'});
    H.append(gutter, H.svg('rect', {class: 'gutter-bg', x: 0, y: 0, width: Core.LABEL_W,
      height: box.height}));
    lanes.forEach((lane, seat) => {
      const middle = Core.HEADER + seat * Core.LANE_H + Core.LANE_H / 2;
      H.append(gutter,
        H.svg('rect', {class: 'lane-label-bg', x: 0, y: middle - 13, width: Core.LABEL_W - 6,
          height: 20, rx: 4}),
        H.svg('text', {class: `lane-label seat${seat}`, x: 8, y: middle + 1},
          `SEAT ${seat + 1}`));
    });
    H.append(graph, gutter);

    /* The CENTRE says why the window moved, and only a moved centre may move the
       reader's scroll. A centre that moved with no reader jump behind it is a
       reset, so the strip lands on the selected column again; a reader jump lands
       at the start of the window it asked for; a poll that appends a turn moves
       the window's edges but not its centre, so the reader keeps their place. */
    const jumped = state.journalCenter != null;
    const moved = win.center !== lastCenter;
    lastCenter = win.center;
    if (scroll) {
      scroll.scrollLeft = held;
      if (selected !== lastSelected || (moved && !jumped)) {
        const index = columns.findIndex(entry => entry.turn === selected);
        // Land the selected column two collapsed pills in from the gutter, so the
        // turns it followed stay on screen. The first column has nothing before it.
        const context = index > 0 ? 2 * (Core.PAD + Core.PILL_W + Core.PAD) : 0;
        if (index >= 0) {
          scroll.scrollLeft = Math.max(0, columns[index].x - Core.LABEL_W - context);
        }
      } else if (moved) {
        scroll.scrollLeft = 0;
      }
    }
    pinGutter();
    lastSelected = selected;

    let shown = 0;
    let folded = 0;
    lanes.forEach(lane => win.turns.forEach(turn => {
      if (!expanded.has(turn)) return;
      const entry = lane.byTurn.get(turn);
      if (!entry) return;
      entry.items.forEach(item => {
        if (item.type === 'run') {
          shown += item.calls.length;
          folded += item.calls.length - 1;
        } else shown += 1;
      });
    }));
    $('callCount').textContent = `${shown} ${shown === 1 ? 'call' : 'calls'} shown` +
      (folded > 0 ? ` · ${folded} folded into runs` : '');
    const empty = $('graphEmpty');
    // The empty state speaks for the SELECTED turn. Once the reader has jumped
    // the window away from it, the strip is showing other recorded turns and
    // covering them with "no matching calls" would be a false statement.
    const onScreen = selected != null && columns.some(entry => entry.turn === selected);
    empty.hidden = shown > 0 || (columns.length > 0 && !onScreen);
    empty.textContent = !state.data ? 'Select a match to explore its turns.'
      : selected == null ? 'No seat turns recorded yet.'
        : 'No matching calls recorded for this turn.';
    mark();
    syncHint();
    H.renderDetail();
  }

  /* Arrow keys walk one lane by recorded position; Up and Down cross to the
     nearest item in the neighbouring lane, with the jump pills as the head
     lane. Every item is reachable by Tab as well. */
  function moveFocus(event) {
    const graph = $('actionGraph');
    if (!graph || !event.target || !event.target.closest) return false;
    const active = event.target.closest('[data-lane]');
    if (!active || !graph.contains(active)) return false;
    const all = [...graph.querySelectorAll(
      '.turn-pill:not(.absent), .graph-node, .jump-pill')];
    const laneOf = node => node.getAttribute('data-lane');
    const xOf = node => Number(node.getAttribute('data-x'));
    const inLane = lane => all.filter(node => laneOf(node) === lane)
      .sort((a, b) => xOf(a) - xOf(b));
    const order = ['head', '0', '1'];
    const row = inLane(laneOf(active));
    const index = row.indexOf(active);
    let target = null;
    if (event.key === 'ArrowRight') target = row[index + 1];
    else if (event.key === 'ArrowLeft') target = row[index - 1];
    else if (event.key === 'Home') target = row[0];
    else if (event.key === 'End') target = row[row.length - 1];
    else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      const next = order[order.indexOf(laneOf(active)) + (event.key === 'ArrowDown' ? 1 : -1)];
      const x = xOf(active);
      target = (next == null ? [] : inLane(next)).reduce((best, node) =>
        best == null || Math.abs(xOf(node) - x) < Math.abs(xOf(best) - x) ? node : best, null);
    } else return false;
    if (!target || target === active) return false;
    event.preventDefault();
    target.focus();
    return true;
  }

  const host = document.getElementById('actionGraph');
  if (host) host.addEventListener('keydown', moveFocus);
  const box = document.getElementById('graphScroll');
  if (box) box.addEventListener('scroll', pinGutter);
  window.civArenaJournal = {render, moveFocus};
})();

'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const svgNS = 'http://www.w3.org/2000/svg';
  const state = {runId: '', runPinned: false, turn: null, follow: true, filter: 'all',
    data: null, selectedCall: null, busy: false, timer: null, lastGraph: '', lastCards: '',
    callsByKey: new Map(), lastStrategy: ''};
  const observation = tool => /^(get_|observe|read_|list_|query_|inspect_)/.test(tool || '');
  const noteTool = tool => /(diary|journal|goal|prediction|lesson|note|recall|strategy)/.test(tool || '');
  const kindOf = tool => observation(tool) ? 'Observation' : noteTool(tool) ? 'Note' : 'Action';
  const isAccepted = status => ['accepted', 'ok', 'success', 'completed'].includes(status);
  const text = value => value == null ? 'Not recorded' : typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const count = value => finite(value) ? value.toLocaleString() : '—';
  const humanize = value => String(value || '').replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
  const timestamp = value => {
    if (value == null || value === '') return null;
    const date = typeof value === 'number' ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  };
  const secondsBetween = (a, b) => {
    const start = timestamp(a), end = timestamp(b);
    return start && end ? Math.max(0, (end - start) / 1000) : null;
  };
  const duration = seconds => {
    if (!finite(seconds)) return '—';
    if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
    if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
    const minutes = Math.floor(seconds / 60), remainder = Math.floor(seconds % 60);
    return `${minutes}m ${String(remainder).padStart(2, '0')}s`;
  };
  const localTime = value => {
    const date = timestamp(value);
    return date ? date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : 'Not recorded';
  };
  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content != null) node.textContent = content;
    return node;
  }
  function append(parent, ...children) { children.forEach(child => parent.appendChild(child)); return parent; }
  function badge(status, label) {
    const good = isAccepted(status) || status === 'running';
    const bad = ['rejected', 'failed', 'aborted', 'error'].includes(status);
    return element('span', `badge${good ? ' good' : bad ? ' bad' : ''}`, label || humanize(status || 'Pending'));
  }
  function svg(tag, attrs, content) {
    const node = document.createElementNS(svgNS, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (content != null) node.textContent = content;
    return node;
  }
  function callKey(turn, call) { return `${turn.turn}:${turn.player_id}:${call.seq}`; }
  function selectedTurns() {
    return (state.data?.turns || []).filter(turn => String(turn.turn) === String(state.turn));
  }
  function agentList() {
    const agents = [...(state.data?.agents || [])].sort((a, b) => Number(a.player_id) - Number(b.player_id));
    return [agents[0] || {player_id: 0}, agents[1] || {player_id: 1}];
  }
  function seatTurn(agent) { return selectedTurns().find(turn => String(turn.player_id) === String(agent.player_id)); }
  function updateConnection(ok, message) {
    $('connectionDot').className = `status-dot ${ok ? 'connected' : 'error'}`;
    $('connectionText').textContent = message;
  }
  function renderMetrics() {
    const data = state.data, metrics = data?.metrics || {};
    $('roundCount').textContent = count(metrics.completed_rounds);
    $('turnCount').textContent = count(metrics.completed_seat_turns);
    $('requestCount').textContent = count(metrics.requests);
    $('violationCount').textContent = count(metrics.violations);
    $('violationCount').classList.toggle('has-violations', metrics.violations > 0);
    $('violationFoot').textContent = metrics.violations === 0 ? 'None recorded' : metrics.violations > 0 ? 'Recorded by the watchdog' : 'Awaiting match records';
    const labels = {completed: 'Match complete', aborted: 'Match stopped', running: 'Recent activity',
      stalled: 'No recent events', incomplete: 'Incomplete record', startup: 'Starting match'};
    const status = data?.status;
    const node = badge(status, labels[status] || humanize(status || 'Waiting for a match'));
    $('matchStatus').className = node.className;
    $('matchStatus').textContent = node.textContent;
    const last = timestamp(data?.last_event_at);
    $('lastEvent').textContent = last ? `Last event ${localTime(last)}` : 'No events received yet';
    const warnings = (data?.warnings || []).filter(value => value != null);
    $('warnings').replaceChildren(...warnings.map(value => element('p', '', text(value))));
    $('warnings').hidden = warnings.length === 0;
  }
  function renderTurnSelect() {
    const values = [...new Set((state.data?.turns || []).map(turn => turn.turn))].sort((a, b) => Number(a) - Number(b));
    if (state.follow || !values.some(value => String(value) === String(state.turn))) state.turn = values.at(-1) ?? null;
    const select = $('turnSelect');
    select.replaceChildren(...(values.length ? values.map(value => {
      const option = element('option', '', `Turn ${value}`); option.value = String(value); return option;
    }) : [element('option', '', '—')]));
    select.disabled = !values.length;
    select.value = state.turn == null ? '' : String(state.turn);
    $('graphTurn').textContent = state.turn == null ? '' : ` / ${state.turn}`;
  }
  function renderCards() {
    const agents = agentList();
    const signature = JSON.stringify([agents, selectedTurns()]);
    if (signature === state.lastCards) return;
    state.lastCards = signature;
    const cards = agents.map((agent, seat) => {
      const turn = seatTurn(agent);
      const calls = turn?.calls || [];
      const model = agent.model || agent.agent_id || 'Agent not recorded';
      const card = element('article', `seat-card ${seat === 0 ? 'gold' : 'teal'}`);
      const identity = append(element('div', 'seat-identity'), element('div', 'seat-icon', seat === 0 ? '♜' : '♝'),
        append(element('div'), element('p', 'eyebrow', `SEAT ${seat + 1}`), element('h2', '', model)));
      const statusLabels = {completed: 'Turn complete', running: 'Turn in progress', active: 'Turn in progress',
        aborted: 'Turn stopped', incomplete: 'Unfinished', pending: 'Awaiting turn'};
      append(card, append(element('div', 'seat-header'), identity,
        badge(turn?.status, turn ? statusLabels[turn.status] || humanize(turn.status || 'Recorded turn') : 'Awaiting turn')));
      const first = calls.filter(call => kindOf(call.tool) === 'Action' && isAccepted(call.status))
        .find(call => timestamp(call.ended_at));
      const firstDelay = first ? secondsBetween(turn?.started_at, first.ended_at) : null;
      const elapsed = turn?.ended_at ? secondsBetween(turn.started_at, turn.ended_at) : turn?.elapsed_s;
      const timings = element('div', 'seat-timings');
      [['First accepted action', duration(firstDelay)],
        [turn?.ended_at ? 'Turn completed in' : 'Turn elapsed', duration(elapsed)],
        ['Model requests', count(turn?.requests)]].forEach(([label, value]) => {
        append(timings, append(element('div'), element('span', '', label), element('strong', '', value)));
      });
      append(card, timings);
      const notes = (turn?.notes || []).filter(note => isAccepted(note.status));
      const noteBox = element('section', 'seat-notes');
      append(noteBox, append(element('div', 'notes-heading'), element('h3', '', 'Recorded plans & notes'),
        element('span', '', notes.length ? `${notes.length} recorded` : '')));
      const list = element('div', 'notes-list');
      if (!notes.length) append(list, element('p', 'note-empty', 'No plans or notes recorded for this turn.'));
      notes.forEach(note => {
        const entry = element('p', 'note');
        append(entry, element('span', 'note-label', humanize(note.tool || 'Note')), element('span', '', text(note.text)));
        if (note.confidence != null) append(entry, element('span', 'model-confidence', `Model-reported confidence: ${text(note.confidence)}`));
        append(list, entry);
      });
      append(card, append(noteBox, list));
      return card;
    });
    $('seatCards').replaceChildren(...cards);
  }
  function renderGraph() {
    const turns = selectedTurns(), agents = agentList();
    const signature = JSON.stringify([state.runId, turns, state.filter]);
    state.callsByKey = new Map();
    turns.forEach(turn => (turn.calls || []).forEach(call => state.callsByKey.set(callKey(turn, call), {turn, call})));
    if (signature === state.lastGraph) { renderDetail(); return; }
    state.lastGraph = signature;
    const lanes = agents.map(agent => {
      const turn = seatTurn(agent);
      const calls = (turn?.calls || []).filter(call => state.filter === 'all' ||
        (state.filter === 'observations' ? kindOf(call.tool) === 'Observation' : kindOf(call.tool) === 'Action'));
      return {agent, turn, calls};
    });
    const graph = $('actionGraph');
    const maximum = Math.max(...lanes.map(lane => lane.calls.length), 0);
    const height = maximum ? maximum * 75 + 59 : 300;
    graph.setAttribute('viewBox', `0 0 720 ${height}`);
    graph.replaceChildren();
    const defs = svg('defs');
    ['gold', 'teal'].forEach((name, index) => {
      const marker = svg('marker', {id: `arrow-${name}`, viewBox: '0 0 10 10', refX: 5, refY: 5,
        markerWidth: 5, markerHeight: 5, orient: 'auto-start-reverse'});
      append(marker, svg('path', {d: 'M 0 0 L 10 5 L 0 10 z', fill: index === 0 ? '#806e4d' : '#3b746b'}));
      append(defs, marker);
    });
    append(graph, defs);
    lanes.forEach(({turn, calls}, seat) => {
      const x = seat === 0 ? 29 : 389, color = seat === 0 ? '#e5bd73' : '#65cbb8';
      append(graph, svg('text', {x: x + 5, y: 27, fill: color, class: 'lane-label'}, `SEAT ${seat + 1}`));
      if (!calls.length && maximum) append(graph, svg('text', {x: x + 5, y: 70, fill: '#8292a8', 'font-size': 11}, 'No matching calls recorded'));
      calls.forEach((call, index) => {
        const y = 44 + index * 75, key = callKey(turn, call);
        if (index > 0) append(graph, svg('path', {d: `M ${x + 149} ${y - 22} L ${x + 149} ${y - 7}`,
          stroke: seat === 0 ? '#806e4d' : '#3b746b', 'stroke-width': 1.2, 'marker-end': `url(#arrow-${seat === 0 ? 'gold' : 'teal'})`}));
        const group = svg('g', {class: `graph-node seat-${seat === 0 ? 'gold' : 'teal'}${state.selectedCall === key ? ' selected' : ''}`, role: 'button', tabindex: 0,
          'aria-label': `Seat ${seat + 1}, ${humanize(call.tool)}, ${call.status || 'pending'}, recorded call ${call.seq}`,
          'data-key': key});
        append(group, svg('title', {}, `${call.tool} · ${call.status || 'pending'} · ${localTime(call.ts)}`),
          svg('rect', {x, y, width: 301, height: 54, rx: 8, class: 'node-bg'}),
          svg('text', {x: x + 13, y: y + 24, class: 'node-index'}, String(index + 1).padStart(2, '0')),
          svg('text', {x: x + 42, y: y + 22, class: 'node-label'}, humanize(call.tool).slice(0, 32)),
          svg('text', {x: x + 42, y: y + 39, class: 'node-meta'}, `${kindOf(call.tool)} · ${humanize(call.status || 'pending')}`),
          svg('circle', {cx: x + 285, cy: y + 25, r: 3,
            fill: isAccepted(call.status) ? color : ['rejected', 'failed', 'error'].includes(call.status) ? '#f08f91' : '#64768e'}));
        const activate = () => { state.selectedCall = key; markSelection(); renderDetail(); };
        group.addEventListener('click', activate);
        group.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(); } });
        append(graph, group);
      });
    });
    const shown = lanes.reduce((total, lane) => total + lane.calls.length, 0);
    $('callCount').textContent = `${shown} ${shown === 1 ? 'call' : 'calls'}`;
    $('graphEmpty').hidden = shown > 0;
    $('graphEmpty').textContent = !state.data ? 'Select a match to explore its turns.' :
      state.turn == null ? 'No seat turns recorded yet.' : 'No matching calls recorded for this turn.';
    renderDetail();
  }
  function markSelection() {
    document.querySelectorAll('.graph-node').forEach(node => node.classList.toggle('selected', node.getAttribute('data-key') === state.selectedCall));
  }
  function renderDetail() {
    const selected = state.callsByKey.get(state.selectedCall);
    const body = $('detailBody'), status = $('detailStatus');
    if (!selected) {
      $('detailTitle').textContent = 'Select a call'; status.hidden = true;
      const empty = append(element('div', 'detail-empty'), element('span', '', '⌖'),
        element('p', '', 'Every action leaves a record.'),
        element('small', '', 'Choose a node to see what was requested and what the game returned.'));
      body.replaceChildren(empty); return;
    }
    const scrollPosition = body.scrollTop;
    const {turn, call} = selected;
    $('detailTitle').textContent = humanize(call.tool);
    const nextBadge = badge(call.status);
    status.className = nextBadge.className; status.textContent = nextBadge.textContent; status.hidden = false;
    const meta = element('dl', 'detail-meta');
    const seat = agentList().findIndex(agent => String(agent.player_id) === String(turn.player_id));
    [['Seat / turn', `${seat < 0 ? '—' : seat + 1} / ${turn.turn}`], ['Recorded call', `#${call.seq}`],
      ['Requested at', localTime(call.ts)], ['Response time', duration(secondsBetween(call.ts, call.ended_at))]].forEach(([label, value]) =>
      append(meta, append(element('div'), element('dt', '', label), element('dd', '', value))));
    body.replaceChildren(meta);
    [['Arguments', call.args], ['Result', call.result]].forEach(([label, value]) =>
      append(body, append(element('section', 'detail-section'), element('h3', '', label),
        element('pre', '', value == null ? label === 'Result' ? 'No result recorded yet.' : 'No arguments recorded.' : text(value)))));
    body.scrollTop = scrollPosition;
  }
  function renderStrategy() {
    const signature = JSON.stringify([state.runId, selectedTurns().map(turn => [turn.player_id, turn.turn, turn.strategy, turn.scouting_graph])]);
    if (signature === state.lastStrategy) return;
    state.lastStrategy = signature;
    const panels = agentList().map((agent, seat) => {
      const turn = seatTurn(agent), strategy = turn?.strategy, graph = turn?.scouting_graph;
      const panel = element('article', `strategy-seat ${seat === 0 ? 'gold' : 'teal'}`);
      append(panel, element('h3', '', `Seat ${seat + 1}`));
      if (!strategy) {
        append(panel, element('p', 'subtle', 'No strategy controller record for this turn.'));
        return panel;
      }
      append(panel, badge('completed', strategy.source === 'model' ? 'Model strategy update' : 'Autopilot turn'),
        element('p', 'subtle', `Last model decision: turn ${strategy.last_decision_turn ?? '—'}`));
      const directive = element('details', 'strategy-json');
      append(directive, element('summary', '', 'Strategy directive and triggers'),
        element('pre', '', text({directive: strategy.directive, reasons: strategy.reasons, seed: strategy.seed})));
      append(panel, directive);
      if (!graph) {
        append(panel, element('p', 'subtle', 'Scouting execution has not been recorded.'));
        return panel;
      }
      const decisions = Array.isArray(graph.decisions) ? graph.decisions.filter(x => x && typeof x === 'object') : [];
      if (!decisions.length) append(panel, element('p', 'subtle', 'No scouting decisions recorded.'));
      decisions.forEach(initial => {
        const execution = Array.isArray(graph.execution) ? graph.execution.filter(row => row?.unit_id === initial.unit_id) : [];
        const decision = execution.find(row => row?.decision && typeof row.decision === 'object')?.decision || initial;
        const item = element('section', 'scout-decision');
        append(item, element('h4', '', `${decision.unit_id} · ${decision.override ? 'Tactical override' : 'Autopilot'}`));
        const candidates = Array.isArray(decision.candidates) ? decision.candidates.filter(x => x && typeof x === 'object') : [];
        const selected = decision.selected;
        append(item, element('p', 'subtle', selected ? `Requested ${selected.action}: ${text(selected.args)}` : text(decision.reason || 'No action selected')));
        if (candidates.length) {
          const chart = svg('svg', {viewBox: `0 0 520 ${Math.max(90, candidates.length * 42 + 20)}`, role: 'img',
            'aria-label': `Scouting alternatives for ${decision.unit_id}`});
          const middle = candidates.length * 21 + 10;
          append(chart, svg('circle', {cx: 24, cy: middle, r: 7, fill: seat ? '#65cbb8' : '#e5bd73'}));
          candidates.forEach((candidate, index) => {
            const y = 20 + index * 42, probability = finite(candidate.probability) ? candidate.probability : null;
            const excluded = Boolean(candidate.excluded), color = excluded ? '#64768e' : seat ? '#65cbb8' : '#e5bd73';
            append(chart, svg('path', {d: `M 31 ${middle} L 67 ${y}`, stroke: color, opacity: 0.5, fill: 'none'}),
              svg('rect', {x: 72, y: y - 12, width: probability == null ? 0 : Math.max(0, Math.min(1, probability)) * 120,
                height: 23, rx: 3, fill: color, opacity: 0.3}),
              svg('text', {x: 79, y: y + 4, fill: '#bfcee1', 'font-size': 12},
                `${text(candidate.dest).replace(/\s+/g, ' ')} · ${excluded ? 'excluded' : probability == null ? 'unscored' : (100 * probability).toFixed(1) + '%'} · score ${finite(candidate.score) ? candidate.score.toFixed(2) : '—'}`));
          });
          append(item, chart);
        }
        const details = element('details', 'strategy-json');
        append(details, element('summary', '', 'Scores, seed, and observed execution'),
          element('pre', '', text({decision, execution})));
        append(item, details); append(panel, item);
      });
      if (finite(graph.deferred_units) && graph.deferred_units > 0) {
        append(panel, element('p', 'subtle', `Deferred units: ${text(graph.deferred_units)}`));
      }
      return panel;
    });
    $('strategyChoices').replaceChildren(...panels);
  }
  function render() {
    renderMetrics(); renderTurnSelect(); renderCards(); renderGraph(); renderStrategy();
    $('emptyState').hidden = Boolean(state.data);
  }
  async function request(path) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path, {signal: controller.signal, cache: 'no-store'});
      if (!response.ok) throw new Error(`Request failed (${response.status})`);
      return await response.json();
    } finally { clearTimeout(timer); }
  }
  function updateRuns(runs) {
    const sorted = [...runs].sort((a, b) => (timestamp(b.updated_at)?.getTime() || 0) - (timestamp(a.updated_at)?.getTime() || 0));
    const stillPresent = sorted.some(run => run.id === state.runId);
    if (!state.runPinned || !stillPresent) {
      const candidate = sorted.find(run => !/(^|[-_/])startup($|[-_/])/.test(run.id)) || sorted[0];
      if ((candidate?.id || '') !== state.runId) {
        state.runId = candidate?.id || ''; state.turn = null; state.selectedCall = null; state.data = null;
      }
    }
    const select = $('runSelect');
    select.replaceChildren(...(sorted.length ? sorted.map(run => {
      const option = element('option', '', `${run.id} · ${humanize(run.status)}`); option.value = run.id; return option;
    }) : [element('option', '', 'No matches recorded')]));
    select.value = state.runId; select.disabled = !sorted.length;
  }
  async function refresh() {
    if (state.busy) return;
    clearTimeout(state.timer); state.busy = true;
    try {
      const inventory = await request('/api/runs');
      if (!Array.isArray(inventory.runs)) throw new Error('Unavailable match inventory');
      updateRuns(inventory.runs);
      if (state.runId) {
        const requestedId = state.runId;
        const data = await request(`/api/run?id=${encodeURIComponent(requestedId)}`);
        if (state.runId !== requestedId) return;
        if (!Array.isArray(data.turns) || !Array.isArray(data.agents)) throw new Error('Unavailable match records');
        state.data = data;
      } else state.data = null;
      render();
      updateConnection(true, state.data ? 'Match records synced · every 2s' : 'Connected · waiting for a match');
    } catch (error) {
      updateConnection(false, 'Connection interrupted · retrying');
      if (!state.data) { $('emptyState').hidden = false; $('emptyState').querySelector('h2').textContent = 'Waiting for match records'; }
    } finally {
      state.busy = false; state.timer = setTimeout(refresh, 2000);
    }
  }
  $('runSelect').addEventListener('change', event => {
    state.runId = event.target.value; state.runPinned = true; state.turn = null;
    state.selectedCall = null; state.data = null; state.lastCards = ''; state.lastGraph = '';
    render(); refresh();
  });
  $('turnSelect').addEventListener('change', event => {
    state.turn = event.target.value; state.follow = false; $('followLive').checked = false;
    state.selectedCall = null; renderCards(); renderGraph(); renderStrategy();
    $('graphTurn').textContent = ` / ${state.turn}`;
  });
  $('followLive').addEventListener('change', event => { state.follow = event.target.checked; render(); });
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(item => {
      const active = item === button; item.classList.toggle('selected', active); item.setAttribute('aria-pressed', String(active));
    });
    renderGraph();
  }));
  renderCards(); refresh();
})();

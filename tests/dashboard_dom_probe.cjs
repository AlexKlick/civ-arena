// Execute the production frontend with a minimal DOM. Browser layout is checked separately.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const count = Number(process.argv[3]);
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attributes = {}; this.listeners = {};
    this.className = ''; this._text = ''; this.style = {setProperty(key, value) { this[key] = value; }};
    this.classList = {toggle: (name, yes) => {
      const classes = new Set(this.className.split(' ').filter(Boolean));
      if (yes) classes.add(name); else classes.delete(name); this.className = [...classes].join(' ');
    }};
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  setAttribute(key, value) { this.attributes[key] = value; if (key === 'class') this.className = value; }
  getAttribute(key) { return this.attributes[key]; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  querySelector(tag) { return flatten(this).find(child => child.tag === tag); }
}
const flatten = node => node.children.flatMap(child => [child, ...flatten(child)]);
const nodes = new Map();
const document = {
  getElementById: id => { if (!nodes.has(id)) nodes.set(id, new Element('div')); return nodes.get(id); },
  createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
  querySelectorAll: selector => selector === '.graph-node' ? [...nodes.values()].flatMap(flatten)
    .filter(node => node.className.split(' ').includes('graph-node')) : []
};
const stamp = '2026-09-06T20:00:00Z';
const fixture = {
  status: 'completed', warnings: [], last_event_at: stamp,
  metrics: {completed_rounds: 1, completed_seat_turns: count, requests: count, violations: 0},
  agents: Array.from({length: count}, (_, pid) => ({player_id: pid, agent_id: `model-${pid}`, model: `Model ${pid}`})).reverse(),
  turns: Array.from({length: count}, (_, pid) => ({turn: 1, player_id: pid, status: 'completed',
    started_at: stamp, ended_at: stamp, requests: 1, notes: [],
    calls: [{seq: pid + 10, tool: 'move_unit', args: {unit_id: `u${pid}:1`}, status: 'accepted', ts: stamp, ended_at: stamp}],
    strategy: {source: 'model', last_decision_turn: 1, directive: {version: 1}},
    scouting_graph: {decisions: [{unit_id: `u${pid}:1`, candidates: [{dest: '1,2', probability: 1, score: 1}]}]}
  }))
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {document, console, AbortController,
  setTimeout: () => 1, clearTimeout: () => {}, fetch: async path => ({ok: true,
    json: async () => path === '/api/runs' ? {runs: [{id: 'fixture', updated_at: stamp}]} : fixture})});
setImmediate(() => {
  const cards = document.getElementById('seatCards').children;
  const panels = document.getElementById('strategyChoices').children;
  const graph = document.getElementById('actionGraph');
  const labels = graph.children.filter(node => node.className === 'lane-label');
  const actions = graph.children.filter(node => node.className.includes('graph-node'));
  assert.equal(cards.length, count); assert.equal(panels.length, count); assert.equal(labels.length, count);
  assert.equal(document.getElementById('seatLegend').children.length, count);
  assert.equal(graph.attributes.viewBox.split(' ')[2], String(Math.max(720, count * 360)));
  const xs = actions.map(node => Number(node.children.find(child => child.tag === 'rect').attributes.x));
  for (let i = 0; i < count; i++) {
    assert(cards[i].textContent.includes(`SEAT ${i + 1}Model ${i}`));
    assert(panels[i].textContent.includes(`Seat ${i + 1}`));
    assert.equal(labels[i].textContent, `SEAT ${i + 1}`);
    if (i) assert(xs[i] >= xs[i - 1] + 301, 'action lanes overlap');
    actions[i].listeners.click();
    assert(document.getElementById('detailBody').textContent.includes(`${i + 1} / 1`));
    assert(document.getElementById('detailBody').textContent.includes(`u${i}:1`));
  }
  assert.equal(new Set(cards.map(card => card.style['--seat'])).size, count);
  console.log(JSON.stringify({seats: count, cards: cards.length, scouting_panels: panels.length, lane_x: xs,
    colors: cards.map(card => card.style['--seat']), details_checked: count}));
});

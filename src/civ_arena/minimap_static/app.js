'use strict';
const data = JSON.parse(document.getElementById('data').textContent);
const palette = JSON.parse(document.getElementById('palette').textContent);
const T = palette.tokens;
const $ = id => document.getElementById(id);
const ns = 'http://www.w3.org/2000/svg';
const svg = (tag, attrs, parent) => {
  const e = document.createElementNS(ns, tag);
  Object.entries(attrs).forEach(([k, v]) => { if (v !== null && v !== undefined) e.setAttribute(k, v); });
  if (parent) parent.append(e);
  return e;
};
const text = (tag, value, parent, cls) => {
  const e = document.createElement(tag); e.textContent = value;
  if (cls) e.className = cls;
  parent.append(e); return e;
};
const pretty = value => JSON.stringify(value, null, 2);
const option = (parent, value, label) => {
  const e = text('option', label, parent); e.value = value; return e;
};
const civName = value => typeof value === 'string' && value !== '' ? value : null;
let snapshots = [], rows = [], actors = [], productions = [], windows = [], bounds, view, selected = null;
let tiles = new Map(), seatIds = [], roster = {ids: null, names: new Map()}, layers = {}, latestSeq = () => undefined;
for (const seat of data.seats) option($('perspective'), String(seat.player_id), `Player ${seat.player_id}`);
if (data.scope === 'combined_observation_preview') option($('perspective'), 'union', 'Spectator · combined receipts');
$('scope').textContent = data.scope === 'player_only'
  ? 'Player-only export: other seat packets are not embedded. Blank space is unknown. This is a retained receipt history, not current visibility.'
  : 'SPECTATOR EXPORT: contains multiple private player perspectives. Player selector filters display only. Combined receipts are asynchronous, not a full or simultaneous world map.';
for (const line of data.limits) text('p', line, $('custody'));
text('p', data.axis, $('custody'));
text('pre', pretty(data.event_binding), $('custody'));
text('p', `Artifact SHA-256 ${data.digest}`, $('custody'));
if (data.dashboard_source) {
  text('pre', pretty(data.dashboard_source), $('custody'));
  $('scope').append(document.createTextNode(' Dashboard snapshot: refresh explicitly for newer packets.' +
    (data.dashboard_source.partial_trailing_record_ignored ? ' An incomplete trailing event was ignored; only the complete prefix is shown.' : '')));
}
// Shared pattern definitions: the legend swatches reference them by id too.
{
  const d = svg('defs', {}, $('map'));
  const p = svg('pattern', {id: 'unknown-hatch', patternUnits: 'userSpaceOnUse', width: 6, height: 6, patternTransform: 'rotate(45)'}, d);
  svg('rect', {width: 6, height: 6, fill: palette.terrain.UNKNOWN.fill}, p);
  svg('line', {x1: 0, y1: 0, x2: 0, y2: 6, stroke: palette.terrain.UNKNOWN.hatch, 'stroke-width': 1.2}, p);
}
const terrainFill = key => key === 'UNKNOWN' ? 'url(#unknown-hatch)' : palette.terrain[key].fill;
const classify = id => ownerClass(id, seatIds, roster.ids);
const resolve = alternatives => resolveOwnership(alternatives, latestSeq);
const ownerColorFor = id => ownerColor(id, classify(id), seatIds, roster.ids, palette);
const ownerName = id => roster.names.has(id) ? ` · ${roster.names.get(id)}` : '';
function ownerLabel(id) {
  const cls = classify(id);
  const kind = cls === 'seat' ? 'seat' : cls === 'major' ? 'roster major' : palette.owners[cls].label;
  return `P${id}${ownerName(id)} — ${kind}`;
}
function ownershipSentence(own) {
  if (own.status === 'owned') return `Owned by ${ownerLabel(own.owner_id)}`;
  if (own.status === 'unowned') return 'Observed unowned (-1) at receipt';
  if (own.status === 'null_field') return 'Ownership field supplied as null';
  if (own.status === 'invalid') return 'Ownership value unsupported';
  return 'Ownership not observed (remembered hex or owner key unsupplied)';
}
function receiptSentence(r) {
  const latest = latestSeq(r.player_id);
  return `seq ${r.seq} · T${r.turn} · ` + (r.seq === latest ? `latest for seat P${r.player_id}`
    : `older than seat P${r.player_id}'s latest seq ${latest} (dimmed)`);
}
function computeRoster(snaps) {
  const ids = new Set(), names = new Map();
  let supplied = false;
  for (const s of snaps) {
    if (civName(s.you && s.you.civ_name)) names.set(s.receipt.player_id, s.you.civ_name);
    if (!Array.isArray(s.public_players)) continue;
    supplied = true;
    for (const p of s.public_players) {
      if (!p || !Number.isInteger(p.player_id)) continue;
      ids.add(p.player_id);
      if (civName(p.civ_name)) names.set(p.player_id, p.civ_name);
    }
  }
  return {ids: supplied ? [...ids].sort((a, b) => a - b) : null, names};
}
// Elevation marks are paths, not font glyphs, so no font can turn them into tofu.
const markPath = (x, y, kind) => kind === 'mountain'
  ? `M${fmt(x)} ${fmt(y - 5.5)} L${fmt(x + 5.5)} ${fmt(y + 3.5)} L${fmt(x - 5.5)} ${fmt(y + 3.5)} Z`
  : `M${fmt(x - 5.5)} ${fmt(y + 2)} q2.75 -4.5 5.5 0 q2.75 -4.5 5.5 0`;
function drawMark(parent, d, kind, stale, count) {
  const g = svg('g', {class: `elev elev-${kind}`, 'data-stale': stale ? 1 : 0, 'data-count': count}, parent);
  svg('path', {class: 'elev-halo', d}, g);
  svg('path', {class: 'elev-ink', d}, g);
  return g;
}
function drawUnitBadge(parent, x, y, spec) {
  const role = palette.roles[spec.spec.role] || palette.roles.unknown, r = 7;
  const g = svg('g', {class: `badge badge-unit role-${spec.spec.role}`, 'data-glyph': spec.spec.glyph}, parent);
  if (role.shape === 'square') {
    svg('rect', {class: 'badge-halo', x: x - r - 1.2, y: y - r - 1.2, width: 2 * r + 2.4, height: 2 * r + 2.4, rx: 3.5}, g);
    svg('rect', {class: 'badge-shape', x: x - r + .8, y: y - r + .8, width: 2 * r - 1.6, height: 2 * r - 1.6, rx: 2, fill: spec.color}, g);
  } else {
    svg('circle', {class: 'badge-halo', cx: x, cy: y, r: r + 1.2}, g);
    svg('circle', {class: `badge-shape${role.dashed ? ' dashed' : ''}`, cx: x, cy: y, r, fill: spec.color}, g);
  }
  svg('text', {class: 'badge-glyph', x, y: y + 2.6}, g).textContent = spec.spec.glyph;
  if (Number.isInteger(spec.hpBucket)) {
    const rr = r + 3.4, filled = Math.max(0, Math.min(palette.hp_ring.segments, spec.hpBucket));
    for (let k = 0; k < palette.hp_ring.segments; k++) {
      const step = 360 / palette.hp_ring.segments, a0 = (-90 + k * step + 6) * Math.PI / 180, a1 = (-90 + (k + 1) * step - 6) * Math.PI / 180;
      svg('path', {class: `hp-seg ${k < filled ? 'filled' : 'empty'}`, stroke: k < filled ? spec.color : null,
        d: `M${fmt(x + rr * Math.cos(a0))} ${fmt(y + rr * Math.sin(a0))} A${rr} ${rr} 0 0 1 ${fmt(x + rr * Math.cos(a1))} ${fmt(y + rr * Math.sin(a1))}`}, g);
    }
  }
  if (spec.fortified) svg('circle', {class: 'fortified', cx: x, cy: y, r: r - 2.2}, g);
  return g;
}
function drawCityBadge(parent, x, y, spec) {
  const g = svg('g', {class: 'badge badge-city'}, parent);
  const ring = radius => hexVertices(x, y, radius).map(p => p.map(fmt).join(',')).join(' ');
  svg('polygon', {class: 'badge-halo', points: ring(11)}, g);
  svg('polygon', {class: 'badge-shape', points: ring(9.5), fill: spec.color}, g);
  svg('text', {class: 'badge-glyph city-pop', x, y: y + 2.8}, g).textContent =
    Number.isInteger(spec.population) ? String(spec.population) : '·';
  return g;
}
function borderSample(s, color, style) {
  const b = palette.borders[style];
  svg('line', {class: 'border', x1: -13, y1: 0, x2: 13, y2: 0, stroke: color, 'stroke-width': b.width, 'stroke-dasharray': b.dash}, s);
}
function perspective() {
  const union = $('perspective').value === 'union';
  $('snapshot').replaceChildren();
  if (union) {
    option($('snapshot'), 'latest', 'Latest supplied packet per seat');
  } else {
    const seat = data.seats.find(s => String(s.player_id) === $('perspective').value);
    seat.snapshots.forEach((s, i) => option($('snapshot'), String(i), `T${s.turn} · seq ${s.seq}`));
    $('snapshot').value = String(seat.snapshots.length - 1);
    if ((seat.productive_actions || []).length) {
      option($('snapshot'), 'receipts', 'Latest packet + later action receipts');
      $('snapshot').value = 'receipts';
    }
  }
  draw();
}
function draw() {
  selected = null;
  const union = $('perspective').value === 'union';
  const seats = union ? data.seats : data.seats.filter(s => String(s.player_id) === $('perspective').value);
  const latestReceipts = union || $('snapshot').value === 'receipts';
  snapshots = seats.map(s => latestReceipts ? s.snapshots.at(-1) : s.snapshots[Number($('snapshot').value)]);
  windows = seats.map((s, i) => ({player_id:s.player_id,
    ...(latestReceipts ? s.productive_cutoff || snapshots[i] : snapshots[i])}));
  productions = seats.flatMap((seat, i) => (seat.productive_actions || [])
    .filter(a => a.call.seq <= windows[i].seq && a.call.turn <= windows[i].turn)
    .map(a => a.result && (a.result.seq > windows[i].seq || a.result.turn > windows[i].turn)
      ? {...a, result:null, admission:null, status:'unconfirmed'} : a));
  // Keep each perspective's receipts. Overlap gets a single paint but retains all alternatives.
  tiles = new Map();
  snapshots.forEach(s => s.terrain.forEach(row => {
    if (!tiles.has(row.observation.coord)) tiles.set(row.observation.coord, []);
    tiles.get(row.observation.coord).push(row);
  }));
  rows = [...tiles.values()]; actors = snapshots.flatMap(s => s.actors);
  seatIds = snapshots.map(s => s.receipt.player_id);
  const seqBySeat = new Map(snapshots.map(s => [s.receipt.player_id, s.seq]));
  latestSeq = pid => seqBySeat.get(pid);
  roster = computeRoster(snapshots);
  $('world').replaceChildren(); $('mini-world').replaceChildren(); $('actors').replaceChildren();
  option($('actors'), '', 'Select an actor…');
  layers = {};
  for (const name of ['tiles', 'tints', 'borders', 'elevation', 'paths', 'districts', 'actors']) {
    layers[name] = svg('g', {class: `layer layer-${name}`}, $('world'));
  }
  const marks = {hills: {0: [], 1: []}, mountain: {0: [], 1: []}};
  const tileFragment = document.createDocumentFragment(), miniFragment = document.createDocumentFragment();
  rows.forEach(alternatives => {
    const row = alternatives[0], t = row.observation, cls = classifyTile(t, palette);
    const stale = row.receipt.seq !== latestSeq(row.receipt.player_id) ? 1 : 0;
    const attrs = {points: hexPoints(t.coord), fill: terrainFill(cls.fillKey), opacity: stale ? .5 : 1};
    const h = svg('polygon', {...attrs, class: 'tile', 'data-terrain': cls.fillKey, 'data-elevation': cls.elevation,
      tabindex: 0, role: 'button', 'aria-label': `Hex ${t.coord} · ${terrainName(t, palette)} · ${ownershipSentence(resolve(alternatives))}`});
    tileFragment.append(h);
    const pick = () => selectTile(alternatives);
    h.addEventListener('click', pick);
    h.addEventListener('keydown', e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); pick();}});
    miniFragment.append(svg('polygon', {points: attrs.points, fill: cls.fillKey === 'UNKNOWN' ? palette.terrain.UNKNOWN.fill : attrs.fill, opacity: attrs.opacity}));
    if (cls.elevation) marks[cls.elevation][stale].push(markPath(...point(t.coord), cls.elevation));
  });
  layers.tiles.append(tileFragment); $('mini-world').append(miniFragment);
  for (const [key, g] of tintGroups(tiles, resolve, classify)) {
    svg('path', {class: 'tint', d: hexOutlinePath(g.hexes), fill: ownerColorFor(g.owner_id), 'fill-opacity': palette.tint_opacity,
      'data-owner-class': g.ownerClass, 'data-owner': g.owner_id, 'data-stale': g.stale ? 1 : 0, 'data-key': key}, layers.tints);
  }
  for (const [key, g] of borderSegments(tiles, resolve, classify, palette.borders.inset)) {
    const style = palette.borders[g.style];
    svg('path', {class: 'border', d: segmentPath(g.segments), stroke: ownerColorFor(g.owner_id), 'stroke-width': style.width,
      'stroke-dasharray': style.dash, 'data-owner-class': g.ownerClass, 'data-owner': g.owner_id, 'data-style': g.style,
      'data-stale': g.stale ? 1 : 0, 'data-segments': g.segments.length, 'data-key': key}, layers.borders);
  }
  for (const kind of ['hills', 'mountain']) for (const stale of [0, 1]) {
    if (marks[kind][stale].length) drawMark(layers.elevation, marks[kind][stale].join(' '), kind, stale, marks[kind][stale].length);
  }
  actors.forEach((a, i) => {
    const o = a.observation, [x, y] = point(o.coord), city = a.kind.includes('cities'), owned = a.kind.startsWith('own');
    const barbarian = o.is_barbarian === true, pid = a.receipt.player_id;
    const ownerKnown = Number.isInteger(o.owner_id) && o.owner_id >= 0;
    const color = barbarian ? T.coral : owned ? ownerColorFor(pid) : ownerKnown ? ownerColorFor(o.owner_id) : T.foreign;
    const ownerText = owned ? `own (P${pid})` : barbarian ? 'explicitly classified barbarian'
      : ownerKnown ? `foreign · ${ownerLabel(o.owner_id)}` : 'foreign · owner not supplied';
    const spec = city ? null : unitSpec(o.type, palette);
    const label = city ? (owned ? `P${pid} city` : civName(o.name) || 'foreign city') : barbarian ? '!' : '';
    const g = svg('g', {class: `actor ${city ? 'city' : 'unit'}${owned ? ' own' : ' foreign'}`, tabindex: 0, role: 'button',
      'data-owner-known': owned || ownerKnown ? 1 : 0,
      'aria-label': `${city ? 'City' : spec.label} ${a.id} at ${o.coord} · ${ownerText}${city || spec.known ? '' : ' · unit type not in glyph table'}`}, layers.actors);
    if (city) {
      drawCityBadge(g, x, y, {color, population: o.population});
    } else {
      const hpBucket = Number.isInteger(o.hp_bucket) ? o.hp_bucket : Number.isInteger(o.hp) ? Math.min(4, Math.floor(o.hp / 25)) : null;
      drawUnitBadge(g, x, y, {spec, color, hpBucket, fortified: o.fortified === true});
    }
    const pick = () => selectActor(i);
    g.addEventListener('click', pick); g.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' || ev.key === ' ') {ev.preventDefault(); pick();}
    });
    if (label) svg('text', {class: `label${barbarian ? ' barbarian' : ''}`, x, y: city ? y - 14 : y - 13}, g).textContent = label;
    option($('actors'), String(i), `P${pid} ${a.id} · ${o.type || 'city'} · ${o.coord}`);
  });
  productions.filter(a => a.admission?.kind === 'district').forEach(a => {
    const [x,y] = point(a.admission.coord);
    const e = svg('rect', {class:'production-marker', x:x-8, y:y-8, width:16, height:16,
      tabindex:0, role:'button', 'aria-label':`${a.item_id} placement observed at ${a.admission.coord}`}, layers.districts);
    const pick = () => selectProduction(a);
    e.addEventListener('click', pick);
    e.addEventListener('keydown', ev => {if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();pick();}});
    svg('text',{class:'label',x,y:y+3},layers.districts).textContent='D';
  });
  const coords = [...rows.map(r => r[0].observation.coord), ...actors.map(a => a.observation.coord),
    ...productions.filter(a=>a.admission?.coord).map(a=>a.admission.coord)];
  const xy = coords.map(point), xs=xy.map(p=>p[0]), ys=xy.map(p=>p[1]);
  bounds = xy.length ? [Math.min(...xs)-25,Math.min(...ys)-25,
    Math.max(...xs)-Math.min(...xs)+50,Math.max(...ys)-Math.min(...ys)+50] : [0,0,100,100];
  $('mini').setAttribute('viewBox',bounds.join(' '));
  $('status').textContent = snapshots.map(s => `P${s.receipt.player_id} T${s.turn} / seq ${s.seq}: `+
    `${s.terrain.length} accumulated hexes · ${s.packet_terrain_count} in packet · `+
    `${s.packet_terrain_omitted ?? 'unknown'} omitted · ${s.actors.length} actors`).join(' | ');
  $('selection-title').textContent='Your observation window';
  $('selection-summary').textContent=union ? 'Combined seats may disagree or have different ages. Overlapping tile receipts remain separately inspectable.' : 'Actors belong to the selected packet only. Old terrain is retained with its receipt age; actual visibility is unknown.';
  $('selection').replaceChildren(); $('graph').replaceChildren();
  $('graph-note').textContent='Select an owned unit for recorded candidate choices, or a city for its observed production queue.';
  renderLegend(union);
  renderResearch(union);
  showProductionJournal();
  fit();
}
// Research is packet-scoped, not selection-scoped: it states what that seat's own
// packet recorded, never a tree, a plan or a completion forecast.
function renderResearch(union) {
  const root = $('research'); root.replaceChildren();
  if (union) text('p', 'One block per seat. Each seat states its own packet; seats are asynchronous, so the as-of turns can differ.', root);
  snapshots.forEach(s => {
    const block = text('div', '', root, 'research-seat');
    text('h4', `P${s.receipt.player_id} · packet turn ${s.turn} (seq ${s.seq})`, block);
    const r = s.research;
    if (!r) {
      text('p', 'Research context not supplied in this packet.', block);
      return;
    }
    text('p', `Researching: ${r.researching || 'none recorded'}`, block);
    // An absent researched list is unknown, never a count of zero.
    if (r.researched === null) {
      text('p', 'Researched: not supplied in this packet', block);
    } else {
      text('p', `Researched: ${r.researched.length}`, block);
      const list = document.createElement('details'); block.append(list);
      text('summary', 'List', list);
      const items = document.createElement('ul'); list.append(items);
      r.researched.forEach(name => text('li', name, items));
    }
    const options = r.options || [];
    if (r.options_source === 'observed') {
      text('p', 'Could pick next (recorded options):', block);
      if (!options.length) text('p', 'No options recorded in this packet.', block);
      options.forEach(o => text('span', `${o.tech_id} · ${o.cost}`, block, 'chip'));
    } else {
      text('p', `Options not requested in this packet (${r.options_source || 'source not recorded'})`, block);
    }
  });
}
// The key is drawn by the same functions as the map, so it cannot drift from it.
function renderLegend(union) {
  const root = $('legend-groups'); root.replaceChildren();
  const group = title => { const g = text('div', '', root, 'legend-group'); text('h4', title, g); return g; };
  const item = (g, build, label) => {
    const it = text('div', '', g, 'legend-item');
    if (build) build(svg('svg', {class: 'swatch', viewBox: '-16 -16 32 32', 'aria-hidden': 'true'}, it));
    else text('span', '', it, 'swatch swatch-blank');
    text('span', label, it, 'legend-label');
    return it;
  };
  const ring = radius => hexVertices(0, 0, radius).map(p => p.map(fmt).join(',')).join(' ');
  const tile = (s, key, elevation, stale) => {
    svg('polygon', {class: 'tile', points: ring(12.5), fill: terrainFill(key), opacity: stale ? .5 : 1, 'data-terrain': key}, s);
    if (elevation) drawMark(s, markPath(0, 0, elevation), elevation, false, 1);
  };
  const unit = (s, type, color, hpBucket, fortified) =>
    drawUnitBadge(s, 0, 0, {spec: type ? palette.units[type] : {...palette.unit_fallback, known: false}, color, hpBucket, fortified});
  let g = group('Terrain');
  for (const [key, t] of Object.entries(palette.terrain)) item(g, s => tile(s, key), t.label);
  g = group('Elevation');
  item(g, s => tile(s, 'GRASS', 'hills'), palette.elevation.hills.label);
  item(g, s => tile(s, 'MOUNTAIN', 'mountain'), palette.elevation.mountain.label);
  g = group('Actors');
  for (const [type, u] of Object.entries(palette.units)) item(g, s => unit(s, type, T.seat0, null, false), `${u.glyph} = ${u.label} (${u.role}; ${palette.roles[u.role].shape})`);
  item(g, s => unit(s, null, T.seat0, null, false), `? = ${palette.unit_fallback.label} (dashed)`);
  item(g, s => drawCityBadge(s, 0, 0, {color: T.seat0, population: 12}), 'city — population inside; own actors wear the seat colour');
  item(g, s => unit(s, 'WARRIOR', T.coral, null, false), 'coral + ! = explicitly classified barbarian');
  item(g, s => unit(s, 'WARRIOR', T.foreign, null, false), 'white = foreign · owner not supplied in packet');
  item(g, s => unit(s, 'WARRIOR', T.seat1, 2, false), palette.hp_ring.label);
  item(g, s => unit(s, 'WARRIOR', T.seat1, null, true), 'inner ring = fortified (own units only)');
  item(g, s => svg('rect', {class: 'production-marker', x: -8, y: -8, width: 16, height: 16}, s), '□ historical district placement (queue admission, not completion)');
  g = group('Ownership');
  seatIds.forEach(pid => item(g, s => borderSample(s, ownerColorFor(pid), 'frontier'), ownerLabel(pid)));
  (roster.ids || []).filter(id => !seatIds.includes(id)).forEach(id => item(g, s => borderSample(s, ownerColorFor(id), 'frontier'), ownerLabel(id)));
  if (roster.ids) item(g, s => borderSample(s, palette.owners.nonmajor.stroke, 'frontier'), palette.owners.nonmajor.label);
  else item(g, s => borderSample(s, palette.owners.unclassified.stroke, 'frontier'), palette.owners.unclassified.label);
  item(g, null, palette.owners.unowned.label);
  item(g, null, palette.owners.unobserved.label);
  item(g, s => borderSample(s, T.ink, 'frontier'), palette.borders.frontier.label);
  item(g, s => borderSample(s, T.ink, 'unknown_beyond'), palette.borders.unknown_beyond.label);
  item(g, s => { tile(s, 'GRASS'); svg('polygon', {class: 'tint', points: ring(12.5), fill: T.seat0, 'fill-opacity': palette.tint_opacity}, s); },
    'tint = owner colour over observed owned hexes');
  g = group('Provenance');
  item(g, s => tile(s, 'GRASS', null, true), 'Dim = older packet receipt for that seat (hexes, borders, tints) — receipt age, not visibility');
  item(g, null, 'Blank = unsupplied, not empty world. Extents are observed receipts, not map bounds.');
  item(g, s => svg('line', {class: 'path', x1: -12, y1: 7, x2: 12, y2: -7}, s), 'dashed path = recorded choice, not movement or current orders');
  if (union) item(g, null, 'Union view: each hex takes the newest receipt carrying an owner key; seats are asynchronous.');
}
function setView() {$('map').setAttribute('viewBox',view.join(' '));
  ['x','y','width','height'].forEach((k,i)=>$('viewport').setAttribute(k,view[i]));}
function fit(){
  const aspect=$('map').clientWidth/$('map').clientHeight;
  let [x,y,w,h]=bounds;
  // A hidden or mid-resize map has no finite aspect; keep the raw observed bounds then.
  if(Number.isFinite(aspect)&&aspect>0){
    if(w/h<aspect){const nw=h*aspect;x-=(nw-w)/2;w=nw;}else{const nh=w/aspect;y-=(nh-h)/2;h=nh;}
  }
  view=[x,y,w,h];setView();
}
function zoom(factor){
  const w=Math.min(bounds[2]*8,Math.max(30,view[2]*factor)), h=w*view[3]/view[2];
  view=[view[0]+(view[2]-w)/2,view[1]+(view[3]-h)/2,w,h];setView();
}
function selectTile(alternatives){
  $('actors').value='';$('graph').replaceChildren();
  $('world').querySelectorAll('.path').forEach(e=>e.remove());
  const t = alternatives[0].observation, cls = classifyTile(t, palette), own = resolve(alternatives);
  $('selection-title').textContent=`Hex ${t.coord}`;
  $('selection-summary').textContent='Source terrain and ownership at receipt. Actual fog visibility and current ownership are unknown.';
  $('selection').replaceChildren();
  const dl = document.createElement('dl'); dl.className = 'facts'; $('selection').append(dl);
  const fact = (k, v) => { text('dt', k, dl); text('dd', v, dl); };
  fact('Terrain', `${terrainName(t, palette)} — normalized class ${typeof t.terrain === 'string' ? t.terrain : 'unsupplied'}; fill ${cls.fillKey}${cls.elevation ? `, ${cls.elevation} mark` : ''}`);
  fact('Ownership', ownershipSentence(own));
  if (own.receipt) fact('Ownership receipt', receiptSentence(own.receipt));
  fact('City centre', t.city_id === '' ? 'none observed on this hex at receipt' : typeof t.city_id === 'string' ? t.city_id : 'not observed');
  alternatives.forEach(row => fact(`Receipt P${row.receipt.player_id}`, receiptSentence(row.receipt)));
  const detail=document.createElement('details');$('selection').append(detail);
  text('summary','Raw receipts',detail);
  alternatives.forEach(row=>text('pre',pretty(row),detail));
  $('graph-note').textContent='No legal-action or expansion valuation is inferred for this hex.';
}
function selectActor(i){
  selected=actors[i];$('actors').value=String(i);const a=selected,o=a.observation;
  $('selection-title').textContent=`${o.type || 'City'} · ${a.id}`;
  $('selection-summary').textContent=`P${a.receipt.player_id} packet T${a.receipt.turn}, seq ${a.receipt.seq}. `+
    (a.kind.startsWith('own') ? 'Owned in that packet.' : o.is_barbarian === true ? 'Explicitly classified barbarian in that packet.' : 'Visible foreign actor; hostility is not established.');
  $('selection').replaceChildren();
  text('p',`Coordinate ${o.coord} · ${o.hp !== undefined ? 'HP '+o.hp : o.hp_bucket !== undefined ? 'HP bucket '+o.hp_bucket : 'health unknown'} · movement ${o.movement ?? 'unknown'}`+
    (a.kind.startsWith('own') ? '' : Number.isInteger(o.owner_id) ? ` · ${ownerLabel(o.owner_id)}` : ' · owner not supplied in packet'),$('selection'));
  const detail=document.createElement('details');$('selection').append(detail);
  text('summary','Source observation & packet provenance',detail);text('pre',pretty(a),detail);
  $('graph').replaceChildren();$('world').querySelectorAll('.path').forEach(e=>e.remove());
  const s=snapshots.find(s=>s.receipt.player_id===a.receipt.player_id),g=s.graph;
  if(a.kind.includes('cities')){
    $('graph-note').textContent='Observed queue → production intent. Completion timing, expansion site quality, and feasibility are unknown.';
    text('div',`City ${a.id}`,$('graph'),'card');text('div','↓ observed production queue',$('graph'),'graph-arrow');
    text('div',pretty(o.production_queue ?? 'Queue not supplied'),$('graph'),'card');
    productions.filter(p=>p.admission?.city_id===a.id && p.call.player_id===a.receipt.player_id)
      .forEach(p=>productionCard(p,$('graph')));
    if(g)text('pre',pretty({source:g.receipt,recorded_preferences:g.value.directive?.production_preferences,
      recorded_targets:g.value.directive?.unit_targets}),$('graph'));
    return;
  }
  $('graph-note').textContent='Recorded selection weights are not success probabilities. Dashed lines show historical choices, not promised paths or current orders.';
  if(!a.kind.startsWith('own')||!g){text('p','No owned-unit audit available at or before this packet.',$('graph'));return;}
  text('p',`Audit T${g.receipt.turn}, seq ${g.receipt.seq}; independent of packet position.`,$('graph'));
  const decisions=(g.value.decisions||[]).filter(d=>d.unit_id===a.id);
  if(!decisions.length)text('p','No decision row for this unit in the retained graph.',$('graph'));
  decisions.forEach(d=>{
    text('div',`${d.origin} → ${d.selected?.args?.dest || 'no destination'} · ${d.reason}`,$('graph'),'card selected');
    const start=point(d.origin),dest=d.selected?.args?.dest;
    if(dest && /^-?\d+,-?\d+$/.test(dest)){
      const end=point(dest);svg('path',{class:'path',d:`M${start.join(',')} L${end.join(',')}`},layers.paths);
    }
    (d.candidates||[]).forEach(c=>{
      const card=text('div',`${c.dest} · ${c.excluded || 'candidate'}`,$('graph'),`card ${c.excluded?'excluded':''}`);
      text('p',`Heuristic score ${c.score ?? 'not supplied'} · selection weight ${c.probability ?? 'not supplied'}`,card);
      if(c.components)text('p',pretty(c.components),card);
    });
  });
  const executions=(g.value.execution||[]).filter(e=>e.unit_id===a.id);
  executions.forEach(e=>text('pre',pretty({status:e.status,movement_outcome:e.movement_outcome,
    before:e.before,after:e.after,rejection:e.rejection}),$('graph')));
  text('pre',pretty(g.receipt),$('graph'));
}
function productionCard(a, parent) {
  const receipt = a.result || a.call;
  const window = windows.find(w=>w.player_id===a.call.player_id);
  const packet = snapshots.find(s=>s.receipt.player_id===a.call.player_id);
  const card=text('div',`P${a.call.player_id} ${a.item_id} · ${a.admission?.label || a.status}`,parent,'card');
  text('p',`${a.city_id || 'city unavailable'}${a.admission?.coord ? ' · '+a.admission.coord : ''}`,card);
  text('p',`Receipt T${receipt.turn}, seq ${receipt.seq} · ${window.turn-receipt.turn} turn(s) before receipt window. `+
    `Board packet T${packet.turn}, seq ${packet.seq}.`,card);
  text('p',a.admission ? 'Historical admission only. Construction/project completion and current queue are unproven.' :
    'Journal status only. No placement or queue admission is inferred.',card);
  const detail=document.createElement('details');card.append(detail);
  text('summary','Exact receipt provenance',detail);text('pre',pretty(a),detail);
}
function showProductionJournal() {
  $('production-journal').replaceChildren();
  $('production-window').textContent=windows.map(w=>`P${w.player_id} receipt window T${w.turn}, seq ${w.seq}`).join(' · ');
  if(!productions.length)text('p','No district/project action records in this selected receipt window.',$('production-journal'));
  productions.forEach(a=>productionCard(a,$('production-journal')));
}
function selectProduction(a) {
  $('actors').value='';$('graph').replaceChildren();$('selection').replaceChildren();
  $('world').querySelectorAll('.path').forEach(e=>e.remove());
  $('selection-title').textContent=`${a.item_id} · placement observed`;
  $('selection-summary').textContent=`Historical P${a.call.player_id} district placement at ${a.admission.coord}. Not proof of a completed or currently present district.`;
  productionCard(a,$('selection'));
  $('graph-note').textContent='Requested district → exact queue and plot readback → completion unknown.';
  text('div',`${a.item_id} requested for ${a.city_id}`,$('graph'),'card');
  text('div','↓ accepted with separate native readback',$('graph'),'graph-arrow');
  text('div',`Placement observed at ${a.admission.coord}; queued, not completed`,$('graph'),'card');
}
$('perspective').addEventListener('change',perspective);$('snapshot').addEventListener('change',draw);
$('actors').addEventListener('change',()=>{if($('actors').value!=='')selectActor(Number($('actors').value));});
$('fit').addEventListener('click',fit);$('zoom-in').addEventListener('click',()=>zoom(.7));
$('zoom-out').addEventListener('click',()=>zoom(1/.7));
$('map').addEventListener('wheel',e=>{e.preventDefault();zoom(e.deltaY>0?1.15:1/1.15);},{passive:false});
let drag=null;
$('map').addEventListener('pointerdown',e=>{if(e.target===$('map')){drag=[e.clientX,e.clientY,...view];$('map').setPointerCapture(e.pointerId);}});
$('map').addEventListener('pointermove',e=>{if(drag){view=[drag[2]-(e.clientX-drag[0])*view[2]/$('map').clientWidth,
  drag[3]-(e.clientY-drag[1])*view[3]/$('map').clientHeight,view[2],view[3]];setView();}});
$('map').addEventListener('pointerup',()=>drag=null);$('map').addEventListener('pointercancel',()=>drag=null);
$('mini').addEventListener('click',e=>{const p=$('mini').createSVGPoint();p.x=e.clientX;p.y=e.clientY;
  const w=p.matrixTransform($('mini').getScreenCTM().inverse());view=[w.x-view[2]/2,w.y-view[3]/2,view[2],view[3]];setView();});
window.addEventListener('resize',fit);
if (data.dashboard_source && data.scope === 'combined_observation_preview') $('perspective').value='union';
perspective();

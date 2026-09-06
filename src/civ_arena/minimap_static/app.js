'use strict';
const data = JSON.parse(document.getElementById('data').textContent);
const $ = id => document.getElementById(id);
const ns = 'http://www.w3.org/2000/svg';
const svg = (tag, attrs, parent) => {
  const e = document.createElementNS(ns, tag);
  Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
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
const point = coord => {
  const [q, r] = coord.split(',').map(Number);
  return [Math.sqrt(3) * 14 * (q + r / 2), -21 * r];
};
const hex = coord => {
  const [x, y] = point(coord);
  return Array.from({length: 6}, (_, i) => {
    const a = (60 * i + 30) * Math.PI / 180;
    return `${x + 14 * Math.cos(a)},${y + 14 * Math.sin(a)}`;
  }).join(' ');
};
const color = t => {
  const raw = t.native_terrain?.type || '', biome = t.native_terrain?.biome || t.terrain;
  if (raw.includes('MOUNTAIN')) return '#80898d';
  return {GRASS:'#568473',GRASSLAND:'#568473',PLAINS:'#9b995f',DESERT:'#b39862',
    TUNDRA:'#9da79f',SNOW:'#d8e0df',COAST:'#346985',OCEAN:'#244e6b',HILL:'#828766'}[biome] || '#52656a';
};
let snapshots = [], rows = [], actors = [], bounds, view, selected = null;
for (const seat of data.seats) option($('perspective'), String(seat.player_id), `Player ${seat.player_id}`);
if (data.scope === 'combined_observation_preview') option($('perspective'), 'union', 'Spectator · combined receipts');
$('scope').textContent = data.scope === 'player_only'
  ? 'Player-only export: other seat packets are not embedded. Blank space is unknown. This is a retained receipt history, not current visibility.'
  : 'SPECTATOR EXPORT: contains multiple private player perspectives. Player selector filters display only. Combined receipts are asynchronous, not a full or simultaneous world map.';
for (const line of data.limits) text('p', line, $('custody'));
text('p', data.axis, $('custody'));
text('pre', pretty(data.event_binding), $('custody'));
text('p', `Artifact SHA-256 ${data.digest}`, $('custody'));
function perspective() {
  const union = $('perspective').value === 'union';
  $('snapshot').replaceChildren();
  if (union) {
    option($('snapshot'), 'latest', 'Latest supplied packet per seat');
  } else {
    const seat = data.seats.find(s => String(s.player_id) === $('perspective').value);
    seat.snapshots.forEach((s, i) => option($('snapshot'), String(i), `T${s.turn} · seq ${s.seq}`));
    $('snapshot').value = String(seat.snapshots.length - 1);
  }
  draw();
}
function draw() {
  selected = null;
  const union = $('perspective').value === 'union';
  snapshots = union ? data.seats.map(s => s.snapshots.at(-1)) : [data.seats.find(
    s => String(s.player_id) === $('perspective').value).snapshots[Number($('snapshot').value)]];
  // Keep each perspective's receipts. Overlap gets a single paint but retains all alternatives.
  const tiles = new Map();
  snapshots.forEach(s => s.terrain.forEach(row => {
    if (!tiles.has(row.observation.coord)) tiles.set(row.observation.coord, []);
    tiles.get(row.observation.coord).push(row);
  }));
  rows = [...tiles.values()]; actors = snapshots.flatMap(s => s.actors);
  $('world').replaceChildren(); $('mini-world').replaceChildren(); $('actors').replaceChildren();
  option($('actors'), '', 'Select an actor…');
  rows.forEach(alternatives => {
    const row = alternatives[0], t = row.observation;
    const latest = snapshots.find(s => s.receipt.player_id === row.receipt.player_id).seq;
    const attrs = {points:hex(t.coord), fill:color(t), opacity:row.receipt.seq === latest ? 1 : .50};
    const h = svg('polygon', {...attrs, tabindex:0, role:'button', 'aria-label':`Hex ${t.coord}`}, $('world'));
    const pick = () => selectTile(alternatives);
    h.addEventListener('click', pick);
    h.addEventListener('keydown', e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); pick();}});
    svg('polygon', attrs, $('mini-world'));
    if (t.native_terrain?.hills || t.native_terrain?.type?.includes('MOUNTAIN')) {
      const [x,y] = point(t.coord); svg('text', {x,y:y+3}, $('world')).textContent = '▲';
    }
  });
  actors.forEach((a, i) => {
    const o = a.observation, [x,y] = point(o.coord), city = a.kind.includes('cities');
    const owned = a.kind.startsWith('own'), fill = o.is_barbarian === true ? '#f38c76' :
      owned ? (a.receipt.player_id === 0 ? '#f4d884' : '#80dcd5') : '#c5abd9';
    const attrs = {class:'actor',fill,tabindex:0,role:'button','aria-label':`${a.id} at ${o.coord}`};
    const e = city ? svg('path', {...attrs,d:`M${x},${y-10} l10,10 -10,10 -10,-10 Z`}, $('world')) :
      svg('circle', {...attrs,cx:x,cy:y,r:5.5}, $('world'));
    const pick = () => selectActor(i);
    e.addEventListener('click', pick); e.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' || ev.key === ' ') {ev.preventDefault(); pick();}
    });
    svg('text',{x,y:y-11},$('world')).textContent = city ? (owned ? `P${a.receipt.player_id} city` : 'foreign city') :
      o.is_barbarian === true ? '!' : '';
    option($('actors'),String(i),`P${a.receipt.player_id} ${a.id} · ${o.type || 'city'} · ${o.coord}`);
  });
  const coords = [...rows.map(r => r[0].observation.coord), ...actors.map(a => a.observation.coord)];
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
  fit();
}
function setView() {$('map').setAttribute('viewBox',view.join(' '));
  ['x','y','width','height'].forEach((k,i)=>$('viewport').setAttribute(k,view[i]));}
function fit(){
  const aspect=$('map').clientWidth/$('map').clientHeight;
  let [x,y,w,h]=bounds;
  if(w/h<aspect){const nw=h*aspect;x-=(nw-w)/2;w=nw;}else{const nh=w/aspect;y-=(nh-h)/2;h=nh;}
  view=[x,y,w,h];setView();
}
function zoom(factor){
  const w=Math.min(bounds[2]*8,Math.max(30,view[2]*factor)), h=w*view[3]/view[2];
  view=[view[0]+(view[2]-w)/2,view[1]+(view[3]-h)/2,w,h];setView();
}
function selectTile(alternatives){
  $('actors').value='';$('graph').replaceChildren();
  $('world').querySelectorAll('.path').forEach(e=>e.remove());
  $('selection-title').textContent=`Hex ${alternatives[0].observation.coord}`;
  $('selection-summary').textContent='Source terrain and ownership at receipt. Actual fog visibility and current ownership are unknown.';
  $('selection').replaceChildren();
  alternatives.forEach(row=>text('pre',pretty(row),$('selection')));
  $('graph-note').textContent='No legal-action or expansion valuation is inferred for this hex.';
}
function selectActor(i){
  selected=actors[i];$('actors').value=String(i);const a=selected,o=a.observation;
  $('selection-title').textContent=`${o.type || 'City'} · ${a.id}`;
  $('selection-summary').textContent=`P${a.receipt.player_id} packet T${a.receipt.turn}, seq ${a.receipt.seq}. `+
    (a.kind.startsWith('own') ? 'Owned in that packet.' : o.is_barbarian === true ? 'Explicitly classified barbarian in that packet.' : 'Visible foreign actor; hostility is not established.');
  $('selection').replaceChildren();
  text('p',`Coordinate ${o.coord} · ${o.hp !== undefined ? 'HP '+o.hp : o.hp_bucket !== undefined ? 'HP bucket '+o.hp_bucket : 'health unknown'} · movement ${o.movement ?? 'unknown'}`,$('selection'));
  const detail=document.createElement('details');$('selection').append(detail);
  text('summary','Source observation & packet provenance',detail);text('pre',pretty(a),detail);
  $('graph').replaceChildren();$('world').querySelectorAll('.path').forEach(e=>e.remove());
  const s=snapshots.find(s=>s.receipt.player_id===a.receipt.player_id),g=s.graph;
  if(a.kind.includes('cities')){
    $('graph-note').textContent='Observed queue → production intent. Completion timing, expansion site quality, and feasibility are unknown.';
    text('div',`City ${a.id}`,$('graph'),'card');text('div','↓ observed production queue',$('graph'),'graph-arrow');
    text('div',pretty(o.production_queue ?? 'Queue not supplied'),$('graph'),'card');
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
      const end=point(dest);svg('path',{class:'path',d:`M${start.join(',')} L${end.join(',')}`},$('world'));
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
window.addEventListener('resize',fit);perspective();

'use strict';
(() => {
  const projections = JSON.parse(document.getElementById('projection-data').textContent);
  const svg = document.getElementById('graph');
  const inspector = document.getElementById('inspector');
  const select = document.getElementById('examples');
  const ns = 'http://www.w3.org/2000/svg';
  let current, allEdges, positions, camera, initial, selected, dragging;
  const el = (tag, text, parent, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    if (parent) parent.append(node);
    return node;
  };
  const shape = (tag, attrs, parent, text) => {
    const node = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    parent.append(node);
    return node;
  };
  const label = id => id.replace(/^[A-Z]+_/, '').replaceAll('_', ' ');
  const data = (title, value, open = false) => {
    const block = el('details', undefined, inspector);
    block.open = open;
    el('summary', title, block);
    el('pre', JSON.stringify(value, null, 2), block);
  };
  const applyCamera = () => svg.setAttribute('viewBox', `${camera.x} ${camera.y} ${camera.w} ${camera.h}`);
  const zoom = factor => {
    if (camera.w * factor < 160 || camera.w * factor > 50000) return;
    camera.x += camera.w * (1-factor)/2;
    camera.y += camera.h * (1-factor)/2;
    camera.w *= factor; camera.h *= factor;
    applyCamera();
  };
  const header = (title, kind) => {
    inspector.replaceChildren();
    el('div', kind, inspector, 'eyebrow');
    el('h2', title, inspector);
    el('p', 'Source preview · effective rules and current feasibility unknown', inspector, 'notice');
  };
  function inspectNode(id) {
    selected = id;
    for (const node of svg.querySelectorAll('.node')) node.classList.toggle('selected', node.dataset.nodeId === id);
    const node = current.nodes.find(row => row.id === id);
    header(label(id), node ? `SOURCE FACT / ${node.kind}` : 'DEPTH FRONTIER / UNKNOWN');
    el('p', id, inspector, 'digest');
    if (!node) {
      el('p', 'This target is outside the supplied query depth. No node attributes or observations are inferred.', inspector);
      data('Boundary source edges', current.unexpanded_edges.filter(edge => edge.target === id), true);
      return;
    }
    el('h3', 'Supplied observations', inspector);
    for (const [predicate, fact] of Object.entries(node.observations || {})) {
      const supplied = fact.evidence_class === 'supplied_observation';
      el('div', supplied ? `${predicate}: ${fact.value} · supplied observation` : `${predicate}: unknown · not observed`,
        inspector, `fact${supplied ? '' : ' unknown-fact'}`);
      if (supplied) el('p', `Evidence: ${fact.evidence_id}`, inspector, 'digest');
    }
    el('p', current.observation_authority, inspector, 'notice');
    const groups = current.prerequisite_options.filter(group => group.source === id);
    for (const group of groups) {
      el('h3', group.relation.replaceAll('_', ' '), inspector);
      el('p', `Connective: ${group.connective}. ${group.why_unverified}`, inspector, 'notice');
      data('Source edges and target observations', current.edges.filter(edge => group.edge_ids.includes(edge.id)));
    }
    data('Source declarations / provenance', node.declarations, true);
    data('Source attributes', node.attributes);
    data('Modifier requirement sets (unevaluated)', current.source_requirement_sets);
    data('Unresolved source references', current.unresolved_source_references);
  }
  function inspectEdge(edge) {
    header(edge.relation.replaceAll('_', ' '), 'SOURCE RELATION');
    el('p', `${edge.source} → ${edge.target}`, inspector, 'digest');
    el('p', `Prerequisite connective: ${edge.group_semantics || 'unverified'}. This relation is not proof of an executable action.`, inspector, 'notice');
    data('Exact source edge / provenance', edge, true);
  }
  function draw() {
    current = projections[Number(select.value)];
    allEdges = [...current.edges, ...current.other_relations, ...current.unexpanded_edges];
    const nodeMap = new Map(current.nodes.map(node => [node.id, node]));
    for (const edge of current.unexpanded_edges) if (!nodeMap.has(edge.target)) nodeMap.set(edge.target, null);
    const levels = new Map([[current.root, 0]]);
    let frontier = [current.root];
    while (frontier.length) {
      const next = [];
      for (const source of frontier) for (const edge of allEdges) {
        if (edge.source === source && !levels.has(edge.target)) {
          levels.set(edge.target, levels.get(source)+1); next.push(edge.target);
        }
      }
      frontier = next;
    }
    for (const id of nodeMap.keys()) if (!levels.has(id)) levels.set(id, 0);
    const columns = new Map();
    for (const id of nodeMap.keys()) {
      const depth = levels.get(id);
      if (!columns.has(depth)) columns.set(depth, []);
      columns.get(depth).push(id);
    }
    const maxRows = Math.max(...Array.from(columns.values(), rows => rows.length));
    const height = Math.max(420, maxRows*112+80);
    const width = Math.max(700, columns.size*310+60);
    positions = new Map();
    for (const [depth, rows] of columns) rows.sort().forEach((id, index) => {
      positions.set(id, {x: 30+depth*310, y: (height-rows.length*112)/2+index*112});
    });
    svg.replaceChildren();
    const defs = shape('defs', {}, svg);
    const marker = shape('marker', {id:'arrow', viewBox:'0 0 10 10', refX:9, refY:5, markerWidth:7, markerHeight:7, orient:'auto-start-reverse'}, defs);
    shape('path', {d:'M 0 0 L 10 5 L 0 10 z', fill:'#7896ad'}, marker);
    const layer = shape('g', {}, svg);
    for (const edge of allEdges) {
      const a = positions.get(edge.source), b = positions.get(edge.target);
      const path = `M ${a.x+230} ${a.y+39} C ${a.x+275} ${a.y+39}, ${b.x-45} ${b.y+39}, ${b.x} ${b.y+39}`;
      const boundary = current.unexpanded_edges.some(row => row.id === edge.id);
      const other = current.other_relations.some(row => row.id === edge.id);
      shape('path', {d:path, class:`edge-line${boundary?' boundary':other?' other':''}`}, layer);
      const hit = shape('path', {d:path, class:'edge-hit', tabindex:0, role:'button', 'aria-label':`${edge.source} ${edge.relation} ${edge.target}`}, layer);
      hit.addEventListener('click', () => inspectEdge(edge));
      hit.addEventListener('keydown', event => { if (event.key === 'Enter') inspectEdge(edge); });
      shape('text', {x:(a.x+230+b.x)/2, y:(a.y+b.y)/2+29, class:'edge-label', 'text-anchor':'middle'}, layer,
        edge.relation.replaceAll('requires_', '').replaceAll('_', ' '));
    }
    for (const [id, node] of nodeMap) {
      const p = positions.get(id);
      const observations = Object.values(node?.observations || {});
      const fact = observations.find(row => row.evidence_class === 'supplied_observation');
      const group = shape('g', {class:`node${node?'':' frontier-node'}`, transform:`translate(${p.x},${p.y})`, tabindex:0, role:'button', 'aria-label':`Inspect ${id}`, 'data-node-id':id, 'data-observed':Boolean(fact)}, layer);
      shape('rect', {width:230, height:78, rx:9}, group);
      shape('text', {x:14, y:27}, group, label(id).length > 27 ? label(id).slice(0,25)+'…' : label(id));
      shape('text', {x:14, y:46, class:'node-caption'}, group, node ? node.kind.toUpperCase() : 'OUTSIDE QUERY DEPTH');
      shape('text', {x:14, y:64, class:'node-caption'}, group, fact ? `${fact.predicate}: ${fact.value} · supplied` : 'Observation unknown');
      shape('title', {}, group, id);
      group.addEventListener('click', () => inspectNode(id));
      group.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {event.preventDefault(); inspectNode(id);}
      });
    }
    initial = {x:0, y:0, w:width, h:height}; camera = {...initial}; applyCamera();
    document.getElementById('graph-summary').textContent = `${current.nodes.length} source nodes · ${allEdges.length} relations · depth ${current.depth}`;
    document.getElementById('frontier-summary').textContent = current.unexpanded_edges.length ?
      `${current.unexpanded_edges.length} source relations cross the query depth. Dashed targets have no inferred state.` :
      'No remaining edges at this query depth. This does not mean complete effective-game coverage.';
    const list = document.getElementById('frontier-list'); list.replaceChildren();
    for (const edge of current.unexpanded_edges) {
      const button = el('button', `${label(edge.source)} → ${label(edge.target)}`, list);
      button.addEventListener('click', () => inspectEdge(edge));
    }
    inspectNode(current.root);
  }
  projections.forEach((projection, index) => {
    const option = el('option', `${label(projection.root)} · supplied turn ${projection.observation_snapshot.turn} · depth ${projection.depth}`, select);
    option.value = index;
  });
  select.addEventListener('change', draw);
  document.getElementById('zoom-in').addEventListener('click', () => zoom(.8));
  document.getElementById('zoom-out').addEventListener('click', () => zoom(1.25));
  document.getElementById('fit').addEventListener('click', () => {camera={...initial};applyCamera();});
  document.getElementById('artifact').addEventListener('click', () => {
    header('Artifact identity', 'SUPPLIED STATIC PROJECTION');
    for (const key of ['projection_digest','catalog_digest','observation_digest']) {
      el('h3', key, inspector); el('p', current[key], inspector, 'digest');
    }
    el('p', 'Digest checks detect changed bytes; they do not authenticate the supplied observations.', inspector, 'notice');
    data('Original projection (unchanged)', current, true);
  });
  svg.addEventListener('pointerdown', event => {
    if (event.target.closest('.node,.edge-hit')) return;
    dragging={x:event.clientX,y:event.clientY,camera:{...camera}};svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener('pointermove', event => {
    if (!dragging) return;
    const scale = Math.max(camera.w/svg.clientWidth,camera.h/svg.clientHeight);
    camera.x=dragging.camera.x-(event.clientX-dragging.x)*scale;
    camera.y=dragging.camera.y-(event.clientY-dragging.y)*scale;applyCamera();
  });
  svg.addEventListener('pointerup', () => {dragging=null;});
  svg.addEventListener('pointercancel', () => {dragging=null;});
  svg.addEventListener('wheel', event => {event.preventDefault();zoom(event.deltaY>0?1.1:1/1.1);}, {passive:false});
  draw();
})();

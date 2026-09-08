'use strict';
// Pure hex geometry, terrain classification and ownership resolution for the
// observed atlas. No DOM access: pytest runs this file under Node to pin
// the neighbour table, border rules and classification contracts. It is
// concatenated in front of app.js into the single CSP-hash-pinned script.
const HEX_R = 14;
// Edge i runs from vertex i (at 30° + 60°·i, SVG y-down) to vertex i+1; its
// outward normal points at the axial neighbour NEIGHBORS[i].
const NEIGHBORS = [[1, -1], [0, -1], [-1, 0], [-1, 1], [0, 1], [1, 0]];
const HAS = (o, k) => o !== null && typeof o === 'object' && Object.prototype.hasOwnProperty.call(o, k);
function parseCoord(coord) {
  const m = typeof coord === 'string' ? /^(-?\d+),(-?\d+)$/.exec(coord) : null;
  return m ? [Number(m[1]), Number(m[2])] : null;
}
function point(coord) {
  const [q, r] = parseCoord(coord);
  return [Math.sqrt(3) * HEX_R * (q + r / 2), -1.5 * HEX_R * r];
}
function hexVertices(x, y, radius = HEX_R) {
  return Array.from({length: 6}, (_, i) => {
    const a = (60 * i + 30) * Math.PI / 180;
    return [x + radius * Math.cos(a), y + radius * Math.sin(a)];
  });
}
const fmt = n => String(Number(n.toFixed(2)));
function hexPoints(coord, radius = HEX_R) {
  const [x, y] = point(coord);
  return hexVertices(x, y, radius).map(p => p.map(fmt).join(',')).join(' ');
}
function neighbourCoord(coord, edge) {
  const [q, r] = parseCoord(coord), [dq, dr] = NEIGHBORS[edge];
  return `${q + dq},${r + dr}`;
}
function insetEdge(coord, edge, inset) {
  const [x, y] = point(coord), v = hexVertices(x, y, HEX_R - inset);
  const a = v[edge], b = v[(edge + 1) % 6];
  return [a[0], a[1], b[0], b[1]];
}
function terrainKey(biome, palette) {
  const key = HAS(palette.terrain_aliases, biome) ? palette.terrain_aliases[biome] : biome;
  return HAS(palette.terrain, key) ? key : 'UNKNOWN';
}
// Native type wins; normalized classes only when native metadata is absent.
function classifyTile(t, palette) {
  const native = t && t.native_terrain !== null && typeof t.native_terrain === 'object' ? t.native_terrain : null;
  const type = native && typeof native.type === 'string' ? native.type.replace(/^TERRAIN_/, '') : '';
  const normalized = t && typeof t.terrain === 'string' ? t.terrain : '';
  const mountain = type.endsWith('_MOUNTAIN') || (!type && normalized === 'MOUNTAIN');
  const hills = !mountain && ((native !== null && native.hills === true) || (!type && normalized === 'HILL'));
  const nativeBiome = native && typeof native.biome === 'string' ? native.biome : null;
  const biome = nativeBiome !== null ? nativeBiome : normalized;
  // source = where the fill came from: native metadata, or the normalized class.
  return {fillKey: mountain ? 'MOUNTAIN' : terrainKey(biome, palette),
          elevation: mountain ? 'mountain' : hills ? 'hills' : null,
          source: nativeBiome !== null || (mountain && type) ? 'native' : normalized ? 'normalized' : 'none'};
}
function terrainName(t, palette) {
  const native = t && t.native_terrain !== null && typeof t.native_terrain === 'object' ? t.native_terrain : null;
  if (native === null || native === undefined) {
    if (t && typeof t.terrain === 'string') return `normalized class ${t.terrain} (native type not supplied)`;
    return 'terrain not supplied';
  }
  if (typeof native.type !== 'string') {
    const key = typeof native.biome === 'string' ? terrainKey(native.biome, palette) : 'UNKNOWN';
    if (key === 'UNKNOWN') return 'native type not supplied';
    return palette.terrain[key].label.replace(/ \(.*\)$/, '') + (native.hills === true ? ' hills' : '') +
      ' (native biome; type not supplied)';
  }
  const name = native.type.replace(/^TERRAIN_/, '');
  const suffix = name.endsWith('_HILLS') ? ' hills' : name.endsWith('_MOUNTAIN') ? ' mountain' : '';
  const base = name.replace(/_(HILLS|MOUNTAIN)$/, '');
  const key = terrainKey(base, palette);
  if (key === 'UNKNOWN') return `unsupported native token ${native.type}`;
  return palette.terrain[key].label.replace(/ \(.*\)$/, '') + suffix;
}
// Ownership from the newest receipt that carries the owner_id KEY. A missing
// key is "unobserved" (remembered terrain), never "unowned" (-1).
function resolveOwnership(alternatives, latestSeq) {
  let best = null;
  for (const row of alternatives) {
    if (!HAS(row.observation, 'owner_id')) continue;
    if (best === null || row.receipt.seq > best.receipt.seq) best = row;
  }
  if (best === null) return {status: 'unobserved', owner_id: null, receipt: null, stale: false};
  const id = best.observation.owner_id;
  const status = id === -1 ? 'unowned' : id === null ? 'null_field'
    : Number.isInteger(id) && id >= 0 ? 'owned' : 'invalid';
  const latest = latestSeq ? latestSeq(best.receipt.player_id) : best.receipt.seq;
  return {status, owner_id: status === 'owned' ? id : null, receipt: best.receipt,
          stale: latest !== undefined && best.receipt.seq !== latest};
}
function ownerClass(id, seats, roster) {
  if (seats.includes(id)) return 'seat';
  if (roster === null) return 'unclassified';
  return roster.includes(id) ? 'major' : 'nonmajor';
}
function ownerColor(id, cls, seats, roster, palette) {
  const t = palette.tokens, o = palette.owners;
  if (cls === 'seat') return id === 0 ? t.seat0 : t.seat1;
  if (cls === 'major') {
    const rank = roster.filter(p => !seats.includes(p)).indexOf(id);
    return rank >= 0 && rank < o.majors.length ? o.majors[rank] : o.major_overflow;
  }
  return cls === 'nonmajor' ? o.nonmajor.stroke : o.unclassified.stroke;
}
function unitSpec(type, palette) {
  const known = typeof type === 'string' && HAS(palette.units, type);
  return {...(known ? palette.units[type] : palette.unit_fallback), known};
}
// One segment per (owned tile, edge): solid where the neighbour's ownership is
// observed and differs (or is -1); dashed where the neighbour is unsupplied or
// unobserved, which marks the edge of observation, not a territory extent.
function borderSegments(tiles, resolve, classify, inset) {
  const groups = new Map();
  for (const [coord, alternatives] of tiles) {
    const me = resolve(alternatives);
    if (me.status !== 'owned') continue;
    const cls = classify(me.owner_id);
    for (let edge = 0; edge < 6; edge++) {
      const neighbour = tiles.get(neighbourCoord(coord, edge));
      let style = 'unknown_beyond';
      if (neighbour) {
        const other = resolve(neighbour);
        if (other.status === 'owned' && other.owner_id === me.owner_id) continue;
        if (other.status === 'owned' || other.status === 'unowned') style = 'frontier';
      }
      const key = `${cls}:${me.owner_id}|${style}|${me.stale ? 1 : 0}`;
      if (!groups.has(key)) groups.set(key, {ownerClass: cls, owner_id: me.owner_id, style, stale: me.stale, segments: []});
      groups.get(key).segments.push(insetEdge(coord, edge, inset));
    }
  }
  return groups;
}
function tintGroups(tiles, resolve, classify) {
  const groups = new Map();
  for (const [coord, alternatives] of tiles) {
    const me = resolve(alternatives);
    if (me.status !== 'owned') continue;
    const cls = classify(me.owner_id), key = `${cls}:${me.owner_id}|${me.stale ? 1 : 0}`;
    if (!groups.has(key)) groups.set(key, {ownerClass: cls, owner_id: me.owner_id, stale: me.stale, hexes: []});
    groups.get(key).hexes.push(coord);
  }
  return groups;
}
function segmentPath(segments) {
  return segments.map(s => `M${fmt(s[0])} ${fmt(s[1])} L${fmt(s[2])} ${fmt(s[3])}`).join(' ');
}
function hexOutlinePath(coords, radius = HEX_R) {
  return coords.map(c => 'M' + hexVertices(...point(c), radius).map(p => p.map(fmt).join(' ')).join(' L') + ' Z').join(' ');
}
// Recorded decision overlay. One audited decision row becomes hex outlines on the
// map: outline width states the recorded selection weight (a seeded action choice,
// never a success probability), hatch states exclusion, the solid edge states the
// recorded chosen move. Nothing is inferred: a candidate without a finite recorded
// weight stays "unscored" at the thinnest outline, a destination that fails
// parseCoord is dropped and counted, and a selection without a destination draws
// no edge.
const DECISION_INSET = 2.5;
// Centre distance below which two 8px labels on one row overprint each other.
const DECISION_LABEL_GAP = 70;
function weightOf(candidate) {
  const p = candidate !== null && typeof candidate === 'object' ? candidate.probability : null;
  return typeof p === 'number' && Number.isFinite(p) ? Math.min(1, Math.max(0, p)) : null;
}
function candidateStroke(weight) {
  return 1 + 3 * (typeof weight === 'number' && Number.isFinite(weight) ? weight : 0);
}
// Printed weights must never round toward certainty: a weight just under 1 reads
// ">0.99", never "1.00", and a weight just above 0 reads "<0.01", never "0.00".
// Exact 0 and exact 1 are recorded values and print as themselves. The 3-decimal
// value stays in data-weight, the aria-label and the tooltip.
function weightText(weight) {
  if (typeof weight !== 'number' || !Number.isFinite(weight)) return 'unscored';
  if (weight > 0 && weight < 0.005) return '<0.01';
  if (weight > 0.995 && weight < 1) return '>0.99';
  return weight.toFixed(2);
}
// A label printed under its hex would otherwise read as belonging to the hex it sits
// on, so a below-side label states its direction: "↑ " means the hex above this text.
function decisionLabel(coord, label, side = 'above') {
  const [x, y] = point(coord);
  return {coord, x, y: side === 'below' ? y + HEX_R + 9 : y - HEX_R - 3,
          text: side === 'below' ? `↑ ${label}` : label, side};
}
function decisionShapes(decision) {
  const d = decision !== null && typeof decision === 'object' ? decision : {};
  const selected = d.selected !== null && typeof d.selected === 'object' ? d.selected : null;
  const action = selected !== null && typeof selected.action === 'string' ? selected.action : null;
  const reason = typeof d.reason === 'string' ? d.reason : null;
  const rows = Array.isArray(d.candidates) ? d.candidates : [];
  if (parseCoord(d.origin) === null) {
    return {origin: null, action, reason, chosen: null, candidates: [], labels: [],
            dropped: rows.length};
  }
  const [ox, oy] = point(d.origin);
  const args = selected !== null && selected.args !== null && typeof selected.args === 'object'
    ? selected.args : {};
  const dest = parseCoord(args.dest) === null ? null : args.dest;
  const candidates = [];
  let dropped = 0;
  for (const row of rows) {
    const c = row !== null && typeof row === 'object' ? row : {};
    if (parseCoord(c.dest) === null) { dropped++; continue; }
    const weight = weightOf(c), [cx, cy] = point(c.dest);
    candidates.push({coord: c.dest, points: hexPoints(c.dest, HEX_R - DECISION_INSET), cx, cy,
      weight, unscored: weight === null,
      excluded: typeof c.excluded === 'string' && c.excluded !== '' ? c.excluded : null,
      score: typeof c.score === 'number' && Number.isFinite(c.score) ? c.score : null,
      components: c.components !== null && typeof c.components === 'object' &&
        !Array.isArray(c.components) ? c.components : null,
      chosen: c.dest === dest, strokeWidth: candidateStroke(weight)});
  }
  const labels = [];
  if (dest !== null) {
    const picked = candidates.find(c => c.coord === dest);
    const w = picked === undefined ? null : picked.weight;
    labels.push(decisionLabel(dest, `chosen · ${w === null ? 'unscored' : `weight ${weightText(w)}`}`));
    // At most one runner-up label: the strongest recorded weight that was neither
    // chosen nor excluded. Unscored candidates cannot win a weight label.
    const rest = candidates.filter(c => !c.chosen && c.excluded === null && c.weight !== null)
      .sort((a, b) => b.weight - a.weight || (a.coord < b.coord ? -1 : a.coord > b.coord ? 1 : 0));
    if (rest.length) {
      const second = decisionLabel(rest[0].coord, `weight ${weightText(rest[0].weight)}`);
      // Two neighbouring candidates share a label row, and one label is wider than a
      // hex: printing both above their hexes overprints them into nonsense. The
      // runner-up label then moves below its own hex; it is never dropped.
      const overprints = Math.abs(second.y - labels[0].y) <= 10 &&
        Math.abs(second.x - labels[0].x) < DECISION_LABEL_GAP;
      labels.push(overprints ? decisionLabel(second.coord, second.text, 'below') : second);
    }
  } else if (action !== null) {
    labels.push(decisionLabel(d.origin, `${action} (recorded)`));
  }
  return {origin: {coord: d.origin, x: ox, y: oy}, action, reason,
          chosen: dest === null ? null
            : {coord: dest, x1: ox, y1: oy, x2: point(dest)[0], y2: point(dest)[1]},
          candidates, labels, dropped};
}

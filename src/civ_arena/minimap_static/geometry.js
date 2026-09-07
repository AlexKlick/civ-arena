'use strict';
// Pure hex geometry, terrain classification and ownership resolution for the
// observed atlas. No DOM access: pytest evaluates this file under Node to pin
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

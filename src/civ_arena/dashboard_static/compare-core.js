'use strict';

/* Pure logic behind the match room's comparison views: research folding, the
   seat diff, chart scales and segments, the per-turn decision sentences and
   tech-tree depth.

   Every function here is a function of its arguments only. It never touches the
   page, the network or the shared renderer helpers, so the browser and the test
   harness evaluate exactly the same code. Nothing is invented: an unrecorded
   number stays null, a gap stays a gap, and a history that was not retained
   says so instead of reading as an empty set. */

const Core = (() => {
  const ACCEPTED = ['accepted', 'ok', 'success', 'completed'];
  const STEPS = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
  const UNRETAINED = 'history not retained at this turn';

  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const isAccepted = status => ACCEPTED.includes(status);
  const humanize = value => String(value || '').replaceAll('_', ' ')
    .replace(/\b\w/g, character => character.toUpperCase());
  const short = key => String(key || '').replaceAll('_', ' ');
  const label = id => humanize(String(id == null ? '' : id).toLowerCase());
  function number(value) {
    if (finite(value)) return value;
    if (typeof value !== 'string' || value.trim() === '') return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  function fmt(value) {
    if (!finite(value)) return 'n/a';
    if (Number.isInteger(value)) return value.toLocaleString();
    return Math.abs(value) >= 10 ? Math.round(value).toLocaleString() : value.toFixed(1);
  }

  /* ---------------------------------------------------------------- research */

  function foldSeat(seat, cutoff) {
    const rows = (Array.isArray(seat.history) ? seat.history : [])
      .filter(row => row && typeof row === 'object');
    const set = new Set(), at = new Map();
    let last = null;
    rows.forEach(row => {
      if (cutoff != null && number(row.turn) > cutoff) return;
      (Array.isArray(row.removed) ? row.removed : []).forEach(tech => {
        set.delete(tech); at.delete(tech);
      });
      // A baseline row folds packets that were not retained, so the turn it
      // carries is where the fold stopped, never where a name was acquired.
      const when = row.baseline === true ? null : number(row.turn);
      (Array.isArray(row.added) ? row.added : []).forEach(tech => {
        set.add(tech);
        if (!at.has(tech)) at.set(tech, when);
      });
      last = row;
    });
    // A capped history opens with a synthetic baseline row. Before that row's
    // turn nothing was retained, so an empty set there would be a false claim.
    const first = rows.length ? rows[0] : null;
    const from = first ? number(first.turn) : null;
    const retained = !(cutoff != null && first !== null && first.baseline === true &&
      from != null && cutoff < from);
    const latest = seat.latest && typeof seat.latest === 'object' ? seat.latest : null;
    const latestTurn = latest ? number(latest.turn) : null;
    // The retained history is capped, so the latest packet stays authoritative
    // whenever the selected turn is at or beyond it.
    const useLatest = Boolean(latest) &&
      (cutoff == null || (latestTurn != null && latestTurn <= cutoff));
    const researched = useLatest
      ? new Set(Array.isArray(latest.researched) ? latest.researched : [])
      : set;
    if (useLatest) researched.forEach(tech => { if (!at.has(tech)) at.set(tech, null); });
    const source = useLatest ? latest : last;
    return {
      seat, researched, at, latest, retained,
      retainedFrom: retained ? null : from,
      turn: source ? number(source.turn) : null,
      seq: source ? number(source.seq) : null,
      researching: source && source.researching != null ? String(source.researching) : null,
    };
  }

  function diffOf(folds) {
    const shared = [], only = folds.map(() => []);
    // A seat whose history was not retained at this turn knows nothing here, so
    // nothing can be called shared while any seat is in that state.
    const known = folds.map(fold => Boolean(fold) && fold.retained !== false);
    const unretained = known.some(value => !value);
    const union = new Set();
    folds.forEach((fold, index) => {
      if (known[index]) fold.researched.forEach(tech => union.add(tech));
    });
    [...union].sort().forEach(tech => {
      const holders = folds.map((fold, index) => known[index] && fold.researched.has(tech));
      if (folds.length && !unretained && holders.every(Boolean)) shared.push(tech);
      else holders.forEach((has, index) => { if (has) only[index].push(tech); });
    });
    return {shared, only, unretained};
  }

  // Recorded options are shown only when the packet says it observed them. An
  // observed but empty list is a recorded fact, not an unasked question, and
  // any other token names the source instead of implying a list exists.
  function optionsNote(latest) {
    const row = latest && typeof latest === 'object' ? latest : null;
    const source = row && row.options_source != null ? String(row.options_source) : null;
    const options = row && Array.isArray(row.research_options) ? row.research_options : [];
    if (source === 'observed') {
      return options.length ? null : 'No options recorded in this packet (observed)';
    }
    return `Options not requested in this packet (${source || 'not recorded'})`;
  }

  // Chips exist only for an observed list; every other packet yields a note and
  // no chips, so an unobserved list can never be drawn as a menu of choices.
  function optionsView(latest) {
    const note = optionsNote(latest);
    if (note !== null) return {chips: [], note};
    return {chips: latest.research_options.map(option => ({
      tech_id: option && option.tech_id != null ? String(option.tech_id) : null,
      cost: number(option && option.cost)})), note: null};
  }

  // The three per-seat half states a tree node can show, decided in one place.
  function halfState(fold, id) {
    const unretained = Boolean(fold) && fold.retained === false;
    const known = Boolean(fold) && !unretained;
    return {done: known && fold.researched.has(id),
            researching: known && fold.researching === id,
            unretained};
  }

  // One seat's series over the shared turn axis. A turn with no row, or a field
  // that is not a number, is a gap: it stays null and never becomes zero.
  function seatSeries(rows, turns, key) {
    const byTurn = rows instanceof Map ? rows
      : new Map((Array.isArray(rows) ? rows : [])
        .filter(row => row && typeof row === 'object').map(row => [number(row.turn), row]));
    return (Array.isArray(turns) ? turns : []).map(turn => {
      const row = byTurn.get(turn);
      return row ? number(row[key]) : null;
    });
  }

  function statusFor(fold, id) {
    if (!fold) return 'no retained packet';
    if (fold.retained === false) return UNRETAINED;
    if (fold.researching === id) return 'researching';
    if (!fold.researched.has(id)) return 'not yet';
    const at = fold.at.get(id);
    return at == null ? 'researched, packet turn not retained' : `researched at turn ${at}`;
  }

  /* ------------------------------------------------------------------ charts */

  function niceMax(value) {
    if (!(value > 0)) return 1;
    const exponent = Math.pow(10, Math.floor(Math.log10(value)));
    const scaled = value / exponent;
    return (STEPS.find(step => scaled <= step + 1e-9) || 10) * exponent;
  }

  // Points join only when they are consecutive recorded turns for that seat: a
  // missing value and a missing turn both break the line rather than bridge it.
  function seriesSegments(values, turns) {
    const list = Array.isArray(values) ? values : [];
    const axis = Array.isArray(turns) ? turns : [];
    const runs = [];
    let current = null;
    list.forEach((raw, index) => {
      const value = number(raw), turn = number(axis[index]);
      if (value == null || turn == null) { current = null; return; }
      const point = {index, turn, value};
      if (current && current[current.length - 1].turn + 1 === turn) current.push(point);
      else { current = [point]; runs.push(current); }
    });
    return runs;
  }

  // Ticks are turn values on a turn-linear axis, not row positions.
  function axisTicks(turns) {
    const values = (Array.isArray(turns) ? turns : []).map(number).filter(finite);
    if (!values.length) return [];
    const low = Math.min(...values), high = Math.max(...values);
    const step = Math.max(1, Math.ceil((high - low) / 8));
    const ticks = [];
    for (let turn = low; turn <= high; turn += step) ticks.push(turn);
    if (ticks[ticks.length - 1] !== high) ticks.push(high);
    return ticks;
  }

  /* -------------------------------------------------------------- decisions */

  function directiveText(delta) {
    if (!delta || typeof delta !== 'object') return 'No directive record';
    const fields = (Array.isArray(delta.changed_fields) ? delta.changed_fields : []).map(String);
    const reasons = (Array.isArray(delta.reasons) ? delta.reasons : []).map(String);
    const source = delta.source == null ? null : String(delta.source);
    const changed = fields.join(', ') || 'none';
    if (delta.changed === null) {
      return `First recorded directive · ${source == null ? 'source not recorded' : humanize(source)}`;
    }
    if (source === null) return 'Directive recorded · source not recorded';
    if (source === 'model') {
      return `Model update · ${reasons.join(', ') || 'no reasons recorded'} · changed: ${changed}`;
    }
    if (source === 'autopilot') {
      return delta.changed === true ? `Changed under autopilot · ${changed}`
        : `Unchanged · autopilot · last decision T${delta.last_decision_turn == null ? '—' : delta.last_decision_turn}`;
    }
    return `${humanize(source)} · changed: ${changed}`;
  }

  function economyLines(rows) {
    if (!Array.isArray(rows) || !rows.length) return [{text: 'not recorded', bad: false}];
    return rows.filter(row => row && typeof row === 'object').map(row => {
      const status = row.status == null ? null : String(row.status);
      const accepted = isAccepted(status);
      const rejected = status != null && !accepted;
      const glyph = status == null ? '◌' : accepted ? '✓' : '✕';
      const parts = [`${short(row.tool || 'tool not recorded')} ` +
        `${row.item == null ? 'item not recorded' : String(row.item)} ${glyph}`];
      if (rejected) parts.push(String(row.rejection || row.outcome || 'rejected, no detail recorded'));
      else if (row.reason) parts.push(String(row.reason));
      if (number(row.eligible) === 0) parts.push('production: no eligible item');
      return {text: parts.join(' · '), bad: rejected};
    });
  }

  function growthText(growth) {
    if (!growth || typeof growth !== 'object') return 'not recorded';
    const threats = growth.threats && typeof growth.threats === 'object' ? growth.threats : {};
    const counts = [threats.confirmed_barbarian, threats.unknown, threats.unclassified]
      .map(value => (number(value) == null ? '—' : String(number(value)))).join('/');
    const minimum = number(growth.minimum_military_count);
    let line = `${String(growth.mode || 'mode not recorded')} · min military ` +
      `${minimum == null ? '—' : minimum} · threats ${counts}`;
    const mission = growth.settlement_mission;
    if (mission && typeof mission === 'object') {
      line += ` · settling ${String(mission.site || 'site not recorded')} ` +
        `(${String(mission.status || 'status not recorded')})`;
    }
    return line;
  }

  /* ------------------------------------------------------------------- tree */

  // Same-era prerequisite depth, memoised. A prerequisite cycle has no honest
  // depth, so every node on the in-progress chain that reached it lays out at 0.
  function treeDepths(nodes, edges) {
    const era = new Map();
    (Array.isArray(nodes) ? nodes : []).forEach(node => {
      if (node && typeof node === 'object' && node.id != null) {
        era.set(String(node.id), String(node.era));
      }
    });
    const prereqs = new Map([...era.keys()].map(id => [id, []]));
    (Array.isArray(edges) ? edges : []).forEach(edge => {
      if (!Array.isArray(edge) || edge.length < 2) return;
      const subject = String(edge[0]), prereq = String(edge[1]);
      if (!era.has(subject) || !era.has(prereq) || subject === prereq) return;
      prereqs.get(subject).push(prereq);
    });
    const depth = new Map(), open = new Set(), cyclic = new Set();
    const depthOf = id => {
      if (depth.has(id)) return depth.get(id);
      if (open.has(id)) { open.forEach(node => cyclic.add(node)); return 0; }
      open.add(id);
      const same = prereqs.get(id).filter(prereq => era.get(prereq) === era.get(id));
      const value = same.length ? 1 + Math.max(...same.map(depthOf)) : 0;
      open.delete(id);
      depth.set(id, cyclic.has(id) ? 0 : value);
      return depth.get(id);
    };
    [...era.keys()].sort().forEach(depthOf);
    return depth;
  }

  return {finite, isAccepted, humanize, short, label, number, fmt, foldSeat, diffOf, statusFor,
    optionsNote, optionsView, halfState, seatSeries, niceMax, seriesSegments, axisTicks,
    directiveText, economyLines, growthText, treeDepths, UNRETAINED};
})();

if (typeof window !== 'undefined') window.civArenaCompareCore = Core;
else module.exports = Core;

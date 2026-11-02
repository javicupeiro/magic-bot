// Draw the known graph topology; only same-graph, consecutive observed starts light its edges.
// Coordinates stay fixed across turns. Node colors and visit counts come exclusively from SSE steps.
const svgNS = 'http://www.w3.org/2000/svg';
const mainNodes = [
  ['route', 20, 12], ['decompose', 160, 12], ['retrieve', 300, 12], ['expand', 440, 12],
  ['generate', 580, 12], ['validate', 720, 12], ['review', 860, 12], ['finalize', 1000, 12],
  ['remember', 1140, 12], ['end', 1280, 12], ['clarify', 20, 76], ['cards', 160, 76],
  ['check_cards', 300, 76], ['repair', 720, 76], ['retrieve_more', 860, 76],
];
// Each path represents a real edge in build_graph(), including both terminal branches and local repair.
const mainEdges = [
  ['route', 'decompose', 'M132 29H160'], ['decompose', 'retrieve', 'M272 29H300'],
  ['retrieve', 'expand', 'M412 29H440'], ['expand', 'generate', 'M552 29H580'],
  ['generate', 'validate', 'M692 29H720'], ['validate', 'review', 'M832 29H860'],
  ['review', 'finalize', 'M972 29H1000'], ['finalize', 'remember', 'M1112 29H1140'],
  ['remember', 'end', 'M1252 29H1280'], ['route', 'clarify', 'M76 46V76'],
  ['route', 'cards', 'M100 46V60H216V76'], ['cards', 'check_cards', 'M272 93H300'],
  ['check_cards', 'decompose', 'M356 76V66H244V46'],
  ['check_cards', 'remember', 'M356 110V122H1176V46'],
  ['clarify', 'remember', 'M76 110V138H1216V46'],
  ['review', 'retrieve_more', 'M916 46V76'],
  ['retrieve_more', 'repair', 'M860 93H832'], ['repair', 'validate', 'M776 76V46'],
  ['review', 'repair', 'M888 46V60H804V76'],
];
const cardNodes = ['prepare', 'model', 'tools', 'collect', 'finish'].map((name, index) => [`cards.${name}`, 20 + 140 * index, 34]);
const cardEdges = [
  ['cards.prepare', 'cards.model', 'M132 51H160'], ['cards.model', 'cards.tools', 'M272 51H300'],
  ['cards.tools', 'cards.collect', 'M412 51H440'], ['cards.collect', 'cards.model', 'M496 68V98H216V68'],
  ['cards.model', 'cards.finish', 'M216 34V12H636V34'],
  ['cards.collect', 'cards.finish', 'M552 51H580'],
];
const recoveryNodes = new Set(['retrieve_more', 'repair']);
const duration = value => Number.isFinite(value) ? `${(value / 1000).toFixed(3)} s` : 'Not available';

function element(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}

function svgElement(tag, attributes) {
  const result = document.createElementNS(svgNS, tag);
  for (const [key, value] of Object.entries(attributes)) result.setAttribute(key, value);
  return result;
}

// Keep real HTML buttons over a decorative SVG: keyboard selection needs no custom SVG focus handling.
function drawGraph(container, nodes, edges, width, height, prefix, onSelect) {
  const canvas = element('div', undefined, 'workflow-canvas');
  canvas.style.setProperty('--graph-width', `${width}px`);
  canvas.style.height = `${height}px`;
  const svg = svgElement('svg', { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' });
  const definitions = svgElement('defs', {});
  for (const color of ['idle', 'completed', 'recovery']) {
    const marker = svgElement('marker', { id: `${prefix}-${color}`, viewBox: '0 0 10 10', refX: 9,
      refY: 5, markerWidth: 5, markerHeight: 5, orient: 'auto-start-reverse' });
    marker.append(svgElement('path', { d: 'M0 0L10 5L0 10Z', class: `arrow-${color}` }));
    definitions.append(marker);
  }
  svg.append(definitions);
  const edgeElements = edges.map(([from, to, d]) => {
    const path = svgElement('path', { d, class: 'workflow-edge', 'data-from': from, 'data-to': to,
      'marker-end': `url(#${prefix}-idle)` });
    svg.append(path);
    return { from, to, path, prefix };
  });
  canvas.append(svg);
  const nodeElements = nodes.map(([name, x, y]) => {
    const button = element(name === 'end' ? 'div' : 'button', undefined, 'workflow-node');
    button.dataset.node = name;
    button.style.left = `${x / width * 100}%`;
    button.style.top = `${y}px`;
    button.style.width = `${112 / width * 100}%`;
    const icon = element('span', '○', 'workflow-symbol');
    icon.setAttribute('aria-hidden', 'true');
    const title = element('span', name === 'end' ? 'End' : name.replace('cards.', ''), 'workflow-name');
    const count = element('span', '', 'workflow-count');
    count.setAttribute('aria-hidden', 'true');
    button.append(icon, title, count);
    if (name !== 'end') {
      button.type = 'button';
      button.addEventListener('click', () => onSelect(name));
    }
    canvas.append(button);
    return { name, button, icon, count };
  });
  container.append(canvas);
  return { nodes: nodeElements, edges: edgeElements };
}

export function mountWorkflowMap(onSelect) {
  // Updates reuse buttons so SSE never steals focus or resets horizontal scrolling.
  const main = drawGraph(document.getElementById('workflow-main'), mainNodes, mainEdges, 1412, 150, 'main', onSelect);
  const cards = drawGraph(document.getElementById('workflow-cards'), cardNodes, cardEdges, 720, 112, 'cards', onSelect);
  const trail = document.getElementById('workflow-trail');
  const summary = document.getElementById('workflow-status');
  const subgraph = document.getElementById('workflow-card-section');
  const toggle = document.getElementById('workflow-card-toggle');
  toggle.addEventListener('click', () => {
    subgraph.hidden = !subgraph.hidden;
    toggle.setAttribute('aria-expanded', String(!subgraph.hidden));
  });
  const panel = document.getElementById('workflow');
  panel.addEventListener('toggle', () => {
    document.getElementById('workflow-toggle-label').textContent = panel.open ? 'Hide map' : 'Show map';
  });
  let previousTurn;
  const chips = new Map();

  return function update(turn, selected) {
    if (turn !== previousTurn) {
      previousTurn = turn;
      trail.replaceChildren();
      chips.clear();
    }
    const steps = turn?.steps ?? [];
    const visits = new Map();
    const observedEdges = new Set();
    // The enclosing cards node overlaps its children. Never connect it to a child or
    // mistake their interleaved events for transitions in the main graph.
    for (const nested of [false, true]) {
      let previous;
      for (const step of steps.filter(item => item.name.startsWith('cards.') === nested)) {
        if (previous) observedEdges.add(`${previous.name}>${step.name}`);
        previous = step;
      }
    }
    for (const step of steps) {
      const group = visits.get(step.name) ?? [];
      group.push(step);
      visits.set(step.name, group);
      if (!chips.has(step.id)) {
        const item = element('li');
        const chip = element('button', undefined, 'workflow-chip');
        chip.type = 'button';
        chip.addEventListener('click', () => onSelect(step.name));
        item.append(chip);
        trail.append(item);
        chips.set(step.id, chip);
      }
      const chip = chips.get(step.id);
      chip.textContent = `${step.name} #${group.length}`;
      chip.dataset.status = step.status;
      chip.title = `${step.status} · ${duration(step.duration)}`;
      chip.setAttribute('aria-pressed', String(selected === step.name));
    }
    const last = steps.at(-1);
    const active = steps.filter(step => step.status === 'Running').at(-1);
    summary.textContent = active ? `${active.name} · visit ${visits.get(active.name).length}` : (turn?.status ?? 'Waiting for events');
    summary.dataset.status = active ? 'Running' : (turn?.status ?? 'Waiting');
    for (const { name, button, icon, count } of [...main.nodes, ...cards.nodes]) {
      const group = visits.get(name) ?? [];
      const latest = group.at(-1);
      const status = name === 'end' && turn?.status === 'Turn finished' ? 'Completed' : (latest?.status ?? 'Not visited');
      button.dataset.status = status;
      button.classList.toggle('recovery', recoveryNodes.has(name) && status === 'Completed');
      icon.textContent = { Running: '●', Completed: '✓', Failed: '!', Interrupted: '■' }[status] ?? '○';
      count.textContent = group.length ? `×${group.length}` : '';
      const description = `${name === 'end' ? 'End' : name}: ${status}${group.length ? `, ${group.length} visit(s), latest duration: ${duration(latest.duration)}` : ''}`;
      button.setAttribute('aria-label', description);
      button.title = description;
      if (name !== 'end') button.setAttribute('aria-pressed', String(selected === name));
    }
    if (turn?.status === 'Turn finished' && last?.name === 'remember') observedEdges.add('remember>end');
    for (const { from, to, path, prefix } of [...main.edges, ...cards.edges]) {
      const visited = observedEdges.has(`${from}>${to}`);
      const recovery = recoveryNodes.has(from) || recoveryNodes.has(to) || (from === 'cards.collect' && to === 'cards.model');
      const color = visited ? (recovery ? 'recovery' : 'completed') : 'idle';
      path.dataset.visited = String(visited);
      path.setAttribute('class', `workflow-edge ${color}`);
      path.setAttribute('marker-end', `url(#${prefix}-${color})`);
    }
    document.getElementById('workflow-trail-empty').hidden = steps.length > 0;
    const cardVisits = steps.filter(step => step.name.startsWith('cards.')).length;
    toggle.textContent = `Card subgraph${cardVisits ? ` · ${cardVisits} steps` : ''}`;
  };
}

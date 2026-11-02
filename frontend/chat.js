// Own small UI state; routing, evidence selection and conversation memory stay on the server.
import { readEvents, requestJSON } from './stream.js';
import { mountWorkflowMap } from './workflow.js';

const element = id => document.getElementById(id);
const state = { conversation: null, messages: [], busy: false, turn: null, selectedStep: null };
const updateWorkflowMap = mountWorkflowMap(name => {
  state.selectedStep = name;
  renderExecution();
});
const labels = {
  route: 'Route question', cards: 'Cards · enclosing subgraph', check_cards: 'Check card evidence',
  'cards.prepare': 'Cards · prepare', 'cards.model': 'Cards · model', 'cards.tools': 'Cards · tools',
  'cards.collect': 'Cards · collect evidence', 'cards.finish': 'Cards · response',
  decompose: 'Decompose question', retrieve: 'Retrieve rules', expand: 'Expand references',
  generate: 'Generate answer', validate: 'Validate citations', review: 'Review answer',
  retrieve_more: 'Retry · retrieve more', repair: 'Retry · repair answer', finalize: 'Finalize',
  clarify: 'Request clarification', remember: 'Store conversation',
};
const formatDuration = value => Number.isFinite(value) ? `${(value / 1000).toFixed(3)} s` : 'Not available';
const formatTokens = value => Number.isInteger(value) ? String(value) : 'Not available';

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}

function notice(text, error = false) {
  element('notice').textContent = text;
  element('notice').classList.toggle('error', error);
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ['send', 'new', 'reload', 'conversations', 'message']) element(id).disabled = busy;
  element('chat-status').textContent = busy ? 'Working' : 'Ready';
}

// Never interpret model, source or catalogue strings as HTML, including in history.
function renderMessages() {
  const container = element('messages');
  container.replaceChildren();
  if (!state.messages.length) container.append(node('p', 'Ask in English or Spanish. Search for a card or explore a rules interaction.', 'empty'));
  for (const message of state.messages) {
    const article = node('article', undefined, `message ${message.role}${message.pending ? ' pending' : ''}`);
    article.append(node('p', message.role === 'user' ? 'You' : 'Magic bot', 'message-label'));
    article.append(node('div', message.content, 'message-content'));
    if (message.result) renderEvidence(article, message.result);
    container.append(article);
  }
  container.scrollTop = container.scrollHeight;
}

// Only absolute HTTP(S) catalogue URLs can cause image requests; absent/broken images keep the text.
function imageURL(value) {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

function renderEvidence(container, result) {
  for (const source of result.sources ?? []) {
    const details = node('details');
    details.append(node('summary', `[${source.source_id}] ${source.title}`));
    details.append(node('p', `${source.kind} · Pages ${source.page_start}–${source.page_end} · Version ${source.version}`, 'source-meta'));
    details.append(node('p', source.excerpt, 'source-excerpt'));
    container.append(details);
  }
  if (!result.cards?.length) return;
  const cards = node('div', undefined, 'cards');
  for (const card of result.cards) {
    const article = node('article', undefined, 'card');
    article.append(node('h3', card.name));
    article.append(node('p', `Mana: ${card.mana_cost ?? 'Not available'} · Value: ${card.cmc ?? 'Not available'}`));
    const fallback = node('p', 'Image not available.', 'image-fallback');
    const url = imageURL(card.image_url);
    if (url) {
      const image = node('img');
      image.alt = card.name;
      image.loading = 'lazy';
      image.referrerPolicy = 'no-referrer';
      image.addEventListener('error', () => image.replaceWith(fallback), { once: true });
      image.src = url;
      article.append(image);
    } else article.append(fallback);
    article.append(node('p', card.text ?? 'Card text not available.', 'card-text'));
    cards.append(article);
  }
  container.append(cards);
}

// Update existing step nodes so arriving events do not close expanded citations or steal keyboard focus.
function renderExecution() {
  const turn = state.turn;
  element('execution-status').textContent = turn?.status ?? 'Waiting';
  element('duration').textContent = formatDuration(turn?.duration);
  const usage = turn?.usage ?? [];
  const reported = usage.filter(item => Number.isInteger(item.total_tokens));
  element('tokens').textContent = reported.length ? String(reported.reduce((sum, item) => sum + item.total_tokens, 0)) : 'Not available';
  element('usage-note').textContent = `${usage.length} model usage event(s); sum of reported totals only, possibly partial. Parent and child durations overlap and are never added.`;
  const active = turn?.steps.filter(step => step.status === 'Running') ?? [];
  element('current').textContent = active.length ? `Current: ${active.map(step => labels[step.name] ?? step.name).join(' / ')}` : 'No active step.';
  const selected = state.selectedStep;
  const selectedVisits = turn?.steps.filter(step => step.name === selected) ?? [];
  element('step-selection').textContent = selected
    ? `${selected} · ${selectedVisits.length} observed visit(s).${selectedVisits.length ? ' Durations are shown per visit.' : ' This step has not run in this turn.'}`
    : 'Showing all observed steps. Select a map node to inspect its visits.';
  element('show-all-steps').hidden = !selected;
  const visits = new Map();
  for (const step of turn?.steps ?? []) {
    const visit = (visits.get(step.name) ?? 0) + 1;
    visits.set(step.name, visit);
    if (!step.element) {
      step.element = node('li');
      element('steps').append(step.element);
    }
    step.element.hidden = Boolean(selected && step.name !== selected);
    step.element.dataset.step = step.name;
    step.element.className = `step${step.name.startsWith('cards.') ? ' child' : ''}${step.status === 'Running' ? ' active' : ''}${step.status === 'Failed' ? ' failed' : ''}`;
    step.element.replaceChildren(node('div', labels[step.name] ?? step.name, 'step-title'),
      node('p', `Visit ${visit} · ${step.status} · ${formatDuration(step.duration)}`, 'step-meta'));
    for (const tool of step.tools) step.element.append(node('p', `Tool: ${tool.tool} · ${tool.status} · ${formatDuration(tool.duration_ms)} · Arguments: not available`, 'step-meta'));
    for (const call of step.usage) step.element.append(node('p', `Model tokens · input: ${formatTokens(call.input_tokens)} · output: ${formatTokens(call.output_tokens)} · total: ${formatTokens(call.total_tokens)}`, 'step-meta'));
  }
  updateWorkflowMap(turn, selected);
}

function resetExecution(status) {
  state.selectedStep = null;
  state.turn = status ? { status, steps: [], usage: [], sequence: 0, id: null } : null;
  element('steps').replaceChildren();
  element('evidence').replaceChildren();
  renderExecution();
}

// Correlate occurrences by opaque step_id, never by name (nodes can run more than once).
function receiveEvent(event) {
  const turn = state.turn;
  if (event.conversation_id !== state.conversation || (turn.id && event.turn_id !== turn.id)) throw new Error('Unexpected conversation or turn in stream. Reload history.');
  if (event.sequence <= turn.sequence) return;
  if (event.sequence !== turn.sequence + 1) throw new Error('Missing execution events. Reload history.');
  turn.sequence = event.sequence;
  turn.id = event.turn_id;
  if (event.type === 'turn_started') turn.status = 'Running';
  if (event.type === 'step_started') turn.steps.push({ id: event.step_id, name: event.step, status: 'Running', tools: [], usage: [] });
  const step = turn.steps.find(item => item.id === event.step_id);
  if (event.type === 'step_completed' && step) Object.assign(step, { status: 'Completed', duration: event.duration_ms });
  if (event.type === 'tool' && step) step.tools.push(event);
  if (event.type === 'model_usage') {
    turn.usage.push(event);
    if (step) step.usage.push(event);
  }
  if (event.type === 'sources') {
    element('evidence').replaceChildren(node('h3', 'Sources'));
    renderEvidence(element('evidence'), { sources: event.sources });
  }
  if (event.type === 'error') {
    if (step) Object.assign(step, { status: 'Failed', duration: event.duration_ms });
    if (event.code === 'execution_failed') throw new Error('Turn failed. Observed steps are preserved. Reload history before sending again.');
  }
  if (event.type === 'answer') {
    turn.status = 'Turn finished';
    turn.duration = event.duration_ms;
    state.messages.at(-1).pending = false;
    state.messages.push({ role: 'assistant', content: event.result.answer, result: event.result });
    renderMessages();
    notice('Turn finished. A clarification or abstention can also finish a turn; this is not a factual approval.');
  }
  renderExecution();
}

async function refreshConversations() {
  const conversations = await requestJSON('/conversations');
  const select = element('conversations');
  select.replaceChildren(new Option('New conversation', ''));
  for (const item of conversations) select.add(new Option(item.conversation_id, item.conversation_id));
  select.value = state.conversation ?? '';
}

// Commit fetched history only after a successful read; preserve unconfirmed local messages on reload.
async function loadHistory(identifier) {
  const history = identifier ? await requestJSON(`/conversations/${encodeURIComponent(identifier)}/messages`) : { messages: [] };
  const local = identifier === state.conversation ? state.messages : [];
  const persisted = history.messages.map(message => ({ ...message }));
  // Match the known prefix before reconciling a possibly persisted interrupted turn.
  // Keep failed messages in their original position when later turns did complete.
  let cursor = 0;
  for (const message of local) {
    const match = persisted[cursor]?.role === message.role && persisted[cursor]?.content === message.content;
    if (match) cursor += 1;
    else if (message.pending) {
      persisted.splice(cursor, 0, message);
      cursor += 1;
    }
  }
  state.conversation = identifier;
  state.messages = persisted;
  if (identifier) localStorage.setItem('magic-bot-conversation', identifier);
  else localStorage.removeItem('magic-bot-conversation');
  element('conversations').value = identifier ?? '';
  renderMessages();
  resetExecution();
  notice(state.messages.some(message => message.pending)
    ? 'History loaded. The unconfirmed message is kept locally; decide whether to send it again.'
    : 'History loaded. Past execution events, card images and structured sources are not included in the history contract.');
}

async function runControl(action) {
  if (state.busy) return;
  setBusy(true);
  try { await action(); }
  catch (error) { notice(error.message, true); element('conversations').value = state.conversation ?? ''; }
  finally { setBusy(false); }
}

// One user action creates at most one POST. Failed/uncertain turns need explicit user recovery.
async function sendMessage(event) {
  event.preventDefault();
  const message = element('message').value;
  if (state.busy || !message.trim() || message.length > 16000) return;
  setBusy(true);
  try {
    if (!state.conversation) {
      const created = await requestJSON('/conversations', { method: 'POST' });
      state.conversation = created.conversation_id;
      localStorage.setItem('magic-bot-conversation', state.conversation);
      await refreshConversations();
    }
    state.messages.push({ role: 'user', content: message, pending: true });
    element('message').value = '';
    renderMessages();
    resetExecution('Connecting');
    notice('Waiting for backend events…');
    const response = await fetch(`/conversations/${encodeURIComponent(state.conversation)}/messages`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message }),
    });
    await readEvents(response, receiveEvent);
  } catch (error) {
    if (state.turn) {
      state.turn.status = 'Failed / uncertain';
      for (const step of state.turn.steps) if (step.status === 'Running') step.status = 'Interrupted';
      renderExecution();
    }
    if (!element('message').value) element('message').value = message;
    notice(`${error.message} No automatic retry was made.`, true);
  } finally {
    setBusy(false);
    element('message').focus();
  }
}

element('show-all-steps').addEventListener('click', () => {
  state.selectedStep = null;
  renderExecution();
});
element('composer').addEventListener('submit', sendMessage);
element('message').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    element('composer').requestSubmit();
  }
});
element('new').addEventListener('click', () => runControl(async () => {
  const created = await requestJSON('/conversations', { method: 'POST' });
  await refreshConversations();
  await loadHistory(created.conversation_id);
}));
element('reload').addEventListener('click', () => runControl(async () => {
  await refreshConversations();
  await loadHistory(state.conversation);
}));
element('conversations').addEventListener('change', event => {
  const identifier = event.target.value || null;
  runControl(() => loadHistory(identifier));
});
runControl(async () => {
  await refreshConversations();
  const saved = localStorage.getItem('magic-bot-conversation');
  const exists = [...element('conversations').options].some(option => option.value === saved);
  await loadHistory(exists ? saved : null);
});

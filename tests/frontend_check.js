// Run in the fixture browser: await (await import('/_checks.js')).runChecks().
// This exercises the real DOM/modules against scripted HTTP, not model wording or CSS snapshots.
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const get = id => document.getElementById(id);
const text = id => get(id).textContent;
async function waitFor(predicate) {
  const deadline = Date.now() + 5000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error('Timed out waiting for browser state');
    await new Promise(resolve => setTimeout(resolve, 25));
  }
}
async function send(message, inspect) {
  get('message').value = message;
  get('composer').requestSubmit();
  await waitFor(() => get('send').disabled);
  // A second submission while pending must not create another POST.
  get('composer').dispatchEvent(new Event('submit', { cancelable: true }));
  if (inspect) await inspect();
  await waitFor(() => !get('send').disabled);
}

export async function runChecks() {
  await waitFor(() => !get('send').disabled);
  const { readEvents } = await import('/ui/stream.js');
  const prose = 'Daño · 日本語';
  const bytes = new TextEncoder().encode(`: comment\r\n\r\nevent: answer\r\ndata: ${JSON.stringify({ type: 'answer', result: { answer: prose } })}\r\n\r\n`);
  const received = [];
  await readEvents(new Response(new ReadableStream({ start(controller) {
    for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
    controller.close();
  } }), { headers: { 'Content-Type': 'text/event-stream' } }), event => received.push(event));
  assert(received.length === 1 && received[0].result.answer === prose, 'Bytewise UTF-8/CRLF parsing failed');
  let truncated = false;
  try { await readEvents(new Response('data: {"type":"turn_started"}\n\n', { headers: { 'Content-Type': 'text/event-stream' } }), () => {}); }
  catch { truncated = true; }
  assert(truncated, 'EOF without a terminal event must fail');

  const before = (await (await fetch('/_stats')).json()).posts.length;
  const reviewNode = () => document.querySelector('.workflow-node[data-node="review"]');
  await send('¿Cómo interactúan first strike y deathtouch?', async () => {
    await waitFor(() => reviewNode().dataset.status === 'Running');
    const button = reviewNode();
    const top = button.getBoundingClientRect().top;
    button.focus();
    button.click();
    await waitFor(() => document.querySelector('.workflow-node[data-node="retrieve_more"]').dataset.status === 'Running');
    assert(document.activeElement === button, 'SSE updates stole map-node keyboard focus');
    await waitFor(() => button.dataset.status === 'Running' && button.querySelector('.workflow-count').textContent === '×2');
    assert(reviewNode() === button && button.getBoundingClientRect().top === top, 'Repeated visits moved or replaced a map node');
    assert(text('step-selection').includes('2 observed visit(s)'), 'Selection did not update during repeated execution');
  });
  assert(text('messages').includes('First strike deals'), 'Answer missing');
  assert(text('steps').includes('Retry · repair'), 'Review/retry events missing');
  assert(get('steps').querySelectorAll('.step-title').length === 21, 'Step occurrences were lost');
  assert([...get('steps').querySelectorAll('.step-title')].filter(item => item.textContent === 'Cards · model').length === 2, 'Repeated model steps collapsed');
  const mapNode = name => document.querySelector(`.workflow-node[data-node="${name}"]`);
  const mapEdge = (from, to) => document.querySelector(`.workflow-edge[data-from="${from}"][data-to="${to}"]`);
  assert(mapNode('review').querySelector('.workflow-count').textContent === '×2', 'Map collapsed repeated review visits');
  assert(mapNode('validate').querySelector('.workflow-count').textContent === '×2', 'Map collapsed repeated validation');
  assert(mapNode('cards.model').querySelector('.workflow-count').textContent === '×2', 'Map collapsed card-model cycles');
  assert(mapEdge('cards.collect', 'cards.model').dataset.visited === 'true', 'Card loop was not traced');
  assert(mapEdge('review', 'retrieve_more').dataset.visited === 'true' && mapEdge('repair', 'validate').dataset.visited === 'true', 'Recovery loop was not traced');
  assert(mapEdge('check_cards', 'decompose').dataset.visited === 'true', 'Verified interaction did not continue to rules on map');
  assert(mapEdge('check_cards', 'remember').dataset.visited === 'false', 'Map invented a card-only exit');
  assert(mapNode('clarify').dataset.status === 'Not visited' && mapNode('end').dataset.status === 'Completed', 'Map invented a branch or lost terminal event');
  assert(get('workflow-trail').children.length === 21, 'Chronological map history lost visits');
  mapNode('review').click();
  assert(text('step-selection').includes('2 observed visit(s)'), 'Selected node visit count missing');
  assert([...get('steps').children].filter(item => !item.hidden).length === 2, 'Selected node did not filter occurrence details');
  mapNode('clarify').click();
  assert(text('step-selection').includes('has not run'), 'Unvisited node invents observations');
  get('show-all-steps').click();
  assert([...get('steps').children].every(item => !item.hidden), 'Show all did not reset node filter');
  get('workflow-card-toggle').click();
  assert(!get('workflow-card-section').hidden && get('workflow-card-toggle').getAttribute('aria-expanded') === 'true', 'Card subgraph did not expand');
  get('workflow-card-toggle').click();
  get('workflow').open = false;
  assert(!get('workflow').open, 'Map did not collapse');
  get('workflow').open = true;
  assert(text('tokens') === '10', 'Model tokens counted incorrectly');
  assert(text('duration') === '0.123 s', 'Turn duration must use the terminal wall time');
  assert(text('steps').includes('0.000 s') && text('steps').includes('not available'), 'Zero/unknown distinction lost');
  assert(text('messages').includes('日本語') && text('messages').includes('Version 2026-09'), 'Source provenance missing');
  assert(!document.querySelector('.source-excerpt script, .source-excerpt img, .card-text b'), 'External HTML was interpreted');
  assert(document.querySelectorAll('.image-fallback').length >= 1, 'Unsafe/absent images not handled');
  await waitFor(() => [...document.querySelectorAll('#messages .card img')].some(image => image.naturalWidth > 0));
  assert(!document.querySelector('img[src^="javascript:"]'), 'Unsafe image URL was accepted');
  const detail = document.querySelector('#messages details');
  detail.querySelector('summary').click();
  assert(detail.open, 'Citation did not expand');
  const first = get('conversations').value;
  get('reload').click();
  await waitFor(() => !get('reload').disabled);
  assert(text('messages').includes('First strike deals') && !get('steps').children.length, 'History reload lost messages or fabricated steps');
  assert(mapNode('review').dataset.status === 'Not visited' && !get('workflow-trail').children.length, 'History reload fabricated map events');
  get('new').click();
  await waitFor(() => !get('new').disabled);
  const second = get('conversations').value;
  assert(first !== second && !document.querySelector('.message'), 'New conversation was not isolated');
  get('conversations').value = first;
  get('conversations').dispatchEvent(new Event('change'));
  await waitFor(() => !get('conversations').disabled);
  assert(text('messages').includes('First strike deals'), 'Conversation switching failed');
  await send('/cards');
  assert(!text('steps').includes('Retrieve rules') && text('messages').includes('Found Sample.'), 'Card search continued into rules');
  assert(mapEdge('check_cards', 'remember').dataset.visited === 'true' && mapNode('retrieve').dataset.status === 'Not visited', 'Card-only map route is incorrect');
  get('workflow').open = false;
  await send('/local-repair');
  assert(!get('workflow').open, 'Arriving events unexpectedly expanded the map');
  get('workflow').open = true;
  assert(mapEdge('route', 'decompose').dataset.visited === 'true' && mapNode('cards').dataset.status === 'Not visited', 'Rules route invented card execution');
  assert(mapEdge('review', 'repair').dataset.visited === 'true' && mapEdge('review', 'retrieve_more').dataset.visited === 'false', 'Local repair invented an additional search');
  assert(mapNode('review').querySelector('.workflow-count').textContent === '×2', 'Collapsed map lost repeated visits');
  await send('/clarify');
  assert(text('messages').includes('Which card') && text('execution-status') === 'Turn finished', 'Clarification did not end turn');
  assert(mapEdge('clarify', 'remember').dataset.visited === 'true' && mapNode('cards').dataset.status === 'Not visited', 'Clarification map route is incorrect');
  await send('/error');
  assert(text('notice').includes('Turn failed') && text('steps').includes('Failed'), 'Failure did not preserve observed steps');
  assert(mapNode('retrieve').dataset.status === 'Failed' && mapNode('end').dataset.status === 'Not visited', 'Map hides failure or invents completion');
  assert(get('message').value === '/error' && text('messages').includes('/error'), 'Failed message was lost');
  get('reload').click();
  await waitFor(() => !get('reload').disabled);
  assert(text('messages').includes('/error'), 'Reload lost unconfirmed message');
  await send('/cut');
  assert(text('notice').includes('Connection ended') && text('steps').includes('Interrupted'), 'Truncated turn not recoverable');
  assert(mapNode('cards.tools').dataset.status === 'Interrupted', 'Map hides interrupted card step');
  await send('/busy');
  assert(text('notice').includes('busy'), 'HTTP error not shown');
  await send('/clarify');
  assert(text('execution-status') === 'Turn finished', 'Could not recover after errors');
  get('reload').click();
  await waitFor(() => !get('reload').disabled);
  assert(document.querySelectorAll('.message.pending').length === 3, 'Pending messages were lost during history reconciliation');
  const after = (await (await fetch('/_stats')).json()).posts.length;
  assert(after - before === 8, `Unexpected POST count: ${after - before}`);
  return { passed: true, posts: after - before, checks: 'SSE UTF-8/CRLF/EOF, bilingual answer/sources, repeated card cycles, workflow routes/recovery/selection/reset/failure, metrics, safe DOM/images, reload/switch, cards, clarification, failure/recovery, duplicate submit' };
}

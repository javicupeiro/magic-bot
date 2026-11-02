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
async function send(message) {
  get('message').value = message;
  get('composer').requestSubmit();
  await waitFor(() => get('send').disabled);
  // A second submission while pending must not create another POST.
  get('composer').dispatchEvent(new Event('submit', { cancelable: true }));
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
  await send('¿Cómo interactúan first strike y deathtouch?');
  assert(text('messages').includes('First strike deals'), 'Answer missing');
  assert(text('steps').includes('Retry · repair'), 'Review/retry events missing');
  assert(get('steps').querySelectorAll('.step-title').length === 20, 'Step occurrences were lost');
  assert([...get('steps').querySelectorAll('.step-title')].filter(item => item.textContent === 'Cards · model').length === 2, 'Repeated model steps collapsed');
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
  await send('/clarify');
  assert(text('messages').includes('Which card') && text('execution-status') === 'Turn finished', 'Clarification did not end turn');
  await send('/error');
  assert(text('notice').includes('Turn failed') && text('steps').includes('Failed'), 'Failure did not preserve observed steps');
  assert(get('message').value === '/error' && text('messages').includes('/error'), 'Failed message was lost');
  get('reload').click();
  await waitFor(() => !get('reload').disabled);
  assert(text('messages').includes('/error'), 'Reload lost unconfirmed message');
  await send('/cut');
  assert(text('notice').includes('Connection ended') && text('steps').includes('Interrupted'), 'Truncated turn not recoverable');
  await send('/busy');
  assert(text('notice').includes('busy'), 'HTTP error not shown');
  await send('/clarify');
  assert(text('execution-status') === 'Turn finished', 'Could not recover after errors');
  get('reload').click();
  await waitFor(() => !get('reload').disabled);
  assert(document.querySelectorAll('.message.pending').length === 3, 'Pending messages were lost during history reconciliation');
  const after = (await (await fetch('/_stats')).json()).posts.length;
  assert(after - before === 7, `Unexpected POST count: ${after - before}`);
  return { passed: true, posts: after - before, checks: 'SSE UTF-8/CRLF/EOF, bilingual answer/sources, repeated card cycles, metrics, safe DOM/images, reload/switch, cards, clarification, failure/recovery, duplicate submit' };
}

// Consume POST SSE as frames, independently of UTF-8 and network chunk boundaries.
export async function readEvents(response, onEvent) {
  if (!response.ok) {
    const code = (await response.json().catch(() => null))?.detail?.code;
    throw new Error(code === 'conversation_busy'
      ? 'This conversation is busy. Reload history before sending again.'
      : `Request failed (HTTP ${response.status}). Reload history before trying again.`);
  }
  if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body) {
    throw new Error('Expected an event stream. Reload history before sending again.');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let buffer = '';
  let terminal = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      // The API uses LF; accept CRLF too, including a CR/LF split between reads.
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const data = frame.split(/\r?\n/).filter(line => line.startsWith('data:'))
          .map(line => line.slice(5).replace(/^ /, '')).join('\n');
        if (!data) continue;
        const event = JSON.parse(data);
        onEvent(event);
        terminal = event.type === 'answer' || (event.type === 'error' && event.code === 'execution_failed');
        if (terminal) return;
      }
      if (done) break;
    }
    throw new Error('Connection ended before a final result. Reload history; the turn may have completed.');
  } finally {
    // Cancels unread bytes after a terminal event or parsing error; never retries POST.
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export async function requestJSON(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`Request failed (HTTP ${response.status}). Try reloading history.`);
  return response.json();
}

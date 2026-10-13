// Return YouTube cookies only from the explicitly selected local browser page.
// Python captures this output in memory; no cookie export or log is created.
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
try {
  const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const address = new URL(request.socket);
  if (address.protocol !== 'ws:' || address.hostname !== '127.0.0.1') throw new Error();
  const cookies = await new Promise((resolve, reject) => {
    const socket = new WebSocket(request.socket);
    let finished = false;
    const finish = (value, failed = false) => {
      if (finished) return;
      finished = true;
      clearTimeout(timeout);
      socket.close();
      if (failed) reject(new Error());
      else resolve(value);
    };
    const timeout = setTimeout(() => finish(null, true), 15000);
    socket.addEventListener('open', () => socket.send(JSON.stringify({
      id: 1, method: 'Network.getCookies', params: { urls: ['https://www.youtube.com/'] }
    })));
    const fail = () => finish(null, true);
    socket.addEventListener('error', fail);
    socket.addEventListener('close', fail);
    socket.addEventListener('message', (event) => {
      try {
        const response = JSON.parse(event.data);
        if (response.id !== 1) return;
        if (response.error || !Array.isArray(response.result?.cookies)) throw new Error();
        finish(response.result.cookies.filter((cookie) => {
          const domain = String(cookie.domain).replace(/^\./, '').toLowerCase();
          return domain === 'youtube.com' || domain.endsWith('.youtube.com');
        }));
      } catch { fail(); }
    });
  });
  process.stdout.write(JSON.stringify(cookies));
} catch {
  process.stderr.write('Local browser session unavailable\n');
  process.exitCode = 1;
}

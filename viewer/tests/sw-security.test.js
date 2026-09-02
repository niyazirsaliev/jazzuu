const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function boot() {
  const listeners = new Map();
  const puts = [];
  const fetches = [];
  const cache = {
    addAll: async () => {},
    put: async (request) => { puts.push(request.url); },
  };
  const sandbox = {
    URL,
    Promise,
    self: {
      location: { origin: 'https://viewer.test' },
      addEventListener: (type, fn) => listeners.set(type, fn),
      skipWaiting: async () => {},
      clients: { claim: async () => {} },
    },
    caches: {
      open: async () => cache,
      keys: async () => [],
      delete: async () => true,
      match: async () => undefined,
    },
    fetch: async (request) => {
      fetches.push(request.url);
      return {
        status: 200,
        type: 'basic',
        clone() { return this; },
      };
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'sw.js'), 'utf8'),
    sandbox,
  );

  async function request(pathname, options = {}) {
    let responsePromise = null;
    const request = {
      url: `https://viewer.test${pathname}`,
      method: options.method || 'GET',
      headers: new Map(Object.entries(options.headers || {})),
    };
    listeners.get('fetch')({
      request,
      respondWith(value) { responsePromise = Promise.resolve(value); },
    });
    if (responsePromise) await responsePromise;
    await new Promise((resolve) => setImmediate(resolve));
    return { intercepted: responsePromise !== null };
  }

  return { request, puts, fetches };
}

test('service worker never intercepts encoded private routes', async () => {
  const app = boot();
  const sensitive = [
    '/%61pi/recordings',
    '/%61udio/rec1?download=1',
    '/%6cogin',
  ];

  for (const pathname of sensitive) {
    const result = await app.request(pathname, {
      headers: { Authorization: 'Bearer secret-must-not-be-cached' },
    });
    assert.equal(result.intercepted, false, pathname);
  }
  assert.deepEqual(app.puts, []);
  assert.deepEqual(app.fetches, []);
});

test('service worker never touches private API or mutation traffic', async () => {
  const app = boot();
  // Reads of private data, a write, and the code-carrying detail route. None
  // may be intercepted: anything the worker handles can end up in Cache API,
  // where it outlives the session that was allowed to see it.
  const privateTraffic = [
    ['/api/recordings', 'GET'],
    ['/api/recordings/rec1', 'GET'],
    ['/api/recordings?limit=200', 'GET'],
    ['/api/search?q=секрет', 'GET'],
    ['/audio/rec1', 'GET'],
    ['/api/recordings/rec1', 'POST'],
    ['/api/recordings/rec1', 'DELETE'],
    ['/api/recordings/rec1', 'PATCH'],
    ['/mcp', 'POST'],
    ['/mcp', 'GET'],
  ];

  for (const [pathname, method] of privateTraffic) {
    const result = await app.request(pathname, {
      method,
      headers: { Authorization: 'Bearer secret-must-not-be-cached' },
    });
    assert.equal(result.intercepted, false, `${method} ${pathname}`);
  }
  assert.deepEqual(app.puts, []);
  assert.deepEqual(app.fetches, []);
});

test('service worker caches only exact same-origin shell resources without query strings', async () => {
  const app = boot();
  assert.equal((await app.request('/app.js')).intercepted, true);
  assert.deepEqual(app.puts, ['https://viewer.test/app.js']);

  assert.equal((await app.request('/app.js?token=secret')).intercepted, false);
  assert.equal((await app.request('/recording-page')).intercepted, false);
  assert.deepEqual(app.puts, ['https://viewer.test/app.js']);
});


test('localization release advances the installed shell cache identity', () => {
  const source = fs.readFileSync(path.join(__dirname, '../app/static/sw.js'), 'utf8');
  assert.match(source, /zapisi-shell-v24/);
});

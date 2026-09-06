#!/usr/bin/env node
// Self-test for the M34 auth-coverage predicate. Runs the real script against throwaway
// fixtures and asserts the exit code. No framework. `node check-route-auth-coverage.selftest.js`
'use strict';
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const SCRIPT = path.join(__dirname, 'check-route-auth-coverage.js');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'm34-'));
const w = (rel, body) => { const f = path.join(tmp, rel); fs.mkdirSync(path.dirname(f), { recursive: true }); fs.writeFileSync(f, body); };
const run = (args, env) => {
  const opts = { cwd: tmp, encoding: 'utf8', env: { ...process.env, ...(env || {}) } };
  try { return { code: 0, out: execFileSync(process.execPath, [SCRIPT, ...args], opts) }; }
  catch (e) { return { code: e.status, out: (e.stdout || '') + (e.stderr || '') }; }
};
let failures = 0;
const t = (name, cond, extra) => { if (cond) console.log(`  ok   ${name}`); else { failures++; console.log(`  FAIL ${name}${extra ? '\n' + extra : ''}`); } };

w('src/api/ok.ts', `router.get('/a', authenticate, h);\n`);
w('src/api/opt.ts', `router.get('/optional', optionalAuthenticate, h);\n`);
w('src/api-admin/ok.ts', `router.post('/b', requireAuth, h);\n`);
fs.mkdirSync(path.join(tmp, 'src/empty'), { recursive: true });
w('auth-exceptions.json', JSON.stringify({ 'src/api/opt.ts:GET /optional': { status: 'intentional-public', reason: 'x' } }, null, 1));

let r;
r = run(['src/api', 'auth-exceptions.json']);
t('single populated root passes', r.code === 0, r.out);

r = run(['src/does-not-exist', 'auth-exceptions.json']);
t('DEFECT 1: absent root FAILS instead of passing vacuously', r.code === 1 && /found no routes/.test(r.out), r.out);

r = run(['src/empty', 'auth-exceptions.json']);
t('DEFECT 1: existing but empty root FAILS', r.code === 1 && /found no routes/.test(r.out), r.out);

r = run(['src/api src/api-admin', 'auth-exceptions.json']);
t('DEFECT 2: space-separated roots both scanned', r.code === 0 && /src\/api-admin: 1 route/.test(r.out), r.out);

r = run(['src/api,src/api-admin', 'auth-exceptions.json']);
t('DEFECT 2: comma-separated roots also accepted', r.code === 0, r.out);

r = run(['src/api', 'src/api-admin', 'auth-exceptions.json']);
t('DEFECT 2: multiple positional roots also accepted', r.code === 0, r.out);

r = run(['src/api src/empty', 'auth-exceptions.json']);
t('DEFECT 2+1: an empty SECOND root fails a run that would otherwise pass', r.code === 1 && /found no routes under: src\/empty/.test(r.out), r.out);

fs.writeFileSync(path.join(tmp, 'auth-exceptions.json'), JSON.stringify({}, null, 1));
r = run(['src/api', 'auth-exceptions.json']);
t('DEFECT 3: optionalAuthenticate no longer counts as auth', r.code === 1 && /opt\.ts:GET \/optional/.test(r.out), r.out);

fs.writeFileSync(path.join(tmp, 'auth-exceptions.json'), JSON.stringify({
  'src/api/ok.ts:GET /a': { status: 'tracked-gap', reason: 'stale' },
  'src/api/opt.ts:GET /optional': { status: 'intentional-public', reason: 'x' },
}, null, 1));
r = run(['src/api', 'auth-exceptions.json']);
t('DEFECT 4: entry on an already-gated route FAILS', r.code === 1 && /BOTH auth middleware and an allowlist entry/.test(r.out), r.out);


// ── comment awareness ────────────────────────────────────────────────────────
w('src/cmt/a.ts', [
  "// router.get('/ghost', h);",                                  // line-commented: not a route
  "/* router.get('/block-ghost', h); */",                         // block-commented: not a route
  "/*\n router.get('/multi-ghost', h);\n*/",                      // multi-line block: not a route
  "const base = 'https://example.com/x'; router.get('/real', authenticate, h);", // `//` inside a STRING earlier on the line — the case the old heuristic got wrong
  "router.get('/live', authenticate, h);",
].join('\n') + '\n');
fs.writeFileSync(path.join(tmp, 'auth-exceptions.json'), JSON.stringify({}, null, 1));
r = run(['src/cmt', 'auth-exceptions.json']);
t('DEFECT 5: commented-out registrations are not counted as routes', r.code === 0 && /2 live route/.test(r.out), r.out);
t('DEFECT 5: a URL inside a string does not look like a comment', r.code === 0 && !/\/real/.test(r.out), r.out);
t('DEFECT 5: the ignored count is reported, not hidden', /3 commented-out registration\(s\) ignored/.test(r.out), r.out);

w('src/cmt2/b.ts', [
  "// router.post('/pair', multipart, authenticate, h);",  // the AUTHENTICATED copy, commented
  "router.post('/pair', multipart, h);",                   // the LIVE one, ungated
].join('\n') + '\n');
fs.writeFileSync(path.join(tmp, 'auth-exceptions.json'), JSON.stringify({}, null, 1));
r = run(['src/cmt2', 'auth-exceptions.json']);
t('DEFECT 5: a live ungated route is NOT masked by a commented authenticated twin', r.code === 1 && /b\.ts:POST \/pair/.test(r.out), r.out);


// ── DEFECT 6: the environment cannot decide what "authenticated" means ───────
// This is the one that let a pull request disarm the gate that judged it. On a
// pull_request event the workflow supplying `env:` is taken from the PR's own branch,
// so appending your new handler's name to the accepted-token list credited your ungated
// route while every other route kept matching as before: green, one route added.
//
// The fixture is that attack, exactly. `dumpEverything` is the handler on an ungated
// route; the poison is the real default list with that one name appended, which is what
// makes it survive every other check — nothing legitimate stops matching.
w('src/env/a.ts', "router.get('/internal/dump', dumpEverything);\n");
fs.writeFileSync(path.join(tmp, 'auth-exceptions.json'), JSON.stringify({}, null, 1));

const POISON = {
  AUTH_TOKENS: 'authenticate,requireAuth,verifyAuthToken,userAuth,verifyToken,'
    + 'authMiddleware,manageUserAuthorization,requireVerifiedUserId,verifyInternalKey,'
    + 'verifyMachineToken,dumpEverything',
};

r = run(['src/env', 'auth-exceptions.json']);
t('DEFECT 6: the ungated route fails on a clean environment', r.code === 1 && /a\.ts:GET \/internal\/dump/.test(r.out), r.out);

r = run(['src/env', 'auth-exceptions.json'], POISON);
t('DEFECT 6: AUTH_TOKENS in the environment CANNOT credit it', r.code === 1 && /a\.ts:GET \/internal\/dump/.test(r.out), r.out);

// And the inverse, so this cannot pass by the predicate simply ignoring everything:
// a genuinely gated route still passes with the same poisoned environment present.
w('src/env2/b.ts', "router.get('/fine', authenticate, h);\n");
r = run(['src/env2', 'auth-exceptions.json'], POISON);
t('DEFECT 6: a genuinely gated route still passes with the same environment set', r.code === 0, r.out);

// ── DEFECT 7: the matcher reads the REGISTRATION, not the receiver's NAME ────
// It required the receiver's identifier to contain "router", so two services registering on
// `fastify.` and `app.` were invisible. What saved them was the empty-root refusal firing on a
// directory that looked empty — a safety net catching a design flaw, not a design — and that net
// has a hole: a root that is NOT empty reports green over every tree it does not name.
w('src/recv/frameworks.ts', "router.get('/by-router', authenticate, h);\napiRouter.post('/by-api-router', authenticate, h);\napp.get('/by-app', authenticate, h);\nfastify.post('/by-fastify', authenticate, h);\nserver.put('/by-server', authenticate, h);\ninstance.delete('/by-anything', authenticate, h);\n");
r = run(['src/recv', 'auth-exceptions.json']);
t('DEFECT 7: every receiver name is matched, not just *router*', r.code === 0 && /6 live route/.test(r.out), r.out);

// The real shapes those two services use. Fastify and the Fastify-style `app` take an OPTIONS
// OBJECT between the path and the handler, and that object is where their auth lives
// (`preHandler: [authenticate]`). A rule rejecting an object as the second argument would hide
// exactly the routes this change exists to see.
w('src/opts/fastify.ts', "fastify.post('/with-empty-opts', {}, async (req, reply) => {});\napp.get('/with-prehandler', { preHandler: [authenticate] }, async (req, reply) => {});\napp.get('/ungated-with-opts', { schema: {} }, async (req, reply) => {});\n");
r = run(['src/opts', 'auth-exceptions.json']);
t('DEFECT 7: an options object between path and handler does not hide the route', r.code === 1 && /with-empty-opts/.test(r.out) && /ungated-with-opts/.test(r.out), r.out);
t('DEFECT 7: auth inside that options object still counts as gated', r.code === 1 && !/with-prehandler/.test(r.out), r.out);

// ── DEFECT 8: an outbound HTTP call is not a route ──────────────────────────
// Broadening the receiver makes every HTTP client a candidate; ten exist in the estate today.
// TWO properties separate a registration from a call and both are needed: a route path is
// RELATIVE, and a registration passes a handler AFTER the path. The awaited case needs a third.
w('src/out/clients.ts', "const a = await axios.get('https://example.com/v1/thing');\nconst b = await axios.get(`${base}/user/get/${id}`, { headers });\nconst c = await apiHelper.get(`/admin/users/${id}`);\nawait http.post('/internal/ping', { timeout: 5 });\nconst d = db.delete(userContacts).where(eq(x, y));\nrouter.get('/a-real-one', authenticate, h);\n");
r = run(['src/out', 'auth-exceptions.json']);
t('DEFECT 8: outbound HTTP clients and ORM calls are not counted as routes', r.code === 0 && /1 live route/.test(r.out), r.out);

// ── DEFECT 9: a fixture is not a route, and "test" is not a substring rule ──
// A real false alarm: 30 registrations across three services, all fixtures inside a file named
// `*.selftest.js`, indistinguishable from real routes by shape. The old filter matched `.test.`
// and missed `.selftest.`. The rule must be the CONVENTION — a dot-delimited suffix or a
// conventional directory — never a bare substring, or a real route file named for a product
// feature is silently dropped.
w('src/fx/handler.selftest.ts', "router.get('/fixture-ghost', h);\n");
w('src/fx/thing.test.ts', "router.get('/test-ghost', h);\n");
w('src/fx/__tests__/x.ts', "router.get('/dir-ghost', h);\n");
w('src/fx/test-mode/live.ts', "router.get('/genuinely-a-route', authenticate, h);\n");
w('src/fx/latest.ts', "router.get('/also-a-route', authenticate, h);\n");
r = run(['src/fx', 'auth-exceptions.json']);
t('DEFECT 9: fixtures in .selftest./.test./__tests__ are not counted', r.code === 0 && !/ghost/.test(r.out), r.out);
t('DEFECT 9: a real route file whose NAME merely contains "test" is still scanned', /2 live route/.test(r.out), r.out);

// ── DEFECT 10: an EMPTY path is a route — it serves the router's own mount point ──
// Caught by diffing all eleven measured services old-vs-new before merging: requiring the path to
// start with "/" silently dropped one real, gated route — `routerApi.get('', authMiddleware, h)`,
// which serves the path its router is mounted at. A discriminator that is too STRICT is a silent
// under-count, the same class of failure as one that is too loose.
w('src/mnt/index.ts', "routerApi.get('', authMiddleware, (req, res) => {});\nrouterApi.get('/child', authMiddleware, (req, res) => {});\n");
r = run(['src/mnt', 'auth-exceptions.json']);
t('DEFECT 10: an empty path is a route (the router mount point), not skipped', r.code === 0 && /2 live route/.test(r.out), r.out);


// ── DEFECT 11: a fire-and-forget client call IS counted, and that is the safe direction ──
// The residue of the three discriminators: `httpClient.post('/x', payload);` is relative, passes a
// second argument, and does not consume its result — indistinguishable BY SHAPE from a registration
// without a parser. It is counted, so it appears as an ungated route and costs an allowlist entry
// with a written reason. That is the direction to fail in: an over-count is loud and one person
// pays for it once, an under-count is silent and nobody pays until an incident. Asserted rather
// than argued, so the next edit to CONSUMED moves it in front of a red test instead of quietly.
w('src/ff/notify.ts', "httpClient.post('/webhooks/notify', payload);\nqueue.publish('/topic/x', msg);\nrouter.get('/real', authenticate, h);\n");
r = run(['src/ff', 'auth-exceptions.json']);
t('DEFECT 11: an unconsumed client call is over-counted, not silently dropped', r.code === 1 && /\/webhooks\/notify/.test(r.out), r.out);
t('DEFECT 11: a non-verb method (queue.publish) is not a route', !/\/topic\/x/.test(r.out), r.out);

// ── DEFECT 12: .use() is excluded — the behaviour, not the paragraph about it ──
// Two shapes, one exclusion. Mounting a sub-router double-counts leaves this walk already counts
// from the router's own file. An inline-handler .use() is a reachable CENSUS surface but not a
// GATE surface: no verb, so "GET /x is ungated" cannot be said about it and no allowlist key can
// name it. That is a stated ceiling of this gate — an app whose endpoints live on .use() is
// invisible to it, and the entry file's mounts are what catch that, not this regex.
w('src/mount/app.ts', "app.use('/api', apiRouter);\napp.use('/inline', (req, res, next) => next());\nrouter.get('/counted', authenticate, h);\n");
r = run(['src/mount', 'auth-exceptions.json']);
t('DEFECT 12: .use() is not counted, for either mounting or an inline handler', r.code === 0 && /1 live route/.test(r.out), r.out);


// ── DEFECT 13: a commented-out middleware is not a middleware ────────────────
// Found in the field, not invented. Two live registrations on the largest social surface in the
// estate read `// verifyAuthToken,` inside their argument list, and the gate scored both as GATED
// because it tested the RAW argument text. A missed route is a hole you can see; a route reported
// as PROTECTED while serving anonymous traffic is a hole that closes the investigation.
w('src/ghost/routes.ts', "router.post('/looks-gated',\n  multipart,\n  // authenticate,\n  handler\n);\nrouter.post('/really-gated',\n  multipart,\n  authenticate,\n  handler\n);\n");
fs.writeFileSync(path.join(tmp, 'auth-exceptions.json'), JSON.stringify({}, null, 1));
r = run(['src/ghost', 'auth-exceptions.json']);
t('DEFECT 13: auth commented out INSIDE the registration does not count as gated', r.code === 1 && /looks-gated/.test(r.out), r.out);
t('DEFECT 13: the identical registration with it live is still gated', !/really-gated/.test(r.out), r.out);
t('DEFECT 13: both multi-line registrations were counted at all', /2 route/.test(r.out), r.out);


fs.rmSync(tmp, { recursive: true, force: true });
console.log(failures ? `\n${failures} self-test failure(s)` : '\nall self-tests passed');
process.exit(failures ? 1 : 0);

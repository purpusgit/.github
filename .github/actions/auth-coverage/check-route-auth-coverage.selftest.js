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

fs.rmSync(tmp, { recursive: true, force: true });
console.log(failures ? `\n${failures} self-test failure(s)` : '\nall self-tests passed');
process.exit(failures ? 1 : 0);

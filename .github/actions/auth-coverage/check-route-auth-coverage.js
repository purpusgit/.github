#!/usr/bin/env node
// M34 gate: every route must be gated by a verified-identity check, or listed in
// auth-exceptions.json with a reason. Regex-based, not a real parser — ceiling: a route
// registration split unusually across lines, or built from a dynamic path variable rather
// than a string literal, could slip past undetected. It also cannot see that a registration
// is commented out, and cannot see the mount graph, so a dead registration still counts.
// Upgrade to an AST-based check (e.g. ts-morph) if any of that proves to be a real gap.
// Only checks .get/.post/.put/.patch/.delete — .use() stays excluded, and now that the
// matcher accepts ANY receiver that reason needs stating rather than assuming. A .use() is
// one of two things. Mounting a sub-router (app.use(path, someRouter)) is not a route: its
// leaves are counted when this walks that router's own file, so counting the mount too
// double-counts every one of them. A .use() carrying an inline handler IS a reachable
// surface, but it is a CENSUS surface, not a GATE surface — it has no verb, so "GET /x is
// ungated" cannot be said about it and no allowlist key can name it. Excluding it is a
// stated ceiling of this gate, not an oversight; an app whose real endpoints live on .use()
// is invisible to it, and that is the case to catch by reading the entry file's mounts.
//
// FOUR WAYS THIS GATE USED TO REPORT GREEN WITHOUT MEANING IT, all now refused:
//   1. An absent or empty root scanned nothing and passed. A repo whose routes live in
//      src/routes rather than src/api got a green tick over an empty directory.
//   2. A repo with a second API surface had it silently unscanned, because only one root
//      was ever passed. Roots are now a list, and every one of them must yield routes.
//   3. `optionalAuthenticate` counted as a verified identity. A route on optional auth is
//      reachable WITHOUT credentials — that is what the word means — so it passed exactly
//      the routes this gate exists to catch. It is no longer an auth token; a deliberately
//      public route belongs in auth-exceptions.json with a reason.
//   4. The accepted-token list came from the ENVIRONMENT, and on a pull request the
//      workflow that sets the environment comes from the pull request's own branch. The
//      diff being judged chose the definition of "authenticated" it was judged by. The
//      list is now a constant in this file; see the note above it.
'use strict';
const fs = require('fs');
const path = require('path');

// Roots: one or many. Space- or comma-separated in argv[2], and/or extra positional args
// before the allowlist. `node check.js "src/api src/api-admin" auth-exceptions.json`.
const argv = process.argv.slice(2);
let ALLOWLIST_PATH = 'auth-exceptions.json';
if (argv.length > 1 && /auth-exceptions|\.json$/.test(argv[argv.length - 1])) ALLOWLIST_PATH = argv.pop();
const ROOTS = (argv.join(' ') || 'src/api').split(/[\s,]+/).map(s => s.trim()).filter(Boolean);

// ⛔ THIS LIST IS A CONSTANT. It used to read `process.env.AUTH_TOKENS ||` first, and that
// one `||` was the whole gate's undoing: on a pull_request the workflow supplying the
// environment is taken from the PULL REQUEST'S OWN BRANCH, so two lines of `env:` in the
// same diff decided what "authenticated" means for the diff being judged. Appending your
// new handler's name to the default list credits your ungated route and leaves every other
// route matching as before — green, one route added, nothing else disturbed. The self-test
// did not catch it either: it runs as another step of the same job, inherits the same
// environment, and its cases still pass because the default names are all still present.
//
// So the predicate no longer takes ANY input from the environment. What a service is
// allowed to vary — which directories to scan — it declares in a file this action reads
// from the BASE branch (see action.yml). What it must not vary is here, in the shared
// repository, where the branch under review cannot reach it.
//
// `manageUserAuthorization` is service_auth's own middleware and is a real verified-identity
// check, not a name that looks like one: it validates the Authorization header, verifies the
// token, rejects an expired one, and confirms the session row still exists before calling
// next() — 401 on every failure path. Added on that reading, not on the strength of its name.
// Its sibling `deviceMiddleware`, which sits at six registrations in the same service, is
// deliberately NOT here: it carries device metadata and checks no identity.
//
// Adding a name here is a change to this repository, reviewable on its own, by whoever owns
// this tree — which is the point. Do not reintroduce an environment or input override.
const AUTH_TOKENS = [
  'authenticate',
  'requireAuth',
  'verifyAuthToken',
  'userAuth',
  'verifyToken',
  'authMiddleware',
  'manageUserAuthorization',
  'requireVerifiedUserId',
  'verifyInternalKey',
  'verifyMachineToken',
];

function walk(dir, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    // Normalized to forward slashes here (not just at match time) — path.join uses the
    // platform separator, but every allowlist key in auth-exceptions.json is forward-slash.
    const p = path.join(dir, entry.name).split(path.sep).join('/');
    // Fixture exclusion is by CONVENTION — a dot-delimited suffix or a conventional
    // directory — never a bare substring. `.selftest.` was being missed while a real route
    // file named `latest.ts` or living under `test-mode/` must stay scanned.
    if (entry.isDirectory()) {
      if (!/^(__tests__|__mocks__|node_modules)$/.test(entry.name)) walk(p, out);
    } else if (/\.(ts|js)$/.test(entry.name)
               && !/\.(test|spec|selftest|selfcheck|check)\.(ts|js)$/.test(entry.name)) out.push(p);
  }
  return out;
}

let allowlist = {};
if (fs.existsSync(ALLOWLIST_PATH)) allowlist = JSON.parse(fs.readFileSync(ALLOWLIST_PATH, 'utf8'));


// Comment and string awareness. The regex alone cannot tell a live registration from one that is
// commented out, and that blindness has produced three wrong answers on this fleet — phantom routes
// counted in two services, and worst, a pair of live routes whose auth had been REMOVED being filed
// as dead code because a commented-out copy carrying the middleware matched first. A single pass
// marks every index that is inside a line comment or a block comment; a match that starts on a
// marked index is not a route. String and template literals are tracked too, so a `//` inside a URL
// ("https://…") no longer looks like the start of a comment — the failure a simpler
// is-there-a-slash-slash-earlier-on-this-line check makes.
// Known ceiling: a regex literal is not tracked. A route registration cannot begin inside one, and
// no `/*`-opening regex exists in this fleet's route files; if that changes, this needs a real lexer.
function commentMask(src) {
  const masked = new Uint8Array(src.length); // 1 = inside a comment
  let i = 0, state = 'code';
  while (i < src.length) {
    const c = src[i], d = src[i + 1];
    if (state === 'code') {
      if (c === '/' && d === '/') { state = 'line'; masked[i] = masked[i + 1] = 1; i += 2; continue; }
      if (c === '/' && d === '*') { state = 'block'; masked[i] = masked[i + 1] = 1; i += 2; continue; }
      if (c === "'" || c === '"' || c === '`') { state = c; i++; continue; }
      i++; continue;
    }
    if (state === 'line') { masked[i] = 1; if (c === '\n') state = 'code'; i++; continue; }
    if (state === 'block') { masked[i] = 1; if (c === '*' && d === '/') { masked[i + 1] = 1; i += 2; state = 'code'; continue; } i++; continue; }
    if (c === '\\') { i += 2; continue; }          // escape inside a string
    if (c === state) { state = 'code'; i++; continue; } // closing quote
    i++;
  }
  return masked;
}

// Matches: <any receiver>.<verb>(  '<path>'  , ...rest-of-args...  ) ;
// The receiver used to have to contain "router". That made the gate blind to two whole
// services registering on `fastify.` and `app.` — and what hid it was the empty-root refusal
// firing, i.e. a safety net catching a design flaw. The receiver is now unconstrained, so
// three discriminators below do the work the name used to do badly.
const ROUTE_RE = /\b([A-Za-z_$][\w$]*)\s*\.\s*(get|post|put|patch|delete)\s*\(\s*(['"`])((?:\\.|(?!\3).)*)\3([\s\S]*?)\)\s*;/g;

// An outbound HTTP client call is the shape a broadened receiver picks up by accident
// (axios.get, apiHelper.get, http.post) and so is an ORM verb (db.delete(t).where(...)).
// Three properties separate a registration from a call, and all three are needed:
const RELATIVE_PATH   = /^(\/|\*|$)/;                 // a route path is relative — and '' is a
                                                      // real route: the router's own mount point
const HAS_FURTHER_ARG = /^\s*,/;                      // a registration passes a handler AFTER the path
const CONSUMED        = /(await|=|\?\?|\|\||&&)\s*$/; // a client call's return value is consumed

const violations = [];
const redundant = [];
const emptyRoots = [];
let checked = 0;
let commented = 0;

for (const ROOT of ROOTS) {
  let here = 0;
  for (const file of walk(ROOT)) {
    const src = fs.readFileSync(file, 'utf8');
    const masked = commentMask(src);
    ROUTE_RE.lastIndex = 0;
    let m;
    while ((m = ROUTE_RE.exec(src))) {
      if (masked[m.index]) { commented++; continue; }   // a commented-out registration is not a route
      const [, , verb, , routePath, rest] = m;
      if (!RELATIVE_PATH.test(routePath)) continue;                            // an absolute URL is a client call
      if (!HAS_FURTHER_ARG.test(rest)) continue;                               // no handler follows: not a registration
      if (CONSUMED.test(src.slice(Math.max(0, m.index - 40), m.index))) continue; // its value is consumed: a call
      checked++; here++;
      const hasAuthToken = AUTH_TOKENS.some(t => rest.includes(t));
      const bareKey = `${verb.toUpperCase()} ${routePath}`;
      const fileKey = `${file}:${bareKey}`;
      const allowed = allowlist[fileKey] || allowlist[bareKey];
      if (!hasAuthToken && !allowed) violations.push(fileKey);
      // An allowlist entry on an already-gated route is load-bearing the moment the
      // middleware goes: delete `authenticate` and the gate stays green because the entry
      // still says the route may be open. It reads as documentation and behaves as a back
      // door, so it is a failure, not a note.
      if (hasAuthToken && allowed) redundant.push(fileKey);
    }
  }
  console.log(`  ${ROOT}: ${here} route(s)`);
  if (here === 0) emptyRoots.push(ROOT);
}

let failed = false;
if (emptyRoots.length) {
  failed = true;
  console.error(`\nM34 auth-coverage gate FAILED — found no routes under: ${emptyRoots.join(', ')}`);
  console.error('Refusing to pass vacuously. Either the root is wrong for this repo (routes often live in');
  console.error('src/routes or routes/, not src/api), or the directory is empty. A gate that scans nothing');
  console.error('and reports green is worse than no gate — it is a green tick over an unexamined surface.\n');
}
if (violations.length) {
  failed = true;
  console.error(`\nM34 auth-coverage gate FAILED — ${violations.length} of ${checked} route(s) checked have no verified-identity middleware and no allowlist entry:\n`);
  for (const v of violations) console.error(`  ${v}`);
  console.error(`\nEither add the correct auth middleware, or add an entry to ${ALLOWLIST_PATH}, e.g.:\n  "POST /some/route": { "status": "intentional-public" | "tracked-gap", "reason": "..." }\n`);
}
if (redundant.length) {
  failed = true;
  console.error(`\nM34 auth-coverage gate FAILED — ${redundant.length} route(s) have BOTH auth middleware and an allowlist entry:\n`);
  for (const v of redundant) console.error(`  ${v}`);
  console.error(`\nThese entries pre-authorise a regression: remove the middleware and the gate still passes,`);
  console.error(`because the entry says the route may be open. Delete them from ${ALLOWLIST_PATH} — a route`);
  console.error(`that is gated needs no exception. (If the entry exists only because a commented-out copy of`);
  console.error(`the registration also matches, delete the commented line instead.)\n`);
}
if (failed) process.exit(1);
console.log(`M34 auth-coverage gate passed — ${checked} live route(s) checked across ${ROOTS.length} root(s), all gated or explicitly allowlisted` + (commented ? `; ${commented} commented-out registration(s) ignored.` : '.'));

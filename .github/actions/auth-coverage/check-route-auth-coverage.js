#!/usr/bin/env node
// M34 gate: every route must be gated by a verified-identity check, or listed in
// auth-exceptions.json with a reason. Regex-based, not a real parser — ceiling: a route
// registration split unusually across lines, or built from a dynamic path variable rather
// than a string literal, could slip past undetected. Upgrade to an AST-based check
// (e.g. ts-morph) if that ever proves to be a real gap in practice.
// Only checks .get/.post/.put/.patch/.delete — .use() is deliberately excluded. .use() in
// this fleet is exclusively sub-router mounting (app.use(path, someRouter)) or global
// middleware, never a route handler on its own; the leaf routes inside a mounted router are
// already checked individually when this walks that router's own file. Including .use() just
// produced one duplicate false-positive per mount point with no additional coverage.
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2] || 'src/api';
const ALLOWLIST_PATH = process.argv[3] || 'auth-exceptions.json';
const AUTH_TOKENS = (process.env.AUTH_TOKENS ||
  'authenticate,optionalAuthenticate,requireAuth,verifyAuthToken,userAuth,verifyToken,authMiddleware,requireVerifiedUserId,verifyInternalKey,verifyMachineToken'
).split(',').map(s => s.trim()).filter(Boolean);

function walk(dir, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    // Normalized to forward slashes here (not just at match time) — path.join uses the
    // platform separator, but every allowlist key in auth-exceptions.json is forward-slash.
    // fs.readdirSync/existsSync both accept forward slashes on win32, so this is safe to
    // recurse on directly rather than needing a separate display-only conversion.
    const p = path.join(dir, entry.name).split(path.sep).join('/');
    if (entry.isDirectory()) walk(p, out);
    else if (/\.(ts|js)$/.test(entry.name) && !/\.(test|spec|selfcheck|check)\.(ts|js)$/.test(entry.name)) out.push(p);
  }
  return out;
}

let allowlist = {};
if (fs.existsSync(ALLOWLIST_PATH)) allowlist = JSON.parse(fs.readFileSync(ALLOWLIST_PATH, 'utf8'));

// Matches: <anything with "router" in it>.<verb>(  '<path>'  , ...rest-of-args...  ) ;
const ROUTE_RE = /\b[\w$]*[Rr]outer\w*\s*\.\s*(get|post|put|patch|delete)\s*\(\s*(['"`])((?:\\.|(?!\2).)*)\2([\s\S]*?)\)\s*;/g;

let violations = [];
let checked = 0;
for (const file of walk(ROOT)) {
  const src = fs.readFileSync(file, 'utf8');
  let m;
  while ((m = ROUTE_RE.exec(src))) {
    const [, verb, , routePath, rest] = m;
    checked++;
    const hasAuthToken = AUTH_TOKENS.some(t => rest.includes(t));
    const bareKey = `${verb.toUpperCase()} ${routePath}`;
    const fileKey = `${file}:${bareKey}`;
    const allowed = allowlist[fileKey] || allowlist[bareKey];
    if (!hasAuthToken && !allowed) {
      violations.push(fileKey);
    }
  }
}

if (violations.length) {
  console.error(`\nM34 auth-coverage gate FAILED — ${violations.length} of ${checked} route(s) checked have no verified-identity middleware and no allowlist entry:\n`);
  for (const v of violations) console.error(`  ${v}`);
  console.error(`\nEither add the correct auth middleware, or add an entry to ${ALLOWLIST_PATH}, e.g.:\n  "POST /some/route": { "status": "intentional-public" | "tracked-gap", "reason": "..." }\n`);
  process.exit(1);
}
console.log(`M34 auth-coverage gate passed — ${checked} route(s) checked, all gated or explicitly allowlisted.`);

// Fixture for the M34 auth-coverage gate. Registration shape copied from the real
// surface it guards — service_orbit_orgs/src/api/index.ts, where all 362 matched
// registrations read `routerApi.<verb>('<path>', (req, res) => { ... });`.
import { Router } from 'express';
import { authenticate } from '../middleware/auth';

const routerApi = Router();

// Legal shape 1 — gated by a verified-identity middleware.
routerApi.get('/organizations/:identifier', authenticate, (req, res) => {
  const controller = new OrgController(req, res);
  controller.get();
});

// Legal shape 2 — ungated, but carrying an explicit auth-exceptions.json entry.
routerApi.post('/searchUsersByFullName', (req, res) => {
  const controller = new OrgSearchController(req, res);
  controller.search();
});

// THE PLANTED VIOLATION — a new route added with no auth middleware and no
// auth-exceptions.json entry. This is how the defect actually arrives: a PR adds
// a handler and nobody notices the missing middleware.
routerApi.patch("/organizations/org-dimension", (req, res) => {
  const validator = new OrgDimensionValidator(req, res);
  validator.apply();
});

export default routerApi;

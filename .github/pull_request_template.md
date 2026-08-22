## What this changes

<!-- One or two sentences. What behaviour is different after this merges? -->

## Spec

<!-- REQUIRED if this PR came from a spec. Write "none" if it did not. -->

Spec: `<exact filename>`

- [ ] I wrote this PR's number and URL into that spec, in the same session I raised it
- [ ] I moved the spec to its Completed folder — the moment the PR was RAISED, not at merge
- [ ] Before executing, I re-verified the spec's factual claims against live code, and any
      claim that had gone stale is corrected in the spec or noted here

<!-- A spec that has been executed but does not say so is indistinguishable from one nobody
     has started, and the next session redoes the work. That has now happened three times. -->

## Verification

<!-- REQUIRED. Paste the command you ran and its ACTUAL output, not a summary.
     "CI is green" is not verification — CI runs automatically on every push and
     the reviewer can see it. This section is for what YOU checked that CI cannot. -->

```
# command:
# output:
```

- [ ] All checks green on this PR — CI runs on open and on every push, so check before requesting review
- [ ] Branch is up to date with the base — required on every repo; a stale-green PR cannot merge
- [ ] I ran the relevant gate locally and it exited 0

## Self-critique

<!-- REQUIRED. What is weakest about this change? What did you NOT verify?
     What would you check first if it broke in production?
     An empty or dismissive answer here is the best single predictor of a defect. -->

---

<!-- Delete the sections below that do not apply. -->

<details><summary><b>Content, copy or translation changes</b></summary>

- [ ] I ran `git log -S'<the value>' --all` before changing any existing value — if it had a prior value I am restoring it, not authoring a replacement
- [ ] I read related keys as a FAMILY (all weekdays, all months, all units), not one at a time — every defect found so far was invisible key-by-key and obvious family-by-family
- [ ] Brand names, technical tokens and transliterations left as-is

</details>

<details><summary><b>Schema or migration changes</b></summary>

- [ ] Migration number claimed from a LIVE listing of the migrations folder at dispatch time, not from a plan written earlier
- [ ] Every table and column named here was confirmed against the live schema, not assumed
- [ ] Rollback path stated

</details>

<details><summary><b>Shared package changes</b></summary>

- [ ] No export removed from a barrel file and no method removed from a factory class — "unused in this repo" never means unused
- [ ] Consumers of any changed public API identified

</details>

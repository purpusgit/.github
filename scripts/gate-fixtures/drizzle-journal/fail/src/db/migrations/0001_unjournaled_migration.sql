-- Written, committed, and (in the real incident) applied to a live database by
-- hand — but never added to meta/_journal.json, so `drizzle-kit migrate` cannot
-- see it. This is the service_ai_v_call 0011/0013 failure mode the gate exists
-- to catch. Do not add a journal entry for this file; it is the violation.
ALTER TABLE "calls" ADD COLUMN "unjournaled_col" text;

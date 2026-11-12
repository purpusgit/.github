#!/usr/bin/env python3
"""Self-test for scripts/declaration_gate.py (spec: specs offline-first/declaration-ci-check.md).

Every case builds a small git repo (a base commit, then a PR commit), runs the gate on it and reads the findings.
Part 1 replays the four PRs of spec section 8. Part 2 is one test per rule. Part 3 is the extractors, one per stack.
Part 4 is the modes and the output. Part 5 proves each rule's test fails when the rule is broken: it switches the
rule off (or swaps a broken DC5 matcher in) and requires the rule's own test to go red.

Run: python3 scripts/test_declaration_gate.py
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import declaration_gate as G  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────────────
def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def make_repo(base, head):
    """base, head: {path: text or None}. Returns the repo dir with a `main` base commit and the PR checked out."""
    d = tempfile.mkdtemp(prefix="declgate-")
    sh(d, "git", "init", "-q", "-b", "main")
    sh(d, "git", "config", "user.email", "t@example.com"); sh(d, "git", "config", "user.name", "t")

    def write(files):
        for p, text in files.items():
            fp = os.path.join(d, p)
            if text is None:
                if os.path.exists(fp):
                    os.remove(fp)
                continue
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w") as fh:
                fh.write(text)
    write(base); sh(d, "git", "add", "-A"); sh(d, "git", "commit", "-q", "--allow-empty", "-m", "base")
    sh(d, "git", "checkout", "-q", "-b", "pr")
    write(head); sh(d, "git", "add", "-A"); sh(d, "git", "commit", "-q", "--allow-empty", "-m", "pr")
    return d


def data(tables=(), schema_paths=(("migrations/*.sql", "sql"),), default_schema="svc", **extra):
    cfg = {"version": 1, "default_schema": default_schema,
           "schema_paths": [{"glob": g, "kind": k} for g, k in schema_paths], "tables": list(tables)}
    cfg.update(extra)
    return yaml.safe_dump(cfg, sort_keys=False)


def entry(table, fields, **over):
    e = {"table": table, "scope_class": "org", "placement": "personal_or_content", "placement_key": "org_idfr",
         "meaning": "A test table.", "fields": {f: f"The {f}" for f in fields}, "phone_fields": [], "secret_like": {}}
    e.update(over)
    return e


def run(base, head, mode="report", owners=None):
    d = make_repo(base, head)
    own = None
    if owners is not None:
        own = os.path.join(d, ".owners.yaml")
        with open(own, "w") as fh:
            fh.write(yaml.safe_dump(owners))
    return G.judge(d, "main", mode, own)


def rules(rep, level=None):
    return sorted((f["rule"], f["table"]) for f in rep.findings if level in (None, f["level"]))


def codes(rep):
    return sorted(f["rule"] for f in rep.findings)


CREATE_T = "CREATE TABLE svc.t (\n  id INT NOT NULL,\n  org_idfr INT NULL,\n  name VARCHAR(10)\n);\n"
T_ENTRY = entry("svc.t", ["id", "org_idfr", "name"])


# ── part 1: the four replayed PRs (spec section 8) ───────────────────────────────────────────────────────
ORGS_SQL = """-- Migration: meta.search_query_log, one row per search (specs enterprise-search D8)
-- New table only: no existing row changes. The TypeScript runner is the way to run this.

CREATE TABLE IF NOT EXISTS meta.search_query_log (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    entity VARCHAR(16) NOT NULL,
    took_ms INT NOT NULL,
    q_script ENUM('latin', 'devanagari', 'other_indic', 'mixed', 'other') NOT NULL,
    q_text VARCHAR(100) NULL,
    org_idfr INT NULL,
    outcome ENUM('ok', 'timeout', 'unavailable') NOT NULL DEFAULT 'ok',
    KEY idx_search_query_log_entity_created (entity, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""
ORGS_TS = "export const CREATE_SEARCH_QUERY_LOG = `CREATE TABLE IF NOT EXISTS meta.search_query_log (\n" + \
    ORGS_SQL.split("(\n", 1)[1].rsplit(") ENGINE", 1)[0] + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;`;\n"
ORGS_COLS = ["id", "created_at", "entity", "took_ms", "q_script", "q_text", "org_idfr", "outcome"]
ORGS_BASE = {"declarations/data.yaml": data(schema_paths=(("src/migrations/*.sql", "sql"), ("src/migrations/*.ts", "tsdl")), default_schema="meta"),
             "src/migrations/001_base.sql": "CREATE TABLE meta.organization (id INT, org_idfr INT);\n"}
ORGS_HEAD = {"src/migrations/create_search_query_log.sql": ORGS_SQL, "src/migrations/create_search_query_log.ts": ORGS_TS}

SOCIAL_FILES = {
    "orbit-social/utils.js": "const MongoCollections = {\n  posts: 'posts',\n  post_comments: 'post_comments',\n};\nmodule.exports = { MongoCollections };\n",
    "models/posts.js": "const mongoose = require('mongoose');\nconst { MongoCollections } = require('../orbit-social/utils');\n"
                       "const PostSchema = require('../orbit-social/models/schema/posts_collection.schema');\n"
                       "module.exports = mongoose.model(MongoCollections.posts, PostSchema);\n",
}
POSTS_BASE_SCHEMA = "const { Schema } = require('mongoose');\nconst PostSchema = new Schema(\n  {\n    owner_idfr: {\n      type: String,\n      required: true,\n    },\n"\
                    "    org_identifier: {\n      type: String,\n    },\n  },\n  { timestamps: true }\n);\nmodule.exports = PostSchema;\n"
POSTS_HEAD_SCHEMA = POSTS_BASE_SCHEMA.replace("    org_identifier:", "    identifier: {\n      type: String,\n    },\n    org_identifier:")
SOCIAL_CFG = dict(schema_paths=(("orbit-social/models/schema/**/*.js", "mongoose"), ("models/*.js", "mongoose"),
                                ("orbit-social/utils.js", "mongoose")), default_schema="service_mongo_social")

CHAT_001 = "CREATE TABLE belongity_chat.thread_member (\n  thread_id BIGINT NOT NULL,\n  user_idfr INT NOT NULL,\n  org_identifier INT NULL\n);\n"
CHAT_038 = """-- 038_thread_member_cleared_at.sql (specs#849 §10, Delete chat): when this member
-- cleared the thread for themselves. Add-only: nullable, no default, no backfill.
DROP PROCEDURE IF EXISTS _migration_038;

CREATE PROCEDURE _migration_038()
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'thread_member' AND column_name = 'cleared_at'
  ) THEN
    ALTER TABLE belongity_chat.thread_member ADD COLUMN cleared_at DATETIME NULL;
  END IF;
END;

CALL _migration_038();
DROP PROCEDURE _migration_038;
"""

VR_001 = "CREATE TABLE goalcaller.goal (\n  id INT NOT NULL,\n  org_idfr INT NULL\n);\n"
VR_022 = "-- 022: provenance columns\nALTER TABLE contact_graph.contact ADD COLUMN provenance VARCHAR(20) NULL;\n" \
         "ALTER TABLE goalcaller.goal ADD COLUMN provenance VARCHAR(20) NULL;\n"


class ReplayedPRs(unittest.TestCase):
    def test_orgs_1668_new_table_without_entry_is_dc1_once(self):
        rep = run(ORGS_BASE, ORGS_HEAD)
        self.assertEqual(rules(rep), [("DC1", "meta.search_query_log")])  # the .ts twin names the same table: no DC9
        self.assertEqual(rep.touched["meta.search_query_log"], ["create"])

    def test_orgs_1668_with_a_complete_entry_is_clean(self):
        head = dict(ORGS_HEAD, **{"declarations/data.yaml": data([entry("meta.search_query_log", ORGS_COLS, scope_class="excluded")],
                                  schema_paths=(("src/migrations/*.sql", "sql"), ("src/migrations/*.ts", "tsdl")), default_schema="meta")})
        rep = run(ORGS_BASE, head)
        self.assertEqual([f for f in rep.findings if f["rule"] != "DC8"], [])

    def test_social_368_changed_collection_without_entry_is_dc1(self):
        base = dict(SOCIAL_FILES, **{"declarations/data.yaml": data(**SOCIAL_CFG), "orbit-social/models/schema/posts_collection.schema.js": POSTS_BASE_SCHEMA})
        head = {"orbit-social/models/schema/posts_collection.schema.js": POSTS_HEAD_SCHEMA}
        rep = run(base, head)
        self.assertEqual(rules(rep), [("DC1", "service_mongo_social.posts")])

    def test_social_368_entry_missing_the_new_field_is_dc6(self):
        old = entry("service_mongo_social.posts", ["owner_idfr", "org_identifier"], placement_key="org_identifier")
        base = dict(SOCIAL_FILES, **{"declarations/data.yaml": data([old], **SOCIAL_CFG), "orbit-social/models/schema/posts_collection.schema.js": POSTS_BASE_SCHEMA})
        rep = run(base, {"orbit-social/models/schema/posts_collection.schema.js": POSTS_HEAD_SCHEMA})
        self.assertEqual(rules(rep), [("DC6", "service_mongo_social.posts")])
        self.assertIn("identifier", rep.findings[0]["msg"])
        new = entry("service_mongo_social.posts", ["owner_idfr", "org_identifier", "identifier"], placement_key="org_identifier")
        rep = run(base, {"orbit-social/models/schema/posts_collection.schema.js": POSTS_HEAD_SCHEMA,
                         "declarations/data.yaml": data([new], **SOCIAL_CFG)})
        self.assertEqual(rules(rep), [])

    def test_chat_231_alter_inside_a_stored_procedure_is_dc1(self):
        base = {"declarations/data.yaml": data(default_schema="belongity_chat"), "migrations/001.sql": CHAT_001}
        rep = run(base, {"migrations/038_thread_member_cleared_at.sql": CHAT_038})
        self.assertEqual(rules(rep), [("DC1", "belongity_chat.thread_member")])

    def test_chat_231_complete_entry_with_the_new_column_is_clean(self):
        e = entry("belongity_chat.thread_member", ["thread_id", "user_idfr", "org_identifier", "cleared_at"], placement_key="org_identifier")
        base = {"declarations/data.yaml": data([e], default_schema="belongity_chat"), "migrations/001.sql": CHAT_001}
        self.assertEqual(rules(run(base, {"migrations/038_thread_member_cleared_at.sql": CHAT_038})), [])
        e["fields"].pop("cleared_at")
        base["declarations/data.yaml"] = data([e], default_schema="belongity_chat")
        self.assertEqual(rules(run(base, {"migrations/038_thread_member_cleared_at.sql": CHAT_038})), [("DC6", "belongity_chat.thread_member")])

    def test_voicereach_85_foreign_table_is_dc7_and_own_table_is_dc1(self):
        base = {"declarations/data.yaml": data(default_schema="goalcaller"), "migrations/001.sql": VR_001}
        rep = run(base, {"migrations/022_provenance.sql": VR_022})
        self.assertEqual(rules(rep), [("DC1", "goalcaller.goal"), ("DC7", "contact_graph.contact")])

    def test_voicereach_85_listing_the_foreign_change_satisfies_dc7_a(self):
        cfg = data([], default_schema="goalcaller", foreign_changes=[{"table": "contact_graph.contact", "owner": "service_goalcaller_contact_graph", "reason": "provenance"}])
        base = {"declarations/data.yaml": data(default_schema="goalcaller"), "migrations/001.sql": VR_001}
        rep = run(base, {"migrations/022_provenance.sql": VR_022, "declarations/data.yaml": cfg})
        self.assertNotIn(("DC7", "contact_graph.contact"), rules(rep))
        self.assertEqual(rep.foreign[0]["cols"], ["provenance"])  # handed to the owner in the summary



# ── part 2: one check per rule. Each is a plain function so part 5 can re-run it with the rule broken ───────
BASE_T = {"declarations/data.yaml": data(), "migrations/001.sql": CREATE_T}


def with_entry(e, **extra):
    return {"declarations/data.yaml": data([e], **extra)}


def check_DC1(tc):
    tc.assertEqual(rules(run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T})), [("DC1", "svc.t")])
    tc.assertEqual(rules(run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T, **with_entry(T_ENTRY)})), [])
    # an existing table nobody touches is never judged (no backfill), only listed
    rep = run(BASE_T, {"migrations/002.sql": "CREATE TABLE svc.u (id INT);\n", **with_entry(entry("svc.u", ["id"], placement_key="central"))})
    tc.assertEqual(rules(rep), [])
    tc.assertEqual(rep.inventory, ["svc.t"])


def check_DC2(tc):
    bad = entry("svc.t", ["id", "org_idfr", "name"], scope_class="everyone", placement="elsewhere", placement_key="", meaning="  ")
    rep = run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T, **with_entry(bad)})
    tc.assertEqual([f["rule"] for f in rep.findings if f["rule"] == "DC2"], ["DC2"] * 4)
    ex = entry("svc.t", ["id", "org_idfr", "name"], scope_class="excluded")  # excluded still needs a placement (D4)
    tc.assertEqual(rules(run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T, **with_entry(ex)})), [])
    ex["placement"] = None
    tc.assertEqual(rules(run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T, **with_entry(ex)})), [("DC2", "svc.t")])


def check_DC3(tc):
    rep = run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T, **with_entry(dict(T_ENTRY, placement_key="not_a_column"))})
    tc.assertEqual(rules(rep), [("DC3", "svc.t")])
    tc.assertEqual(rep.findings[0]["level"], "error")
    for key in ("central", "ORG_IDFR"):
        tc.assertEqual(rules(run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T, **with_entry(dict(T_ENTRY, placement_key=key))})), [])
    # no CREATE in the repo: only the columns its ALTERs name are known, so the rest is a warning, not an error
    base = {"declarations/data.yaml": data([entry("svc.u", ["a"], placement_key="zzz")])}
    rep = run(base, {"migrations/002.sql": "ALTER TABLE svc.u ADD COLUMN a INT;\n"})
    tc.assertEqual([(f["rule"], f["level"]) for f in rep.findings], [("DC3", "warning")])


def check_DC4(tc):
    rep = run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T, **with_entry(dict(T_ENTRY, phone_fields=["ghost"]))})
    tc.assertEqual(rules(rep), [("DC4", "svc.t")])
    e = entry("svc.t", ["id", "org_idfr", "name", "no_such_column"])
    rep = run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T, **with_entry(e)})
    tc.assertEqual(rules(rep), [("DC4", "svc.t")])
    tc.assertIn("no_such_column", rep.findings[0]["msg"])
    tc.assertEqual(rules(run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T, **with_entry(dict(T_ENTRY, phone_fields=["id"]))})), [])


SECRET_TABLE = "CREATE TABLE svc.t (\n  id INT,\n  org_idfr INT,\n  access_token_ct VARBINARY(64)\n);\n"


def check_DC5(tc):
    cols = ["id", "org_idfr", "access_token_ct"]
    run5 = lambda **over: rules(run({"declarations/data.yaml": data()}, {"migrations/001.sql": SECRET_TABLE, **with_entry(entry("svc.t", cols, **over))}))
    tc.assertEqual(run5(), [("DC5", "svc.t")])  # new secret-like column, no decision
    tc.assertEqual(run5(secret_like={"access_token_ct": "exclude"}), [])
    tc.assertEqual(run5(secret_like={"access_token_ct": {"keep": "needed to refresh the token"}}), [])
    tc.assertEqual(run5(secret_like={"access_token_ct": {"keep": ""}}), [("DC5", "svc.t")])  # keep needs a reason
    tc.assertEqual(run5(secret_like={"access_token_ct": "exclude"}, phone_fields=["access_token_ct"]), [("DC5", "svc.t")])
    rep = run({"declarations/data.yaml": data()}, {"migrations/001.sql": SECRET_TABLE, **with_entry(entry("svc.t", cols, secret_like={"access_token_ct": "exclude"}))})
    tc.assertEqual(rep.secret_new, [{"table": "svc.t", "col": "access_token_ct", "decision": "exclude"}])
    tc.assertIn("svc.t.access_token_ct", G.render(rep, "main"))  # handed to OFFLINE-01 in the summary


MUST_MATCH = ["token_hash", "access_token_ct", "key_ciphertext", "password", "passwd", "client_secret", "api_key", "private_key",
              "token_iv", "token_tag", "key_id", "request_hash", "token_issued_at", "otp_code", "salt", "credential_ref", "apikey"]
MUST_NOT_MATCH = ["hashtag", "hashtags", "tokens_used", "monkey", "keyword", "tag", "hotpot", "salted_fish", "ciphertexts", "pass", "name"]


def check_DC5_matcher(tc):
    for n in MUST_MATCH:
        tc.assertTrue(G.is_secret_like(n), f"{n} must look like a secret")
    for n in MUST_NOT_MATCH:
        tc.assertFalse(G.is_secret_like(n), f"{n} must not look like a secret")


def check_DC6(tc):
    ent = with_entry(T_ENTRY)
    alter = {"migrations/002.sql": "ALTER TABLE svc.t ADD COLUMN extra INT, ADD KEY idx_extra (extra);\n"}
    base = dict(BASE_T, **ent)
    rep = run(base, alter)
    tc.assertEqual(rules(rep), [("DC6", "svc.t")])
    tc.assertEqual(len([f for f in rep.findings if f["rule"] == "DC6"]), 1)  # the KEY is not a column
    both = dict(alter, **with_entry(entry("svc.t", ["id", "org_idfr", "name", "extra"])))
    tc.assertEqual(rules(run(base, both)), [])


OWNERS_OK = {"svc.foreign": "owner_repo"}
FOREIGN_SQL = {"migrations/002.sql": "ALTER TABLE svc.foreign ADD COLUMN c INT;\n"}


def foreign_cfg(owner="owner_repo"):
    return data([], foreign_changes=[{"table": "svc.foreign", "owner": owner, "reason": "needs a column"}])


def check_DC7(tc):
    tc.assertEqual(rules(run(BASE_T, FOREIGN_SQL)), [("DC7", "svc.foreign")])  # (a) not listed
    listed = dict(FOREIGN_SQL, **{"declarations/data.yaml": foreign_cfg()})
    tc.assertNotIn(("DC7", "svc.foreign"), rules(run(BASE_T, listed)))
    # (b) block mode: a warning until owners.yaml exists, an error when the owner differs, clean when it matches
    rep = run(BASE_T, listed, "block")
    tc.assertEqual([(f["rule"], f["level"]) for f in rep.findings if f["rule"] == "DC7"], [("DC7", "warning")])
    rep = run(BASE_T, dict(listed, **{"declarations/data.yaml": foreign_cfg("someone_else")}), "block", owners=OWNERS_OK)
    tc.assertEqual([(f["rule"], f["level"]) for f in rep.findings if f["rule"] == "DC7"], [("DC7", "error")])
    rep = run(BASE_T, listed, "block", owners=OWNERS_OK)
    tc.assertEqual([f for f in rep.findings if f["rule"] == "DC7"], [])
    # a table created in this PR is never a foreign change
    tc.assertEqual(rules(run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T})), [("DC1", "svc.t")])


def check_DC8(tc):
    # a PR that excludes a table cannot hide it from itself: the base copy is used
    head = {"migrations/001.sql": CREATE_T, "declarations/data.yaml": data(exclude_tables=["t"])}
    rep = run({"declarations/data.yaml": data()}, head)
    tc.assertEqual(rules(rep), [("DC1", "svc.t"), ("DC8", "-")])
    tc.assertEqual([f["level"] for f in rep.findings if f["rule"] == "DC8"], ["warning"])
    # a PR that creates data.yaml is judged with every path in the repo as schema_paths
    rep = run({"README.md": "x\n"}, {"anywhere/at/all.sql": CREATE_T, "declarations/data.yaml": data()})
    tc.assertEqual(rules(rep), [("DC1", "svc.t"), ("DC8", "-")])
    # unchanged config: no DC8
    tc.assertNotIn("DC8", codes(run(BASE_T, {"migrations/002.sql": "CREATE TABLE svc.v (id INT);\n"})))


def check_DC9(tc):
    rep = run(BASE_T, {"migrations/002.sql": "INSERT INTO svc.t (id) VALUES (1);\n"})
    tc.assertEqual(rules(rep), [("DC9", "-")])  # a data-only file under schema_paths
    rep = run(BASE_T, {"migrations/002.sql": "-- CREATE TABLE IF NOT EXISTS is supported on this server\n"})
    tc.assertEqual(codes(rep), ["DC9"])  # prose after CREATE TABLE is DC9, never DC1
    rep = run(BASE_T, {"migrations/002.sql": "-- CREATE TABLE matches a from-empty build. The index survives it.\n"})
    tc.assertEqual(codes(rep), ["DC9"])  # chat 037, a real line: the "name" is a verb, and no stopword list would know
    rep = run({"declarations/data.yaml": data(schema_paths=(("migrations/*.ts", "tsdl"),))},
              {"migrations/a.ts": "const q = `CREATE TABLE ${UNKNOWN_CONST}.x (id INT)`;\n"})
    tc.assertEqual(codes(rep), ["DC9"])  # an unresolved name
    tc.assertTrue(all(f["level"] == "warning" for f in rep.findings))


def check_DC10(tc):
    cfg = {"declarations/data.yaml": data([T_ENTRY]), "migrations/001.sql": CREATE_T}
    ddl = "const q = `ALTER TABLE svc.t ADD COLUMN z INT`;\n"
    tc.assertEqual(rules(run(cfg, {"src/scripts/fix.ts": ddl})), [("DC10", "-")])
    tc.assertEqual(rules(run(cfg, {"src/scripts/fix.test.ts": ddl})), [])  # a default exclude_path
    tc.assertEqual(rules(run(cfg, {"docs/notes.md": "ALTER TABLE svc.t ADD COLUMN z INT;\n"})), [])  # prose is never scanned
    tc.assertEqual(rules(run(cfg, {"src/db.js": "db.collection('brand_new').createIndex({a: 1});\n"})), [("DC10", "-")])
    mdl = {"declarations/data.yaml": data([T_ENTRY]), "migrations/001.sql": CREATE_T}
    tc.assertEqual(rules(run(mdl, {"src/read.js": "const rows = await db.collection('t').find({});\n"})), [])  # a read of a known name


RULE_CHECKS = {"DC1": check_DC1, "DC2": check_DC2, "DC3": check_DC3, "DC4": check_DC4, "DC5": check_DC5, "DC6": check_DC6,
               "DC7": check_DC7, "DC8": check_DC8, "DC9": check_DC9, "DC10": check_DC10}


class Rules(unittest.TestCase):
    pass


for _rule, _fn in RULE_CHECKS.items():
    setattr(Rules, f"test_{_rule}", lambda self, _fn=_fn: _fn(self))
Rules.test_DC5_matcher = lambda self: check_DC5_matcher(self)



# ── part 3: the extractors, one per stack (spec section 3) ──────────────────────────────────────────────────
def touches(base, head, **kw):
    rep = run(base, head, **kw)
    return rep, {t: (ops, rep.added.get(t, [])) for t, ops in rep.touched.items()}


def sql_head(sql, path="migrations/002.sql"):
    return {path: sql}


class SqlExtractor(unittest.TestCase):
    def test_prepare_string_and_stored_procedure_ddl(self):
        sql = "SET @ddl := 'ALTER TABLE svc.t ADD COLUMN extra INT';\nPREPARE s FROM @ddl;\nEXECUTE s;\n"
        _, t = touches(BASE_T, sql_head(sql))
        self.assertEqual(t, {"svc.t": (["alter"], ["extra"])})

    def test_commented_record_block_counts_but_prose_does_not(self):
        sql = "-- CREATE TABLE svc.rec (\n--   id INT,\n--   note VARCHAR(5)\n-- );\n-- Run CREATE TABLE svc.zzz first, then the rest.\n"
        _, t = touches(BASE_T, sql_head(sql))
        self.assertEqual(t, {"svc.rec": (["create"], ["id", "note"])})

    def test_temporary_tables_never_count(self):
        rep, t = touches(BASE_T, sql_head("CREATE TEMPORARY TABLE svc.tmp (id INT);\n"))
        self.assertEqual(t, {})
        self.assertEqual(codes(rep), ["DC9"])

    def test_use_sets_the_schema_for_bare_names_and_quotes_are_stripped(self):
        sql = "USE other;\nCREATE TABLE `things` (id INT);\nCREATE TABLE \"inner\".\"n\" (id INT);\n"
        _, t = touches(BASE_T, sql_head(sql))
        self.assertEqual(sorted(t), ["inner.n", "other.things"])

    def test_drop_and_rename(self):
        rep, t = touches(BASE_T, sql_head("DROP TABLE IF EXISTS svc.a, svc.b;\nRENAME TABLE svc.c TO svc.d;\n"))
        self.assertEqual(sorted(t), ["svc.a", "svc.b", "svc.c", "svc.d"])
        self.assertEqual([f["table"] for f in rep.findings if f["rule"] == "DC1"], ["svc.d"])  # a dropped table needs no entry

    def test_create_index_touches_its_table(self):
        rep, t = touches(BASE_T, sql_head("CREATE UNIQUE INDEX idx_name ON svc.t (name);\nDROP INDEX idx_old ON svc.t;\n"))
        self.assertEqual(t, {"svc.t": (["index"], [])})
        self.assertEqual(rules(rep), [("DC1", "svc.t")])

    def test_alter_clauses_with_commas_inside_parentheses(self):
        sql = "ALTER TABLE svc.t\n  ADD COLUMN amount DECIMAL(10,2) NOT NULL DEFAULT 0,\n  ADD COLUMN flag TINYINT(1),\n" \
              "  DROP COLUMN old_col,\n  ADD UNIQUE KEY uq_flag (flag),\n  CHANGE COLUMN nm name2 VARCHAR(9);\n"
        _, t = touches(BASE_T, sql_head(sql))
        self.assertEqual(t, {"svc.t": (["alter"], ["amount", "flag", "name2"])})

    def test_postgres_quoted_names_and_statement_breakpoints(self):
        sql = 'ALTER TABLE "public"."users" ADD COLUMN IF NOT EXISTS "bio" text;--> statement-breakpoint\nCREATE INDEX IF NOT EXISTS "i" ON "public"."users" USING btree ("bio");\n'
        _, t = touches(BASE_T, sql_head(sql))
        self.assertEqual(t, {"public.users": (["alter", "index"], ["bio"])})

    def test_a_new_column_line_inside_an_old_create_adds_only_that_column(self):
        head = {"migrations/001.sql": CREATE_T.replace("  name VARCHAR(10)", "  name VARCHAR(10),\n  extra INT"),
                **with_entry(T_ENTRY)}
        rep, t = touches(dict(BASE_T, **with_entry(T_ENTRY)), head)
        self.assertEqual(t, {"svc.t": (["create"], ["extra"])})
        self.assertEqual(rules(rep), [("DC6", "svc.t")])  # the table was not created here, so not every column is new

    def test_a_pure_rename_of_a_schema_file_touches_nothing(self):
        rep, t = touches(BASE_T, {"migrations/001.sql": None, "migrations/001_renamed.sql": CREATE_T})
        self.assertEqual(t, {})

    def test_only_touched_tables_are_judged_the_rest_is_inventory(self):
        base = dict(BASE_T, **{"migrations/005.sql": "CREATE TABLE svc.older (id INT);\n"})
        rep = run(base, {"migrations/006.sql": "ALTER TABLE svc.t ADD COLUMN z INT;\n"})
        self.assertEqual(rules(rep), [("DC1", "svc.t")])  # svc.older is undeclared but untouched
        self.assertEqual(rep.inventory, ["svc.older", "svc.t"])

    def test_exclusions_runner_tables_and_patterns(self):
        base = {"declarations/data.yaml": data(exclude_tables=["%_bak_%"])}
        sql = "CREATE TABLE svc.schema_migrations (v INT);\nCREATE TABLE svc.t_bak_1101 (v INT);\nCREATE TABLE svc.knex_migrations_lock (v INT);\n"
        rep = run(base, sql_head(sql))
        self.assertEqual(rules(rep), [])
        self.assertEqual(rep.excluded, ["svc.knex_migrations_lock", "svc.schema_migrations", "svc.t_bak_1101"])


class OtherStacks(unittest.TestCase):
    def test_typescript_runner_tenant_schema_and_constants_resolve_to_one_name(self):
        cfg = data(schema_paths=(("src/migrations/*.sql", "sql"), ("src/migrations/*.ts", "tsdl")), default_schema="meta")
        head = {"src/migrations/org_leads.sql": "CREATE TABLE IF NOT EXISTS {{TENANT_SCHEMA}}.org_leads (id INT);\n",
                "src/migrations/org_leads.ts": "const q = `CREATE TABLE IF NOT EXISTS ${schema}.org_leads (id INT)`;\n",
                "src/consts.ts": "export const SLOTS = 'booking_slots';\n",
                "src/migrations/slots.ts": "const q = `CREATE TABLE IF NOT EXISTS meta.${SLOTS} (id INT)`;\n"}
        _, t = touches({"declarations/data.yaml": cfg}, head)
        self.assertEqual(sorted(t), ["meta.booking_slots", "org_n.org_leads"])

    def test_liquibase_yaml_and_sqlfile_anywhere(self):
        cfg = data(schema_paths=(("changelogs/**", "liquibase"),), default_schema="purpus_auth")
        yml = ("databaseChangeLog:\n  - changeSet:\n      id: 1\n      author: a\n      changes:\n"
               "        - createTable:\n            tableName: widget\n            columns:\n"
               "              - column:\n                  name: id\n                  type: int\n"
               "              - column:\n                  name: api_key_ct\n                  type: varbinary\n"
               "        - addColumn:\n            tableName: widget\n            columns:\n"
               "              - column:\n                  name: color\n                  type: varchar(5)\n"
               "        - sqlFile:\n            path: sql/extra.sql\n")
        head = {"changelogs/001.yaml": yml, "sql/extra.sql": "CREATE TABLE purpus_auth.extra (id INT);\n"}
        rep, t = touches({"declarations/data.yaml": cfg}, head)
        self.assertEqual(t["purpus_auth.widget"], (["alter", "create"], ["api_key_ct", "color", "id"]))
        self.assertEqual(t["purpus_auth.extra"], (["create"], ["id"]))
        self.assertNotIn("DC10", codes(rep))  # the sqlFile is read as schema wherever it lives

    def test_knex_builders_tables_json_and_raw_placeholder(self):
        cfg = data(schema_paths=(("migrations/*/*.js", "knex"),), default_schema="shop")
        head = {"migrations/constant/tables.json": '{"ORDER": "orders"}\n',
                "migrations/0.0.1/create_orders.js":
                    "exports.up = (knex) => knex.schema.createTable(TABLES.ORDER, (table) => {\n  table.increments('id');\n"
                    "  table.string('buyer_token', 64);\n  table.timestamps(true, true);\n});\n",
                "migrations/0.0.2/alter_orders.js":
                    "exports.up = (knex) => knex.schema.alterTable('orders', (t) => {\n  t.string('note');\n  t.dropColumn('old');\n  t.index(['note']);\n});\n",
                "migrations/0.0.3/raw.js": "exports.up = (knex) => knex.raw('ALTER TABLE ?? ADD COLUMN extra INT', [TABLES.ORDER]);\n"}
        _, t = touches({"declarations/data.yaml": cfg}, head)
        self.assertEqual(t, {"shop.orders": (["alter", "create"], ["buyer_token", "created_at", "extra", "id", "note", "updated_at"])})

    def test_mongoose_pluralised_names_options_indexes_and_constants(self):
        schema_base = "const S = new Schema({\n  name: {\n    type: String,\n  },\n}, { timestamps: true });\nmodule.exports = S;\n"
        files = {"models/person.js": "const PersonSchema = require('./schema/person.schema');\nmodule.exports = mongoose.model('Person', PersonSchema);\n",
                 "models/schema/person.schema.js": schema_base,
                 "models/odd.js": "const OddSchema = require('./schema/odd.schema');\nmodule.exports = mongoose.model('Odd', OddSchema);\n",
                 "models/schema/odd.schema.js": schema_base.replace("{ timestamps: true }", "{ collection: 'custom_name' }"),
                 "models/indexes/people.indexes.js": "module.exports = [];\n",
                 "utils.js": "const MongoCollections = {\n  people: 'people',\n};\n"}
        cfg = data(schema_paths=(("models/**/*.js", "mongoose"), ("utils.js", "mongoose")), default_schema="mongo_svc")
        head = {"models/schema/person.schema.js": schema_base.replace("  name:", "  nickname: {\n    type: String,\n  },\n  name:"),
                "models/schema/odd.schema.js": files["models/schema/odd.schema.js"].replace("  name:", "  flavour: {\n    type: String,\n  },\n  name:"),
                "models/indexes/people.indexes.js": "module.exports = [{ nickname: 1 }];\n",
                "utils.js": files["utils.js"].replace("};", "  audit: 'audit',\n};")}
        _, t = touches(dict(files, **{"declarations/data.yaml": cfg}), head)
        self.assertEqual(t["mongo_svc.people"], (["alter", "index"], ["nickname"]))  # Person -> people
        self.assertEqual(t["mongo_svc.custom_name"], (["alter"], ["flavour"]))  # the schema's collection option wins
        self.assertEqual(t["mongo_svc.audit"], (["create"], []))  # a new MongoCollections constant

    def test_mongodb_driver_new_collection_but_not_a_read(self):
        cfg = data([entry("mongo_svc.known", ["a"])], schema_paths=(("src/**/*.ts", "mongodriver"),), default_schema="mongo_svc")
        head = {"src/db/mongo.ts": "await db.collection('audit_log').createIndex({ at: 1 });\nawait db.collection('known').find({});\n"}
        _, t = touches({"declarations/data.yaml": cfg}, head)
        self.assertEqual(t, {"mongo_svc.audit_log": (["create"], [])})

    def test_social_p4_collections_adapter_reads_text_and_asks_for_the_two_missing_labels(self):
        p4 = ("module.exports = {\n    format: 'social-p4-interim-1',\n    service: 'service_mongo_social',\n    collections: [\n        {\n"
              "            collection: 'posts',\n            owner_stream: 'x',\n            scope_class: 'org',\n            reaches_phones: false,\n"
              "            meaning: 'A post.',\n            fields: {\n                _id: 'The id.',\n                identifier: 'The identifier.',\n            },\n        },\n    ],\n};\n")
        base = dict(SOCIAL_FILES, **{"declarations/p4-collections.js": p4, "orbit-social/models/schema/posts_collection.schema.js": POSTS_BASE_SCHEMA})
        rep = run(base, {"orbit-social/models/schema/posts_collection.schema.js": POSTS_HEAD_SCHEMA})
        self.assertEqual(rules(rep), [("DC2", "service_mongo_social.posts")] * 2)  # placement and placement_key are missing


class ReviewFixes(unittest.TestCase):
    """Things found when reviewing the gate against real repos and hostile input."""

    def test_postgres_alter_table_only(self):
        _, t = touches(BASE_T, sql_head("ALTER TABLE ONLY public.users ADD CONSTRAINT users_pk PRIMARY KEY (id);\n"))
        self.assertEqual(t, {"public.users": (["alter"], [])})

    def test_a_missing_phone_fields_is_not_an_error_but_the_p4_reaches_phones_without_a_list_is(self):
        e = entry("svc.t", ["id", "org_idfr", "name"]); e.pop("phone_fields")
        self.assertEqual(rules(run({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T, **with_entry(e)})), [])
        p4 = ("module.exports = {\n  service: 'service_mongo_social',\n  collections: [\n    {\n      collection: 'posts',\n      scope_class: 'org',\n"
              "      placement: 'personal_or_content',\n      placement_key: 'org_identifier',\n      reaches_phones: true,\n      meaning: 'A post.',\n"
              "      fields: { owner_idfr: 'The owner', org_identifier: 'The org', identifier: 'The id' },\n    },\n  ],\n};\n")
        base = dict(SOCIAL_FILES, **{"declarations/p4-collections.js": p4, "orbit-social/models/schema/posts_collection.schema.js": POSTS_BASE_SCHEMA})
        rep = run(base, {"orbit-social/models/schema/posts_collection.schema.js": POSTS_HEAD_SCHEMA})
        self.assertEqual(rules(rep), [("DC4", "service_mongo_social.posts")])
        self.assertIn("names no phone_fields", rep.findings[0]["msg"])

    def test_a_yaml_that_expands_itself_is_refused(self):
        bomb = "a: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\ntables: *b\n"
        d = make_repo(NEW_T[0], {"migrations/001.sql": CREATE_T, "declarations/data.yaml": bomb})
        with self.assertRaises(G.GateError):
            G.judge(d, "main", "report")

    def test_a_changed_schema_file_too_big_to_read_is_a_warning_not_a_silent_skip(self):
        big = "-- " + "x" * 2_100_000 + "\n" + CREATE_T
        rep = run({"declarations/data.yaml": data()}, {"migrations/001.sql": big})
        self.assertEqual([(f["rule"], f["level"]) for f in rep.findings], [("DC9", "warning")])

    def test_hostile_long_identifiers_stay_fast(self):
        import time
        text = "CREATE TABLE " + "a" * 5000 + " !\n" + ("ALTER TABLE " + "b_" * 3000 + "(\n") * 3
        start = time.time()
        G.sql_events(G.Src("x.sql", text), G.Resolver("s"))
        self.assertLess(time.time() - start, 2)


# ── part 4: modes and output ─────────────────────────────────────────────────────────────────────────────
def main_in(base, head, *args):
    d = make_repo(base, head)
    summary = os.path.join(d, ".summary.md")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = G.main(["--repo", d, "--base-ref", "main", "--summary", summary, *args])
    text = ""
    if os.path.exists(summary):
        with open(summary) as fh:
            text = fh.read()
    return rc, out.getvalue(), text


NEW_T = ({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T})


class Modes(unittest.TestCase):
    def test_report_mode_never_fails_and_says_the_error_is_not_blocking(self):
        rc, out, summary = main_in(*NEW_T, "--mode", "report")
        self.assertEqual(rc, 0)
        self.assertIn("::warning file=migrations/001.sql,line=1,title=declaration gate::DC1 svc.t", out)
        self.assertIn("report mode: not blocking", out)
        self.assertIn("## Declaration gate (report)", summary)

    def test_block_mode_fails_on_an_error_and_passes_when_clean(self):
        rc, out, summary = main_in(*NEW_T, "--mode", "block")
        self.assertEqual(rc, 1)
        self.assertIn("::error file=migrations/001.sql,line=1,title=declaration gate::DC1 svc.t", out)
        self.assertIn("FAILED", summary)
        rc, _, summary = main_in(NEW_T[0], {**NEW_T[1], **with_entry(T_ENTRY)}, "--mode", "block")
        self.assertEqual(rc, 0)
        self.assertIn("passed", summary)

    def test_block_mode_ignores_warnings(self):
        rc, _, _ = main_in(BASE_T, {"migrations/002.sql": "INSERT INTO svc.t (id) VALUES (1);\n"}, "--mode", "block")
        self.assertEqual(rc, 0)  # a DC9 warning only

    def test_a_bad_mode_is_a_caller_mistake_and_fails_either_way(self):
        rc, out, _ = main_in(*NEW_T, "--mode", "blok")
        self.assertEqual(rc, 2)
        self.assertIn("mode must be", out)

    def test_a_crash_never_fails_report_and_fails_closed_in_block(self):
        with mock.patch.object(G, "judge", side_effect=RuntimeError("boom")):
            self.assertEqual(main_in(*NEW_T, "--mode", "report")[0], 0)
            rc, out, _ = main_in(*NEW_T, "--mode", "block")
        self.assertEqual(rc, 2)
        self.assertIn("set mode back to report", out)

    def test_invalid_data_yaml_is_the_prs_error(self):
        head = {"migrations/001.sql": CREATE_T, "declarations/data.yaml": "tables: [unclosed\n"}
        self.assertEqual(main_in(NEW_T[0], head, "--mode", "report")[0], 0)
        rc, out, _ = main_in(NEW_T[0], head, "--mode", "block")
        self.assertEqual(rc, 1)
        self.assertIn("not valid YAML", out)

    def test_text_from_a_pr_cannot_inject_workflow_commands(self):
        self.assertEqual(G.clean("a::b%0Ac,d\n::error x"), "a??b?0Ac?d???error x")
        sql = "CREATE TABLE svc.`a::error b%0A::set-output c` (id INT);\n"
        _, out, summary = main_in({"declarations/data.yaml": data()}, {"migrations/001.sql": sql}, "--mode", "block")
        self.assertTrue(all(l.startswith(("::error file=", "::warning file=")) or not l.startswith("::") for l in out.splitlines()))
        self.assertNotIn("a::error", out + summary)
        self.assertNotIn("%0A::set", out + summary)

    def test_the_summary_lists_foreign_columns_for_the_owner(self):
        listed = dict(FOREIGN_SQL, **{"declarations/data.yaml": foreign_cfg()})
        _, _, summary = main_in(BASE_T, listed, "--mode", "report")
        self.assertIn("`svc.foreign`, owner `owner_repo`, new columns: `c`", summary)


# ── part 5: each rule's test must fail when the rule is broken ──────────────────────────────────────────────
@contextlib.contextmanager
def broken(rule):
    G.DISABLED.add(rule)
    try:
        yield
    finally:
        G.DISABLED.discard(rule)


class Mutations(unittest.TestCase):
    pass


def _mutation(rule, fn):
    def test(self):
        fn(self)  # the check passes with the rule intact (control)
        with broken(rule):
            with self.assertRaises(AssertionError, msg=f"the {rule} test still passes with {rule} switched off"):
                fn(self)
    return test


for _rule, _fn in RULE_CHECKS.items():
    setattr(Mutations, f"test_{_rule}_test_fails_when_the_rule_is_broken", _mutation(_rule, _fn))


class SecretMatcherMutations(unittest.TestCase):
    def test_a_matcher_that_matches_substrings_fails_the_must_not_match_cases(self):
        naive = lambda n: any(w in n.lower() for w in ("hash", "token", "secret", "password", "salt", "key"))
        with mock.patch.object(G, "is_secret_like", naive):
            with self.assertRaises(AssertionError):
                check_DC5_matcher(self)

    def test_a_matcher_that_matches_nothing_fails_the_must_match_cases_and_dc5(self):
        with mock.patch.object(G, "is_secret_like", lambda n: False):
            with self.assertRaises(AssertionError):
                check_DC5_matcher(self)
            with self.assertRaises(AssertionError):
                check_DC5(self)

    def test_a_matcher_that_matches_everything_fails_the_must_not_match_cases(self):
        with mock.patch.object(G, "is_secret_like", lambda n: True):
            with self.assertRaises(AssertionError):
                check_DC5_matcher(self)


if __name__ == "__main__":
    unittest.main(verbosity=1)

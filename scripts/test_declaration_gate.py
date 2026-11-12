#!/usr/bin/env python3
"""Self-test for scripts/declaration_gate.py (spec: specs offline-first/declaration-ci-check.md).

Every case builds a small git repo (a base commit, then a PR commit), runs the gate on it and reads the findings.
Part 1 replays the four PRs of spec section 8. Part 2 is one test per rule. Part 3 is the extractors, one per stack.
Part 4 is the modes and the output. Part 5 proves each rule's test fails when the rule is broken: it switches the
rule off (or swaps a broken DC5 matcher in) and requires the rule's own test to go red.

Run: python3 scripts/test_declaration_gate.py
"""
import collections
import contextlib
import io
import os
import re
import shutil
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
    """base, head: {path: text, None to delete, or ("symlink", target)}. Returns the repo dir with a `main` base commit and the PR checked out."""
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
            if isinstance(text, tuple) and text[0] == "symlink":
                if os.path.lexists(fp):
                    os.remove(fp)
                os.symlink(text[1], fp)
                continue
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


def run(base, head, mode="report", owners=None, repo=None):
    d = make_repo(base, head)
    own = None
    if owners is not None:
        own = os.path.join(d, ".owners.yaml")
        with open(own, "w") as fh:
            fh.write(yaml.safe_dump(owners))
    return G.judge(d, "main", mode, own, calling_repo=repo)


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

    def test_a_renamed_schema_file_counts_as_added_so_nothing_hides_in_a_move(self):
        # No rename detection: a moved file is all-new lines, so its tables are touched (see CriticRound1 for the hole this closes).
        rep, t = touches(BASE_T, {"migrations/001.sql": None, "migrations/001_renamed.sql": CREATE_T})
        self.assertEqual(t, {"svc.t": (["create"], [])})  # touched, but the table had these columns at the merge base: none are new
        self.assertEqual(rules(rep), [("DC1", "svc.t")])

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

    def test_each_listed_schema_placeholder_becomes_org_n(self):
        # OFFLINE-01 ruling on orgs' first run (specs#411 6821647751). A report prints `<` as `?`, so `?schema?.t` is `<schema>.t`.
        for ph in ("`<schema>`", "`<tenant_schema>`", "__SCHEMA__", "`__schema__`"):
            with self.subTest(placeholder=ph):
                head = {"migrations/002.sql": f"CREATE TABLE IF NOT EXISTS {ph}.org_leads (id INT);\n"}
                _, t = touches(BASE_T, head)
                self.assertEqual(sorted(t), ["org_n.org_leads"])

    def test_a_placeholder_off_the_list_is_reported_unresolved_and_never_merged(self):
        for ph in ("`<reference>`", "`<other_schema>`", "__tenant__", "`?x?`"):
            with self.subTest(placeholder=ph):
                head = {"migrations/002.sql": f"CREATE TABLE {ph}.org_leads (id INT);\n"}
                rep, t = touches(BASE_T, head)
                self.assertEqual(t, {})  # not a table of any schema
                self.assertEqual(codes(rep), ["DC9"])
                self.assertIn("unresolved schema placeholder", rep.findings[0]["msg"])

    def test_a_table_named_like_a_dunder_is_not_a_placeholder(self):
        _, t = touches(BASE_T, {"migrations/002.sql": "CREATE TABLE __drizzle_migrations__ (id INT);\n"})
        self.assertEqual(sorted(t), ["svc.__drizzle_migrations__"])

    def test_a_bare_schema_name_is_not_a_table(self):
        # The orgs lines that made a row `org_n` (service_orbit_orgs sandbox 7d4bc70, src/migrations/org_leads_hardening.ts 127/139):
        # `${table(schema)}` is a helper call that builds the table's full name, so its name has no table part the gate can read.
        cfg = data(schema_paths=(("src/migrations/*.ts", "tsdl"),), default_schema="meta")
        head = {"src/migrations/org_leads_hardening.ts":
                "await apply(conn, `CREATE TABLE IF NOT EXISTS ${table(schema)} LIKE ${table(reference)}`, dryRun, out, log);\n"
                "if (!columns.has(column.name)) await apply(conn, `ALTER TABLE ${table(schema)} ADD COLUMN ${escapeId(column.name)} ${column.definition}`, dryRun, out, log);\n"}
        rep, t = touches({"declarations/data.yaml": cfg}, head)
        self.assertNotIn("org_n", t)
        self.assertEqual(t, {})
        self.assertEqual(sorted(codes(rep)), ["DC9", "DC9"])  # reported as unresolved, not as a phantom table
        # and a plain `${schema}` alone is not a table either
        rep, t = touches({"declarations/data.yaml": cfg}, {"src/migrations/a.ts": "const q = `ALTER TABLE ${schema} ADD COLUMN c INT`;\n"})
        self.assertEqual(t, {})

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


# ── OFFLINE-01's ruling on #127 (specs#411 6816056808, relayed by GIT-01) ───────────────────────────────────
SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "declarations", "owners.yaml")


class OffLineRuling(unittest.TestCase):
    def test_the_seeded_owners_file_parses_in_the_one_shape_and_holds_d6s_26_owners(self):
        with open(SEED, encoding="utf-8") as fh:
            doc = G.load_yaml(fh.read())
        self.assertEqual(list(doc), ["owners"])  # one shape for anything that writes it: `owners:` a map of schema.table: owner
        owners = doc["owners"]
        self.assertEqual(len(owners), 26)
        self.assertEqual(dict(collections.Counter(owners.values())), {"orbit_orgs": 17, "iap_web_commerce": 8, "broadcast_audience": 1})
        for table, owner in owners.items():
            self.assertRegex(table, r"^[a-z0-9_]+\.[a-z0-9_]+$")
            self.assertRegex(owner, r"^[a-z0-9_]+$")
        self.assertEqual(G.load_owners(SEED), owners)
        # the three contested rows, as D6 rules them
        self.assertEqual((owners["meta.event_ticket_booking"], owners["org_n.org_broadcast"], owners["meta.wallet"]),
                         ("orbit_orgs", "broadcast_audience", "iap_web_commerce"))

    def test_the_reader_stays_tolerant_of_the_other_shapes(self):
        with tempfile.TemporaryDirectory() as d:
            for name, text in (("bare", "meta.wallet: iap_web_commerce\n"),
                               ("list", "- {table: meta.wallet, owner: iap_web_commerce}\n")):
                path = os.path.join(d, name + ".yaml")
                with open(path, "w") as fh:
                    fh.write(text)
                self.assertEqual(G.load_owners(path), {"meta.wallet": "iap_web_commerce"})

    def test_dc7_b_uses_the_seeded_file_in_block_mode(self):
        def verdict(owner):
            cfg = data([], default_schema="svc", foreign_changes=[{"table": "meta.wallet", "owner": owner, "reason": "balance column"}])
            d = make_repo(BASE_T, {"migrations/002.sql": "ALTER TABLE meta.wallet ADD COLUMN c INT;\n", "declarations/data.yaml": cfg})
            return [f for f in G.judge(d, "main", "block", SEED).findings if f["rule"] == "DC7"]
        self.assertEqual(verdict("iap_web_commerce"), [])
        wrong = verdict("orbit_orgs")
        self.assertEqual([f["level"] for f in wrong], ["error"])
        self.assertIn("iap_web_commerce", wrong[0]["msg"])

    def test_the_documented_secret_like_form_is_accepted(self):
        cols = ["id", "org_idfr", "access_token_ct"]

        def verdict(decl):
            return rules(run({"declarations/data.yaml": data()}, {"migrations/001.sql": SECRET_TABLE,
                                                                  **with_entry(entry("svc.t", cols, secret_like={"access_token_ct": decl}))}))
        self.assertEqual(verdict({"decision": "exclude"}), [])
        self.assertEqual(verdict({"decision": "keep", "reason": "needed on the device"}), [])
        self.assertEqual(verdict({"decision": "keep"}), [("DC5", "svc.t")])  # keep needs a reason

    def test_a_dropped_table_with_an_entry_gets_a_note_not_an_error(self):
        base = dict(BASE_T, **with_entry(T_ENTRY))
        rep = run(base, {"migrations/002.sql": "DROP TABLE svc.t;\n"})
        self.assertEqual(rules(rep), [])
        self.assertEqual(rep.removable, ["svc.t"])
        self.assertIn("entry in declarations/data.yaml can be removed", G.render(rep, "main"))
        rep = run(BASE_T, {"migrations/002.sql": "DROP TABLE svc.t;\n"})  # no entry: nothing to remove
        self.assertEqual((rules(rep), rep.removable), ([], []))

    def test_a_mongo_inventory_says_what_it_does_not_count(self):
        line = "collections registered by other means than mongoose.model(...) are not counted"
        base = dict(SOCIAL_FILES, **{"declarations/data.yaml": data(**SOCIAL_CFG), "orbit-social/models/schema/posts_collection.schema.js": POSTS_BASE_SCHEMA})
        rep = run(base, {"orbit-social/models/schema/posts_collection.schema.js": POSTS_HEAD_SCHEMA})
        self.assertIn(line, G.render(rep, "main").lower())
        self.assertNotIn(line, G.render(run(BASE_T, {"migrations/002.sql": "ALTER TABLE svc.t ADD COLUMN z INT;\n"}), "main").lower())


# ── the fresh critic's round on #127 @2144157: four MAJORs and two MINORs (relayed by GIT-01) ─────────────────
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "reusable-declaration-gate.yml")
HOSTILE_YAML = "import os\nopen(os.environ['PWN'], 'w').write('a PR file ran on the runner')\n"
OWN_FOREIGN_BASE = {"declarations/data.yaml": data(default_schema="meta"), "migrations/001.sql": "CREATE TABLE meta.other (id INT);\n"}
ALTER_ORG = "ALTER TABLE meta.organization ADD COLUMN access_token_ct VARBINARY(64);\n"


def foreign_pr(table, owner, **item):
    cfg = data([], default_schema="meta", foreign_changes=[{"table": table, "owner": owner, "reason": "x", **item}])
    return {"migrations/002.sql": f"ALTER TABLE {table} ADD COLUMN access_token_ct VARBINARY(64);\n", "declarations/data.yaml": cfg}


class CriticRound1(unittest.TestCase):
    def test_major1_a_pr_checkout_cannot_shadow_the_workflows_python(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            steps = {s.get("name", ""): s for s in yaml.safe_load(fh)["jobs"]["declaration-gate"]["steps"]}
        install = next(s["run"] for n, s in steps.items() if n.startswith("Install PyYAML"))
        judge = next(s["run"] for n, s in steps.items() if n.startswith("Judge"))
        with tempfile.TemporaryDirectory() as tmp:
            work = make_repo({"declarations/data.yaml": data()},
                             {"migrations/001.sql": CREATE_T, "yaml.py": HOSTILE_YAML, "pip/__init__.py": HOSTILE_YAML, "pip/__main__.py": HOSTILE_YAML})
            origin = os.path.join(tmp, "origin.git")
            sh(tmp, "git", "init", "-q", "--bare", "-b", "main", origin)
            sh(work, "git", "remote", "add", "origin", origin)
            sh(work, "git", "push", "-q", "origin", "main")
            src = os.path.join(work, ".declaration-gate-src")  # what the second checkout step puts there
            os.makedirs(os.path.join(src, "scripts")); os.makedirs(os.path.join(src, "declarations"))
            shutil.copy(os.path.join(ROOT, "scripts", "declaration_gate.py"), os.path.join(src, "scripts"))
            shutil.copy(SEED, os.path.join(src, "declarations"))
            marker = os.path.join(tmp, "PWNED")
            env = {**os.environ, "PWN": marker, "GITHUB_WORKSPACE": work, "RUNNER_TEMP": tmp, "MODE": "report", "BASE_REF": "main",
                   "GITHUB_STEP_SUMMARY": os.path.join(tmp, "summary.md"), "GITHUB_REPOSITORY": "purpusgit/service_x"}
            for name, script in (("install", install), ("judge", judge)):
                r = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=work, env=env, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{name}: {r.stderr[-400:]}")
            self.assertFalse(os.path.exists(marker), "a file from the PR checkout was imported and ran on the runner")
            with open(os.path.join(tmp, "summary.md")) as fh:
                self.assertIn("DC1", fh.read())  # and the gate itself still ran and judged the PR

    def test_major2_a_pr_gitattributes_cannot_hide_the_diff(self):
        head = {".gitattributes": "*.sql -diff\n*.sql binary\n", "migrations/001.sql": CREATE_T}
        self.assertEqual(rules(run({"declarations/data.yaml": data()}, head)), [("DC1", "svc.t")])

    def test_major3_moving_an_excluded_file_into_schema_paths_is_not_invisible(self):
        base = {"declarations/data.yaml": data(), "tests/fx.sql": CREATE_T}
        rep = run(base, {"tests/fx.sql": None, "migrations/002.sql": CREATE_T})
        self.assertEqual(rules(rep), [("DC1", "svc.t")])

    def test_major4_a_repo_cannot_declare_its_own_table_as_foreign(self):
        # the critic's case: orbit_orgs, meta.organization (owners.yaml: orbit_orgs) + a secret-like column, listed as a foreign change
        for name in ("service_orbit_orgs", "purpusgit/service_orbit_orgs", "orbit_orgs", "Service_Orbit_Orgs"):
            d = make_repo(OWN_FOREIGN_BASE, foreign_pr("meta.organization", "goalcaller_contact_graph"))
            errs = rules(G.judge(d, "main", "report", SEED, calling_repo=name), "error")
            self.assertEqual(errs, [("DC1", "meta.organization"), ("DC7", "meta.organization")], name)

    def test_major4_the_item_naming_the_calling_repo_as_owner_is_refused_even_without_owners_yaml(self):
        d = make_repo(OWN_FOREIGN_BASE, foreign_pr("meta.zzz_unlisted", "service_orbit_orgs"))
        errs = rules(G.judge(d, "main", "report", None, calling_repo="purpusgit/service_orbit_orgs"), "error")
        self.assertIn(("DC7", "meta.zzz_unlisted"), errs)

    def test_major4_another_repo_may_still_declare_the_real_owners_table(self):
        d = make_repo(OWN_FOREIGN_BASE, foreign_pr("meta.organization", "orbit_orgs", secret_like={"access_token_ct": {"decision": "exclude"}}))
        rep = G.judge(d, "main", "block", SEED, calling_repo="purpusgit/service_iap_web_commerce")
        self.assertEqual(rules(rep, "error"), [])

    def test_major4_secret_like_columns_added_by_a_foreign_change_need_a_decision(self):
        d = make_repo(OWN_FOREIGN_BASE, foreign_pr("meta.organization", "orbit_orgs"))
        rep = G.judge(d, "main", "report", SEED, calling_repo="service_iap_web_commerce")
        self.assertEqual(rules(rep, "error"), [("DC5", "meta.organization")])
        for decl in ({"decision": "exclude"}, {"decision": "keep", "reason": "the owner needs it on the device"}):
            d = make_repo(OWN_FOREIGN_BASE, foreign_pr("meta.organization", "orbit_orgs", secret_like={"access_token_ct": decl}))
            self.assertEqual(rules(G.judge(d, "main", "report", SEED, calling_repo="service_iap_web_commerce"), "error"), [])

    def test_a_foreign_table_missing_from_owners_yaml_is_an_error_once_the_file_exists(self):
        # DC7 (b): the owner must be listed for the table. A warning only while the file is absent. (An earlier round
        # made a missing row a warning; that let a repo call its own uncreated table foreign with a made-up owner.)
        d = make_repo(OWN_FOREIGN_BASE, foreign_pr("meta.zzz_unlisted", "someone"))
        rep = G.judge(d, "main", "block", SEED, calling_repo="service_x")
        self.assertEqual([(f["rule"], f["level"]) for f in rep.findings if f["rule"] == "DC7"], [("DC7", "error")])
        rep = G.judge(d, "main", "block", os.path.join(d, "no-such-owners.yaml"), calling_repo="service_x")  # file absent
        self.assertEqual([(f["rule"], f["level"]) for f in rep.findings if f["rule"] == "DC7"], [("DC7", "warning")])
        d = make_repo(OWN_FOREIGN_BASE, foreign_pr("meta.wallet", "orbit_orgs"))  # listed, but another repo owns it
        rep = G.judge(d, "main", "block", SEED, calling_repo="service_x")
        self.assertEqual([(f["rule"], f["level"]) for f in rep.findings if f["rule"] == "DC7"], [("DC7", "error")])

    def test_minor_an_unreadable_schema_file_is_an_error_in_block_mode_a_warning_in_report(self):
        big = "-- " + "x" * 2_100_000 + "\n" + CREATE_T
        base, head = {"declarations/data.yaml": data()}, {"migrations/001.sql": big}
        self.assertEqual([(f["rule"], f["level"]) for f in run(base, head, "block").findings], [("DC9", "error")])
        self.assertEqual([(f["rule"], f["level"]) for f in run(base, head, "report").findings], [("DC9", "warning")])


# ── the re-critic's round on #127 @0ffa463: two MAJORs and three MINORs (relayed by GIT-01) ─────────────────
class CriticRound2(unittest.TestCase):
    def test_major1_paths_with_spaces_quotes_and_backslashes_are_read(self):
        # git C-quotes `"` and `\` in patch headers and tab-pads names with spaces; the gate must not parse headers
        names = {"migrations/002 new.sql": "a", 'migrations/002"x.sql': "b", "migrations/002\\y.sql": "c"}
        head = {path: f"CREATE TABLE svc.{tbl} (id INT);\n" for path, tbl in names.items()}
        rep = run({"declarations/data.yaml": data()}, head, "block")
        self.assertEqual(rules(rep, "error"), [("DC1", "svc.a"), ("DC1", "svc.b"), ("DC1", "svc.c")])

    def test_major1_a_deleted_schema_file_is_not_reported_unreadable(self):
        rep = run(dict(BASE_T, **{"migrations/009.sql": "CREATE TABLE svc.gone (id INT);\n"}), {"migrations/009.sql": None}, "block")
        self.assertEqual(rules(rep, "error"), [])

    def test_minor_postgres_rename_without_the_column_keyword_adds_the_new_name(self):
        base = dict(BASE_T, **with_entry(T_ENTRY))
        rep = run(base, {"migrations/002.sql": "ALTER TABLE svc.t RENAME name TO api_token;\n"})
        self.assertEqual(rep.added["svc.t"], ["api_token"])
        # the new name needs a decision (DC5) and a field (DC6); the entry still lists `name`, which the table no longer has (DC4)
        self.assertEqual(rules(rep), [("DC4", "svc.t"), ("DC5", "svc.t"), ("DC6", "svc.t")])
        # RENAME TO (the table), RENAME CONSTRAINT and RENAME INDEX are not column renames
        for sql in ("ALTER TABLE svc.t RENAME TO svc.t2;\n", "ALTER TABLE svc.t RENAME CONSTRAINT a TO b;\n", "ALTER TABLE svc.t RENAME INDEX a TO b;\n"):
            self.assertEqual(run(base, {"migrations/002.sql": sql}).added.get("svc.t", []), [], sql)

    def test_minor_default_schema_is_judged_from_the_base_copy(self):
        # the PR renames the default schema to one that already has an entry, so its unqualified CREATE TABLE would look declared
        base = {"declarations/data.yaml": data(default_schema="svc")}
        head = {"declarations/data.yaml": data([entry("evil.t", ["id"], placement_key="central")], default_schema="evil"),
                "migrations/001.sql": "CREATE TABLE t (id INT);\n"}
        rep = run(base, head)
        self.assertIn(("DC1", "svc.t"), rules(rep))
        self.assertTrue(any(f["rule"] == "DC8" and "default_schema" in f["msg"] for f in rep.findings))

    def test_minor_drop_column_removes_the_column_from_what_the_table_has(self):
        base = dict(BASE_T, **with_entry(T_ENTRY))
        rep = run(base, {"migrations/002.sql": "ALTER TABLE svc.t DROP COLUMN name;\n"})
        self.assertEqual(rules(rep), [("DC4", "svc.t")])  # the entry still lists `name`, which the table no longer has
        self.assertIn("name", rep.findings[0]["msg"])
        rep = run(base, {"migrations/002.sql": "ALTER TABLE svc.t DROP COLUMN IF EXISTS org_idfr;\n"})
        self.assertEqual(sorted(f["rule"] for f in rep.findings), ["DC3", "DC4"])  # the placement key and a field are gone
        for keep in ("DROP INDEX idx_name", "DROP KEY k", "DROP FOREIGN KEY fk", "DROP PRIMARY KEY", "DROP CONSTRAINT c"):
            self.assertEqual(rules(run(base, {"migrations/002.sql": f"ALTER TABLE svc.t {keep};\n"})), [], keep)


# ── the third critic's round on #127 @cc48bc5: three MAJORs (relayed by GIT-01) ──────────────────────────────
LB_CFG = data(schema_paths=(("changelogs/**", "liquibase"),), default_schema="purpus_auth")
LB_BASE = {"declarations/data.yaml": LB_CFG}


def changelog(*changes):
    return "databaseChangeLog:\n" + "".join(f"  - {c}\n" for c in changes)


class CriticRound3(unittest.TestCase):
    def test_major1_referenced_changelog_files_are_read_wherever_they_live_even_under_excluded_paths(self):
        hidden = "CREATE TABLE purpus_auth.hidden (id INT);\n"
        cases = {
            "sqlFile": ({"changelogs/001.yaml": changelog("sqlFile:\n      path: ../docs/ddl.sql\n      relativeToChangelogFile: true"),
                         "docs/ddl.sql": hidden}, "purpus_auth.hidden"),
            "include": ({"changelogs/001.yaml": changelog("include:\n      file: ../tests/inc.yaml\n      relativeToChangelogFile: true"),
                         "tests/inc.yaml": changelog("createTable:\n      tableName: viainclude\n      columns:\n        - column:\n            name: id")},
                        "purpus_auth.viainclude"),
            "includeAll": ({"changelogs/001.yaml": changelog("includeAll:\n      path: ../docs/changes/\n      relativeToChangelogFile: true"),
                            "docs/changes/a.sql": "CREATE TABLE purpus_auth.viaall (id INT);\n"}, "purpus_auth.viaall"),
        }
        for name, (head, table) in cases.items():
            self.assertEqual(rules(run(LB_BASE, head, "block"), "error"), [("DC1", table)], name)

    def test_major2_a_symlink_under_schema_paths_is_an_error_in_block_mode_and_a_warning_in_report(self):
        base = {"declarations/data.yaml": data(), "docs/ddl.sql": "-- nothing yet\n"}
        head = {"migrations/002.sql": ("symlink", "../docs/ddl.sql"), "docs/ddl.sql": CREATE_T}
        block, report = run(base, head, "block"), run(base, head, "report")
        self.assertEqual([(f["rule"], f["level"]) for f in block.findings], [("DC9", "error")])
        self.assertIn("symlink", block.findings[0]["msg"])
        self.assertEqual([(f["rule"], f["level"]) for f in report.findings], [("DC9", "warning")])

    def test_major2_a_symlink_that_already_exists_is_still_refused(self):
        # the PR edits only the target (an excluded path); the schema file that points at it is untouched
        base = {"declarations/data.yaml": data(), "docs/ddl.sql": "-- nothing yet\n", "migrations/002.sql": ("symlink", "../docs/ddl.sql")}
        rep = run(base, {"docs/ddl.sql": CREATE_T}, "block")
        self.assertEqual([(f["rule"], f["level"]) for f in rep.findings], [("DC9", "error")])

    def test_major3_the_judge_step_needs_no_credentials_and_does_not_fetch(self):
        # checkout used persist-credentials: false and fetch-depth: 0, so origin/<base> is already here; a second, authenticated
        # `git fetch` would fail in a private repo. Prove the step works with an origin it cannot reach.
        with open(WORKFLOW, encoding="utf-8") as fh:
            judge = next(s["run"] for s in yaml.safe_load(fh)["jobs"]["declaration-gate"]["steps"] if s.get("name", "").startswith("Judge"))
        with tempfile.TemporaryDirectory() as tmp:
            work = make_repo({"declarations/data.yaml": data()}, {"migrations/001.sql": CREATE_T})
            sh(work, "git", "remote", "add", "origin", os.path.join(tmp, "unreachable.git"))
            sh(work, "git", "update-ref", "refs/remotes/origin/main", "main")  # what fetch-depth: 0 leaves behind
            src = os.path.join(work, ".declaration-gate-src")
            os.makedirs(os.path.join(src, "scripts")); os.makedirs(os.path.join(src, "declarations"))
            shutil.copy(os.path.join(ROOT, "scripts", "declaration_gate.py"), os.path.join(src, "scripts"))
            shutil.copy(SEED, os.path.join(src, "declarations"))
            env = {**os.environ, "GITHUB_WORKSPACE": work, "RUNNER_TEMP": tmp, "MODE": "report", "BASE_REF": "main",
                   "GITHUB_STEP_SUMMARY": os.path.join(tmp, "summary.md"), "GITHUB_REPOSITORY": "purpusgit/service_x"}
            r = subprocess.run(["bash", "-eo", "pipefail", "-c", judge], cwd=work, env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            with open(os.path.join(tmp, "summary.md")) as fh:
                self.assertIn("DC1", fh.read())


# ── the fourth critic's round on #127 @6196706: two MAJORs and two MINORs (relayed by GIT-01) ─────────────────
HID = "CREATE TABLE svc.hid (id INT);\n"
# each of these desyncs the comment masker and the live DDL after it would be swallowed
DESYNC = {
    "a dollar-quoted string holding /*": "SELECT $$ /* $$;\n",
    "--don't (no space after the dashes)": "--don't\nSELECT 'a /* b';\n",
    "a PostgreSQL string ending in a backslash": "SELECT 'C:\\';\nSELECT 'a /* b';\n",
    "a MySQL # comment with an apostrophe": "SELECT 1; # it's\nSELECT 'a /* b';\n",
}
NOTE = "found outside the comment-masked pass"


def copy_gate(dst):
    os.makedirs(os.path.join(dst, "scripts")); os.makedirs(os.path.join(dst, "declarations"))
    shutil.copy(os.path.join(ROOT, "scripts", "declaration_gate.py"), os.path.join(dst, "scripts"))
    shutil.copy(SEED, os.path.join(dst, "declarations"))


class CriticRound4(unittest.TestCase):
    def test_major1_a_desynced_comment_masker_cannot_hide_a_table(self):
        for name, prefix in DESYNC.items():
            rep = run({"declarations/data.yaml": data()}, {"migrations/001.sql": prefix + HID}, "block")
            self.assertEqual(rules(rep, "error"), [("DC1", "svc.hid")], name)
            self.assertIn("svc.hid", rep.unmasked, name)
            self.assertIn(NOTE, G.render(rep, "main"), name)

    def test_major1_a_hidden_column_is_found_too(self):
        base = dict(BASE_T, **with_entry(T_ENTRY))
        rep = run(base, {"migrations/002.sql": "SELECT $$ /* $$;\nALTER TABLE svc.t ADD COLUMN access_token_ct INT;\n"})
        self.assertEqual(rep.added["svc.t"], ["access_token_ct"])
        self.assertEqual(rules(rep), [("DC5", "svc.t"), ("DC6", "svc.t")])

    def test_major1_a_normal_file_gets_no_note(self):
        rep = run({"declarations/data.yaml": data()}, {"migrations/001.sql": "-- a header comment\n/* and a block */\n" + HID})
        self.assertEqual(rep.unmasked, [])
        self.assertNotIn(NOTE, G.render(rep, "main"))

    def test_major1_comments_inside_a_statement_add_no_garbage_columns_and_no_note(self):
        # a real file shape (chat 007): commas, parentheses and apostrophes inside the comments of a CREATE TABLE body
        body = ("CREATE TABLE svc.t (\n  id INT NOT NULL,   -- the id, stable (UUID), it's ours\n  org_idfr INT NULL, -- FK x.y WHERE type='a_type' (see 3, 4)\n"
                "  name VARCHAR(10)    -- 'audio' | 'video', denorm cache\n);\n")
        rep = run({"declarations/data.yaml": data()}, {"migrations/001.sql": body})
        self.assertEqual(rep.added["svc.t"], ["id", "name", "org_idfr"])
        self.assertEqual(rep.unmasked, [])

    def test_major1_a_column_hidden_mid_statement_by_a_desync_is_found(self):
        base = dict(BASE_T, **with_entry(T_ENTRY))
        sql = "--don't\nALTER TABLE svc.t ADD COLUMN a INT DEFAULT 'x /* y', ADD COLUMN access_token_ct INT;\n"
        rep = run(base, {"migrations/002.sql": sql})
        self.assertIn("access_token_ct", rep.added["svc.t"])
        self.assertEqual(rep.unmasked, ["svc.t"])

    def test_major2_missing_pyyaml_never_fails_report_mode_and_fails_block_mode_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "shadow"))
            with open(os.path.join(tmp, "shadow", "yaml.py"), "w") as fh:
                fh.write("raise ImportError('PyYAML is not installed here')\n")
            repo = make_repo(*NEW_T)
            for mode, rc in (("report", 0), ("block", 2)):
                r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "declaration_gate.py"), "--repo", repo, "--base-ref", "main",
                                    "--mode", mode, "--summary", os.path.join(tmp, "s.md")],
                                   env={**os.environ, "PYTHONPATH": os.path.join(tmp, "shadow")}, capture_output=True, text=True)
                self.assertEqual(r.returncode, rc, f"{mode}: {r.stderr[-300:]}")
                self.assertIn("PyYAML", r.stdout, mode)
                self.assertNotIn("Traceback", r.stderr, mode)

    def test_major2_the_workflow_steps_that_can_fail_for_the_environment_do_not_fail_report_mode(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            job = yaml.safe_load(fh)["jobs"]["declaration-gate"]
        expr = "${{ inputs.mode == 'report' }}"
        self.assertEqual(job.get("continue-on-error"), expr)  # block mode stays fail-closed: the expression is false there
        install = next(s for s in job["steps"] if s.get("name", "").startswith("Install PyYAML"))
        self.assertEqual(install.get("continue-on-error"), expr)

    def test_minor_a_gitlink_in_the_tree_does_not_misalign_the_reads_after_it(self):
        # a gitlink (mode 160000) to a commit that exists makes `cat-file --batch` print a whole commit; an unread body would
        # shift every later answer. Keep the gitlink in BOTH trees, with schema files after it.
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "r")
            os.makedirs(d); sh(d, "git", "init", "-q", "-b", "main")
            sh(d, "git", "config", "user.email", "t@example.com"); sh(d, "git", "config", "user.name", "t")
            with open(os.path.join(d, "README"), "w") as fh:
                fh.write("x\n")
            sh(d, "git", "add", "README"); sh(d, "git", "commit", "-q", "-m", "first")
            first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True, check=True).stdout.strip()
            os.makedirs(os.path.join(d, "declarations")); os.makedirs(os.path.join(d, "migrations"))
            with open(os.path.join(d, "declarations", "data.yaml"), "w") as fh:
                fh.write(data(schema_paths=(("migrations/*", "sql"),)))
            with open(os.path.join(d, "migrations", "zzz_base.sql"), "w") as fh:
                fh.write("CREATE TABLE svc.old (id INT);\n")
            sh(d, "git", "add", "declarations", "migrations/zzz_base.sql")
            sh(d, "git", "update-index", "--add", "--cacheinfo", f"160000,{first},migrations/aaa_sub")
            sh(d, "git", "commit", "-q", "-m", "base")
            sh(d, "git", "checkout", "-q", "-b", "pr")
            with open(os.path.join(d, "migrations", "zzz_pr.sql"), "w") as fh:
                fh.write("CREATE TABLE svc.new (id INT);\n")
            sh(d, "git", "add", "migrations/zzz_pr.sql")  # not -A: that would stage the gitlink's deletion
            sh(d, "git", "commit", "-q", "-m", "pr")
            ls = subprocess.run(["git", "ls-tree", "-r", "HEAD"], cwd=d, capture_output=True, text=True, check=True).stdout
            self.assertIn("160000 commit", ls)  # the gitlink really is in the head tree
            self.assertEqual(rules(G.judge(d, "main", "block")), [("DC1", "svc.new")])

    def test_minor_a_pr_that_owns_the_gates_checkout_path_is_refused_in_block_mode_and_cleared_in_report(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            steps = yaml.safe_load(fh)["jobs"]["declaration-gate"]["steps"]
        names = [s.get("name", "") for s in steps]
        guard = next(i for i, n in enumerate(names) if n.startswith("Refuse"))
        self.assertLess(guard, next(i for i, n in enumerate(names) if n.startswith("Fetch the gate script")))  # before the checkout
        for mode, rc in (("block", 1), ("report", 0)):
            with tempfile.TemporaryDirectory() as tmp:
                work, outside = os.path.join(tmp, "work"), os.path.join(tmp, "outside")
                os.makedirs(work); os.makedirs(outside)
                with open(os.path.join(outside, "keep.txt"), "w") as fh:
                    fh.write("not the PR's to touch")
                os.symlink(outside, os.path.join(work, ".declaration-gate-src"))
                r = subprocess.run(["bash", "-eo", "pipefail", "-c", steps[guard]["run"]], cwd=work, env={**os.environ, "MODE": mode},
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, rc, f"{mode}: {r.stdout[-200:]}{r.stderr[-200:]}")
                self.assertTrue(os.path.exists(os.path.join(outside, "keep.txt")), f"{mode}: the link's target was touched")
                self.assertEqual(os.path.lexists(os.path.join(work, ".declaration-gate-src")), mode == "block", mode)  # report removed the link


# ── the fifth critic's round on #127 @e80cfba: one MAJOR and two MINORs (relayed by GIT-01) ───────────────────
KNEX_CFG = data(schema_paths=(("migrations/*/*.js", "knex"),), default_schema="shop")


class CriticRound5(unittest.TestCase):
    def test_major_knex_with_schema_uses_that_schema(self):
        js = "exports.up = (knex) => knex.schema.withSchema('billing').createTable('invoices', (table) => {\n  table.increments('id');\n  table.string('card_token');\n});\n"
        rep, t = touches({"declarations/data.yaml": KNEX_CFG}, {"migrations/0.0.1/a.js": js})
        self.assertEqual(t, {"billing.invoices": (["create"], ["card_token", "id"])})

    def test_major_knex_table_is_the_alias_of_alter_table(self):
        js = "exports.up = (knex) => knex.schema.table('orders', (t) => {\n  t.string('note');\n  t.dropColumn('old');\n});\n"
        rep, t = touches({"declarations/data.yaml": KNEX_CFG}, {"migrations/0.0.1/a.js": js})
        self.assertEqual(t, {"shop.orders": (["alter"], ["note"])})
        js = "exports.up = (knex) => knex.schema.withSchema('billing').table('invoices', (t) => { t.string('api_token'); });\n"
        _, t = touches({"declarations/data.yaml": KNEX_CFG}, {"migrations/0.0.2/b.js": js})
        self.assertEqual(t, {"billing.invoices": (["alter"], ["api_token"])})

    def test_major_the_dc10_scan_sees_the_same_knex_calls_outside_schema_paths(self):
        base = {"declarations/data.yaml": KNEX_CFG}
        for call in ("knex.schema.table('t', (x) => { x.string('a'); })", "knex.schema.withSchema('s').createTable('t', (x) => {})",
                     "db.schema.alterTable('t', (x) => {})", "knex.schema.withSchema('s').table('t', (x) => {})"):
            rep = run(base, {"src/scripts/fix.js": f"await {call};\n"})
            self.assertEqual(rules(rep), [("DC10", "-")], call)
        self.assertEqual(rules(run(base, {"src/scripts/fix.js": "console.table(rows);\n"})), [])  # not the schema builder

    def test_minor_a_utf8_bom_does_not_hide_the_first_line(self):
        rep = run({"declarations/data.yaml": data()}, {"migrations/001.sql": "\ufeff" + CREATE_T}, "block")
        self.assertEqual(rules(rep, "error"), [("DC1", "svc.t")])
        # and a BOM on data.yaml itself is read, not rejected
        d = make_repo(NEW_T[0], {"migrations/001.sql": CREATE_T, "declarations/data.yaml": "\ufeff" + data([T_ENTRY])})
        self.assertEqual(rules(G.judge(d, "main", "block"), "error"), [])

    def test_minor_flow_style_and_quoted_key_liquibase_yaml_is_parsed_as_yaml(self):
        yml = ("databaseChangeLog:\n  - changeSet:\n      id: 1\n      author: a\n      changes:\n"
               "        - createTable: {tableName: flowed, columns: [{column: {name: id, type: int}}, {column: {name: api_key_ct, type: varbinary}}]}\n"
               "        - \"addColumn\": {\"tableName\": \"flowed\", \"columns\": [{\"column\": {\"name\": \"color\"}}]}\n"
               "        - 'renameColumn': {tableName: flowed, oldColumnName: color, newColumnName: shade}\n")
        rep, t = touches(LB_BASE, {"changelogs/001.yaml": yml})
        self.assertEqual(t, {"purpus_auth.flowed": (["alter", "create"], ["api_key_ct", "id", "shade"])})

    def test_minor_flow_style_references_are_followed_too(self):
        hidden = "CREATE TABLE purpus_auth.hidden (id INT);\n"
        cases = {
            "sqlFile": ({"changelogs/001.yaml": "databaseChangeLog:\n  - sqlFile: {path: ../docs/ddl.sql, relativeToChangelogFile: true}\n",
                         "docs/ddl.sql": hidden}, "purpus_auth.hidden"),
            "include": ({"changelogs/001.yaml": "databaseChangeLog:\n  - include: {file: ../tests/inc.yaml, relativeToChangelogFile: true}\n",
                         "tests/inc.yaml": "databaseChangeLog:\n  - createTable: {tableName: viainclude, columns: [{column: {name: id}}]}\n"},
                        "purpus_auth.viainclude"),
        }
        for name, (head, table) in cases.items():
            self.assertEqual(rules(run(LB_BASE, head, "block"), "error"), [("DC1", table)], name)


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

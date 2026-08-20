"""Zero-row schema DDL for the tutorial workloads, as test fixtures.

Each entry is the CREATE TABLE script an empty DuckDB catalog needs so a workload's
templates can be described (``DESCRIBE`` / ``describe_output``) without any data.

Provenance of the captured scripts (checked in so tests never generate or download):

* tpch: dumped from an sf0.01 database built by the tutorial's own generator
  (``tutorials.workloads.tpch.gen_tpc_h_data.generate_tpch_duckdb``, DuckDB dbgen).
* musicbrainz: the demo ``musicbrainz.duckdb`` schema dump, trimmed to the tables the
  tutorial queries read.
* stack: dumped from the demo's ``stack_ce.duckdb``.

clickbench and ceb re-export the DDL that already lives in the repo.
"""

from synnodb.workloads.dataset.gen_clickbench_data import CREATE_HITS_SQL
from tutorials.workloads.ceb.imdb_schema import imdb_schema

TPCH_SCHEMA_SQL = """
CREATE TABLE customer(c_custkey BIGINT NOT NULL, c_name VARCHAR NOT NULL, c_address VARCHAR NOT NULL, c_nationkey INTEGER NOT NULL, c_phone VARCHAR NOT NULL, c_acctbal DECIMAL(15,2) NOT NULL, c_mktsegment VARCHAR NOT NULL, c_comment VARCHAR NOT NULL);

CREATE TABLE lineitem(l_orderkey BIGINT NOT NULL, l_partkey BIGINT NOT NULL, l_suppkey BIGINT NOT NULL, l_linenumber BIGINT NOT NULL, l_quantity DECIMAL(15,2) NOT NULL, l_extendedprice DECIMAL(15,2) NOT NULL, l_discount DECIMAL(15,2) NOT NULL, l_tax DECIMAL(15,2) NOT NULL, l_returnflag VARCHAR NOT NULL, l_linestatus VARCHAR NOT NULL, l_shipdate DATE NOT NULL, l_commitdate DATE NOT NULL, l_receiptdate DATE NOT NULL, l_shipinstruct VARCHAR NOT NULL, l_shipmode VARCHAR NOT NULL, l_comment VARCHAR NOT NULL);

CREATE TABLE nation(n_nationkey INTEGER NOT NULL, n_name VARCHAR NOT NULL, n_regionkey INTEGER NOT NULL, n_comment VARCHAR NOT NULL);

CREATE TABLE orders(o_orderkey BIGINT NOT NULL, o_custkey BIGINT NOT NULL, o_orderstatus VARCHAR NOT NULL, o_totalprice DECIMAL(15,2) NOT NULL, o_orderdate DATE NOT NULL, o_orderpriority VARCHAR NOT NULL, o_clerk VARCHAR NOT NULL, o_shippriority INTEGER NOT NULL, o_comment VARCHAR NOT NULL);

CREATE TABLE part(p_partkey BIGINT NOT NULL, p_name VARCHAR NOT NULL, p_mfgr VARCHAR NOT NULL, p_brand VARCHAR NOT NULL, p_type VARCHAR NOT NULL, p_size INTEGER NOT NULL, p_container VARCHAR NOT NULL, p_retailprice DECIMAL(15,2) NOT NULL, p_comment VARCHAR NOT NULL);

CREATE TABLE partsupp(ps_partkey BIGINT NOT NULL, ps_suppkey BIGINT NOT NULL, ps_availqty BIGINT NOT NULL, ps_supplycost DECIMAL(15,2) NOT NULL, ps_comment VARCHAR NOT NULL);

CREATE TABLE region(r_regionkey INTEGER NOT NULL, r_name VARCHAR NOT NULL, r_comment VARCHAR NOT NULL);

CREATE TABLE supplier(s_suppkey BIGINT NOT NULL, s_name VARCHAR NOT NULL, s_address VARCHAR NOT NULL, s_nationkey INTEGER NOT NULL, s_phone VARCHAR NOT NULL, s_acctbal DECIMAL(15,2) NOT NULL, s_comment VARCHAR NOT NULL);
"""

MUSICBRAINZ_SCHEMA_SQL = """
CREATE TABLE "area" (
    "col_0" VARCHAR,
    "col_1" VARCHAR,
    "col_2" VARCHAR,
    "col_3" VARCHAR,
    "col_4" VARCHAR,
    "col_5" VARCHAR,
    "col_6" VARCHAR,
    "col_7" VARCHAR,
    "col_8" VARCHAR,
    "col_9" VARCHAR,
    "col_10" VARCHAR,
    "col_11" VARCHAR,
    "col_12" VARCHAR,
    "col_13" VARCHAR
);

CREATE TABLE "artist" (
    "col_0" VARCHAR,
    "col_1" VARCHAR,
    "col_2" VARCHAR,
    "col_3" VARCHAR,
    "col_4" VARCHAR,
    "col_5" VARCHAR,
    "col_6" VARCHAR,
    "col_7" VARCHAR,
    "col_8" VARCHAR,
    "col_9" VARCHAR,
    "col_10" VARCHAR,
    "col_11" VARCHAR,
    "col_12" VARCHAR,
    "col_13" VARCHAR,
    "col_14" VARCHAR,
    "col_15" VARCHAR,
    "col_16" VARCHAR,
    "col_17" VARCHAR,
    "col_18" VARCHAR
);

CREATE TABLE "artist_credit_name" (
    "artist_credit" INTEGER,
    "position" SMALLINT,
    "artist" INTEGER,
    "name" VARCHAR,
    "join_phrase" VARCHAR
);

CREATE TABLE "artist_type" (
    "id" INTEGER,
    "name" VARCHAR,
    "parent" INTEGER,
    "child_order" INTEGER,
    "description" VARCHAR,
    "gid" UUID
);

CREATE TABLE "label" (
    "col_0" VARCHAR,
    "col_1" VARCHAR,
    "col_2" VARCHAR,
    "col_3" VARCHAR,
    "col_4" VARCHAR,
    "col_5" VARCHAR,
    "col_6" VARCHAR,
    "col_7" VARCHAR,
    "col_8" VARCHAR,
    "col_9" VARCHAR,
    "col_10" VARCHAR,
    "col_11" VARCHAR,
    "col_12" VARCHAR,
    "col_13" VARCHAR,
    "col_14" VARCHAR,
    "col_15" VARCHAR
);

CREATE TABLE "label_type" (
    "id" INTEGER,
    "name" VARCHAR,
    "parent" INTEGER,
    "child_order" INTEGER,
    "description" VARCHAR,
    "gid" UUID
);

CREATE TABLE "medium" (
    "id" INTEGER,
    "release" INTEGER,
    "position" INTEGER,
    "format" INTEGER,
    "name" VARCHAR,
    "edits_pending" INTEGER,
    "last_updated" TIMESTAMP WITH TIME ZONE,
    "track_count" INTEGER,
    "gid" UUID
);

CREATE TABLE "medium_format" (
    "id" INTEGER,
    "name" VARCHAR,
    "parent" INTEGER,
    "child_order" INTEGER,
    "year" SMALLINT,
    "has_discids" BOOLEAN,
    "description" VARCHAR,
    "gid" UUID
);

CREATE TABLE "recording" (
    "id" INTEGER,
    "gid" UUID,
    "name" VARCHAR,
    "artist_credit" INTEGER,
    "length" INTEGER,
    "comment" VARCHAR,
    "edits_pending" INTEGER,
    "last_updated" TIMESTAMP WITH TIME ZONE,
    "video" BOOLEAN
);

CREATE TABLE "release" (
    "id" INTEGER,
    "gid" UUID,
    "name" VARCHAR,
    "artist_credit" INTEGER,
    "release_group" INTEGER,
    "status" INTEGER,
    "packaging" INTEGER,
    "language" INTEGER,
    "script" INTEGER,
    "barcode" VARCHAR,
    "comment" VARCHAR,
    "edits_pending" INTEGER,
    "quality" SMALLINT,
    "last_updated" TIMESTAMP WITH TIME ZONE
);

CREATE TABLE "release_country" (
    "release" INTEGER,
    "country" INTEGER,
    "date_year" SMALLINT,
    "date_month" SMALLINT,
    "date_day" SMALLINT
);

CREATE TABLE "release_group" (
    "id" INTEGER,
    "gid" UUID,
    "name" VARCHAR,
    "artist_credit" INTEGER,
    "type" INTEGER,
    "comment" VARCHAR,
    "edits_pending" INTEGER,
    "last_updated" TIMESTAMP WITH TIME ZONE
);

CREATE TABLE "release_group_meta" (
    "id" INTEGER,
    "release_count" INTEGER,
    "first_release_date_year" SMALLINT,
    "first_release_date_month" SMALLINT,
    "first_release_date_day" SMALLINT,
    "rating" SMALLINT,
    "rating_count" INTEGER
);

CREATE TABLE "release_group_primary_type" (
    "id" INTEGER,
    "name" VARCHAR,
    "parent" INTEGER,
    "child_order" INTEGER,
    "description" VARCHAR,
    "gid" UUID
);

CREATE TABLE "release_group_tag" (
    "release_group" INTEGER,
    "tag" INTEGER,
    "count" INTEGER,
    "last_updated" TIMESTAMP WITH TIME ZONE
);

CREATE TABLE "release_label" (
    "id" INTEGER,
    "release" INTEGER,
    "label" INTEGER,
    "catalog_number" VARCHAR,
    "last_updated" TIMESTAMP WITH TIME ZONE
);

CREATE TABLE "tag" (
    "id" INTEGER,
    "name" VARCHAR,
    "ref_count" INTEGER
);
"""

STACK_SCHEMA_SQL = """
CREATE TABLE account(id INTEGER, display_name VARCHAR, "location" VARCHAR, about_me VARCHAR, website_url VARCHAR);

CREATE TABLE answer(id INTEGER, site_id INTEGER, question_id INTEGER, creation_date TIMESTAMP, deletion_date TIMESTAMP, score INTEGER, view_count INTEGER, body VARCHAR, owner_user_id INTEGER, last_editor_id INTEGER, last_edit_date TIMESTAMP, last_activity_date TIMESTAMP, title VARCHAR);

CREATE TABLE badge(site_id INTEGER, user_id INTEGER, "name" VARCHAR, date TIMESTAMP);

CREATE TABLE "comment"(id INTEGER, site_id INTEGER, post_id INTEGER, user_id INTEGER, score INTEGER, body VARCHAR, date TIMESTAMP);

CREATE TABLE post_link(site_id INTEGER, post_id_from INTEGER, post_id_to INTEGER, link_type INTEGER, date TIMESTAMP);

CREATE TABLE question(id INTEGER, site_id INTEGER, accepted_answer_id INTEGER, creation_date TIMESTAMP, deletion_date TIMESTAMP, score INTEGER, view_count INTEGER, body VARCHAR, owner_user_id INTEGER, last_editor_id INTEGER, last_edit_date TIMESTAMP, last_activity_date TIMESTAMP, title VARCHAR, favorite_count INTEGER, closed_date TIMESTAMP, tagstring VARCHAR);

CREATE TABLE site(site_id INTEGER, site_name VARCHAR);

CREATE TABLE so_user(id INTEGER, site_id INTEGER, reputation INTEGER, creation_date TIMESTAMP, last_access_date TIMESTAMP, upvotes INTEGER, downvotes INTEGER, account_id INTEGER);

CREATE TABLE tag(id INTEGER, site_id INTEGER, "name" VARCHAR);

CREATE TABLE tag_question(question_id INTEGER, tag_id INTEGER, site_id INTEGER);
"""

WORKLOAD_SCHEMAS: dict[str, str] = {
    "tpch": TPCH_SCHEMA_SQL,
    "clickbench": CREATE_HITS_SQL,
    "ceb": imdb_schema,
    "musicbrainz": MUSICBRAINZ_SCHEMA_SQL,
    "stack": STACK_SCHEMA_SQL,
}


def schema_statements(workload: str) -> list[str]:
    """The workload's DDL split into statements. Comment lines are dropped first; none of
    the scripts embed a semicolon inside a literal."""
    sql_only = "\n".join(
        ln
        for ln in WORKLOAD_SCHEMAS[workload].splitlines()
        if not ln.strip().startswith("--")
    )
    return [part.strip() + ";" for part in sql_only.split(";") if part.strip()]

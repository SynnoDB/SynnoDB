imdb_schema = """
CREATE TABLE aka_name (
    id               INTEGER PRIMARY KEY,
    person_id        INTEGER NOT NULL,
    "name"           VARCHAR,
    imdb_index       VARCHAR,
    name_pcode_cf    VARCHAR,
    name_pcode_nf    VARCHAR,
    surname_pcode    VARCHAR,
    md5sum           VARCHAR
);

CREATE TABLE aka_title (
    id               INTEGER PRIMARY KEY,
    movie_id         INTEGER NOT NULL,
    title            VARCHAR,
    imdb_index       VARCHAR,
    kind_id          INTEGER NOT NULL,
    production_year  INTEGER,
    phonetic_code    VARCHAR,
    episode_of_id    INTEGER,
    season_nr        INTEGER,
    episode_nr       INTEGER,
    note             VARCHAR,
    md5sum           VARCHAR
);

CREATE TABLE cast_info (
    id               INTEGER PRIMARY KEY,
    person_id        INTEGER NOT NULL,
    movie_id         INTEGER NOT NULL,
    person_role_id   INTEGER,
    note             VARCHAR,
    nr_order         INTEGER,
    role_id          INTEGER NOT NULL
);

CREATE TABLE char_name (
    id               INTEGER PRIMARY KEY,
    "name"           VARCHAR NOT NULL,
    imdb_index       VARCHAR,
    imdb_id          INTEGER,
    name_pcode_nf    VARCHAR,
    surname_pcode    VARCHAR,
    md5sum           VARCHAR
);

CREATE TABLE comp_cast_type (
    id               INTEGER PRIMARY KEY,
    kind             VARCHAR NOT NULL
);

CREATE TABLE company_name (
    id               INTEGER PRIMARY KEY,
    "name"           VARCHAR NOT NULL,
    country_code     VARCHAR,
    imdb_id          INTEGER,
    name_pcode_nf    VARCHAR,
    name_pcode_sf    VARCHAR,
    md5sum           VARCHAR
);

CREATE TABLE company_type (
    id               INTEGER PRIMARY KEY,
    kind             VARCHAR
);

CREATE TABLE complete_cast (
    id               INTEGER PRIMARY KEY,
    movie_id         INTEGER,
    subject_id       INTEGER NOT NULL,
    status_id        INTEGER NOT NULL
);

CREATE TABLE info_type (
    id               INTEGER PRIMARY KEY,
    info             VARCHAR NOT NULL
);

CREATE TABLE keyword (
    id               INTEGER PRIMARY KEY,
    keyword          VARCHAR NOT NULL,
    phonetic_code    VARCHAR
);

CREATE TABLE kind_type (
    id               INTEGER PRIMARY KEY,
    kind             VARCHAR
);

CREATE TABLE link_type (
    id               INTEGER PRIMARY KEY,
    link             VARCHAR NOT NULL
);

CREATE TABLE movie_companies (
    id               INTEGER PRIMARY KEY,
    movie_id         INTEGER NOT NULL,
    company_id       INTEGER NOT NULL,
    company_type_id  INTEGER NOT NULL,
    note             VARCHAR
);

CREATE TABLE movie_info (
    id               INTEGER PRIMARY KEY,
    movie_id         INTEGER NOT NULL,
    info_type_id     INTEGER NOT NULL,
    info             VARCHAR NOT NULL,
    note             VARCHAR
);

CREATE TABLE movie_info_idx (
    id               INTEGER PRIMARY KEY,
    movie_id         INTEGER NOT NULL,
    info_type_id     INTEGER NOT NULL,
    info             VARCHAR NOT NULL,
    note             VARCHAR
);

CREATE TABLE movie_keyword (
    id               INTEGER PRIMARY KEY,
    movie_id         INTEGER NOT NULL,
    keyword_id       INTEGER NOT NULL
);

CREATE TABLE movie_link (
    id               INTEGER PRIMARY KEY,
    movie_id         INTEGER NOT NULL,
    linked_movie_id  INTEGER NOT NULL,
    link_type_id     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS "name" (
    id               INTEGER PRIMARY KEY,
    "name"           VARCHAR NOT NULL,
    imdb_index       VARCHAR,
    imdb_id          INTEGER,
    gender           VARCHAR,
    name_pcode_cf    VARCHAR,
    name_pcode_nf    VARCHAR,
    surname_pcode    VARCHAR,
    md5sum           VARCHAR
);

CREATE TABLE person_info (
    id               INTEGER PRIMARY KEY,
    person_id        INTEGER NOT NULL,
    info_type_id     INTEGER NOT NULL,
    info             VARCHAR NOT NULL,
    note             VARCHAR
);

CREATE TABLE role_type (
    id               INTEGER PRIMARY KEY,
    "role"           VARCHAR NOT NULL
);

CREATE TABLE title (
    id               INTEGER PRIMARY KEY,
    title            VARCHAR NOT NULL,
    imdb_index       VARCHAR,
    kind_id          INTEGER NOT NULL,
    production_year  INTEGER,
    imdb_id          INTEGER,
    phonetic_code    VARCHAR,
    episode_of_id    INTEGER,
    season_nr        INTEGER,
    episode_nr       INTEGER,
    series_years     VARCHAR,
    md5sum           VARCHAR
);
"""

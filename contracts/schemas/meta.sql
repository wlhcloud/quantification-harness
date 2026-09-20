-- meta.db schema (v2)
-- Owned by the Python side (quant-sync + quant-engine). Replaces v1 Node core.db.
-- Dropped vs core.db: plugin_runs (Node-specific, unused).
-- All tables identical to core.db otherwise, so the Phase 1 migration is a straight copy.

CREATE TABLE IF NOT EXISTS data_source_configs (
  id         TEXT PRIMARY KEY,
  label      TEXT NOT NULL,
  protocol   TEXT NOT NULL CHECK (protocol IN ('x-api-key', 'official')),
  base_url   TEXT NOT NULL,
  token      TEXT NOT NULL DEFAULT '',
  enabled    INTEGER NOT NULL DEFAULT 1,
  priority   INTEGER NOT NULL DEFAULT 100,
  timeout_ms INTEGER NOT NULL DEFAULT 15000,
  notes      TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_source_routes (
  tool_id            TEXT PRIMARY KEY,
  label              TEXT NOT NULL,
  primary_source_id  TEXT NOT NULL,
  fallback_source_id TEXT,
  updated_at         TEXT NOT NULL,
  FOREIGN KEY (primary_source_id)  REFERENCES data_source_configs(id),
  FOREIGN KEY (fallback_source_id) REFERENCES data_source_configs(id)
);

CREATE TABLE IF NOT EXISTS datasets (
  id         TEXT PRIMARY KEY,
  provider   TEXT NOT NULL,
  frequency  TEXT NOT NULL,
  latest_at  TEXT,
  row_count  INTEGER NOT NULL DEFAULT 0,
  status     TEXT NOT NULL CHECK (status IN ('ready', 'stale', 'missing', 'error')),
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
  id                  TEXT PRIMARY KEY,
  kind                TEXT NOT NULL,
  status              TEXT NOT NULL CHECK (status IN ('running', 'complete', 'error', 'stopped')),
  parameters_json     TEXT NOT NULL DEFAULT '{}',
  total               INTEGER NOT NULL DEFAULT 0,
  completed           INTEGER NOT NULL DEFAULT 0,
  failed              INTEGER NOT NULL DEFAULT 0,
  skipped             INTEGER NOT NULL DEFAULT 0,
  current_item        TEXT,
  started_at          TEXT NOT NULL,
  finished_at         TEXT,
  error               TEXT,
  trigger_source      TEXT NOT NULL DEFAULT 'web',
  notification_status TEXT
);

CREATE TABLE IF NOT EXISTS dictionary_datasets (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  category    TEXT NOT NULL,
  frequency   TEXT NOT NULL,
  description TEXT,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dictionary_fields (
  dataset_id    TEXT NOT NULL,
  field_name    TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  data_type     TEXT NOT NULL,
  unit          TEXT,
  nullable      INTEGER NOT NULL DEFAULT 1,
  is_primary_key INTEGER NOT NULL DEFAULT 0,
  description   TEXT,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (dataset_id, field_name)
);

CREATE TABLE IF NOT EXISTS dictionary_mappings (
  dataset_id          TEXT NOT NULL,
  source_id           TEXT NOT NULL,
  source_field        TEXT NOT NULL,
  standard_field      TEXT NOT NULL,
  transform_expression TEXT,
  enabled             INTEGER NOT NULL DEFAULT 1,
  updated_at          TEXT NOT NULL,
  PRIMARY KEY (dataset_id, source_id, source_field)
);

CREATE TABLE IF NOT EXISTS dictionary_types (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT,
  is_system   INTEGER NOT NULL DEFAULT 0,
  enabled     INTEGER NOT NULL DEFAULT 1,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dictionary_items (
  dictionary_code TEXT NOT NULL,
  item_code       TEXT NOT NULL,
  item_name       TEXT NOT NULL,
  sort_order      INTEGER NOT NULL DEFAULT 100,
  is_system       INTEGER NOT NULL DEFAULT 0,
  enabled         INTEGER NOT NULL DEFAULT 1,
  description     TEXT,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (dictionary_code, item_code)
);

PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE kv_store ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Enable all access for anon" ON kv_store
    FOR ALL USING (true) WITH CHECK (true);
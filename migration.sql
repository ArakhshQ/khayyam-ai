-- Only needed if the app's database role cannot create tables/columns at startup.
CREATE TABLE IF NOT EXISTS call_requests (
  id SERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES users(id),
  name VARCHAR(120) NOT NULL,
  phone VARCHAR(40) NOT NULL,
  plan_kind VARCHAR(10) NOT NULL DEFAULT 'chat',
  plan VARCHAR(20) NOT NULL,
  months INTEGER NOT NULL DEFAULT 1,
  note VARCHAR(500),
  status VARCHAR(12) NOT NULL DEFAULT 'new',
  admin_note VARCHAR(500),
  created_at TIMESTAMP DEFAULT now(),
  handled_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_call_requests_user ON call_requests(user_id);
CREATE INDEX IF NOT EXISTS ix_call_requests_status ON call_requests(status);
CREATE INDEX IF NOT EXISTS ix_call_requests_created ON call_requests(created_at);

CREATE TABLE IF NOT EXISTS plan_events (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  plan_kind VARCHAR(10) NOT NULL,
  plan VARCHAR(20) NOT NULL,
  event VARCHAR(12) NOT NULL,
  expires_at TIMESTAMP,
  actor VARCHAR(60),
  created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_plan_events_user ON plan_events(user_id);
CREATE INDEX IF NOT EXISTS ix_plan_events_created ON plan_events(created_at);

-- If call_requests already existed from an earlier build, CREATE TABLE above is skipped and the
-- columns below may be missing (the app also adds them itself at startup). Safe to run any time.
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS plan_kind VARCHAR(10) DEFAULT 'chat';
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS plan VARCHAR(20);
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS months INTEGER DEFAULT 1;
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS note VARCHAR(500);
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS status VARCHAR(12) DEFAULT 'new';
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS admin_note VARCHAR(500);
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now();
ALTER TABLE call_requests ADD COLUMN IF NOT EXISTS handled_at TIMESTAMP;

ALTER TABLE api_usage_requests ADD COLUMN IF NOT EXISTS intent VARCHAR(24);
ALTER TABLE api_usage_requests ALTER COLUMN intent TYPE VARCHAR(24);
ALTER TABLE api_usage_requests ADD COLUMN IF NOT EXISTS total_ms INTEGER;
ALTER TABLE api_usage_requests ADD COLUMN IF NOT EXISTS db_ms INTEGER;
ALTER TABLE api_usage_requests ADD COLUMN IF NOT EXISTS db_queries INTEGER;
ALTER TABLE api_usage_requests ADD COLUMN IF NOT EXISTS latency_ms INTEGER;
ALTER TABLE api_usage_requests ADD COLUMN IF NOT EXISTS overhead_tokens INTEGER;
CREATE INDEX IF NOT EXISTS ix_api_created ON api_usage_requests(created_at);

UPDATE users SET email_verified = TRUE WHERE is_verified = TRUE AND (email_verified IS NULL OR email_verified = FALSE);

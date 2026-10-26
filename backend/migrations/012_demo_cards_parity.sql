-- ============================================================
-- 012_demo_cards_parity.sql
--
-- Brings demo_cards up to the same shape as cards so the public demo page
-- can run the real profit math on it.
--
-- demo_cards has been around since 002 (anyone can read it, nobody can write
-- it) but it never got migrations 007-010, so it was missing every cost and
-- proceeds column profit.py needs. Without those, a demo card can only show a
-- purchase price and a sale price, which is the exact gross-profit bug the
-- whole app exists to fix.
--
-- What this adds:
--   1. The 007 cost/proceeds columns, plus serial from 010.
--   2. grading_cost as a plain column. The real app adds up
--      grading_submissions per card, but that table is per-user and has no
--      demo twin. Baking the total into one column lets the demo endpoint
--      hand profit.py the same number without a second table.
--   3. The CHECK constraints that cards already has. 005 flagged that
--      demo_cards had zero constraints while 003 treated it as a faithful
--      mirror, so it accepted values cards rejects. Closing that here.
--   4. Explicit GRANTs. Supabase is changing the default on 2026-10-30 so new
--      tables in public are NOT automatically readable by anon/authenticated.
--      demo_cards predates that, but the grant is written out so this file
--      says what the access actually is instead of relying on a default.
--
-- What this deliberately does NOT add:
--   user_id, buyer_name, notes, hold_thesis, ebay_comp_url.
--   Those are either private or strategy, and the demo should not be able to
--   leak them even if the API asked for them.
--
-- Safe to run more than once.
-- ============================================================

-- --- 1. Columns the profit math needs ---
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS serial             text;
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS shipping_in        numeric(12,2);
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS purchase_tax       numeric(12,2);
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS other_costs        numeric(12,2);
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS shipping_out       numeric(12,2);
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS platform_fees      numeric(12,2);
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS shipping_collected numeric(12,2);
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS sale_channel       text;
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS position_type      text;
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS lane               text;
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS est_market_value   numeric(12,2);
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS updated_at         timestamptz NOT NULL DEFAULT now();

-- --- 2. Grading total, flattened ---
-- The real app joins grading_submissions. The demo has no such table, so the
-- snapshot script adds up the submissions per card and writes the total here.
-- Default 0 (not null) so profit.py never sees a missing grading cost and
-- quietly treats it as free.
ALTER TABLE demo_cards ADD COLUMN IF NOT EXISTS grading_cost numeric(12,2) NOT NULL DEFAULT 0;

-- --- 3. The constraints cards already has ---
-- Same allowed values as 004 and 007, lowercase. Without these, demo_cards
-- would accept a status the real table rejects, and the demo would show a
-- state the app can't actually produce.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'demo_cards_status_check') THEN
    ALTER TABLE demo_cards ADD CONSTRAINT demo_cards_status_check
      CHECK (status IN ('in_hand','listed','sold','at_grading','in_transit','traded_away'));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'demo_cards_position_type_check') THEN
    ALTER TABLE demo_cards ADD CONSTRAINT demo_cards_position_type_check
      CHECK (position_type IS NULL OR position_type IN ('flip','hold'));
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'demo_cards_serial_check') THEN
    -- Same rule as 010: NULL means not numbered, but '' is not allowed, so a
    -- blank box can't masquerade as a failed read.
    ALTER TABLE demo_cards ADD CONSTRAINT demo_cards_serial_check
      CHECK (serial IS NULL OR length(btrim(serial)) > 0);
  END IF;
END $$;

-- --- 4. Access, written out instead of assumed ---
-- Read-only to the world. There is no INSERT/UPDATE/DELETE policy, so the
-- anon and authenticated roles cannot write even though they can select.
-- The snapshot script loads it with the service role key, which skips RLS.
ALTER TABLE demo_cards ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "demo_public_read" ON demo_cards;
CREATE POLICY "demo_public_read" ON demo_cards FOR SELECT TO anon, authenticated
  USING (true);

-- Required from 2026-10-30. Table-level SELECT only; no INSERT/UPDATE/DELETE.
GRANT SELECT ON TABLE demo_cards TO anon, authenticated;
REVOKE INSERT, UPDATE, DELETE ON TABLE demo_cards FROM anon, authenticated;

COMMENT ON TABLE demo_cards IS
  'Public read-only snapshot powering the portfolio demo. Loaded by '
  'backend/demo_snapshot.py with scaled dollar figures. Contains no user_id '
  'and no private fields. Never joined to the real cards table.';

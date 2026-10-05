-- ============================================================
-- 002_enable_rls.sql
-- Turns on Row Level Security for every table.
--
-- I built the 6 tables right in the Supabase dashboard (cards,
-- incoming_shipments, grading_submissions, trades, trade_items, demo_cards).
-- This turns on RLS and adds owner policies so the database itself makes sure
-- each user only sees their own rows, no matter what the API does.
--
-- Safe to run more than once. Every policy gets dropped first if it exists.
-- ============================================================

-- cards.user_id should never be empty (the other tables already require it).
-- Default it to the logged in user.
ALTER TABLE cards ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE cards ALTER COLUMN user_id SET DEFAULT auth.uid();

-- --- Tables with a user_id: you can only see and change your own rows ---
ALTER TABLE cards ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "cards_owner" ON cards;
CREATE POLICY "cards_owner" ON cards FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

ALTER TABLE incoming_shipments ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "incoming_owner" ON incoming_shipments;
CREATE POLICY "incoming_owner" ON incoming_shipments FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

ALTER TABLE grading_submissions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "grading_owner" ON grading_submissions;
CREATE POLICY "grading_owner" ON grading_submissions FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

ALTER TABLE trades ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "trades_owner" ON trades;
CREATE POLICY "trades_owner" ON trades FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

-- --- trade_items: no user_id, so it goes by who owns the trade ---
ALTER TABLE trade_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "trade_items_via_trade" ON trade_items;
CREATE POLICY "trade_items_via_trade" ON trade_items FOR ALL TO authenticated
  USING (EXISTS (SELECT 1 FROM trades t
                 WHERE t.id = trade_items.trade_id AND t.user_id = auth.uid()))
  WITH CHECK (EXISTS (SELECT 1 FROM trades t
                      WHERE t.id = trade_items.trade_id AND t.user_id = auth.uid()));

-- --- demo_cards: sample data anyone can see ---
-- No user_id column. Anyone can read it, even without logging in, but nobody
-- can change it since there's no insert/update/delete policy. Add data through
-- the SQL editor, which skips RLS.
ALTER TABLE demo_cards ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "demo_public_read" ON demo_cards;
CREATE POLICY "demo_public_read" ON demo_cards FOR SELECT TO anon, authenticated
  USING (true);

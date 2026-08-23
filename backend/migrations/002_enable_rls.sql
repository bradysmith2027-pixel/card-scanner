-- ============================================================
-- 002_enable_rls.sql
-- Dreamboat Slabs — enable Row Level Security on all tables.
--
-- Context: the 6-table schema was built directly in the Supabase UI
-- (cards, incoming_shipments, grading_submissions, trades, trade_items,
-- demo_cards). This migration turns on RLS + owner policies so the DB
-- itself enforces per-user isolation (JWT / auth.uid()), independent of
-- any WHERE clause in the API layer.
--
-- Idempotent: every policy is dropped-if-exists before create, and the
-- ENABLE/ALTER lines are safe to re-run, so this can be run repeatedly
-- (e.g. after a partial earlier attempt) without erroring.
-- ============================================================

-- Fix: cards.user_id must always be set (the other user-scoped tables
-- already enforce NON-NULL). Default it to the logged-in user.
ALTER TABLE cards ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE cards ALTER COLUMN user_id SET DEFAULT auth.uid();

-- --- User-scoped tables: owner-only, full access to own rows ---
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

-- --- trade_items: no user_id — inherit ownership from parent trade ---
ALTER TABLE trade_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "trade_items_via_trade" ON trade_items;
CREATE POLICY "trade_items_via_trade" ON trade_items FOR ALL TO authenticated
  USING (EXISTS (SELECT 1 FROM trades t
                 WHERE t.id = trade_items.trade_id AND t.user_id = auth.uid()))
  WITH CHECK (EXISTS (SELECT 1 FROM trades t
                      WHERE t.id = trade_items.trade_id AND t.user_id = auth.uid()));

-- --- demo_cards: shared fixture data, read-only to everyone ---
-- No user_id column. World-readable (incl. anonymous demo visitors),
-- write-locked (no INSERT/UPDATE/DELETE policy => RLS denies writes).
-- Seed via SQL editor / service_role, which bypasses RLS.
ALTER TABLE demo_cards ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "demo_public_read" ON demo_cards;
CREATE POLICY "demo_public_read" ON demo_cards FOR SELECT TO anon, authenticated
  USING (true);

-- ============================================================
-- 008_trade_carry_over_basis.sql
-- Dreamboat Slabs — make a trade an accounting event instead of a fake sale.
--
-- WHY THIS EXISTS (2026-09-14):
--   The `trades` and `trade_items` tables have existed since the original
--   schema, but NO application code has ever touched them. There is no
--   trades router, no endpoint, no UI. Verified 2026-09-14: the only mention
--   of "trade" anywhere in app/ is a comment in profit.py.
--
--   Meanwhile the old spreadsheet recorded trades as BREAK-EVEN SALES. That
--   single shortcut corrupts four metrics at once:
--
--     ROI           — a fake $0-profit row averages into every return figure
--     sell-through  — a trade counts as a sale that never happened
--     hold time     — the clock stops on a card effectively still held
--     cost basis    — the received card arrives from nowhere, with basis $0
--
--   The last one is the expensive one. A $0-basis card reports its ENTIRE
--   eventual sale price as profit. Trade into a card, flip it, and the app
--   claims 100% margin on money actually spent months earlier.
--
--   🔴 This migration must land BEFORE the historical spreadsheet import.
--   Importing first writes the distortion into the permanent record, where
--   it can never again be distinguished from real data.
--
-- THE MODEL — carry-over basis
--   A trade realizes nothing. Basis moves:
--
--     total_basis = SUM(all_in_cost of cards GIVEN) + cash_boot
--
--   then allocates across the cards RECEIVED pro-rata by estimated value.
--   `app/trade_basis.py` is the single definition, the same way profit.py is
--   for profit. Nothing else is permitted to do this math.
--
-- ⚠️ WHY est_value IS NOT A PRICE
--   `trade_items.est_value` is an ALLOCATION WEIGHT, not a valuation claim
--   and not a sale price. It exists only to apportion basis. Never surface
--   it as "what the card is worth" and never feed it into profit.
-- ============================================================


-- ============================================================
-- trades — cash boot and the one case that realizes income
-- ============================================================

ALTER TABLE trades
  ADD COLUMN IF NOT EXISTS cash_boot numeric NOT NULL DEFAULT 0;

COMMENT ON COLUMN trades.cash_boot IS
  'Cash changing hands alongside the cards, SIGNED from Brady''s point of '
  'view: POSITIVE = Brady paid cash (increases basis acquired), NEGATIVE = '
  'Brady received cash (decreases it). Not a fee and not revenue.';

ALTER TABLE trades
  ADD COLUMN IF NOT EXISTS realized_gain numeric NOT NULL DEFAULT 0;

COMMENT ON COLUMN trades.realized_gain IS
  'Normally 0 — a trade realizes nothing. Non-zero ONLY when cash received '
  'exceeds the basis given up: basis cannot go negative, so the excess is '
  'real income and is recorded here rather than silently clamped away.';

ALTER TABLE trades
  DROP CONSTRAINT IF EXISTS trades_realized_gain_nonneg;
ALTER TABLE trades
  ADD CONSTRAINT trades_realized_gain_nonneg CHECK (realized_gain >= 0);


-- ============================================================
-- trade_items — allocation weight and the resulting basis
-- ============================================================

ALTER TABLE trade_items
  ADD COLUMN IF NOT EXISTS est_value numeric;

COMMENT ON COLUMN trade_items.est_value IS
  'ALLOCATION WEIGHT ONLY — estimated market value at trade time, used to '
  'apportion carried basis across multiple received cards. NOT a price, NOT '
  'a valuation, and never an input to profit. NULL on every row of a trade '
  'forces an even split, which destroys per-card ROI — the API warns.';

ALTER TABLE trade_items
  ADD COLUMN IF NOT EXISTS allocated_basis numeric;

COMMENT ON COLUMN trade_items.allocated_basis IS
  'The basis this line item carried, written at trade time by '
  'app/trade_basis.py. Audit trail: lets a later reader reconstruct how a '
  'received card got its purchase_price without re-running the allocation.';

ALTER TABLE trade_items
  DROP CONSTRAINT IF EXISTS trade_items_est_value_nonneg;
ALTER TABLE trade_items
  ADD CONSTRAINT trade_items_est_value_nonneg
  CHECK (est_value IS NULL OR est_value >= 0);

ALTER TABLE trade_items
  DROP CONSTRAINT IF EXISTS trade_items_allocated_basis_nonneg;
ALTER TABLE trade_items
  ADD CONSTRAINT trade_items_allocated_basis_nonneg
  CHECK (allocated_basis IS NULL OR allocated_basis >= 0);


-- ============================================================
-- Indexes — the lookups the trade lineage actually needs
-- ============================================================

-- "How did this card get here / where did it go?" walks trade_items by card.
CREATE INDEX IF NOT EXISTS idx_trade_items_card_id
  ON trade_items (card_id);

CREATE INDEX IF NOT EXISTS idx_trade_items_trade_id
  ON trade_items (trade_id);


-- ============================================================
-- VERIFY (run after applying)
-- ============================================================
--
--   SELECT column_name, data_type, column_default, is_nullable
--   FROM information_schema.columns
--   WHERE table_name IN ('trades','trade_items')
--   ORDER BY table_name, ordinal_position;
--
--   -- Every trade must balance: basis out + boot = basis in.
--   -- This should return ZERO rows. Any row is a broken trade.
--   SELECT t.id,
--          t.cash_boot,
--          SUM(ti.allocated_basis) FILTER (WHERE ti.direction = 'received')
--            AS basis_in
--   FROM trades t
--   JOIN trade_items ti ON ti.trade_id = t.id
--   GROUP BY t.id, t.cash_boot
--   HAVING SUM(ti.allocated_basis) FILTER (WHERE ti.direction = 'received')
--          IS NULL;

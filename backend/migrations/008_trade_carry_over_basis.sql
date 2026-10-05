-- ============================================================
-- 008_trade_carry_over_basis.sql
-- Makes a trade move the cost from one card to another instead of counting
-- as a sale.
--
-- The trades and trade_items tables have been there since the start, but no
-- code ever used them. No router, no endpoint, no screen.
--
-- In my old spreadsheet I logged trades as sales that broke even. That one
-- shortcut messed up four things:
--
--     ROI           - a fake $0 profit sale gets averaged into everything
--     sell-through  - counts a sale that never happened
--     hold time     - stops the clock on a card I basically still have
--     cost          - the card I got back shows up with a $0 cost
--
-- The last one is the big one. A card with a $0 cost shows its whole sale
-- price as profit, even though I really paid for it months before.
--
-- This had to go in before importing my spreadsheet. Otherwise the wrong
-- numbers would get saved and I couldn't tell them apart from the real ones
-- later.
--
-- How it works:
--   A trade doesn't make or lose money. The cost moves:
--
--     total_basis = SUM(all_in_cost of cards GIVEN) + cash_boot
--
--   and gets split across the cards I GOT based on what each is worth. The
--   math is only in app/trade_basis.py, same as profit.py for profit.
--
-- est_value isn't a price. It's only used to split up the cost. Don't show it
-- as what the card is worth and don't use it in profit.
-- ============================================================


-- ============================================================
-- trades: cash added or received, and the one case where it's profit
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
-- trade_items: the value used to split the cost, and the cost each card got
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
-- Indexes for looking up trade history
-- ============================================================

-- "Where did this card come from / where did it go?" looks up trade_items by card.
CREATE INDEX IF NOT EXISTS idx_trade_items_card_id
  ON trade_items (card_id);

CREATE INDEX IF NOT EXISTS idx_trade_items_trade_id
  ON trade_items (trade_id);


-- ============================================================
-- Check it worked (run after applying)
-- ============================================================
--
--   SELECT column_name, data_type, column_default, is_nullable
--   FROM information_schema.columns
--   WHERE table_name IN ('trades','trade_items')
--   ORDER BY table_name, ordinal_position;
--
--   -- Every trade should balance: cost out + cash = cost in.
--   -- This should return nothing. Any row is a trade that's off.
--   SELECT t.id,
--          t.cash_boot,
--          SUM(ti.allocated_basis) FILTER (WHERE ti.direction = 'received')
--            AS basis_in
--   FROM trades t
--   JOIN trade_items ti ON ti.trade_id = t.id
--   GROUP BY t.id, t.cash_boot
--   HAVING SUM(ti.allocated_basis) FILTER (WHERE ti.direction = 'received')
--          IS NULL;

-- ============================================================
-- 009_purchase_lots.sql
-- Dreamboat Slabs — one payment, many cards.
--
-- WHY THIS EXISTS (2026-09-14):
--   Verified against the codebase on 2026-09-14: there is NO purchase_lots
--   table and NO lot_id column anywhere in the backend. The app cannot record
--   a bundle buy, so every card from a lot gets a hand-guessed cost basis.
--
--   That is the most expensive gap in the system, because of what Brady's own
--   sales history says:
--
--     Cash sales >= $300 (single cards, bought near comp):
--        7 cards, $4,048 cost, $232 net  ->   5.7% ROI
--     Sub-$300 (Discord LOTS at ~$1.27/card):
--       28 cards, $1,294 cost, $504 net  ->  39% ROI
--
--   His best channel by a factor of seven is the one the system cannot record.
--   Deploying T1 into lots without this means the gates (>=60% sell-through,
--   >=20% net margin) get measured on invented numbers.
--
-- THE ALLOCATION RULE — pro-rata by estimated value, never an even split.
--   `app/lot_basis.py` is the single definition; `app/allocation.py` does the
--   penny-exact apportionment shared with trades.
--
--   Even-split is not a "simpler default" — it is actively destructive. Give a
--   $120 hit and a $0.50 common the same basis and the hit reports a ~2,900%
--   return while the commons look like disasters. Per-card ROI is then noise
--   forever, and ROI is exactly what the tranche gates read.
--
-- 🔴 THE BULK REMAINDER — the part that makes this usable in real life
--   Nobody enters 46 commons. Brady buys a 50-card lot, enters the 4 worth
--   entering, and the rest goes in a box. Spread the whole lot price across
--   only those 4 and each absorbs basis it never cost.
--
--   `bulk_remainder_value` is the estimated value of the cards NOT entered.
--   It joins the denominator and absorbs its share, which is stored in
--   `bulk_basis`. Entered cards then carry only what they actually cost, and
--   the lot still balances to the penny:
--
--       SUM(cards.purchase_price WHERE lot_id = L) + L.bulk_basis
--         = L.total_cost + L.shipping_in + L.purchase_tax + L.other_costs
-- ============================================================


CREATE TABLE IF NOT EXISTS purchase_lots (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL DEFAULT auth.uid()
                    REFERENCES auth.users (id) ON DELETE CASCADE,

  -- What the lot cost. Split the same way a card's cost is split, so there is
  -- ONE idea of "all-in" in this system rather than two competing ones.
  total_cost      numeric NOT NULL DEFAULT 0,
  shipping_in     numeric,
  purchase_tax    numeric,
  other_costs     numeric,

  purchase_date   date NOT NULL,
  source          text,
  seller_name     text,
  card_count      integer,

  -- Estimated value of cards NOT entered individually. See header.
  bulk_remainder_value numeric NOT NULL DEFAULT 0,
  -- The share of lot cost that landed on those un-entered cards.
  bulk_basis           numeric NOT NULL DEFAULT 0,

  notes           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE purchase_lots IS
  'A single payment covering multiple cards. Cost is allocated across the '
  'cards pro-rata by estimated value (app/lot_basis.py), never evenly.';

COMMENT ON COLUMN purchase_lots.bulk_remainder_value IS
  'Estimated TOTAL value of cards in this lot that were not entered '
  'individually (commons, bulk). Participates in the allocation denominator '
  'so entered cards are not overcharged for the whole lot.';

COMMENT ON COLUMN purchase_lots.bulk_basis IS
  'The portion of the lot cost allocated to un-entered cards. Written at '
  'creation. Lot balances when SUM(card purchase_price) + bulk_basis = all-in.';

COMMENT ON COLUMN purchase_lots.total_cost IS
  'The lot PRICE only. Shipping, tax, and other costs are separate columns — '
  'same convention as cards.purchase_price (migration 007).';

ALTER TABLE purchase_lots
  DROP CONSTRAINT IF EXISTS purchase_lots_money_nonneg;
ALTER TABLE purchase_lots
  ADD CONSTRAINT purchase_lots_money_nonneg CHECK (
    total_cost >= 0
    AND (shipping_in IS NULL OR shipping_in >= 0)
    AND (purchase_tax IS NULL OR purchase_tax >= 0)
    AND (other_costs IS NULL OR other_costs >= 0)
    AND bulk_remainder_value >= 0
    AND bulk_basis >= 0
  );

ALTER TABLE purchase_lots
  DROP CONSTRAINT IF EXISTS purchase_lots_source_check;
ALTER TABLE purchase_lots
  ADD CONSTRAINT purchase_lots_source_check CHECK (
    source IS NULL OR source = ANY (ARRAY[
      'discord'::text,
      'facebook'::text,
      'instagram'::text,
      'ebay'::text,
      'whatnot'::text,
      'show'::text,
      'private_seller'::text,
      'other'::text
    ])
  );


-- ============================================================
-- cards.lot_id — which lot a card came from
-- ============================================================

ALTER TABLE cards
  ADD COLUMN IF NOT EXISTS lot_id uuid
    REFERENCES purchase_lots (id) ON DELETE SET NULL;

COMMENT ON COLUMN cards.lot_id IS
  'The lot this card was bought in, if any. NULL means a single-card buy. '
  'ON DELETE SET NULL: deleting a lot must never delete inventory.';

CREATE INDEX IF NOT EXISTS idx_cards_lot_id ON cards (lot_id);
CREATE INDEX IF NOT EXISTS idx_purchase_lots_user_date
  ON purchase_lots (user_id, purchase_date DESC);


-- ============================================================
-- RLS — same owner policy as every other table (migration 002)
-- ============================================================

ALTER TABLE purchase_lots ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS purchase_lots_owner ON purchase_lots;
CREATE POLICY purchase_lots_owner ON purchase_lots
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());


-- ============================================================
-- updated_at trigger (matches cards, per migration 003)
-- ============================================================

DROP TRIGGER IF EXISTS set_purchase_lots_updated_at ON purchase_lots;
CREATE TRIGGER set_purchase_lots_updated_at
  BEFORE UPDATE ON purchase_lots
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ============================================================
-- VERIFY (run after applying)
-- ============================================================
--
--   -- Every lot must balance. This should return ZERO rows.
--   SELECT l.id,
--          l.total_cost + COALESCE(l.shipping_in,0)
--            + COALESCE(l.purchase_tax,0) + COALESCE(l.other_costs,0) AS all_in,
--          COALESCE(SUM(c.purchase_price), 0) + l.bulk_basis           AS allocated
--   FROM purchase_lots l
--   LEFT JOIN cards c ON c.lot_id = l.id
--   GROUP BY l.id, l.total_cost, l.shipping_in, l.purchase_tax,
--            l.other_costs, l.bulk_basis
--   HAVING COALESCE(SUM(c.purchase_price), 0) + l.bulk_basis
--          <> l.total_cost + COALESCE(l.shipping_in,0)
--             + COALESCE(l.purchase_tax,0) + COALESCE(l.other_costs,0);

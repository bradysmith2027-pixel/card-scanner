-- ============================================================
-- 009_purchase_lots.sql
-- Lots: one payment, a bunch of cards.
--
-- There was no purchase_lots table and no lot_id anywhere, so I couldn't log a
-- lot. Every card from a lot just got a cost I guessed.
--
-- That's a big deal because lots are where I make the most:
--
--     Single cards over $300 (bought near comps):
--        7 cards, $4,048 cost, $232 profit  ->   5.7% ROI
--     Under $300 (Discord lots at ~$1.27 a card):
--       28 cards, $1,294 cost, $504 profit  ->  39% ROI
--
-- My best channel by far was the one the app couldn't track.
--
-- The cost gets split based on what each card is worth, never evenly. The math
-- is in app/lot_basis.py, and app/allocation.py does the to-the-cent split
-- that trades use too.
--
-- An even split would wreck the numbers. If a $120 card and a $0.50 common got
-- the same cost, the big card would show like a 2,900% return and the commons
-- would look like huge losses.
--
-- The bulk remainder:
--   I'm not entering 46 commons. I buy a 50 card lot, enter the 4 worth it, and
--   box the rest. If the whole lot price went on those 4, they'd each carry way
--   more cost than they should.
--
--   bulk_remainder_value is about what the cards I didn't enter are worth. It
--   takes its share of the cost, which gets saved in bulk_basis. That way the
--   cards I entered only carry what they really cost, and the lot still adds
--   up to the cent:
--
--       SUM(cards.purchase_price WHERE lot_id = L) + L.bulk_basis
--         = L.total_cost + L.shipping_in + L.purchase_tax + L.other_costs
-- ============================================================


CREATE TABLE IF NOT EXISTS purchase_lots (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL DEFAULT auth.uid()
                    REFERENCES auth.users (id) ON DELETE CASCADE,

  -- What the lot cost. Split up the same way as a single card's cost, so
  -- "all-in" means the same thing everywhere.
  total_cost      numeric NOT NULL DEFAULT 0,
  shipping_in     numeric,
  purchase_tax    numeric,
  other_costs     numeric,

  purchase_date   date NOT NULL,
  source          text,
  seller_name     text,
  card_count      integer,

  -- About what the cards I didn't enter are worth. See the top of the file.
  bulk_remainder_value numeric NOT NULL DEFAULT 0,
  -- The part of the lot's cost that went to those cards.
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
-- cards.lot_id: which lot a card came from
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
-- RLS: same owner policy as every other table (migration 002)
-- ============================================================

ALTER TABLE purchase_lots ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS purchase_lots_owner ON purchase_lots;
CREATE POLICY purchase_lots_owner ON purchase_lots
  FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());


-- ============================================================
-- updated_at trigger (same as cards, from migration 003)
-- ============================================================

DROP TRIGGER IF EXISTS set_purchase_lots_updated_at ON purchase_lots;
CREATE TRIGGER set_purchase_lots_updated_at
  BEFORE UPDATE ON purchase_lots
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ============================================================
-- Check it worked (run after applying)
-- ============================================================
--
--   -- Every lot should add up. This should return nothing.
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

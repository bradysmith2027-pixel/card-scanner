-- ============================================================
-- 007_true_profit_fields.sql
-- Dreamboat Slabs — the columns honest profit is computed from, plus the
-- strategy dimensions the monthly review reports on.
--
-- WHY THIS EXISTS (2026-09-13):
--   Profit was computed as `sale_price - purchase_price` in four independent
--   places (export.py:59, format.ts:27, viz.ts:68, and the grading cost was
--   never joined at all). No fees, no shipping, no grading, no tax. A
--   $100 -> $200 flip reported +$100 when the truth was ~$39.50 — roughly a
--   2.5x overstatement.
--
--   That was a bad habit at ~$10k of inventory. At $40-50k it is the thing
--   that decides which lanes get the next tranche, and since the capital is
--   entirely Brady's own there is NO outside investor reviewing the numbers.
--   The calculation is the only check that exists. Every buy decision has
--   been made against a number ~2.5x reality.
--
--   The columns below are the inputs. Migration 007 does not change any
--   behaviour on its own — `app/profit.py` (the single profit definition)
--   is what consumes them, and nothing else is permitted to do the math.
--
-- ⚠️ THE CONVENTION CLASH — READ BEFORE ENTERING OR IMPORTING DATA
--   Brady's spreadsheet-era rule was: "Card Cost is price you paid after
--   shipping & taxes" — i.e. `purchase_price` was already ALL-IN.
--
--   This migration splits that into purchase_price + shipping_in +
--   purchase_tax, because the split is what makes per-channel and
--   per-lane analysis possible.
--
--   The risk is silent and it runs one direction: entering an all-in number
--   into the price-only field DOUBLE-COUNTS shipping and tax, overstating
--   cost basis and understating ROI. Nothing errors. Nothing looks wrong.
--
--   Two consequences:
--     1. The manual-entry form must label `purchase_price` as CARD PRICE
--        ONLY, with shipping and tax as visibly separate fields.
--     2. The spreadsheet import (Phase 2) must map the sheet's all-in
--        "Card Cost" to purchase_price and leave shipping_in/purchase_tax
--        at 0 — NOT attempt to decompose it. Historical rows keep the old
--        convention honestly rather than being invented into the new one.
--
-- ⚠️ EXISTING ROWS
--   `position_type` is NOT NULL DEFAULT 'flip', so every card already in the
--   table backfills as a flip. Confirm that is actually true of the ~$10k
--   currently held and fix any exceptions IMMEDIATELY after applying this —
--   because of the immutability rule below, a mislabelled card cannot be
--   corrected through the API afterward.
--
-- ⚠️ LOWERCASE VALUES
--   Every value set here is lowercase, matching the rest of the schema. The
--   one exception in this database is grading_submissions.grading_company
--   (PSA/BGS/CGC/SGC), which is uppercase and has already caused one 23514
--   outage. Do not repeat that inconsistency.
--
-- IDEMPOTENT
--   Uses ADD COLUMN IF NOT EXISTS throughout, so re-running is safe. The
--   CHECK constraints ride along with their columns for the same reason.
-- ============================================================

-- ------------------------------------------------------------
-- 1. Cost inputs — the buy side
--
--    Defaulting to 0 rather than NULL is deliberate: these feed a SUM, and
--    NULL would poison all_in_cost for every card that predates this
--    migration. 0 is the honest value for "no shipping was charged."
-- ------------------------------------------------------------
alter table cards add column if not exists shipping_in   numeric not null default 0
  check (shipping_in >= 0);
alter table cards add column if not exists purchase_tax  numeric not null default 0
  check (purchase_tax >= 0);
alter table cards add column if not exists other_costs   numeric not null default 0
  check (other_costs >= 0);

-- ------------------------------------------------------------
-- 2. Cost + revenue inputs — the sell side
--
--    shipping_collected is REVENUE, not a cost. It is the shipping fee the
--    customer pays on top of the card price (mostly eBay — the "green
--    shipping column" in Brady's sheet). Omitting it was the mirror image
--    of the gross-profit bug: it made every eBay sale look WORSE than it
--    actually was. It belongs in net_proceeds, added not subtracted.
-- ------------------------------------------------------------
alter table cards add column if not exists shipping_out       numeric not null default 0
  check (shipping_out >= 0);
alter table cards add column if not exists platform_fees      numeric not null default 0
  check (platform_fees >= 0);
alter table cards add column if not exists shipping_collected numeric not null default 0
  check (shipping_collected >= 0);

-- ------------------------------------------------------------
-- 3. position_type — flip vs hold
--
--    🔒 THE RULE: a card enters the hold bucket AT PURCHASE and is never
--    reclassified. A flip that did not sell is a LOSS, not a collection
--    piece.
--
--    Why this is enforced in structure rather than trusted to discipline:
--    with no wall between the pools, "personal collection" is the perfect
--    hiding place for failed flips. Reclassify one and the position leaves
--    the performance report, ROI looks clean, and the monthly review shows
--    profit while capital sits frozen in cards nobody wanted. Nobody has to
--    act in bad faith for this to happen — which is exactly why it cannot
--    be left to good intentions.
--
--    The database cannot express "immutable column" directly. Enforcement
--    lives in the API: `position_type` is OMITTED from the CardUpdate model,
--    the same treatment id / user_id / created_at / updated_at already get.
--    See app/routers/cards.py.
-- ------------------------------------------------------------
alter table cards add column if not exists position_type text not null default 'flip'
  check (position_type in ('flip', 'hold'));

-- ------------------------------------------------------------
-- 4. Reporting dimensions
--
--    `lane` drives §③ of the monthly review — the by-lane table is the
--    section that decides where the next tranche goes. Without this column
--    that section cannot be produced at all.
--
--    Nullable on purpose: existing cards have no lane and guessing one
--    would fabricate the very data the report is supposed to reveal.
-- ------------------------------------------------------------
alter table cards add column if not exists lane text
  check (lane in ('graded_arb', 'raw_to_grade', 'sealed', 'optcg', 'other'));

--    Where a card actually SOLD. The app had acquisition_source (the buy
--    side) but nothing for the sell side. Brady sells across Discord (no
--    fees), Facebook, Instagram, eBay, and card shows — wildly different
--    fee structures. Without this, platform_fees is an unexplainable
--    number and "which channel nets most per hour?" is unanswerable.
alter table cards add column if not exists sale_channel text
  check (sale_channel in ('discord', 'facebook', 'instagram', 'ebay', 'show', 'other'));

alter table cards add column if not exists buyer_name text;

-- ------------------------------------------------------------
-- 5. Valuation + thesis
--
--    est_market_value is "what it's worth in hand" and powers the
--    "Inventory at est. market" line. Brady's convention: NULL it on sale,
--    because once a card is sold the actual price is the truth and a stale
--    estimate sitting beside it is just noise.
--
--    hold_thesis is the written reason a card entered the hold bucket.
--    Required in the UI when position_type = 'hold' (not enforced here —
--    a CHECK would fail every existing row). Its purpose is accountability:
--    an unwritten thesis is how "I'll keep this one" becomes strategy after
--    the fact.
-- ------------------------------------------------------------
alter table cards add column if not exists est_market_value numeric
  check (est_market_value is null or est_market_value >= 0);
alter table cards add column if not exists hold_thesis text;

-- ------------------------------------------------------------
-- 6. Indexes for the reporting queries this unlocks
--
--    All three are filtered/grouped on by the monthly review. Partial index
--    on lane because it is nullable and NULL lanes are never grouped.
-- ------------------------------------------------------------
create index if not exists cards_position_type_idx on cards (user_id, position_type);
create index if not exists cards_lane_idx          on cards (user_id, lane) where lane is not null;
create index if not exists cards_sale_channel_idx  on cards (user_id, sale_channel) where sale_channel is not null;

-- ------------------------------------------------------------
-- 7. Column documentation — so the next person reading the schema in the
--    Supabase UI gets the conventions, not just the types.
-- ------------------------------------------------------------
comment on column cards.purchase_price is
  'CARD PRICE ONLY. Shipping and tax are separate (shipping_in, purchase_tax). Differs from the spreadsheet-era all-in convention — see migration 007.';
comment on column cards.shipping_in is
  'Shipping paid to acquire the card. Part of all_in_cost.';
comment on column cards.purchase_tax is
  'Sales tax paid on purchase. Part of all_in_cost.';
comment on column cards.other_costs is
  'Any other acquisition cost not covered above. Part of all_in_cost.';
comment on column cards.shipping_out is
  'Shipping paid to send the card to a buyer. Subtracted from net_proceeds.';
comment on column cards.platform_fees is
  'Marketplace/payment fees on the sale. Subtracted from net_proceeds.';
comment on column cards.shipping_collected is
  'REVENUE: shipping the customer paid on top of the card price. ADDED to net_proceeds, not subtracted.';
comment on column cards.position_type is
  'flip | hold. IMMUTABLE after creation - enforced in the API by omitting it from CardUpdate. A flip that did not sell is a loss, never a collection piece.';
comment on column cards.lane is
  'Strategy lane for the monthly by-lane review, which decides tranche allocation.';
comment on column cards.sale_channel is
  'Where the card actually sold. Explains platform_fees; enables per-channel margin.';
comment on column cards.est_market_value is
  'What the card is worth in hand. Convention: set to NULL once sold.';
comment on column cards.hold_thesis is
  'Written reason this card entered the hold bucket. Required by the UI when position_type = hold.';

-- ============================================================
-- VERIFY (run manually after applying)
--
--   select column_name, data_type, is_nullable, column_default
--     from information_schema.columns
--    where table_name = 'cards'
--      and column_name in ('shipping_in','shipping_out','platform_fees',
--                          'other_costs','purchase_tax','shipping_collected',
--                          'position_type','lane','sale_channel','buyer_name',
--                          'est_market_value','hold_thesis')
--    order by column_name;
--
--   -- every existing card should now be 'flip' — confirm that is correct
--   select position_type, count(*) from cards group by position_type;
--
--   -- constraints landed?
--   select conname, pg_get_constraintdef(oid)
--     from pg_constraint
--    where conrelid = 'cards'::regclass and contype = 'c'
--    order by conname;
-- ============================================================

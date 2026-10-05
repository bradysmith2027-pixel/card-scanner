-- ============================================================
-- 007_true_profit_fields.sql
-- Adds the columns real profit needs, plus the fields my monthly review
-- reports on.
--
-- Profit was being calculated as sale_price - purchase_price in a few
-- different places, and grading cost wasn't included anywhere. No fees, no
-- shipping, no grading, no tax. A $100 card I sold for $200 showed +$100 when
-- I really made about $39.50.
--
-- That wasn't great at ~$10k of inventory. Now that I'm putting $40-50k in,
-- these numbers decide where my money goes next, and it's all my own money so
-- nobody else is checking them.
--
-- This migration only adds the columns. app/profit.py is what actually uses
-- them, and it's the only place that does the math.
--
-- Heads up when entering or importing data:
--   In my spreadsheet, "Card Cost" was the price after shipping and tax, so
--   purchase_price was the all-in number.
--
--   Now it's split into purchase_price + shipping_in + purchase_tax, so I can
--   see costs by channel and by lane.
--
--   If I put an all-in number in the price field, shipping and tax get counted
--   twice and my ROI looks worse. Nothing would error, it would just be wrong.
--
--   So:
--     1. The add card form has to label purchase_price as the card price only,
--        with shipping and tax as their own fields.
--     2. When I import the spreadsheet, Card Cost goes into purchase_price and
--        shipping_in/purchase_tax stay 0. Don't try to split it up, I'd just be
--        making up numbers.
--
-- Cards already in the table:
--   position_type defaults to 'flip', so every existing card becomes a flip.
--   Check that's right for what I'm holding and fix anything wrong right after
--   running this, because it can't be changed through the API later.
--
-- All the values here are lowercase like the rest of the database. The only
-- uppercase ones are grading_company (PSA/BGS/CGC/SGC), which already caused
-- an error once. Don't do that again.
--
-- Safe to run more than once (ADD COLUMN IF NOT EXISTS everywhere).
-- ============================================================

-- ------------------------------------------------------------
-- 1. Costs when buying
--
--    These default to 0 instead of NULL on purpose. They get added together,
--    and a NULL would make all_in_cost empty for every card added before this.
--    0 just means no shipping was charged.
-- ------------------------------------------------------------
alter table cards add column if not exists shipping_in   numeric not null default 0
  check (shipping_in >= 0);
alter table cards add column if not exists purchase_tax  numeric not null default 0
  check (purchase_tax >= 0);
alter table cards add column if not exists other_costs   numeric not null default 0
  check (other_costs >= 0);

-- ------------------------------------------------------------
-- 2. Costs and money in when selling
--
--    shipping_collected is money in, not a cost. It's the shipping the buyer
--    pays on top of the card (mostly eBay, the green shipping column in my
--    spreadsheet). Leaving it out made every eBay sale look worse than it was.
--    It gets added in net_proceeds.
-- ------------------------------------------------------------
alter table cards add column if not exists shipping_out       numeric not null default 0
  check (shipping_out >= 0);
alter table cards add column if not exists platform_fees      numeric not null default 0
  check (platform_fees >= 0);
alter table cards add column if not exists shipping_collected numeric not null default 0
  check (shipping_collected >= 0);

-- ------------------------------------------------------------
-- 3. position_type: flip or hold
--
--    The rule: a card is a flip or a hold from the day I buy it and never
--    changes. If a flip doesn't sell, that's a loss, it doesn't get to become
--    part of my collection.
--
--    It's enforced in the code instead of just trusting myself. Flips and holds
--    come out of the same money, so it would be way too easy to move bad flips
--    into the collection. Then my ROI would look better than it really is while
--    money sits in cards nobody wants.
--
--    The database can't make a column unchangeable, so the API does it.
--    position_type is left out of CardUpdate, same as id, user_id, created_at
--    and updated_at. See app/routers/cards.py.
-- ------------------------------------------------------------
alter table cards add column if not exists position_type text not null default 'flip'
  check (position_type in ('flip', 'hold'));

-- ------------------------------------------------------------
-- 4. Reporting fields
--
--    lane is what the by-lane section of my monthly review uses, which is how
--    I decide where the next chunk of money goes. Without it I can't make that
--    section at all.
--
--    It can be empty on purpose. Older cards don't have a lane, and guessing
--    one would just make up the data the report is supposed to show me.
-- ------------------------------------------------------------
alter table cards add column if not exists lane text
  check (lane in ('graded_arb', 'raw_to_grade', 'sealed', 'optcg', 'other'));

--    Where a card actually sold. I had acquisition_source for buying but
--    nothing for selling. I sell on Discord (no fees), Facebook, Instagram,
--    eBay and at card shows, and they all have different fees. Without this I
--    can't explain platform_fees or figure out which channel makes me the most.
alter table cards add column if not exists sale_channel text
  check (sale_channel in ('discord', 'facebook', 'instagram', 'ebay', 'show', 'other'));

alter table cards add column if not exists buyer_name text;

-- ------------------------------------------------------------
-- 5. Value and reason for holding
--
--    est_market_value is what the card's worth right now, for the "Inventory at
--    est. market" number. I clear it when a card sells, since then I have the
--    real price.
--
--    hold_thesis is why I'm keeping a card. The app requires it for holds (not
--    in the database, a CHECK would fail on every existing card). If I don't
--    write down why when I buy it, "I'll keep this one" turns into an excuse
--    later.
-- ------------------------------------------------------------
alter table cards add column if not exists est_market_value numeric
  check (est_market_value is null or est_market_value >= 0);
alter table cards add column if not exists hold_thesis text;

-- ------------------------------------------------------------
-- 6. Indexes for the report queries
--
--    The monthly review filters and groups by all three. The lane index skips
--    empty lanes since those never get grouped.
-- ------------------------------------------------------------
create index if not exists cards_position_type_idx on cards (user_id, position_type);
create index if not exists cards_lane_idx          on cards (user_id, lane) where lane is not null;
create index if not exists cards_sale_channel_idx  on cards (user_id, sale_channel) where sale_channel is not null;

-- ------------------------------------------------------------
-- 7. Descriptions on the columns, so they show up in the Supabase dashboard.
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
-- Check it worked (run after applying)
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
--   -- every existing card should be 'flip' now, make sure that's right
--   select position_type, count(*) from cards group by position_type;
--
--   -- did the constraints get added?
--   select conname, pg_get_constraintdef(oid)
--     from pg_constraint
--    where conrelid = 'cards'::regclass and contype = 'c'
--    order by conname;
-- ============================================================

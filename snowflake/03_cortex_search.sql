-- ============================================================================
-- Bookly Support Agent — policy knowledge base + Cortex Search service
--
-- Run after 01_schema.sql / 02_seed_data.sql, in the same database/schema.
-- Requires Cortex Search enabled for your account/region, and a warehouse
-- to build/refresh the index -- swap COMPUTE_WH below for the warehouse
-- in your .env.
-- ============================================================================

CREATE OR REPLACE TABLE policy_docs (
    doc_id      VARCHAR(20)     NOT NULL,
    topic       VARCHAR(50)     NOT NULL, -- shipping | returns | password_reset | payment | international
    title       VARCHAR(255)    NOT NULL,
    content     VARCHAR(4000)   NOT NULL,
    PRIMARY KEY (doc_id)
);

DELETE FROM policy_docs;

-- More granular than a 5-row "one answer per topic" table on purpose --
-- edge cases (damaged items, gifts, lost packages) get their own doc, so
-- semantic search can match a customer's own phrasing to the right one.
INSERT INTO policy_docs (doc_id, topic, title, content) VALUES
    ('POL-01', 'shipping', 'Standard & expedited shipping',
     'Standard shipping takes 4-6 business days within the continental US and is free on orders over $35. Expedited shipping (2-3 business days) is available at checkout for an extra $6.99.'),
    ('POL-02', 'shipping', 'Late or missing packages',
     'If tracking shows delivered but the customer says the package never arrived, or tracking hasn''t updated in 5+ business days, treat it as lost in transit: offer a free reshipment or a full refund, the customer''s choice. Do not make them wait out the carrier''s own claims process first.'),
    ('POL-03', 'returns', '30-day return window',
     'Books can be returned within 30 days of delivery for a full refund, as long as they are in resellable condition. After 30 days we cannot offer a refund, but store credit may be available case-by-case -- escalate that to a human agent.'),
    ('POL-04', 'returns', 'Damaged, defective, or wrong item',
     'If a book arrives damaged, defective, or isn''t what the customer ordered, waive the resellable-condition requirement and process the return or replacement immediately. Don''t require the customer to ship the damaged item back before a refund or replacement goes out.'),
    ('POL-05', 'returns', 'Returning a gift',
     'Items marked as a gift at checkout can be returned for Bookly store credit within 60 days of delivery, even without the original purchaser''s account or order number.'),
    ('POL-06', 'password_reset', 'Resetting your password',
     'Go to bookly.com/login, click "Forgot password", and enter the email on file. A reset link is emailed immediately and expires after 60 minutes.'),
    ('POL-07', 'password_reset', 'Reset email never arrives',
     'If the customer says the reset email never arrived, have them check spam/promotions first and confirm the email matches their account exactly (case and typos included). If it still hasn''t arrived after 10 minutes, send another one -- links can expire silently without bouncing.'),
    ('POL-08', 'payment', 'Accepted payment methods',
     'We accept Visa, Mastercard, American Express, Discover, PayPal, and Bookly gift cards. We do not currently support Buy Now Pay Later options.'),
    ('POL-09', 'payment', 'When your card is charged',
     'Cards are authorized at checkout but only actually charged once the order ships, not when it''s placed. A pending authorization that never settles will drop off the customer''s statement within 5-7 business days.'),
    ('POL-10', 'international', 'International shipping & customs',
     'Bookly ships to over 40 countries; delivery takes 10-20 business days. International orders may be subject to customs fees charged by the destination country, which are the customer''s responsibility and are not included in the order total.');

-- ON names the text column that gets embedded and searched; ATTRIBUTES
-- are extra columns returned/filterable alongside it. TARGET_LAG controls
-- how quickly edits to policy_docs are reflected.
CREATE OR REPLACE CORTEX SEARCH SERVICE bookly_policy_search
    ON content
    ATTRIBUTES topic, title
    WAREHOUSE = COMPUTE_WH
    TARGET_LAG = '1 hour'
    EMBEDDING_MODEL = 'snowflake-arctic-embed-l-v2.0'
AS (
    SELECT doc_id, topic, title, content
    FROM policy_docs
);

-- Quick sanity check from a worksheet (the app queries this via the
-- Python SDK instead -- see app/snowflake_client.py::search_policies):
--   SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
--     'bookly_policy_search',
--     '{"query": "is there a fee to ship to canada", "columns": ["title","content"], "limit": 3}'
--   );

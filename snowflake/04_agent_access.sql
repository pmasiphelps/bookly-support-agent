-- ============================================================================
-- Bookly Support Agent — least-privilege Snowflake access for the AGENT
--
-- Run this AFTER 01_schema.sql / 02_seed_data.sql / 03_cortex_search.sql.
-- This one needs a role that can create roles, users, and warehouses --
-- ACCOUNTADMIN is simplest for a trial account.
--
-- The identity that deploys the app (you, with broad rights) is not the
-- identity the app runs as (BOOKLY_AGENT_SVC, with almost none). 01-03
-- build the objects; 04 creates something with barely enough privilege to
-- use them.
--
-- Before running: generate a key pair with scripts/generate_agent_keypair.sh
-- and paste the printed public key below in place of <PASTE_PUBLIC_KEY_HERE>.
-- ============================================================================

USE ROLE ACCOUNTADMIN;

-- 1. A dedicated, auto-suspending warehouse -- the agent never shares one
-- with another workload and can't run up cost beyond its own XS footprint.
CREATE WAREHOUSE IF NOT EXISTS bookly_agent_wh
    WAREHOUSE_SIZE = 'XSMALL'
    AUTO_SUSPEND = 60
    AUTO_RESUME = TRUE
    INITIALLY_SUSPENDED = TRUE
    STATEMENT_TIMEOUT_IN_SECONDS = 30
    COMMENT = 'Dedicated warehouse for the Bookly support agent -- not shared with other workloads.';

-- 2. A role that can do exactly what app/tools.py's tools do, and nothing
-- else: read customers/orders/order_items, append (never update or
-- delete) returns, and search -- not read -- the policy content.
CREATE ROLE IF NOT EXISTS bookly_agent_role
    COMMENT = 'Minimal runtime role for the Bookly support agent service user. Not for humans.';

GRANT USAGE ON WAREHOUSE bookly_agent_wh TO ROLE bookly_agent_role;
GRANT USAGE ON DATABASE bookly TO ROLE bookly_agent_role;
GRANT USAGE ON SCHEMA bookly.support TO ROLE bookly_agent_role;

GRANT SELECT ON TABLE bookly.support.customers TO ROLE bookly_agent_role;
GRANT SELECT ON TABLE bookly.support.orders TO ROLE bookly_agent_role;
GRANT SELECT ON TABLE bookly.support.order_items TO ROLE bookly_agent_role;

-- Append-only: initiate_return only ever INSERTs. No UPDATE, no DELETE.
GRANT SELECT, INSERT ON TABLE bookly.support.returns TO ROLE bookly_agent_role;

-- Search, don't SELECT: USAGE on the Cortex Search service, nothing on
-- policy_docs itself -- app/tools.py never issues that query, and now the
-- database would refuse it even if a bug tried to.
GRANT USAGE ON CORTEX SEARCH SERVICE bookly.support.bookly_policy_search TO ROLE bookly_agent_role;

GRANT ROLE bookly_agent_role TO ROLE SYSADMIN;

-- 3. The service identity itself. TYPE = SERVICE_AGENT is Snowflake's
-- purpose-built user type for an automated agent using its own identity
-- and privileges -- no password is ever set; auth is key-pair only (see
-- scripts/generate_agent_keypair.sh and app/snowflake_client.py).
--
-- NOTE: if CREATE USER errors on an invalid TYPE, swap in TYPE = SERVICE
-- (the older non-interactive user type) -- nothing else here changes.
CREATE USER IF NOT EXISTS bookly_agent_svc
    TYPE = SERVICE_AGENT
    DEFAULT_ROLE = bookly_agent_role
    DEFAULT_WAREHOUSE = bookly_agent_wh
    RSA_PUBLIC_KEY = '<PASTE_PUBLIC_KEY_HERE>'
    COMMENT = 'Service identity for the Bookly support agent (app/snowflake_client.py). Key-pair auth only, no password.';

GRANT ROLE bookly_agent_role TO USER bookly_agent_svc;

-- Verification -- run as bookly_agent_role (not ACCOUNTADMIN) to prove
-- the grants above are both sufficient AND minimal:
--
--   USE ROLE bookly_agent_role;
--   USE WAREHOUSE bookly_agent_wh;
--   SELECT * FROM bookly.support.orders LIMIT 1;         -- works
--   INSERT INTO bookly.support.returns (return_id, order_id, order_item_id, reason)
--     VALUES ('RET-TEST01', 'BK-1001', 'OI-10011', 'test');  -- works
--   SELECT * FROM bookly.support.policy_docs LIMIT 1;    -- fails: no privilege
--   DELETE FROM bookly.support.returns;                  -- fails: no privilege
--   CREATE TABLE bookly.support.whoops (x INT);           -- fails: no privilege

-- Key rotation, without downtime: generate a new pair, then
--   ALTER USER bookly_agent_svc SET RSA_PUBLIC_KEY_2 = '<new public key>';
-- point the app at the new private key, confirm it connects, then
--   ALTER USER bookly_agent_svc UNSET RSA_PUBLIC_KEY;

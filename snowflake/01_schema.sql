-- ============================================================================
-- Bookly Support Agent — Snowflake schema
--
-- Run this after creating a database/schema, e.g.:
--   CREATE DATABASE IF NOT EXISTS BOOKLY;
--   CREATE SCHEMA IF NOT EXISTS BOOKLY.SUPPORT;
--   USE DATABASE BOOKLY; USE SCHEMA SUPPORT;
-- ============================================================================

CREATE OR REPLACE TABLE customers (
    customer_id     VARCHAR(20)     NOT NULL,
    email           VARCHAR(255)    NOT NULL,
    full_name       VARCHAR(255)    NOT NULL,
    created_at      TIMESTAMP_NTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (customer_id)
);

CREATE OR REPLACE TABLE orders (
    order_id            VARCHAR(20)    NOT NULL,
    customer_id         VARCHAR(20)    NOT NULL,
    order_date          DATE           NOT NULL,
    status              VARCHAR(30)    NOT NULL, -- processing | shipped | out_for_delivery | delivered | cancelled
    total_amount        NUMBER(10, 2)  NOT NULL,
    carrier             VARCHAR(50),
    tracking_number     VARCHAR(50),
    estimated_delivery  DATE,
    delivered_at        DATE,
    PRIMARY KEY (order_id)
);

CREATE OR REPLACE TABLE order_items (
    order_item_id   VARCHAR(20)     NOT NULL,
    order_id        VARCHAR(20)     NOT NULL,
    book_title      VARCHAR(255)    NOT NULL,
    isbn            VARCHAR(20),
    quantity        NUMBER(5, 0)    NOT NULL,
    unit_price      NUMBER(10, 2)   NOT NULL,
    PRIMARY KEY (order_item_id)
);

-- Populated by initiate_return -- the one write path in the app.
CREATE OR REPLACE TABLE returns (
    return_id           VARCHAR(20)     NOT NULL,
    order_id            VARCHAR(20)     NOT NULL,
    order_item_id       VARCHAR(20)     NOT NULL,
    reason              VARCHAR(255)    NOT NULL,
    status              VARCHAR(30)     NOT NULL DEFAULT 'requested', -- requested | approved | rejected
    requested_refund    NUMBER(10, 2),
    created_at          TIMESTAMP_NTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (return_id)
);

-- The policy/FAQ knowledge base lives in 03_cortex_search.sql, since it's
-- queried by semantic search, not by primary key.

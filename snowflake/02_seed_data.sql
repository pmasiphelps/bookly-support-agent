-- ============================================================================
-- Bookly Support Agent — mock seed data
--
-- Dates are relative to CURRENT_DATE() so the "delivered 5 days ago,
-- eligible for return" / "delivered 45 days ago, expired" scenarios stay
-- true no matter when this loads.
-- ============================================================================

DELETE FROM order_items;
DELETE FROM orders;
DELETE FROM customers;
DELETE FROM returns;

INSERT INTO customers (customer_id, email, full_name, created_at) VALUES
    ('CUST-001', 'alice@bookly-demo.com', 'Alice Nguyen',  DATEADD(day, -400, CURRENT_TIMESTAMP())),
    ('CUST-002', 'bob@bookly-demo.com',   'Bob Martinez',  DATEADD(day, -220, CURRENT_TIMESTAMP())),
    ('CUST-003', 'carol@bookly-demo.com', 'Carol Osei',    DATEADD(day, -90,  CURRENT_TIMESTAMP())),
    ('CUST-004', 'dave@bookly-demo.com',  'Dave Chen',     DATEADD(day, -600, CURRENT_TIMESTAMP()));

-- BK-1001: delivered 5 days ago -> inside the 30-day return window
INSERT INTO orders (order_id, customer_id, order_date, status, total_amount, carrier, tracking_number, estimated_delivery, delivered_at) VALUES
    ('BK-1001', 'CUST-001', DATEADD(day, -9, CURRENT_DATE()), 'delivered', 24.98, 'UPS', '1Z999AA10123456784', DATEADD(day, -5, CURRENT_DATE()), DATEADD(day, -5, CURRENT_DATE()));

-- BK-1002: shipped 2 days ago, still in transit, two line items
INSERT INTO orders (order_id, customer_id, order_date, status, total_amount, carrier, tracking_number, estimated_delivery, delivered_at) VALUES
    ('BK-1002', 'CUST-002', DATEADD(day, -3, CURRENT_DATE()), 'shipped', 42.97, 'USPS', '9400111899223197428941', DATEADD(day, 2, CURRENT_DATE()), NULL);

-- BK-1003: placed today, still processing, no tracking yet
INSERT INTO orders (order_id, customer_id, order_date, status, total_amount, carrier, tracking_number, estimated_delivery, delivered_at) VALUES
    ('BK-1003', 'CUST-003', CURRENT_DATE(), 'processing', 15.99, NULL, NULL, DATEADD(day, 6, CURRENT_DATE()), NULL);

-- BK-1004: delivered 45 days ago -> outside the 30-day return window
INSERT INTO orders (order_id, customer_id, order_date, status, total_amount, carrier, tracking_number, estimated_delivery, delivered_at) VALUES
    ('BK-1004', 'CUST-004', DATEADD(day, -50, CURRENT_DATE()), 'delivered', 18.50, 'FedEx', '789123456123', DATEADD(day, -45, CURRENT_DATE()), DATEADD(day, -45, CURRENT_DATE()));

-- BK-1005: second order for Alice, out for delivery today
INSERT INTO orders (order_id, customer_id, order_date, status, total_amount, carrier, tracking_number, estimated_delivery, delivered_at) VALUES
    ('BK-1005', 'CUST-001', DATEADD(day, -2, CURRENT_DATE()), 'out_for_delivery', 31.98, 'UPS', '1Z999AA10199999991', CURRENT_DATE(), NULL);

INSERT INTO order_items (order_item_id, order_id, book_title, isbn, quantity, unit_price) VALUES
    ('OI-10011', 'BK-1001', 'The Hobbit',                         '9780547928227', 1, 14.99),
    ('OI-10012', 'BK-1001', 'A Short History of Nearly Everything','9780767908184', 1,  9.99),
    ('OI-10021', 'BK-1002', 'Project Hail Mary',                  '9780593135204', 1, 19.99),
    ('OI-10022', 'BK-1002', 'Dune',                                '9780441013593', 1, 22.98),
    ('OI-10031', 'BK-1003', 'Educated: A Memoir',                  '9780399590504', 1, 15.99),
    ('OI-10041', 'BK-1004', 'Sapiens: A Brief History of Humankind','9780062316097', 1, 18.50),
    ('OI-10051', 'BK-1005', 'The Name of the Wind',                '9780756404079', 1, 16.99),
    ('OI-10052', 'BK-1005', 'Circe',                                '9780316556347', 1, 14.99);

-- Policy/FAQ content is seeded in 03_cortex_search.sql, next to the Cortex
-- Search service that indexes it.

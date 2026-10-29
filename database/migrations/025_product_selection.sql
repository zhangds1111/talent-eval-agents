-- 选品第 2 阶段：商品数值属性 + 市场×品类必需认证规则 + 已登记认证（幂等）
CREATE TABLE IF NOT EXISTS product_attributes (
    product_id UUID PRIMARY KEY REFERENCES employee_profiles(id) ON DELETE CASCADE,
    model_no VARCHAR(64) NOT NULL,
    currency VARCHAR(8) NOT NULL,
    target_price NUMERIC(12, 2) NOT NULL CHECK (target_price > 0),
    landed_cost NUMERIC(12, 2) NOT NULL CHECK (landed_cost >= 0),
    is_electrical BOOLEAN NOT NULL DEFAULT false,
    is_wireless BOOLEAN NOT NULL DEFAULT false,
    has_lithium_battery BOOLEAN NOT NULL DEFAULT false,
    for_children BOOLEAN NOT NULL DEFAULT false,
    food_contact BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS certification_rules (
    id SERIAL PRIMARY KEY,
    market VARCHAR(64) NOT NULL,
    category VARCHAR(255) NOT NULL DEFAULT '*',
    applies_when VARCHAR(32) NOT NULL CHECK (applies_when IN ('always', 'electrical', 'wireless', 'lithium_battery', 'children', 'food_contact')),
    certification VARCHAR(64) NOT NULL,
    rationale TEXT NOT NULL,
    UNIQUE (market, category, applies_when, certification)
);

CREATE TABLE IF NOT EXISTS product_certifications (
    id SERIAL PRIMARY KEY,
    product_id UUID NOT NULL REFERENCES employee_profiles(id) ON DELETE CASCADE,
    certification VARCHAR(64) NOT NULL,
    document_no VARCHAR(128) NOT NULL,
    applicable_model VARCHAR(64),
    issued_on DATE,
    valid_until DATE,
    source_material VARCHAR(255) NOT NULL,
    UNIQUE (product_id, certification, document_no)
);

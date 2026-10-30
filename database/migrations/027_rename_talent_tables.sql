-- 选品化改名：运行历史/会话表去掉 talent 前缀
ALTER TABLE IF EXISTS talent_runs RENAME TO product_runs;
ALTER TABLE IF EXISTS talent_run_steps RENAME TO product_run_steps;
ALTER TABLE IF EXISTS talent_conversations RENAME TO product_conversations;
ALTER INDEX IF EXISTS idx_talent_runs_status RENAME TO idx_product_runs_status;
ALTER INDEX IF EXISTS idx_talent_runs_started_at RENAME TO idx_product_runs_started_at;
ALTER INDEX IF EXISTS idx_talent_runs_trace_id RENAME TO idx_product_runs_trace_id;
ALTER INDEX IF EXISTS idx_talent_runs_conversation_turn RENAME TO idx_product_runs_conversation_turn;
ALTER INDEX IF EXISTS idx_talent_run_steps_run_position RENAME TO idx_product_run_steps_run_position;
ALTER INDEX IF EXISTS idx_talent_conversations_status_updated RENAME TO idx_product_conversations_status_updated;
ALTER TABLE IF EXISTS product_run_steps RENAME CONSTRAINT uq_talent_run_step TO uq_product_run_step;
COMMENT ON TABLE product_runs IS '选品评估 Run 的最新状态、报告与引用快照';
-- 自动生成的约束名（RENAME CONSTRAINT 会同时改主键索引名）
ALTER TABLE IF EXISTS product_runs RENAME CONSTRAINT talent_runs_pkey TO product_runs_pkey;
ALTER TABLE IF EXISTS product_runs RENAME CONSTRAINT talent_runs_turn_no_check TO product_runs_turn_no_check;
ALTER TABLE IF EXISTS product_run_steps RENAME CONSTRAINT talent_run_steps_pkey TO product_run_steps_pkey;
ALTER TABLE IF EXISTS product_run_steps RENAME CONSTRAINT talent_run_steps_position_check TO product_run_steps_position_check;
ALTER TABLE IF EXISTS product_run_steps RENAME CONSTRAINT talent_run_steps_run_id_fkey TO product_run_steps_run_id_fkey;
ALTER TABLE IF EXISTS product_conversations RENAME CONSTRAINT talent_conversations_pkey TO product_conversations_pkey;

-- Ficha de registro editable a mano por el asesor (Lily/Fabiola): campos que Sofía
-- no captura (fecha_nacimiento, escuela_procedencia, promocion, ciclo_escolar…).
-- Reunión Fabiola 2026-08-07 / follow-up. Aplicado en prod. Idempotente.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS ficha jsonb NOT NULL DEFAULT '{}'::jsonb;

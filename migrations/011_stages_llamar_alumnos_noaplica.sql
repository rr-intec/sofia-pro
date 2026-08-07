-- Nuevas columnas del CRM pedidas en la reunión con Fabiola (2026-08-07):
--   llamar    → escalaciones que ventas toma directo (presupuesto, prepa, 3er toque sin respuesta)
--   alumnos   → ya son alumnos (uniformes/dudas internas); se atienden aparte, Sofía apagada
--   no_aplica → no es admisiones (RH, spam, número equivocado); descartado, Sofía apagada
-- Aplicado en prod el 2026-08-07. Idempotente.
ALTER TYPE lead_stage ADD VALUE IF NOT EXISTS 'llamar';
ALTER TYPE lead_stage ADD VALUE IF NOT EXISTS 'alumnos';
ALTER TYPE lead_stage ADD VALUE IF NOT EXISTS 'no_aplica';

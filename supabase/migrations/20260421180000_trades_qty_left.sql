-- Quantidade restante após realização parcial (monitorização / dashboard).
alter table public.trades
  add column if not exists qty_left double precision;

comment on column public.trades.qty_left is
  'Contratos/base restantes após PARTIAL_TP_50; actualizado pelo maestro (logger).';

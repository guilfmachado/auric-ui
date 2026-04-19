-- Execute no SQL Editor do Supabase (ou como migration).

create table if not exists public.decisions (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  symbol text not null,
  action text not null check (action in ('BUY', 'SELL', 'HOLD')),
  confidence real,
  raw_response text,
  market_snapshot jsonb not null default '{}'::jsonb,
  model text,
  replicate_prediction_id text
);

create index if not exists idx_decisions_created_at on public.decisions (created_at desc);

create table if not exists public.trades (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  decision_id uuid references public.decisions (id) on delete set null,
  symbol text not null,
  side text not null check (side in ('buy', 'sell')),
  amount numeric not null,
  price numeric,
  order_id text,
  status text not null default 'submitted',
  raw_exchange jsonb default '{}'::jsonb,
  mode text not null default 'paper'
);

create index if not exists idx_trades_created_at on public.trades (created_at desc);

alter table public.decisions enable row level security;
alter table public.trades enable row level security;

-- Ajuste policies conforme sua auth; com service_role no backend RLS pode ser bypassed.
-- Para uso apenas server-side com service_role, as políticas abaixo permitem tudo ao role service.

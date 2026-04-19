-- Saldo USDT em Futuros (USDT-M), atualizado pelo main.py a cada ciclo.
create table if not exists public.wallet_status (
  id smallint primary key default 1 check (id = 1),
  usdt_futures double precision not null default 0,
  updated_at timestamptz not null default now()
);

comment on table public.wallet_status is 'Snapshot do saldo USDT na carteira de Futuros (ccxt fetch_balance).';

insert into public.wallet_status (id, usdt_futures)
values (1, 0)
on conflict (id) do nothing;

alter table public.wallet_status enable row level security;

create policy "wallet_status_select_anon"
  on public.wallet_status for select
  to anon, authenticated
  using (true);

create policy "wallet_status_update_anon"
  on public.wallet_status for update
  to anon, authenticated
  using (true)
  with check (true);

create policy "wallet_status_insert_anon"
  on public.wallet_status for insert
  to anon, authenticated
  with check (true);

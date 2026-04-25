-- Garante consistência: sem posição aberta => entry_price = 0
-- Pode rodar em produção sem apagar dados.

-- 1) Corrige dados já gravados inconsistentes
update public.wallet_status
set entry_price = 0
where coalesce(posicao_aberta, false) = false
  and coalesce(entry_price, 0) <> 0;

-- 2) Trigger para manter consistência em inserts/updates futuros
create or replace function public.enforce_wallet_status_entry_price_consistency()
returns trigger
language plpgsql
as $$
begin
  if coalesce(new.posicao_aberta, false) = false then
    new.entry_price := 0;
  end if;
  return new;
end;
$$;

drop trigger if exists trg_wallet_status_entry_price_consistency on public.wallet_status;

create trigger trg_wallet_status_entry_price_consistency
before insert or update on public.wallet_status
for each row
execute function public.enforce_wallet_status_entry_price_consistency();

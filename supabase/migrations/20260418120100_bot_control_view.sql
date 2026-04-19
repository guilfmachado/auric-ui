-- Pedágio no main.py: nome `bot_control`; dados = mesma linha que `bot_status` (dashboard).
create or replace view public.bot_control as
  select id, is_active, updated_at
  from public.bot_status;

comment on view public.bot_control is 'Alias read-only do master switch (id=1); fonte: bot_status.';

grant select on public.bot_control to anon, authenticated;

-- Dashboard: Switch faz UPDATE em `bot_control` (view → bot_status).
grant update (is_active, updated_at) on public.bot_control to anon, authenticated;

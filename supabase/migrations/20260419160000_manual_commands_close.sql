-- Permite comando de fecho de emergência na fila manual.
alter table public.manual_commands
  drop constraint if exists manual_commands_command_check;

alter table public.manual_commands
  add constraint manual_commands_command_check
  check (command in ('LONG', 'SHORT', 'CLOSE'));

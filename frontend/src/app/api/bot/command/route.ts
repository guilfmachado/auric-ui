import { createClient } from "@supabase/supabase-js";
import { NextResponse } from "next/server";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

// SERVICE_ROLE_KEY no Vercel: bypassa RLS quando necessário.
const supabase =
  url && serviceKey ? createClient(url, serviceKey) : null;

export async function POST(req: Request) {
  try {
    const secret = process.env.BOT_COMMAND_API_SECRET;
    if (!secret) {
      return NextResponse.json(
        { ok: false, error: "BOT_COMMAND_API_SECRET não configurado no servidor" },
        { status: 500 },
      );
    }

    const authHeader = req.headers.get("authorization");
    if (authHeader !== `Bearer ${secret}`) {
      return NextResponse.json({ error: "Não autorizado" }, { status: 401 });
    }

    if (!supabase) {
      return NextResponse.json(
        {
          ok: false,
          error:
            "Supabase: defina NEXT_PUBLIC_SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY",
        },
        { status: 500 },
      );
    }

    const body = (await req.json()) as { value?: unknown; active?: unknown };
    const value =
      body.value === undefined || body.value === null ? "" : String(body.value);
    const active =
      typeof body.active === "boolean"
        ? body.active
        : String(body.active).toLowerCase() === "true" ||
          body.active === 1;

    const { data, error } = await supabase
      .from("bot_commands")
      .upsert(
        {
          key: "market_observation",
          value,
          active,
          updated_at: new Date().toISOString(),
        },
        { onConflict: "key" },
      )
      .select();

    if (error) throw error;

    return NextResponse.json({ ok: true, row: data?.[0] ?? null });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ ok: false, error: message }, { status: 500 });
  }
}

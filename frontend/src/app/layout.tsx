import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Toaster } from "react-hot-toast";

import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Auric · Final Boss Terminal",
  description:
    "Terminal de trading quantitativo — desk midnight, gauges e intelligence log.",
};

/** Evita cache estático de rota na Vercel — dados Supabase/logs devem ser frescos a cada visita. */
export const dynamic = "force-dynamic";
export const revalidate = 0;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="pt-BR" className="dark">
      <body
        className={`${geistSans.variable} ${geistMono.variable} font-sans antialiased bg-[#09090b]`}
      >
        {children}
        <Toaster
          position="top-center"
          toastOptions={{
            className: "!bg-zinc-900 !text-zinc-100 !border !border-zinc-700",
          }}
        />
      </body>
    </html>
  );
}

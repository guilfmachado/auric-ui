"use client";

import { motion } from "framer-motion";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type Props = {
  children: ReactNode;
  className?: string;
};

/** Cartão Midnight City + brilho suave ao hover. */
export function TerminalCard({ children, className }: Props) {
  return (
    <motion.div
      whileHover={{
        boxShadow:
          "0 0 0 1px rgba(63, 63, 70, 0.9), 0 0 24px rgba(16, 185, 129, 0.08)",
      }}
      transition={{ duration: 0.2 }}
      className={cn(
        "rounded-xl border border-[#27272a] bg-[#18181b] p-5 shadow-none",
        className
      )}
    >
      {children}
    </motion.div>
  );
}

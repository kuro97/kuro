import React from "react";
import { cva } from "class-variance-authority";
import { cn } from "../../lib/utils";

// Варианты бейджей в стиле shadcn/ui
const badgeVariants = cva(
  "inline-flex items-center rounded-md border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary text-primary-foreground shadow hover:bg-primary/80",
        secondary: "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80",
        destructive: "border-transparent bg-destructive text-destructive-foreground shadow hover:bg-destructive/80",
        outline: "text-foreground",
        // Кастомные варианты для статусов звонков
        answered: "bg-green-500/15 text-green-400 border-green-500/30",
        noAnswer: "bg-slate-500/15 text-slate-400 border-slate-500/30",
        failed: "bg-red-500/15 text-red-400 border-red-500/30",
        busy: "bg-amber-500/15 text-amber-400 border-amber-500/30",
        dynamic: "bg-blue-500/15 text-blue-400 border-blue-500/30",
        static: "bg-slate-500/15 text-slate-400 border-slate-500/30",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
);

function Badge({ className, variant, ...props }) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

export { Badge, badgeVariants };

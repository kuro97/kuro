import { clsx } from "clsx";
import { twMerge } from "tailwind-merge";

// Утилита для объединения Tailwind-классов с разрешением конфликтов
export function cn(...inputs) {
  return twMerge(clsx(inputs));
}

import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function truncate(str: string, len = 8) {
  return str.slice(0, len) + '…'
}

export function formatDate(iso: string) {
  return new Date(iso).toLocaleString()
}

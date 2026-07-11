import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

export interface KeyValueItem {
  label: ReactNode
  value: ReactNode
  valueClassName?: string
}

interface KeyValueListProps {
  items: KeyValueItem[]
  className?: string
}

export function KeyValueList({ items, className }: KeyValueListProps) {
  return (
    <dl className={cn("space-y-3", className)}>
      {items.map((item, index) => (
        <div
          key={`${String(item.label)}-${index}`}
          className="grid grid-cols-[minmax(0,0.9fr)_minmax(0,1.4fr)] items-start gap-3 border-b border-border/70 pb-3 last:border-b-0 last:pb-0"
        >
          <dt className="text-[0.68rem] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
            {item.label}
          </dt>
          <dd
            className={cn(
              "min-w-0 text-right text-sm font-medium tracking-tight text-foreground break-all",
              item.valueClassName
            )}
          >
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

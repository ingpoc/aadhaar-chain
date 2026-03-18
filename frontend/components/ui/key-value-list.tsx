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
          className="flex items-start justify-between gap-4 border-b border-border/70 pb-3 last:border-b-0 last:pb-0"
        >
          <dt className="text-[0.68rem] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
            {item.label}
          </dt>
          <dd
            className={cn(
              "text-right text-sm font-medium tracking-tight text-foreground",
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

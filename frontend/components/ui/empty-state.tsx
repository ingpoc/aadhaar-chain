import type { ReactNode } from "react"
import type { LucideIcon } from "lucide-react"
import { FolderOpen } from "lucide-react"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

interface EmptyStateProps {
  title: string
  description: string
  action?: ReactNode
  icon?: LucideIcon
}

export function EmptyState({
  title,
  description,
  action,
  icon,
}: EmptyStateProps) {
  const Icon = icon ?? FolderOpen

  return (
    <Card>
      <CardHeader className="items-start gap-4">
        <div className="flex size-12 items-center justify-center rounded-2xl bg-secondary text-foreground">
          <Icon className="size-5" />
        </div>
        <div className="space-y-2">
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
      </CardHeader>
      {action ? <CardContent>{action}</CardContent> : null}
    </Card>
  )
}

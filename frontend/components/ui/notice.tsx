import type { ReactNode } from "react"
import type { LucideIcon } from "lucide-react"
import {
  AlertCircle,
  CheckCircle2,
  Info,
  TriangleAlert,
} from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { type StatusTone } from "@/components/ui/status-badge"

const iconByTone: Record<StatusTone, LucideIcon> = {
  neutral: Info,
  info: Info,
  success: CheckCircle2,
  warning: TriangleAlert,
  destructive: AlertCircle,
}

interface NoticeProps {
  tone?: StatusTone
  title?: string
  children: ReactNode
  icon?: LucideIcon
  className?: string
}

export function Notice({
  tone = "info",
  title,
  children,
  icon,
  className,
}: NoticeProps) {
  const Icon = icon ?? iconByTone[tone]

  return (
    <Alert variant={tone === "neutral" ? "default" : tone} className={className}>
      <Icon className="size-4" />
      <div>
        {title ? <AlertTitle>{title}</AlertTitle> : null}
        <AlertDescription>{children}</AlertDescription>
      </div>
    </Alert>
  )
}

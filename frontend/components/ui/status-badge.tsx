import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

export type StatusTone = "neutral" | "info" | "success" | "warning" | "destructive"

const toneByStatus: Record<string, StatusTone> = {
  active: "success",
  approve: "success",
  approved: "success",
  complete: "success",
  completed: "success",
  connected: "success",
  granted: "success",
  issued: "success",
  valid: "success",
  verified: "success",
  pending: "warning",
  partial: "warning",
  processing: "info",
  in_progress: "info",
  manual_review: "warning",
  manual_review_required: "warning",
  missing: "warning",
  not_applicable: "neutral",
  not_issued: "neutral",
  not_required: "neutral",
  failed: "destructive",
  reject: "destructive",
  rejected: "destructive",
  revoked: "destructive",
  missing_contract: "destructive",
  error: "destructive",
}

export function formatStatusLabel(value: string) {
  return value.replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase())
}

function toneForStatus(status?: string): StatusTone {
  if (!status) {
    return "neutral"
  }

  return toneByStatus[status.toLowerCase()] ?? "neutral"
}

interface StatusBadgeProps {
  status?: string
  tone?: StatusTone
  label?: string
  className?: string
}

export function StatusBadge({
  status,
  tone,
  label,
  className,
}: StatusBadgeProps) {
  const resolvedTone = tone ?? toneForStatus(status)
  const resolvedLabel =
    label ?? (status ? formatStatusLabel(status) : "Status")

  return (
    <Badge variant={resolvedTone} className={cn(className)}>
      {resolvedLabel}
    </Badge>
  )
}

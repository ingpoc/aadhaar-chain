import { cn } from "@/lib/utils"
import { type StatusTone } from "@/components/ui/status-badge"

const toneClassName: Record<StatusTone, string> = {
  neutral: "bg-muted-foreground/45",
  info: "bg-primary",
  success: "bg-success",
  warning: "bg-warning",
  destructive: "bg-destructive",
}

interface StatusDotProps {
  tone?: StatusTone
  className?: string
}

export function StatusDot({
  tone = "neutral",
  className,
}: StatusDotProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "inline-flex size-2.5 rounded-full",
        toneClassName[tone],
        className
      )}
    />
  )
}

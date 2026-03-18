import { cva, type VariantProps } from "class-variance-authority"
import type { LucideIcon } from "lucide-react"
import type { ReactNode } from "react"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { cn } from "@/lib/utils"

const shellVariants = cva("", {
  variants: {
    tone: {
      neutral: "",
      info: "border-primary/15 bg-accent/70",
      success: "border-success/20 bg-success-soft/90",
      warning: "border-warning/20 bg-warning-soft/90",
      destructive: "border-destructive/20 bg-destructive-soft/90",
    },
  },
  defaultVariants: {
    tone: "neutral",
  },
})

interface VerificationFlowShellProps
  extends VariantProps<typeof shellVariants> {
  title: string
  description?: string
  icon?: LucideIcon
  badge?: ReactNode
  className?: string
  children: ReactNode
}

export function VerificationFlowShell({
  title,
  description,
  icon: Icon,
  badge,
  tone,
  className,
  children,
}: VerificationFlowShellProps) {
  return (
    <Card className={cn(shellVariants({ tone }), className)}>
      <CardHeader>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="space-y-2">
            <CardTitle className="flex items-center gap-3">
              {Icon ? (
                <span className="flex size-10 items-center justify-center rounded-2xl bg-background/80 text-foreground">
                  <Icon className="size-5" />
                </span>
              ) : null}
              <span>{title}</span>
            </CardTitle>
            {description ? <CardDescription>{description}</CardDescription> : null}
          </div>
          {badge}
        </div>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

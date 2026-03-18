import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const alertVariants = cva(
  "relative w-full rounded-[1.5rem] border px-5 py-4 text-sm tracking-tight shadow-sm [&>svg+div]:translate-y-[-2px] [&>svg]:absolute [&>svg]:left-5 [&>svg]:top-4 [&:has(svg)]:pl-12",
  {
    variants: {
      variant: {
        default: "border-border bg-background text-foreground [&>svg]:text-muted-foreground",
        info: "border-primary/15 bg-accent text-foreground [&>svg]:text-primary",
        warning: "border-warning/20 bg-warning-soft text-foreground [&>svg]:text-warning",
        success: "border-success/20 bg-success-soft text-foreground [&>svg]:text-success",
        destructive:
          "border-destructive/20 bg-destructive-soft text-foreground [&>svg]:text-destructive",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

const Alert = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement> & VariantProps<typeof alertVariants>
>(({ className, variant, ...props }, ref) => (
  <div
    ref={ref}
    role="alert"
    className={cn(alertVariants({ variant }), className)}
    {...props}
  />
))
Alert.displayName = "Alert"

const AlertTitle = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLHeadingElement>
>(({ className, ...props }, ref) => (
  <h5
    ref={ref}
    className={cn("mb-1 font-semibold leading-none tracking-tight text-foreground", className)}
    {...props}
  />
))
AlertTitle.displayName = "AlertTitle"

const AlertDescription = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLParagraphElement>
>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    className={cn("text-sm text-muted-foreground [&_p]:leading-relaxed", className)}
    {...props}
  />
))
AlertDescription.displayName = "AlertDescription"

export { Alert, AlertTitle, AlertDescription }

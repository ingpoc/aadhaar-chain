import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex w-fit shrink-0 items-center justify-center rounded-full border px-3 py-1 text-[0.68rem] font-semibold uppercase tracking-[0.18em] whitespace-nowrap transition-colors",
  {
    variants: {
      variant: {
        default: "border-primary/20 bg-primary text-primary-foreground",
        secondary: "border-border bg-secondary text-secondary-foreground",
        outline: "border-border bg-background text-foreground",
        neutral: "border-border bg-background text-muted-foreground",
        info: "border-primary/20 bg-accent text-primary",
        success: "border-success/20 bg-success-soft text-success",
        warning: "border-warning/20 bg-warning-soft text-warning",
        destructive: "border-destructive/20 bg-destructive-soft text-destructive",
      },
    },
    defaultVariants: {
      variant: "outline",
    },
  }
)

function Badge({
  className,
  variant,
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : "span"

  return (
    <Comp
      data-slot="badge"
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  )
}

export { Badge, badgeVariants }

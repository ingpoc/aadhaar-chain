import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-full text-sm font-semibold tracking-tight transition-[transform,background-color,border-color,color,box-shadow] duration-200 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-4 [&_svg]:shrink-0 outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground shadow-[0_14px_32px_rgba(0,82,165,0.16)] hover:-translate-y-px hover:bg-primary-strong hover:shadow-[0_18px_36px_rgba(0,82,165,0.18)]",
        destructive:
          "bg-destructive text-destructive-foreground shadow-[0_14px_30px_rgba(184,58,47,0.15)] hover:-translate-y-px hover:bg-[#9f3026]",
        outline:
          "border border-border bg-background text-foreground shadow-sm hover:-translate-y-px hover:border-border-strong hover:bg-secondary",
        secondary:
          "bg-secondary text-secondary-foreground hover:-translate-y-px hover:bg-accent",
        ghost: "text-foreground hover:bg-secondary",
        link: "text-primary underline-offset-4 hover:text-primary-strong hover:underline",
      },
      size: {
        default: "h-11 px-5",
        sm: "h-9 px-4 text-xs",
        lg: "h-12 px-6 text-base",
        icon: "size-11",
        "icon-sm": "size-9",
        "icon-lg": "size-12",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot : "button"

  return (
    <Comp
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }

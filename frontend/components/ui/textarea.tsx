import * as React from "react"

import { cn } from "@/lib/utils"

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "min-h-32 w-full rounded-[1.5rem] border border-input bg-background px-4 py-3 text-sm tracking-tight text-foreground shadow-sm transition-[border-color,box-shadow] outline-none placeholder:text-muted-foreground focus-visible:border-primary/30 focus-visible:ring-4 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    />
  )
}

export { Textarea }

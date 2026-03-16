import { Card, CardContent } from "@/components/ui/card"
import { Loader2, CheckCircle2 } from "lucide-react"

export default function InvestigationStatus() {
  // props is globally injected but may be undefined on first paint
  if (typeof props === "undefined" || !props) return null

  const steps = props.steps || []
  const current = props.current || "Initializing..."
  const isDone = props.status === "done"

  return (
    <Card className="w-full border-border/40 bg-muted/20 my-1">
      <CardContent className="pt-3 pb-3 px-4">

        {/* Header row */}
        <div className="flex items-center gap-2 mb-2">
          {isDone
            ? <CheckCircle2 className="h-4 w-4 text-green-500 shrink-0" />
            : <Loader2 className="h-4 w-4 animate-spin text-primary shrink-0" />
          }
          <span className="text-sm font-medium">
            {isDone ? "Investigation complete" : current}
          </span>
        </div>

        {/* Tool steps */}
        {steps.length > 0 && (
          <div className="border-l-2 border-border/40 pl-3 ml-2 space-y-1">
            {steps.map((step, i) => (
              <div key={step.tool + i} className="flex items-start gap-2">
                {step.done
                  ? <CheckCircle2 className="h-3 w-3 text-muted-foreground mt-0.5 shrink-0" />
                  : <Loader2 className="h-3 w-3 animate-spin text-primary mt-0.5 shrink-0" />
                }
                <div className="min-w-0">
                  <span className="text-xs font-mono text-muted-foreground">{step.tool}</span>
                  {step.detail && (
                    <p className="text-xs text-muted-foreground/60 truncate">{step.detail}</p>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

      </CardContent>
    </Card>
  )
}

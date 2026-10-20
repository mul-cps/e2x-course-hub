import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

interface InfoFieldProps {
  label: string;
  value: ReactNode;
  className?: string;
}

export const InfoField: React.FC<InfoFieldProps> = ({
  label,
  value,
  className,
}) => (
  <div className={cn("flex flex-col gap-1", className)}>
    <span className="font-semibold text-muted-foreground text-xs uppercase tracking-wide">
      {label}
    </span>
    <span className="text-foreground text-base">{value}</span>
  </div>
);

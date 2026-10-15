import * as React from "react";
import * as Primitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "../../lib/utils";
export const Dialog = Primitive.Root;
export const DialogTrigger = Primitive.Trigger;
export const DialogClose = Primitive.Close;
export const DialogTitle = Primitive.Title;
export const DialogDescription = Primitive.Description;
export const DialogContent = React.forwardRef<
  React.ElementRef<typeof Primitive.Content>,
  React.ComponentPropsWithoutRef<typeof Primitive.Content> & {
    closeDisabled?: boolean;
  }
>(({ className, children, closeDisabled = false, ...props }, ref) => (
  <Primitive.Portal>
    <Primitive.Overlay className="dialog-overlay" />
    <Primitive.Content
      ref={ref}
      className={cn("dialog-content", className)}
      {...props}
    >
      {children}
      <Primitive.Close
        className="dialog-close"
        aria-label="Close"
        disabled={closeDisabled}
      >
        <X size={20} />
      </Primitive.Close>
    </Primitive.Content>
  </Primitive.Portal>
));
DialogContent.displayName = "DialogContent";

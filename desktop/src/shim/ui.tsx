// Web implementations of the @decky/ui surface consumed by index.tsx.
//
// Scope is defined by what the plugin actually imports — nothing more. If a
// build fails on a missing export, add it here rather than editing index.tsx,
// which must stay byte-identical between the two targets.
import {
  forwardRef,
  useCallback,
  useRef,
  type CSSProperties,
  type ReactNode,
  type Ref,
} from "react";
import { pushModal, type ModalHandle } from "./overlays";
import { registerFocusable } from "./gamepad";
import { GamepadButtonId } from "./gamepad-buttons";

export { Navigation, Router } from "./router";

// Re-exported under the name index.tsx imports it by.
export { GamepadButtonId as GamepadButton };

// ── Focus containers ────────────────────────────────────────────────────────

// Focusable is the single most-used import (168 sites) and the cheapest to
// replace: on desktop, gamepad focus traversal collapses to ordinary DOM
// focus. `onActivate` is the gamepad A-button, which maps to click + Enter.
export type FocusableProps = {
  children?: ReactNode;
  style?: CSSProperties;
  className?: string;
  onActivate?: (e: any) => void;
  onClick?: (e: any) => void;
  onCancel?: (e: any) => void;
  // Steam controller callbacks the plugin attaches to Focusables. These are
  // driven by the gamepad manager (gamepad.ts), which routes button presses up
  // the Focusable ancestor chain.
  onButtonDown?: (e: any) => void;
  onButtonUp?: (e: any) => void;
  onCancelButton?: (e: any) => void;
  onSecondaryButton?: (e: any) => void;
  onOptionsButton?: (e: any) => void;
  onMenuButton?: (e: any) => void;
  onGamepadDirection?: (e: any) => void;
  onGamepadBlur?: (e: any) => void;
  onGamepadFocus?: (e: any) => void;
  noFocusRing?: boolean;
  autoFocus?: boolean;
  focusWithinClassName?: string;
  actionDescriptionMap?: Record<string, string>;
  "flow-children"?: string;
  [key: string]: any;
};

export const Focusable = forwardRef(function Focusable(
  props: FocusableProps,
  ref: Ref<HTMLDivElement>,
) {
  const {
    children,
    style,
    className,
    onActivate,
    onClick,
    onCancel,
    onButtonDown,
    onButtonUp,
    onCancelButton,
    onSecondaryButton,
    onOptionsButton,
    onMenuButton,
    onGamepadDirection,
    onGamepadBlur,
    onGamepadFocus,
    noFocusRing,
    autoFocus,
    focusWithinClassName,
    actionDescriptionMap,
    // Steam layout hint with no web equivalent — drop it so React doesn't warn
    // about an unknown DOM attribute.
    "flow-children": _flowChildren,
    ...rest
  } = props;

  const activate = onActivate ?? onClick;

  // Disabled controls (e.g. GameActionButton with disabled) render as this
  // Focusable WITHOUT a `disabled` prop — the callers gate their own onClick and
  // only signal disabled visually, by dimming inline opacity. Detect that and
  // drop the tabindex entirely (not -1: a -1 element is still script-focusable).
  // A non-tabbable div is neither a spatial-nav target nor a `.focus()` landing
  // spot, so index.tsx's footer focus-repair (Back's onFocused →
  // _forceGamepadFocus → el.focus() on the primary) can't strand gamepad focus
  // on a greyed-out, invisible button. Inline opacity only reflects what a
  // caller set directly (CSS keyframe fades don't appear here), so this keys off
  // the disabled convention specifically. Restored automatically when the
  // control re-enables (opacity back to 1 → re-render).
  const dimmed =
    style?.opacity != null && Number(style.opacity) < 1;
  const tabIndex = dimmed ? undefined : activate ? 0 : -1;

  // Register this Focusable's controller handlers so the gamepad manager can
  // route button events to it (and its ancestors). Re-registers if a handler
  // identity changes. The callback ref also forwards to any external ref.
  const cleanup = useRef<(() => void) | null>(null);
  const setRef = useCallback(
    (el: HTMLDivElement | null) => {
      cleanup.current?.();
      cleanup.current = null;
      if (el) {
        cleanup.current = registerFocusable(el, {
          onButtonDown,
          onButtonUp,
          onCancelButton,
          onSecondaryButton,
          onOptionsButton,
          onMenuButton,
        });
      }
      if (typeof ref === "function") ref(el);
      else if (ref) (ref as any).current = el;
    },
    [ref, onButtonDown, onButtonUp, onCancelButton, onSecondaryButton,
     onOptionsButton, onMenuButton],
  );

  return (
    <div
      ref={setRef}
      style={style}
      className={
        "shim-focusable" +
        (noFocusRing ? " shim-no-focus-ring" : "") +
        (className ? " " + className : "")
      }
      tabIndex={tabIndex}
      autoFocus={autoFocus}
      onClick={activate}
      onKeyDown={(e) => {
        // Don't hijack Enter/Space when the key was pressed inside an editable
        // control nested under this Focusable — the input needs the space to
        // type it and Enter to submit. Without this, keydown bubbling up to a
        // wrapping Focusable eats every space in wizard text fields.
        const t = e.target as HTMLElement;
        const editable =
          t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.isContentEditable;
        if (!editable && activate && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          activate(e);
        } else if ((onCancelButton ?? onCancel) && e.key === "Escape") {
          e.preventDefault();
          (onCancelButton ?? onCancel)!(e);
        }
      }}
      onBlur={onGamepadBlur}
      onFocus={onGamepadFocus}
      {...rest}
    >
      {children}
    </div>
  );
});

// ── Panels ──────────────────────────────────────────────────────────────────

export function PanelSection({
  title,
  children,
}: {
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className="shim-panel-section">
      {title ? <div className="shim-panel-title">{title}</div> : null}
      {children}
    </div>
  );
}

export function PanelSectionRow({ children }: { children?: ReactNode }) {
  return <div className="shim-panel-row">{children}</div>;
}

// ── Buttons ─────────────────────────────────────────────────────────────────

export function ButtonItem({
  children,
  onClick,
  disabled,
  description,
  label,
  bottomSeparator,
  ...rest
}: {
  children?: ReactNode;
  onClick?: (e: any) => void;
  disabled?: boolean;
  description?: ReactNode;
  label?: ReactNode;
  layout?: string;
  bottomSeparator?: string;
  [key: string]: any;
}) {
  // `layout` is a Steam presentation hint; intentionally not forwarded to DOM.
  const { layout: _layout, ...safe } = rest;
  return (
    <div
      className={
        "shim-button-item" + (bottomSeparator ? " shim-sep" : "")
      }
    >
      {label ? <div className="shim-item-label">{label}</div> : null}
      <button
        type="button"
        className="shim-button"
        disabled={disabled}
        onClick={onClick}
        {...safe}
      >
        {children}
      </button>
      {description ? (
        <div className="shim-item-description">{description}</div>
      ) : null}
    </div>
  );
}

export const DialogButton = forwardRef(function DialogButton(
  {
    children,
    onClick,
    disabled,
    style,
    className,
    ...rest
  }: {
    children?: ReactNode;
    onClick?: (e: any) => void;
    disabled?: boolean;
    style?: CSSProperties;
    className?: string;
    [key: string]: any;
  },
  ref: Ref<HTMLButtonElement>,
) {
  return (
    <button
      ref={ref}
      type="button"
      className={"shim-dialog-button" + (className ? " " + className : "")}
      style={style}
      disabled={disabled}
      onClick={onClick}
      {...rest}
    >
      {children}
    </button>
  );
});

// ── Text input ──────────────────────────────────────────────────────────────

export const TextField = forwardRef(function TextField(
  {
    value,
    onChange,
    label,
    placeholder,
    bIsPassword,
    disabled,
    style,
    className,
    mustBeNumeric,
    ...rest
  }: {
    value?: string;
    onChange?: (e: any) => void;
    label?: ReactNode;
    placeholder?: string;
    bIsPassword?: boolean;
    disabled?: boolean;
    style?: CSSProperties;
    className?: string;
    mustBeNumeric?: boolean;
    [key: string]: any;
  },
  ref: Ref<HTMLInputElement>,
) {
  return (
    <div className="shim-textfield">
      {label ? <div className="shim-item-label">{label}</div> : null}
      <input
        ref={ref}
        className={"shim-input" + (className ? " " + className : "")}
        style={style}
        type={bIsPassword ? "password" : mustBeNumeric ? "number" : "text"}
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        onChange={onChange}
        {...rest}
      />
    </div>
  );
});

// ── Modals ──────────────────────────────────────────────────────────────────

export function ModalRoot({
  children,
  closeModal,
  onCancel,
  onEscKeypress,
  bHideCloseIcon,
  ...rest
}: {
  children?: ReactNode;
  closeModal?: () => void;
  onCancel?: () => void;
  onEscKeypress?: () => void;
  bHideCloseIcon?: boolean;
  [key: string]: any;
}) {
  const dismiss = onCancel ?? onEscKeypress ?? closeModal;
  return (
    <div
      className="shim-modal"
      role="dialog"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          (onEscKeypress ?? dismiss)?.();
        }
      }}
      {...rest}
    >
      {!bHideCloseIcon && dismiss ? (
        <button className="shim-modal-close" onClick={dismiss} type="button">
          ✕
        </button>
      ) : null}
      {children}
    </div>
  );
}

/**
 * Decky injects a `closeModal` prop into the element it is handed. We do the
 * same by cloning the element with the handle's Close bound in.
 */
export function showModal(element: any, _parent?: any): ModalHandle {
  return pushModal((handle) => {
    if (element && typeof element === "object" && "type" in element) {
      const Cmp = element.type;
      return <Cmp {...element.props} closeModal={handle.Close} />;
    }
    return element;
  });
}

// ── Context menus ───────────────────────────────────────────────────────────

export function Menu({
  children,
  label,
  cancelText: _cancelText,
  onCancel,
  ...rest
}: {
  children?: ReactNode;
  label?: ReactNode;
  cancelText?: string;
  onCancel?: () => void;
  [key: string]: any;
}) {
  return (
    <div className="shim-menu" {...rest}>
      {label ? <div className="shim-menu-label">{label}</div> : null}
      {children}
    </div>
  );
}

export function MenuItem({
  children,
  onSelected,
  onClick,
  disabled,
  ...rest
}: {
  children?: ReactNode;
  onSelected?: () => void;
  onClick?: () => void;
  disabled?: boolean;
  [key: string]: any;
}) {
  const act = onSelected ?? onClick;
  return (
    <button
      type="button"
      className="shim-menu-item"
      disabled={disabled}
      onClick={() => act?.()}
      {...rest}
    >
      {children}
    </button>
  );
}

export function showContextMenu(element: any, _parent?: any): ModalHandle {
  return pushModal((handle) => (
    <div className="shim-context-menu" onClick={handle.Close}>
      {element}
    </div>
  ));
}

// ── Misc ────────────────────────────────────────────────────────────────────

export const staticClasses = {
  Title: "shim-title",
};

// Web implementations of the @decky/ui surface consumed by index.tsx.
//
// Scope is defined by what the plugin actually imports — nothing more. If a
// build fails on a missing export, add it here rather than editing index.tsx,
// which must stay byte-identical between the two targets.
import {
  forwardRef,
  type CSSProperties,
  type ReactNode,
  type Ref,
} from "react";
import { pushModal, type ModalHandle } from "./overlays";

export { Navigation, Router } from "./router";

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

  return (
    <div
      ref={ref}
      style={style}
      className={
        "shim-focusable" +
        (noFocusRing ? " shim-no-focus-ring" : "") +
        (className ? " " + className : "")
      }
      tabIndex={activate ? 0 : -1}
      autoFocus={autoFocus}
      onClick={activate}
      onKeyDown={(e) => {
        if (activate && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          activate(e);
        } else if (onCancel && e.key === "Escape") {
          e.preventDefault();
          onCancel(e);
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

// Steam's controller button codes. Only the members index.tsx references are
// guaranteed accurate; the numeric values match Steam's enum ordering.
export enum GamepadButton {
  OK = 1,
  CANCEL = 2,
  SECONDARY = 3,
  OPTIONS = 4,
  START = 5,
  SELECT = 6,
  TRIGGER_LEFT = 7,
  TRIGGER_RIGHT = 8,
  BUMPER_LEFT = 9,
  BUMPER_RIGHT = 10,
  DIR_UP = 11,
  DIR_DOWN = 12,
  DIR_LEFT = 13,
  DIR_RIGHT = 14,
}

// Steam's controller button codes. Kept in its own module so both ui.tsx (which
// re-exports it as GamepadButton for index.tsx) and gamepad.ts can use it
// without a circular import. Only the members index.tsx references are
// guaranteed accurate; the numeric values match Steam's enum ordering.
export enum GamepadButtonId {
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

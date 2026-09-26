// A small, accessible toggle. Used for the always-on dot and the preferences that mirror the panel's
// chips, so "on" reads the same everywhere in the app.

interface Props {
  checked: boolean;
  onChange: (value: boolean) => void;
  label?: string;
  hint?: string;
  disabled?: boolean;
}

export function Switch({ checked, onChange, label, hint, disabled }: Props) {
  return (
    <label className={`switch ${disabled ? "switch--disabled" : ""}`} title={hint}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="switch__track">
        <span className="switch__thumb" />
      </span>
      {label && <span className="switch__label">{label}</span>}
    </label>
  );
}

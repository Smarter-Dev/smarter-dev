/** Number and date formatting that matches the Python originals character for character. */

/** Python's `f"{value:,}"` for integers. */
export function thousands(value: number): string {
  const negative = value < 0;
  const digits = Math.abs(Math.trunc(value)).toString();
  let grouped = "";
  for (let index = 0; index < digits.length; index += 1) {
    if (index > 0 && (digits.length - index) % 3 === 0) {
      grouped += ",";
    }
    grouped += digits[index];
  }
  return negative ? `-${grouped}` : grouped;
}

export interface NaiveTimestamp {
  year: number;
  month: number;
  day: number;
}

const ISO_TIMESTAMP =
  /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/;

/** Parses the *literal* calendar fields, the way `datetime.fromisoformat(...).strftime(...)`
 * does — no timezone conversion, so an offset never shifts the printed date. */
export function parseNaiveTimestamp(value: string): NaiveTimestamp | null {
  const match = ISO_TIMESTAMP.exec(value.trim());
  if (match === null) {
    return null;
  }
  const [, year, month, day] = match;
  const parsed = {
    year: Number(year),
    month: Number(month),
    day: Number(day),
  };
  if (parsed.month < 1 || parsed.month > 12 || parsed.day < 1 || parsed.day > 31) {
    return null;
  }
  return parsed;
}

function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

/** Python's `strftime("%m-%d")`. */
export function formatMonthDay(timestamp: NaiveTimestamp): string {
  return `${pad(timestamp.month)}-${pad(timestamp.day)}`;
}

/** Python's `strftime("%m/%d/%y")`. */
export function formatShortDate(timestamp: NaiveTimestamp): string {
  return `${pad(timestamp.month)}/${pad(timestamp.day)}/${pad(timestamp.year % 100)}`;
}

/** Milliseconds since the epoch for a naive wall-clock reading, so that
 * subtracting two of them reproduces Python's naive `datetime` arithmetic. */
export function naiveEpochMs(value: string): number | null {
  const match = ISO_TIMESTAMP.exec(value.trim());
  if (match === null) {
    return null;
  }
  const [, year, month, day, hour, minute, second] = match;
  return Date.UTC(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour ?? 0),
    Number(minute ?? 0),
    Number(second ?? 0),
  );
}

/** The same wall-clock reading for an injected `now`, on the same naive basis. */
export function nowNaiveEpochMs(now: Date): number {
  return Date.UTC(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
    now.getHours(),
    now.getMinutes(),
    now.getSeconds(),
    now.getMilliseconds(),
  );
}

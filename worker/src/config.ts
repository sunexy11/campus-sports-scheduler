export interface WorkerEnv {
  TIMEZONE: "Asia/Shanghai";
  MONITOR_START: string;
  MONITOR_END: string;
  QQ_SMTP_USERNAME?: string;
  QQ_SMTP_AUTH_CODE?: string;
  NOTIFICATION_EMAIL?: string;
}

function clockMinutes(value: string, allow24 = false): number {
  const match = /^(\d{2}):(\d{2})$/.exec(value);
  if (!match) throw new Error(`Invalid clock value: ${value}`);
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (allow24 && hour === 24 && minute === 0) return 24 * 60;
  if (hour > 23 || minute > 59) throw new Error(`Invalid clock value: ${value}`);
  return hour * 60 + minute;
}

export function isMonitorActive(now: Date, env: WorkerEnv): boolean {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: env.TIMEZONE,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(now);
  const hour = Number(parts.find((part) => part.type === "hour")?.value);
  const minute = Number(parts.find((part) => part.type === "minute")?.value);
  const current = hour * 60 + minute;
  const start = clockMinutes(env.MONITOR_START);
  const end = clockMinutes(env.MONITOR_END, true);
  return current >= start && current < end;
}


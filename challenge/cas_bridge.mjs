import { createInterface } from "node:readline";

const sdenv = await import("sdenv");
const jsdomFromUrl = sdenv.jsdomFromUrl;
const jsdomFromText = sdenv.jsdomFromText;
const jsdom = sdenv.jsdom || sdenv.default.jsdom;
const logger = sdenv.logger;

// sdenv 默认会把调试数据写到 stdout；桥接协议只允许 stdout 输出一行 JSON。
logger.level = "off";

const BOOKING_HOST = "booking.fudan.edu.cn";
const SPORTS_REFERER =
  "https://booking.fudan.edu.cn/reservation/fe/site/special/special?id=48";
const USER_AGENT =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
const READ_ONLY_PATHS = new Set([
  "/reservation/api/topic/resource-list",
  "/reservation/api/resource/large-screen",
  "/reservation/site/resource/calendar",
  "/reservation/site/resource/detail",
  "/reservation/site/user/detail-mobile",
  "/reservation/site/appointment/appointment-list",
]);
const BOOKING_PATH = "/reservation/site/resource/launch";
function challengeTimeout(name, fallback) {
  const configured = Number.parseInt(process.env[name] || String(fallback), 10);
  return Number.isFinite(configured) && configured >= 1000 ? configured : fallback;
}

// 瑞数通常会在挑战期间写入 cookie。首轮最多等待 8 秒；如果重试请求
// 仍返回 412，再给第二轮挑战最多 8 秒。保留原环境变量名以兼容现有部署。
const FIRST_CHALLENGE_TIMEOUT_MS = challengeTimeout(
  "FUDAN_POST_CHALLENGE_TIMEOUT_MS",
  8000,
);
const RETRY_CHALLENGE_TIMEOUT_MS = challengeTimeout(
  "FUDAN_POST_RETRY_CHALLENGE_TIMEOUT_MS",
  8000,
);
const REQUEST_TIMEOUT_MS = challengeTimeout(
  "FUDAN_REQUEST_TIMEOUT_MS",
  15000,
);

function assertBookingUrl(value) {
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.hostname !== BOOKING_HOST) {
    throw new Error("URL must use the booking HTTPS host");
  }
  return parsed;
}

function validateTicketUrl(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > 4096) {
    throw new Error("ticket_url is required");
  }
  const parsed = assertBookingUrl(value);
  if (!parsed.searchParams.has("ticket")) {
    throw new Error("ticket_url must contain a ticket parameter");
  }
  return parsed.toString();
}

function safeHeaders(input) {
  const headers = {};
  if (!input || typeof input !== "object") return headers;
  for (const [name, value] of Object.entries(input)) {
    if (typeof value !== "string") continue;
    if (/^(cookie|host|authorization|proxy-authorization)$/i.test(name)) continue;
    headers[name] = value;
  }
  return headers;
}

async function fetchBookingGet(startUrl, cookieJar, referer, extraHeaders = {}) {
  let current = assertBookingUrl(startUrl);
  if (!READ_ONLY_PATHS.has(current.pathname) && !current.pathname.endsWith("/api/login/cas")) {
    throw new Error("bridge only permits the configured read-only endpoints");
  }
  for (let redirects = 0; redirects < 10; redirects += 1) {
    const response = await fetch(current.toString(), {
      method: "GET",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      headers: {
        ...safeHeaders(extraHeaders),
        Cookie: cookieJar.getCookieStringSync(current.toString()),
        "User-Agent": USER_AGENT,
        Referer: referer,
      },
      redirect: "manual",
    });
    const setCookie = response.headers.getSetCookie?.() || [];
    for (const item of setCookie) {
      cookieJar.setCookieSync(item, current.toString(), { ignoreError: true });
    }
    const location = response.headers.get("location");
    if (!location || response.status < 300 || response.status >= 400) {
      return response;
    }
    await response.arrayBuffer();
    current = assertBookingUrl(new URL(location, current).toString());
  }
  throw new Error("too many booking redirects");
}

async function fetchBookingPost(url, cookieJar, referer, body) {
  const current = assertBookingUrl(url);
  if (current.pathname !== BOOKING_PATH) {
    throw new Error("bridge only permits the configured booking endpoint");
  }
  const response = await fetch(current.toString(), {
    method: "POST",
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    headers: {
      Cookie: cookieJar.getCookieStringSync(current.toString()),
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json, text/plain, */*",
      Origin: `https://${BOOKING_HOST}`,
      "X-Requested-With": "XMLHttpRequest",
      "Sec-Fetch-Site": "same-origin",
      "Sec-Fetch-Mode": "cors",
      "Sec-Fetch-Dest": "empty",
      "User-Agent": USER_AGENT,
      Referer: SPORTS_REFERER,
    },
    body,
    redirect: "manual",
  });
  const setCookie = response.headers.getSetCookie?.() || [];
  for (const item of setCookie) {
    cookieJar.setCookieSync(item, current.toString(), { ignoreError: true });
  }
  return response;
}

async function executeChallenge(response, cookieJar, referer, timeoutMs) {
  const contentType = response.headers.get("content-type") || "";
  if (response.status !== 412 || !contentType.includes("text/html")) {
    return {
      attempted: false,
      completed: false,
      completion_signal: null,
      elapsed_ms: null,
    };
  }
  const startedAt = performance.now();
  // Some 瑞数 pages write the useful cookie but never emit the synthetic
  // sdenv:exit event that the page normally uses for navigation.  Snapshot
  // the jar and accept a cookie change as an early completion signal too.
  const initialCookie = cookieJar.getCookieStringSync(response.url);
  const html = await response.text();
  if (html.length > 4 * 1024 * 1024) {
    throw new Error("anti-bot challenge is too large");
  }
  let exitResolve;
  const exited = new Promise((resolve) => {
    exitResolve = resolve;
  });
  let cookieResolve;
  const cookieChanged = new Promise((resolve) => {
    cookieResolve = resolve;
  });
  class SameHostResourceLoader extends jsdom.ResourceLoader {
    fetch(url, options = {}) {
      const parsed = new URL(url);
      if (parsed.protocol !== "https:" || parsed.hostname !== BOOKING_HOST) {
        return null;
      }
      return super.fetch(url, {
        ...options,
        headers: {
          ...(options.headers || {}),
          Cookie: cookieJar.getCookieStringSync(url),
          Referer: referer,
          "User-Agent": USER_AGENT,
        },
      });
    }
  }
  const dom = await jsdomFromText(html, {
    url: response.url,
    referrer: referer,
    cookieJar,
    runScripts: "dangerously",
    resources: new SameHostResourceLoader({
      strictSSL: false,
      userAgent: USER_AGENT,
    }),
    beforeParse(window) {
      window.addEventListener("sdenv:exit", (event) => {
        const eventId = event.detail?.eventId;
        const nextUrl = event.detail?.url;
        if (["location.replace", "location.assign"].includes(eventId) && nextUrl) {
          exitResolve(nextUrl);
        }
      });
    },
    consoleConfig: {
      log: () => {},
      info: () => {},
      warn: () => {},
      error: () => {},
    },
  });
  const cookiePoll = setInterval(() => {
    const currentCookie = cookieJar.getCookieStringSync(response.url);
    if (currentCookie !== initialCookie) {
      cookieResolve(true);
    }
  }, 50);
  const completion = await Promise.race([
    exited.then(() => ({ completed: true, signal: "event" })),
    cookieChanged.then(() => ({ completed: true, signal: "cookie" })),
    new Promise((resolve) =>
      setTimeout(() => resolve({ completed: false, signal: "timeout" }), timeoutMs),
    ),
  ]);
  clearInterval(cookiePoll);
  dom.window.close();
  return {
    attempted: true,
    completed: completion.completed,
    completion_signal: completion.signal,
    elapsed_ms: Math.round(performance.now() - startedAt),
  };
}

async function fetchBookingGetWithChallenge(
  url,
  cookieJar,
  referer,
  extraHeaders = {},
) {
  const startedAt = performance.now();
  const firstGetStartedAt = performance.now();
  let response = await fetchBookingGet(url, cookieJar, referer, extraHeaders);
  const firstGetElapsedMs = Math.round(performance.now() - firstGetStartedAt);
  let challenge = null;
  let retryChallenge = null;
  let retryGetElapsedMs = null;
  let finalGetElapsedMs = null;

  if (response.status === 412) {
    challenge = await executeChallenge(
      response,
      cookieJar,
      referer,
      FIRST_CHALLENGE_TIMEOUT_MS,
    );
    if (challenge.attempted) {
      const retryGetStartedAt = performance.now();
      response = await fetchBookingGet(url, cookieJar, referer, extraHeaders);
      retryGetElapsedMs = Math.round(performance.now() - retryGetStartedAt);
      if (response.status === 412) {
        retryChallenge = await executeChallenge(
          response,
          cookieJar,
          referer,
          RETRY_CHALLENGE_TIMEOUT_MS,
        );
        if (retryChallenge.attempted) {
          const finalGetStartedAt = performance.now();
          response = await fetchBookingGet(url, cookieJar, referer, extraHeaders);
          finalGetElapsedMs = Math.round(performance.now() - finalGetStartedAt);
        }
      }
    }
  }

  return {
    response,
    timing: {
      request_elapsed_ms: Math.round(performance.now() - startedAt),
      first_get_elapsed_ms: firstGetElapsedMs,
      retry_get_elapsed_ms: retryGetElapsedMs,
      final_get_elapsed_ms: finalGetElapsedMs,
      challenge_elapsed_ms: challenge?.elapsed_ms ?? null,
      challenge_completed: challenge?.completed ?? null,
      challenge_completion_signal: challenge?.completion_signal ?? null,
      retry_challenge_elapsed_ms: retryChallenge?.elapsed_ms ?? null,
      retry_challenge_completed: retryChallenge?.completed ?? null,
      retry_challenge_completion_signal:
        retryChallenge?.completion_signal ?? null,
    },
  };
}

function positiveInteger(value, name) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value;
}

function bookingForm(request) {
  const groupId = positiveInteger(request.group_id, "group_id");
  const periodId = positiveInteger(request.period_id, "period_id");
  const number = request.number === undefined ? 1 : positiveInteger(request.number, "number");
  if (number > 100) throw new Error("number is too large");
  if (!Array.isArray(request.sub_resource_ids) || request.sub_resource_ids.length === 0) {
    throw new Error("sub_resource_ids must be a non-empty array");
  }
  const subIds = request.sub_resource_ids.map((value) => positiveInteger(value, "sub_resource_id"));
  if (typeof request.date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(request.date)) {
    throw new Error("date must be YYYY-MM-DD");
  }
  const phone = request.phone === undefined ? "" : String(request.phone);
  const form = new URLSearchParams();
  form.set("data", JSON.stringify([{
    resource_ids: subIds,
    group_id: groupId,
    period: [{ date: request.date, period_id: periodId }],
    number,
  }]));
  form.set("data_colle", phone ? JSON.stringify([{
    name: "手机号",
    value: phone,
    verify: null,
    type: "mobile",
  }]) : "[]");
  form.set("code", "");
  form.set("number", String(number));
  form.set("collective", "0");
  form.set("captcha", JSON.stringify({ token: "", pointJson: "" }));
  return form;
}

async function runChallenge(ticketUrl) {
  const cookieJar = new jsdom.CookieJar();
  let redirectResolve;
  const redirect = new Promise((resolve) => {
    redirectResolve = resolve;
  });
  const dom = await jsdomFromUrl(ticketUrl, {
    cookieJar,
    userAgent: USER_AGENT,
    beforeParse(window) {
      window.addEventListener("sdenv:exit", (event) => {
        const eventId = event.detail?.eventId;
        const nextUrl = event.detail?.url;
        if (!["location.replace", "location.assign"].includes(eventId) || !nextUrl) {
          return;
        }
        redirectResolve(nextUrl);
      });
    },
    consoleConfig: {
      log: () => {},
      info: () => {},
      warn: () => {},
      error: () => {},
    },
  });
  void dom;
  const nextUrl = await Promise.race([
    redirect,
    new Promise((resolve) => setTimeout(() => resolve(null), 15000)),
  ]);
  if (nextUrl) {
    // The initial CAS page has already executed its own challenge in the
    // jsdom session above. Keep this one-time ticket redemption on the
    // original path; business GET requests use the retry wrapper later.
    const response = await fetchBookingGet(nextUrl, cookieJar, ticketUrl);
    await response.arrayBuffer();
  }
  return { cookieJar };
}

let state = null;

async function handle(request) {
  if (!request || typeof request !== "object") {
    throw new Error("request must be an object");
  }
  if (request.ticket_url !== undefined) {
    if (state) throw new Error("bridge session is already initialized");
    const ticketUrl = validateTicketUrl(request.ticket_url);
    state = { ticketUrl, ...(await runChallenge(ticketUrl)) };
    return { ok: true };
  }
  if (request.op === "book_resource") {
    return handleBooking(request);
  }
  if (request.op !== "get" || !state) {
    throw new Error("bridge session is not initialized or operation is not read-only");
  }
  const url = assertBookingUrl(request.url);
  if (!READ_ONLY_PATHS.has(url.pathname)) {
    throw new Error("bridge only permits the configured read-only endpoints");
  }
  const { response, timing } = await fetchBookingGetWithChallenge(
    url.toString(),
    state.cookieJar,
    state.ticketUrl,
    request.headers,
  );
  const body = await response.text();
  if (body.length > 4 * 1024 * 1024) throw new Error("response is too large");
  return {
    ok: true,
    status: response.status,
    content_type: response.headers.get("content-type") || "",
    body,
    timing,
  };
}

async function handleBooking(request) {
  if (!state) throw new Error("bridge session is not initialized");
  const form = bookingForm(request);
  const startedAt = performance.now();
  const firstPostStartedAt = performance.now();
  let response = await fetchBookingPost(
    `${"https://"}${BOOKING_HOST}${BOOKING_PATH}`,
    state.cookieJar,
    state.ticketUrl,
    form,
  );
  const firstPostElapsedMs = Math.round(performance.now() - firstPostStartedAt);
  let challenge = null;
  let retryChallenge = null;
  let retryPostElapsedMs = null;
  let finalPostElapsedMs = null;
  if (response.status === 412) {
    challenge = await executeChallenge(
      response,
      state.cookieJar,
      state.ticketUrl,
      FIRST_CHALLENGE_TIMEOUT_MS,
    );
    if (challenge.attempted) {
      const retryPostStartedAt = performance.now();
      response = await fetchBookingPost(
        `${"https://"}${BOOKING_HOST}${BOOKING_PATH}`,
        state.cookieJar,
        state.ticketUrl,
        form,
      );
      retryPostElapsedMs = Math.round(performance.now() - retryPostStartedAt);
      if (response.status === 412) {
        retryChallenge = await executeChallenge(
          response,
          state.cookieJar,
          state.ticketUrl,
          RETRY_CHALLENGE_TIMEOUT_MS,
        );
        if (retryChallenge.attempted) {
          const finalPostStartedAt = performance.now();
          response = await fetchBookingPost(
            `${"https://"}${BOOKING_HOST}${BOOKING_PATH}`,
            state.cookieJar,
            state.ticketUrl,
            form,
          );
          finalPostElapsedMs = Math.round(performance.now() - finalPostStartedAt);
        }
      }
    }
  }
  const body = await response.text();
  if (body.length > 4 * 1024 * 1024) throw new Error("response is too large");
  return {
    ok: true,
    status: response.status,
    content_type: response.headers.get("content-type") || "",
    body,
    timing: {
      submit_elapsed_ms: Math.round(performance.now() - startedAt),
      first_post_elapsed_ms: firstPostElapsedMs,
      retry_post_elapsed_ms: retryPostElapsedMs,
      final_post_elapsed_ms: finalPostElapsedMs,
      challenge_elapsed_ms: challenge?.elapsed_ms ?? null,
      challenge_completed: challenge?.completed ?? null,
      challenge_completion_signal: challenge?.completion_signal ?? null,
      retry_challenge_elapsed_ms: retryChallenge?.elapsed_ms ?? null,
      retry_challenge_completed: retryChallenge?.completed ?? null,
      retry_challenge_completion_signal:
        retryChallenge?.completion_signal ?? null,
    },
  };
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  if (!line.trim()) continue;
  let result;
  try {
    result = await handle(JSON.parse(line));
  } catch (error) {
    result = {
      ok: false,
      error: error instanceof Error ? error.message : "CAS bridge failed",
    };
  }
  process.stdout.write(JSON.stringify(result) + "\n");
}

// sdenv can leave timers/listeners alive, so explicitly terminate after stdin closes.
setTimeout(() => process.exit(0), 0);

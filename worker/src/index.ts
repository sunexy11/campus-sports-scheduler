import { isMonitorActive, type WorkerEnv } from "./config";

export default {
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return Response.json({ ok: true, phase: "scaffold", liveBooking: false });
    }
    return new Response("Not found", { status: 404 });
  },

  async scheduled(controller: ScheduledController, env: WorkerEnv): Promise<void> {
    const scheduled = new Date(controller.scheduledTime);
    if (!isMonitorActive(scheduled, env)) return;

    // Safety scaffold only. Live queries and booking are added after the
    // cross-platform session and read-only API checks pass.
    console.log(JSON.stringify({ event: "monitor_tick", liveBooking: false }));
  },
} satisfies ExportedHandler<WorkerEnv>;


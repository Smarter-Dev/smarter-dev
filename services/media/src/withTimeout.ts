/** Runs one render under a deadline, handing the work an AbortSignal it can honour.
 *
 * The deadline is checked twice: the signal fires for work that can be
 * interrupted, and the elapsed time is compared after the fact for work that
 * cannot — card painting is synchronous native code that blocks the event loop,
 * so its timer callback would not even run until the paint had finished.
 */

import { RenderTimeoutError } from "./errors.js";

export async function withTimeout<T>(
  timeoutMs: number,
  description: string,
  work: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  const deadline = Date.now() + timeoutMs;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const expired = () => controller.signal.aborted || Date.now() > deadline;

  try {
    const result = await work(controller.signal);
    if (expired()) {
      throw new RenderTimeoutError(`${description} exceeded ${timeoutMs}ms`);
    }
    return result;
  } catch (cause) {
    if (!(cause instanceof RenderTimeoutError) && expired()) {
      throw new RenderTimeoutError(`${description} exceeded ${timeoutMs}ms`);
    }
    throw cause;
  } finally {
    clearTimeout(timer);
  }
}

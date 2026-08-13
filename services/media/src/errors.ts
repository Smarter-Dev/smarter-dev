/** The error envelope every non-2xx response uses, and the classes that build it. */

export type MediaErrorCode =
  | "unauthorized"
  | "not_found"
  | "invalid_request"
  | "unsupported_media_type"
  | "payload_too_large"
  | "render_failed"
  | "render_timeout"
  | "internal_error"
  | "not_ready";

export interface MediaErrorBody {
  error: {
    code: MediaErrorCode;
    message: string;
    detail?: Record<string, unknown>;
  };
}

export class MediaError extends Error {
  readonly code: MediaErrorCode;
  readonly statusCode: number;
  readonly detail: Record<string, unknown> | undefined;

  constructor(
    code: MediaErrorCode,
    statusCode: number,
    message: string,
    detail?: Record<string, unknown>,
  ) {
    super(message);
    this.name = new.target.name;
    this.code = code;
    this.statusCode = statusCode;
    this.detail = detail;
  }

  toBody(): MediaErrorBody {
    const error: MediaErrorBody["error"] = { code: this.code, message: this.message };
    if (this.detail !== undefined) {
      error.detail = this.detail;
    }
    return { error };
  }
}

export class UnauthorizedError extends MediaError {
  constructor(message = "Missing or invalid bearer token") {
    super("unauthorized", 401, message);
  }
}

export class NotFoundError extends MediaError {
  constructor(message = "Unknown path") {
    super("not_found", 404, message);
  }
}

export class InvalidRequestError extends MediaError {
  constructor(message: string, detail?: Record<string, unknown>) {
    super("invalid_request", 400, message, detail);
  }
}

export class UnsupportedMediaTypeError extends MediaError {
  constructor(message: string, detail?: Record<string, unknown>) {
    super("unsupported_media_type", 415, message, detail);
  }
}

export class PayloadTooLargeError extends MediaError {
  constructor(message = "Request body exceeded the route limit") {
    super("payload_too_large", 413, message);
  }
}

export class RenderFailedError extends MediaError {
  constructor(message: string, detail?: Record<string, unknown>) {
    super("render_failed", 422, message, detail);
  }
}

export class RenderTimeoutError extends MediaError {
  constructor(message: string, detail?: Record<string, unknown>) {
    super("render_timeout", 504, message, detail);
  }
}

export class InternalError extends MediaError {
  constructor(message = "Internal error", detail?: Record<string, unknown>) {
    super("internal_error", 500, message, detail);
  }
}

export class NotReadyError extends MediaError {
  constructor(message = "MathJax is still initialising") {
    super("not_ready", 503, message);
  }
}

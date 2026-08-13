/** Request schema for `POST /v1/latex`. */

export const MAX_SOURCE_CHARS = 1800;

export const LATEX_BODY_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["source"],
  properties: {
    source: { type: "string", minLength: 1, maxLength: MAX_SOURCE_CHARS },
  },
} as const;

export const AUDIO_QUERY_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    bitrate: { type: "string", pattern: "^\\d{1,4}k?$", default: "48k" },
  },
} as const;

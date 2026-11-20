/** Environment parsing. Every failure here stops the process before it listens. */

import { decodeValue } from "./credentials.js";

export interface MediaConfig {
  apiKey: string;
  port: number;
  logLevel: string;
  latexTimeoutMs: number;
  audioTimeoutMs: number;
  cardTimeoutMs: number;
  imageTag: string;
}

const MINIMUM_API_KEY_LENGTH = 16;

function readInteger(environment: NodeJS.ProcessEnv, name: string, fallback: number): number {
  const raw = environment[name];
  if (raw === undefined || raw === "") {
    return fallback;
  }
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer, got ${JSON.stringify(raw)}`);
  }
  return parsed;
}

function readApiKey(environment: NodeJS.ProcessEnv): string {
  const encoded = (environment.HVTH7T ?? "").trim();
  if (encoded) {
    try {
      const value = decodeValue(encoded).trim();
      if (value) {
        return value;
      }
      console.warn("credential: HVTH7T decodes to an empty value; using legacy names");
    } catch {
      console.error("credential: HVTH7T is not a valid v1 value; using legacy names");
    }
  }
  // --- PHASE 1 LEGACY FALLBACK: delete this block in phase 2 ---
  return (environment.MEDIA_API_KEY ?? "").trim();
  // --- END PHASE 1 LEGACY FALLBACK ---
}

export function loadConfig(environment: NodeJS.ProcessEnv = process.env): MediaConfig {
  const apiKey = readApiKey(environment);
  if (apiKey.length < MINIMUM_API_KEY_LENGTH) {
    throw new Error(
      `HVTH7T must be set and at least ${MINIMUM_API_KEY_LENGTH} characters long`,
    );
  }

  return {
    apiKey,
    port: readInteger(environment, "MEDIA_PORT", 8080),
    logLevel: environment.MEDIA_LOG_LEVEL ?? "info",
    latexTimeoutMs: readInteger(environment, "MEDIA_LATEX_TIMEOUT_MS", 5000),
    audioTimeoutMs: readInteger(environment, "MEDIA_AUDIO_TIMEOUT_MS", 30000),
    cardTimeoutMs: readInteger(environment, "MEDIA_CARD_TIMEOUT_MS", 5000),
    imageTag: environment.MEDIA_IMAGE_TAG ?? "dev",
  };
}

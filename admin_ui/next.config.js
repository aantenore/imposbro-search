import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const projectRoot = dirname(fileURLToPath(import.meta.url));

const REFERRER_POLICIES = new Set([
  'no-referrer',
  'no-referrer-when-downgrade',
  'origin',
  'origin-when-cross-origin',
  'same-origin',
  'strict-origin',
  'strict-origin-when-cross-origin',
]);

function booleanSetting(env, name, fallback) {
  const rawValue = env[name];
  if (rawValue === undefined || rawValue === '') return fallback;

  const value = String(rawValue).trim().toLowerCase();
  if (value === 'true') return true;
  if (value === 'false') return false;
  throw new Error(`${name} must be either "true" or "false".`);
}

function safeHeaderValue(value, name) {
  const normalized = String(value || '').trim();
  if (/\r|\n/.test(normalized)) {
    throw new Error(`${name} must not contain carriage returns or newlines.`);
  }
  return normalized;
}

function positiveIntegerSetting(env, name, fallback) {
  const rawValue = env[name];
  if (rawValue === undefined || rawValue === '') return fallback;

  const normalized = String(rawValue).trim();
  if (!/^\d+$/.test(normalized)) {
    throw new Error(`${name} must be a positive integer.`);
  }
  const value = Number.parseInt(normalized, 10);
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new Error(`${name} must be a positive integer.`);
  }
  return value;
}

function frameAncestorsSetting(env) {
  const value = safeHeaderValue(
    env.ADMIN_UI_FRAME_ANCESTORS || "'none'",
    'ADMIN_UI_FRAME_ANCESTORS'
  );
  if (!value || value.includes(';')) {
    throw new Error('ADMIN_UI_FRAME_ANCESTORS must be a non-empty CSP source list.');
  }
  if (value.split(/\s+/).includes("'none'") && value !== "'none'") {
    throw new Error("ADMIN_UI_FRAME_ANCESTORS cannot combine 'none' with other sources.");
  }
  return value;
}

function replaceFrameAncestors(policy, frameAncestors) {
  const directives = policy
    .split(';')
    .map((directive) => directive.trim())
    .filter(Boolean)
    .filter((directive) => !/^frame-ancestors(?:\s|$)/i.test(directive));
  directives.push(`frame-ancestors ${frameAncestors}`);
  return `${directives.join('; ')};`;
}

function defaultContentSecurityPolicy(env, frameAncestors) {
  const isProduction = env.NODE_ENV === 'production';
  const connectSources = safeHeaderValue(
    env.ADMIN_UI_CSP_CONNECT_SRC,
    'ADMIN_UI_CSP_CONNECT_SRC'
  );
  const imageSources = safeHeaderValue(
    env.ADMIN_UI_CSP_IMAGE_SRC,
    'ADMIN_UI_CSP_IMAGE_SRC'
  );
  if (connectSources.includes(';') || imageSources.includes(';')) {
    throw new Error('ADMIN_UI_CSP_CONNECT_SRC and ADMIN_UI_CSP_IMAGE_SRC must be CSP source lists.');
  }

  const directives = [
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "form-action 'self'",
    `frame-ancestors ${frameAncestors}`,
    "frame-src 'none'",
    `script-src 'self' 'unsafe-inline'${isProduction ? '' : " 'unsafe-eval'"}`,
    "style-src 'self' 'unsafe-inline'",
    `img-src 'self' data: blob:${imageSources ? ` ${imageSources}` : ''}`,
    "font-src 'self' data:",
    `connect-src 'self'${isProduction ? '' : ' ws: wss:'}${connectSources ? ` ${connectSources}` : ''}`,
    "worker-src 'self' blob:",
    "manifest-src 'self'",
  ];
  if (isProduction) directives.push('upgrade-insecure-requests');
  return `${directives.join('; ')};`;
}

function contentSecurityPolicy(env, frameAncestors) {
  const configuredPolicy = safeHeaderValue(
    env.ADMIN_UI_CONTENT_SECURITY_POLICY,
    'ADMIN_UI_CONTENT_SECURITY_POLICY'
  );
  if (!configuredPolicy) return defaultContentSecurityPolicy(env, frameAncestors);
  return replaceFrameAncestors(configuredPolicy, frameAncestors);
}

/**
 * Build the browser security-header baseline.
 *
 * Next evaluates this configuration while building the standalone application.
 * Deployments that need runtime-specific policy should enforce an equivalent or
 * stricter policy at their trusted ingress as well.
 */
export function buildSecurityHeaders(env = process.env) {
  if (!booleanSetting(env, 'ADMIN_UI_SECURITY_HEADERS_ENABLED', true)) return [];

  const headers = [];
  const frameAncestors = frameAncestorsSetting(env);

  if (booleanSetting(env, 'ADMIN_UI_CSP_ENABLED', true)) {
    headers.push({
      key: 'Content-Security-Policy',
      value: contentSecurityPolicy(env, frameAncestors),
    });
  }

  if (booleanSetting(env, 'ADMIN_UI_NOSNIFF_ENABLED', true)) {
    headers.push({ key: 'X-Content-Type-Options', value: 'nosniff' });
  }

  if (booleanSetting(env, 'ADMIN_UI_REFERRER_POLICY_ENABLED', true)) {
    const referrerPolicy = safeHeaderValue(
      env.ADMIN_UI_REFERRER_POLICY || 'strict-origin-when-cross-origin',
      'ADMIN_UI_REFERRER_POLICY'
    );
    if (!REFERRER_POLICIES.has(referrerPolicy)) {
      throw new Error('ADMIN_UI_REFERRER_POLICY is not a supported referrer policy.');
    }
    headers.push({ key: 'Referrer-Policy', value: referrerPolicy });
  }

  if (booleanSetting(env, 'ADMIN_UI_FRAME_OPTIONS_ENABLED', true)) {
    if (frameAncestors === "'none'") {
      headers.push({ key: 'X-Frame-Options', value: 'DENY' });
    } else if (frameAncestors === "'self'") {
      headers.push({ key: 'X-Frame-Options', value: 'SAMEORIGIN' });
    }
  }

  const hstsEnabled = booleanSetting(
    env,
    'ADMIN_UI_HSTS_ENABLED',
    env.NODE_ENV === 'production'
  );
  if (hstsEnabled) {
    const maxAge = positiveIntegerSetting(
      env,
      'ADMIN_UI_HSTS_MAX_AGE_SECONDS',
      31536000
    );
    const directives = [`max-age=${maxAge}`];
    if (booleanSetting(env, 'ADMIN_UI_HSTS_INCLUDE_SUBDOMAINS', true)) {
      directives.push('includeSubDomains');
    }
    if (booleanSetting(env, 'ADMIN_UI_HSTS_PRELOAD', false)) directives.push('preload');
    headers.push({ key: 'Strict-Transport-Security', value: directives.join('; ') });
  }

  return headers;
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  async headers() {
    return [
      {
        source: '/:path*',
        headers: buildSecurityHeaders(),
      },
    ];
  },
  turbopack: {
    root: projectRoot
  }
};
export default nextConfig;

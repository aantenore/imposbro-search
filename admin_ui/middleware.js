import { NextResponse } from 'next/server';

// API requests are handled by app/api/[[...path]]/route.js (proxy to Query API).
// This middleware is reserved for future auth or other cross-cutting concerns.
export function middleware(request) {
  return NextResponse.next();
}

export const config = {
  matcher: [],
};
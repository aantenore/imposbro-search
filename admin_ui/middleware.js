import { NextResponse } from 'next/server';

export function middleware(request) {
  const pathname = request.nextUrl.pathname;

  if (pathname.startsWith('/api/')) {
    // Removes '/api' from the start of the path.
    // Example: /api/admin/clusters -> /admin/clusters
    const newPath = pathname.replace('/api', '');

    // Constructs the destination URL without '/api'
    const destinationUrl = `${process.env.INTERNAL_QUERY_API_URL}${newPath}${request.nextUrl.search}`;

    // Forwards the request to the new URL
    return NextResponse.rewrite(new URL(destinationUrl));
  }

  return NextResponse.next();
}

export const config = {
  matcher: '/api/:path*',
};
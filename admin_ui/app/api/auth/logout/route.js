import {
  authErrorResponse,
  createLogoutResponse,
  methodNotAllowedResponse,
} from '../../../lib/adminAuth.js';

export function GET() {
  return methodNotAllowedResponse(['POST']);
}

export async function POST(request) {
  try {
    return await createLogoutResponse(request);
  } catch (error) {
    return authErrorResponse(error);
  }
}

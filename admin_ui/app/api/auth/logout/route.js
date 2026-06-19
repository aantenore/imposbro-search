import { authErrorResponse, createLogoutResponse } from '../../../lib/adminAuth.js';

export async function GET(request) {
  try {
    return await createLogoutResponse(request);
  } catch (error) {
    return authErrorResponse(error);
  }
}

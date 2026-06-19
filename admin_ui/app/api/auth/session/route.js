import { authErrorResponse, createSessionStatusResponse } from '../../../lib/adminAuth.js';

export async function GET(request) {
  try {
    return await createSessionStatusResponse(request);
  } catch (error) {
    return authErrorResponse(error);
  }
}

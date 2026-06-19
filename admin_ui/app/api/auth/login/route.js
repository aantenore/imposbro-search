import { authErrorResponse, createLoginResponse } from '../../../lib/adminAuth.js';

export async function GET(request) {
  try {
    return await createLoginResponse(request);
  } catch (error) {
    return authErrorResponse(error);
  }
}

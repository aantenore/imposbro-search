import { authErrorResponse, createCallbackResponse } from '../../../lib/adminAuth.js';

export async function GET(request) {
  try {
    return await createCallbackResponse(request);
  } catch (error) {
    return authErrorResponse(error);
  }
}

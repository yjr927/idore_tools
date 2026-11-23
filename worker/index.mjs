import sandbox from './sandbox.mjs';
import {photoRequest} from './photo.mjs';
export {PhotoJob} from './photo.mjs';
import {planRequest} from './agent.mjs';
export {SandboxAttempt} from './sandbox.mjs';

export default {
  fetch(request, env, ctx) {
    const path = new URL(request.url).pathname;
    if (path.startsWith('/api/photo/')) return photoRequest(request, env);
    if (path.startsWith('/agent/plan/')) return planRequest(request);
    return sandbox.fetch(request, env, ctx);
  }
};

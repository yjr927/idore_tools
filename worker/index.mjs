import sandbox from './sandbox.mjs';
import {planRequest} from './agent.mjs';
export {SandboxAttempt} from './sandbox.mjs';

export default {
  fetch(request, env, ctx) {
    if (new URL(request.url).pathname.startsWith('/agent/plan/')) return planRequest(request);
    return sandbox.fetch(request, env, ctx);
  }
};

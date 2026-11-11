// JSON requests to the instance's API. Errors come back as
// {error: <code>, message: <text>, ...} with a non-2xx status.
//
// Every request needs a session. A 401 `unauthenticated` means it has ended
// (logged out elsewhere, expired, or the password changed), so the page goes
// back to the login wall; callers never see it.

export function toLoginWall() {
  window.location.replace('/login');
}

export async function request(method, path, body) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  let data = null;
  try {
    data = await response.json();
  } catch {
    // empty or non-JSON body
  }
  if (response.status === 401 && data?.error === 'unauthenticated') {
    toLoginWall();
    return new Promise(() => {}); // the page is leaving; nothing to handle
  }
  return { status: response.status, ok: response.ok, data };
}

// Log out and go back to the login wall.
export async function logOut() {
  try {
    await fetch('/api/session', { method: 'DELETE' });
  } finally {
    toLoginWall();
  }
}

// A Camera id is "<device>/<camera>"; keep the slash, encode each part.
export function cameraPath(cameraId) {
  return cameraId.split('/').map(encodeURIComponent).join('/');
}
